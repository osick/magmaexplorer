"""Minimal LLM-driven proof-search loop for magma implications.

This is Layer 4 step 2 of the SAIR competition pipeline: take a hypothesis
equation `from_eq` and a goal `to_eq`, ask an LLM for a sequence of DSL
primitives that bridges them, verify each step with `dsl.execute_step`,
and on success return a `list[Entry]` that `lean_export` can render into a
competition-shaped theorem.

Design constraints (for the future JSON driver — Layer 4 step 3):

- No I/O. The solver does not open files, talk to stdin/stdout, or import
  `anthropic` at module top level. The default `llm` callable is loaded
  lazily so test environments can inject `StubLLM` without an API key.
- No REPL dependency. Imports `entries`, `dsl`, `llm` (types), `term` — but
  NOT `repl`. This keeps the module importable from a competition runner
  that ships without `prompt_toolkit` / `rich`.
- Deterministic verification. Every claimed step is re-executed; the LLM
  is never trusted on whether a step "worked".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from . import dsl as _dsl
from .dsl import DSLError
from .entries import Entry, Item
from .llm import LLMError, LLMResult
from .term import Equation, pretty_equation


LLMCallable = Callable[[list[Item], str], LLMResult]


class SolverError(RuntimeError):
    """Raised when the solver fails to produce a verified derivation."""


@dataclass
class VerificationResult:
    """Outcome of replaying a candidate DSL derivation.

    - `final_equation`: result of the last step (None if no step executed).
    - `annotated_steps`: each step prefixed with `✓`, `✗`, or `?` per the
      same convention `repl._verify_llm_steps` uses. Stored on `Entry.steps`
      and consumed by `lean_export` (which strips the prefix), so the bare
      DSL text MUST stay re-parseable here.
    - `narrated_steps`: same prefixes, but each `✓` row is suffixed with
      `   => <pretty equation>` showing the value that step produced.
      Intended for the LLM-facing retry prompt — *not* for storage, since
      the appended `=>` text would defeat downstream DSL parsing.
    - `ok`: True iff every step parsed AND executed AND (if
      `expected_final` was supplied) the final result matched it.
    - `error`: short human-readable reason for `ok == False`.
    """

    final_equation: Optional[Equation]
    annotated_steps: list[str]
    narrated_steps: list[str]
    ok: bool
    error: Optional[str]


def verify_derivation(
    raw_steps: list[str],
    items: list[Item],
    expected_final: Optional[Equation] = None,
) -> VerificationResult:
    """Re-execute a candidate DSL step list against `items` as the axiom base.

    `items` are the entries the steps may cite via `[i]`; `s1`, `s2`, …
    references resolve to earlier results inside this same step list.
    Stops at the first parse/exec failure (returns ok=False with the failed
    step annotated). When all steps execute, compares the final equation
    against `expected_final` if provided.
    """
    annotated: list[str] = []
    narrated: list[str] = []
    prior_results: list[Equation] = []

    for raw in raw_steps:
        try:
            step = _dsl.parse_step(raw)
        except DSLError as exc:
            annotated.append(f"? {raw}")
            narrated.append(f"? {raw}")
            return VerificationResult(
                final_equation=None,
                annotated_steps=annotated,
                narrated_steps=narrated,
                ok=False,
                error=f"parse failed at step {len(annotated)}: {exc}",
            )
        try:
            result_eq = _dsl.execute_step(step, items, prior_results)
        except DSLError as exc:
            annotated.append(f"✗ {raw}   [{exc}]")
            narrated.append(f"✗ {raw}   [{exc}]")
            return VerificationResult(
                final_equation=None,
                annotated_steps=annotated,
                narrated_steps=narrated,
                ok=False,
                error=f"exec failed at step {len(annotated)}: {exc}",
            )
        annotated.append(f"✓ {raw}")
        narrated.append(f"✓ {raw}   => {pretty_equation(result_eq)}")
        prior_results.append(result_eq)

    if not prior_results:
        return VerificationResult(
            final_equation=None,
            annotated_steps=annotated,
            narrated_steps=narrated,
            ok=False,
            error="no steps were executed",
        )

    final = prior_results[-1]
    if expected_final is not None and final != expected_final:
        return VerificationResult(
            final_equation=final,
            annotated_steps=annotated,
            narrated_steps=narrated,
            ok=False,
            error=(
                f"final {pretty_equation(final)} ≠ goal "
                f"{pretty_equation(expected_final)}"
            ),
        )
    return VerificationResult(
        final_equation=final,
        annotated_steps=annotated,
        narrated_steps=narrated,
        ok=True,
        error=None,
    )


def solve_implication(
    from_eq: Equation,
    to_eq: Equation,
    *,
    max_attempts: int = 3,
    llm: Optional[LLMCallable] = None,
) -> list[Entry]:
    """Try to derive `to_eq` from `from_eq` using the LLM, up to
    `max_attempts` times.

    Returns a 2-element `list[Entry]` on success:
      - `entries[0]`: the hypothesis (axiom-shaped, no sources, no steps).
      - `entries[1]`: the goal with verified DSL steps and `sources=[0]`.

    The returned list is suitable for:
      `lean_export.render_implication_file(entries, 0, 1, name)`.

    Raises `SolverError` when no attempt produces a verified derivation.
    """
    if llm is None:
        from .llm import call_llm
        llm = call_llm

    entries: list[Entry] = [Entry(content=from_eq, sources=[], steps=[])]
    items: list[Item] = [from_eq]

    if from_eq == to_eq:
        # Degenerate identity: no proof work needed. Emit goal entry with
        # empty steps so the renderer falls through to `exact h`-style proof.
        entries.append(Entry(content=to_eq, sources=[0], steps=[]))
        return entries

    last_verdict: Optional[VerificationResult] = None
    last_llm_error: Optional[str] = None
    for _attempt in range(max_attempts):
        command = _build_command(to_eq, last_verdict, last_llm_error)
        try:
            result = llm(items, command)
        except LLMError as exc:
            last_llm_error = str(exc)
            last_verdict = None
            continue

        verdict = verify_derivation(
            list(result.steps), items, expected_final=to_eq
        )
        if not verdict.ok:
            last_verdict = verdict
            last_llm_error = None
            continue

        entries.append(
            Entry(
                content=verdict.final_equation,
                sources=_normalize_sources(result.sources, len(items)),
                steps=verdict.annotated_steps,
            )
        )
        return entries

    final_msg = (
        last_verdict.error
        if last_verdict is not None and last_verdict.error
        else (f"LLM call failed: {last_llm_error}" if last_llm_error else "no attempts made")
    )
    raise SolverError(
        f"failed to derive {pretty_equation(to_eq)} from "
        f"{pretty_equation(from_eq)} after {max_attempts} attempts: "
        f"{final_msg}"
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_command(
    to_eq: Equation,
    last_verdict: Optional[VerificationResult],
    last_llm_error: Optional[str],
) -> str:
    goal = pretty_equation(to_eq)
    lines = [
        f"Derive the equation `{goal}` from entry [0] using the DSL.",
        f"Your final DSL step MUST produce exactly `{goal}`.",
        f"Set the JSON 'equation' field to `{goal}` as well.",
    ]
    if last_verdict is not None:
        lines.append("")
        lines.append(
            f"The previous attempt failed: {last_verdict.error or 'unspecified error'}"
        )
        if last_verdict.narrated_steps:
            lines.append("Here is exactly what each step produced:")
            for i, narrated in enumerate(last_verdict.narrated_steps, start=1):
                lines.append(f"  {i}. {narrated}")
        lines.append(
            "Use this trace to plan a different derivation that actually ends at the goal."
        )
    elif last_llm_error:
        lines.append("")
        lines.append(f"The previous attempt failed: LLM call failed: {last_llm_error}")
        lines.append("Try again.")
    return "\n".join(lines)


def _normalize_sources(sources: list[int], n_items: int) -> list[int]:
    """Drop out-of-range and duplicate source indices; default to `[0]` so
    every derived entry has at least the hypothesis as a source (the
    `lean_export` chain walker uses this to find the path to the goal)."""
    cleaned = sorted({s for s in sources if 0 <= s < n_items})
    return cleaned if cleaned else [0]
