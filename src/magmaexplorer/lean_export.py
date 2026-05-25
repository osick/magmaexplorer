"""Render a list of `Entry` into Lean 4 source code.

This module is a pure library: every function takes a `list[Entry]` (and
plain Python values) and returns a string or raises `ImplicationChainError`.
It does no I/O, owns no global state, and never touches `prompt_toolkit`,
the Anthropic SDK, or any other REPL plumbing. The REPL command handlers
(`_do_lean`, `_do_lean_implication`) are thin wrappers around the two
top-level functions exposed here:

- :func:`render_standalone_file` — full file with one declaration per entry,
  matching the original `/lean` output.
- :func:`render_implication_file` — single competition-shaped theorem proving
  one entry from another, matching `/lean-implication` (equational-theories
  Stage 2 distillation challenge format).

The other public functions (`render_term`, `proof_body`,
`compute_implication_chain`, the `apply_eq_*` helpers, …) are exposed so a
caller — typically an automated competition solver — can assemble its own
proof shape without going through the full file renderers.
"""

from __future__ import annotations

from typing import Callable

from . import dsl as _dsl
from .dsl import DSLError, EntryRef, Ref, StepRef
from .entries import Entry, Item, compute_ancestors
from .term import (
    Definition,
    Equation,
    Op,
    Term,
    Var,
    pretty,
    pretty_entry,
)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


LEAN_BINDERS = "{G : Type _} [Mul G]"
"""Explicit binders attached to every emitted `axiom` / `theorem`.

`axiom` in Lean 4 does not pick up `variable` declarations, and `Type*` is
a Mathlib-only shorthand — making the binders explicit keeps the file
compiling in both vanilla Lean 4 and Mathlib environments without any
imports or `variable` line."""


LEAN_STANDALONE_PREAMBLE = """-- magmaexplorer export: {name}
-- {count} {entries_word}
--
-- Each `axiom` is an input equation (no derivation in the REPL).
-- Each `theorem` carries a proof or a `sorry` placeholder; the comment block
-- above it records the DSL primitives the magmaexplorer REPL used to derive it.
-- Fill in the `by ...` blocks to produce a complete Lean proof script for
-- submission (e.g. to the equational-theories distillation challenge).
--
-- Each declaration carries its own `{{G : Type _}} [Mul G]` binders. `axiom`
-- does not pick up `variable` declarations in Lean 4 and `Type*` is a
-- Mathlib-only shorthand, so making the binders explicit keeps the file
-- compiling in both vanilla Lean 4 and Mathlib environments.
"""


LEAN_IMPLICATION_PREAMBLE = """-- magmaexplorer implication: [{from_idx}] => [{to_idx}]  ({name})
-- Chain length: {n} entries.
-- Hypothesis (h): {from_stmt}
-- Goal:           {to_stmt}
--
-- The hypothesis appears as the proof parameter `h` (no `axiom` declarations,
-- which the equational-theories Stage 2 judge would reject as `incomplete_proof`).
-- Intermediate derivation steps are inlined as universally-quantified `have`
-- blocks; the final tactic block discharges the goal.
--
-- For the equational-theories Lean project, swap `[Mul G]` for `[Magma G]` and
-- `*` for the project's `◇` notation as needed.
"""


PRIMITIVE_NAME: dict[type, str] = {
    _dsl.Sym: "sym",
    _dsl.Inst: "inst",
    _dsl.Trans: "trans",
    _dsl.Rewrite: "rewrite",
    _dsl.Expand: "expand",
    _dsl.Fold: "fold",
}


class ImplicationChainError(ValueError):
    """Raised by :func:`compute_implication_chain` (and downstream callers)
    when the requested implication cannot be packaged into a single proof —
    e.g. the chain involves an unrelated axiom, a definition, or `[from]` is
    not actually an ancestor of `[to]`."""


# ---------------------------------------------------------------------------
# Term rendering
# ---------------------------------------------------------------------------


def render_term(t: Term) -> str:
    """Render a `Term` as a Lean 4 expression over `*`.

    Matches the magma pretty-printer's grouping: left children stay bare,
    right children get parens when they are themselves operators."""
    if isinstance(t, Var):
        return t.name
    left = render_term(t.left)
    right = render_term(t.right)
    if isinstance(t.right, Op):
        right = f"({right})"
    return f"{left} * {right}"


def render_term_arg(t: Term) -> str:
    """Render a `Term` for use as an argument to a Lean function application.

    Same as :func:`render_term` but parenthesises Op terms at the top level so
    `eq_0 (a * b) c` parses correctly (rather than `eq_0 a * b c`)."""
    rendered = render_term(t)
    if isinstance(t, Op):
        return f"({rendered})"
    return rendered


def collect_vars(t: Term) -> set[str]:
    """Return the set of variable names appearing in `t`."""
    if isinstance(t, Var):
        return {t.name}
    return collect_vars(t.left) | collect_vars(t.right)


def render_forall(eq: Equation) -> str:
    """Render a universally quantified Lean equation::

        ∀ x y : G, x * y = y * (x * x)

    Variables are sorted to give a deterministic, readable signature."""
    vars_set = collect_vars(eq.lhs) | collect_vars(eq.rhs)
    if vars_set:
        vars_str = " ".join(sorted(vars_set))
        return f"∀ {vars_str} : G, {render_term(eq.lhs)} = {render_term(eq.rhs)}"
    return f"{render_term(eq.lhs)} = {render_term(eq.rhs)}"


# ---------------------------------------------------------------------------
# DSL step → Lean fragment helpers
# ---------------------------------------------------------------------------


def strip_verify_prefix(raw: str) -> str:
    """Strip the `✓ ` / `✗ ` / `? ` marker the REPL prepends to LLM-derived
    steps during verification, plus any trailing `   [...]` annotation,
    so the result can be re-parsed by :func:`magmaexplorer.dsl.parse_step`."""
    for marker in ("✓ ", "✗ ", "? "):
        if raw.startswith(marker):
            raw = raw[len(marker):]
            break
    if "   [" in raw:
        raw = raw.split("   [", 1)[0].rstrip()
    return raw


def default_eq_name(idx: int) -> str:
    """Default name resolver used by standalone `/lean`: entry `[i]` → `eq_i`."""
    return f"eq_{idx}"


def entry_sorted_vars(entries: list[Entry], idx: int) -> list[str] | None:
    """Return the alpha-sorted bound vars of `entries[idx]` if it is an
    equation; `None` otherwise (definitions cannot be applied as Lean facts,
    nor can out-of-range indices)."""
    if not (0 <= idx < len(entries)):
        return None
    e = entries[idx]
    if not isinstance(e.content, Equation):
        return None
    return sorted(collect_vars(e.content.lhs) | collect_vars(e.content.rhs))


def apply_eq_with_subst(name: str, src_vars: list[str], subst: dict[str, Term]) -> str:
    """Build `<name> arg1 arg2 ...` for Inst.

    Each source var either maps to its substituted Lean term (with arg-style
    parenthesisation for compound terms) or keeps its own name. `name` is the
    Lean identifier for the universally-quantified target equation — typically
    `eq_<i>` in standalone mode, `h` or `h_<i>` in implication mode."""
    args: list[str] = []
    for v in src_vars:
        if v in subst:
            args.append(render_term_arg(subst[v]))
        else:
            args.append(v)
    return f"{name} {' '.join(args)}".strip() if args else name


def apply_eq_for_trans(name: str, src_vars: list[str], goal_vars: list[str]) -> str:
    """Build `<name> arg1 arg2 ...` for Trans (and Rewrite's target).

    Source vars that are also goal vars pass through unchanged. Any "orphan"
    var (in `src_vars` but not in `goal_vars`) is filled with the first goal
    var — any `G` works because all sides of the chain use the same witness,
    so the proof still type-checks."""
    if not src_vars:
        return name
    fallback = goal_vars[0] if goal_vars else "default"
    args = [v if v in goal_vars else fallback for v in src_vars]
    return f"{name} {' '.join(args)}".strip()


# ---------------------------------------------------------------------------
# proof_body — DSL primitive → Lean tactic block
# ---------------------------------------------------------------------------


NameResolver = Callable[[int], str]


def proof_body(
    entry: Entry,
    entries: list[Entry],
    goal_vars: list[str],
    name: NameResolver = default_eq_name,
) -> list[str]:
    """Build the lines that go inside the `by` block of a derived theorem.

    Returns a list of lines pre-indented with 2 spaces. The caller decides
    whether to write the block verbatim (top-level theorem) or re-indent it
    by adding another 2 spaces (nested `have ... := by ...`).

    For a single-step entry: translates the one DSL primitive directly into a
    `intro / exact`-style tactic block.

    For a multi-step entry: replays the chain via `dsl.execute_step` to
    compute every intermediate equation, then emits one
    `have h_s<k> : ∀ vars : G, <eq_k> := by ...` block per non-final step,
    and finally the discharge for the outer goal. Each per-step proof uses
    the *same* single-step translator, but with `StepRef` references now
    resolving to the local `h_s<k>` identifiers.

    Falls back to `["  sorry  -- <reason>"]` only when the chain cannot be
    deterministically replayed (parse error, runtime DSLError) or a primitive
    has no direct Lean counterpart (`expand` / `fold`).

    The `name` resolver maps an entry index to the Lean identifier the proof
    should use for that entry's universally-quantified equation. The default
    (`eq_<i>`) is right for standalone files; implication mode passes a
    resolver returning `h` for the hypothesis index and `h_<i>` for inlined
    intermediates."""
    if len(entry.steps) == 0:
        return ["  sorry  -- no steps to translate"]

    # Parse every step up front; bail on the first parse failure.
    parsed: list[_dsl.Step] = []
    for raw in entry.steps:
        clean = strip_verify_prefix(raw)
        try:
            parsed.append(_dsl.parse_step(clean))
        except DSLError:
            return [f"  sorry  -- step did not parse as DSL: {raw}"]

    # Items view for the executor: it only needs the bare equations/definitions.
    items: list[Item] = [e.content for e in entries]

    # Replay the chain, accumulating prior_results. Abort on first runtime
    # failure — the resulting Lean would otherwise be silently wrong.
    prior_results = []
    for raw, step in zip(entry.steps, parsed):
        try:
            prior_results.append(_dsl.execute_step(step, items, prior_results))
        except DSLError as exc:
            return [f"  sorry  -- step failed at runtime: {raw} [{exc}]"]

    # Single-step path: the original direct translation, now via the shared
    # primitive translator with empty step context.
    if len(parsed) == 1:
        return _translate_step(
            parsed[0],
            entry.steps[0],
            entries,
            prior_results=[],
            goal_vars=goal_vars,
            name=name,
        )

    # Multi-step path: emit `have h_s<k>` blocks for each intermediate, then
    # discharge the outer goal using the final step.
    lines: list[str] = []
    for k in range(len(parsed) - 1):
        step_eq = prior_results[k]
        if not isinstance(step_eq, Equation):
            return [f"  sorry  -- step result is not an equation: {entry.steps[k]}"]
        step_vars = sorted(collect_vars(step_eq.lhs) | collect_vars(step_eq.rhs))
        lines.append(f"  have h_s{k + 1} : {render_forall(step_eq)} := by")
        sub = _translate_step(
            parsed[k],
            entry.steps[k],
            entries,
            prior_results=prior_results[:k],
            goal_vars=step_vars,
            name=name,
        )
        # `sub` already has 2-space indentation for "top-level"; nest by 2 more.
        for ln in sub:
            lines.append("  " + ln)

    # Discharge the outer goal with the last step. It sees ALL prior_results
    # (so its StepRefs resolve), and uses the entry's goal_vars for `intro`.
    final = _translate_step(
        parsed[-1],
        entry.steps[-1],
        entries,
        prior_results=prior_results[: len(parsed) - 1],
        goal_vars=goal_vars,
        name=name,
    )
    lines.extend(final)
    return lines


# ---------------------------------------------------------------------------
# Single-step → Lean (the part the multi-step driver reuses per step)
# ---------------------------------------------------------------------------


def _step_ref_id(idx: int) -> str:
    """Lean identifier for the result of step `idx` (1-based) in a multi-step
    chain. Mirrors the DSL's `s<k>` convention."""
    return f"h_s{idx}"


def _resolve_ref(
    ref: Ref,
    entries: list[Entry],
    prior_results: list[Equation],
    name: NameResolver,
) -> tuple[Equation, str, list[str]] | None:
    """Resolve a DSL `Ref` to (equation, Lean identifier, sorted vars).

    EntryRef → `entries[i].content` + `name(i)` + bound vars of that equation.
    StepRef  → `prior_results[i-1]` + `h_s<i>` + bound vars of that result.

    Returns `None` if the ref points at a non-equation, an out-of-range
    entry, or a step result that isn't available yet."""
    if isinstance(ref, EntryRef):
        if not (0 <= ref.index < len(entries)):
            return None
        eq = entries[ref.index].content
        if not isinstance(eq, Equation):
            return None
        vars_ = sorted(collect_vars(eq.lhs) | collect_vars(eq.rhs))
        return eq, name(ref.index), vars_
    # StepRef — 1-based.
    idx0 = ref.index - 1
    if not (0 <= idx0 < len(prior_results)):
        return None
    eq = prior_results[idx0]
    vars_ = sorted(collect_vars(eq.lhs) | collect_vars(eq.rhs))
    return eq, _step_ref_id(ref.index), vars_


def _translate_step(
    step: _dsl.Step,
    raw: str,
    entries: list[Entry],
    prior_results: list[Equation],
    goal_vars: list[str],
    name: NameResolver,
) -> list[str]:
    """Translate ONE DSL primitive into a 2-space-indented Lean tactic block.

    `prior_results` is the list of intermediate equation values from earlier
    steps in the same multi-step chain; it is `[]` for single-step entries.
    All references resolve through `_resolve_ref`, so both `[i]` (EntryRef)
    and `s<k>` (StepRef) work uniformly."""
    intro_line = f"  intro {' '.join(goal_vars)}" if goal_vars else None

    def _emit(exact_term: str) -> list[str]:
        body = []
        if intro_line is not None:
            body.append(intro_line)
        body.append(f"  exact {exact_term}")
        return body

    if isinstance(step, _dsl.Sym):
        resolved = _resolve_ref(step.target, entries, prior_results, name)
        if resolved is None:
            return [f"  sorry  -- sym target not resolvable: {raw}"]
        _, tgt_name, src_vars = resolved
        applied = f"{tgt_name} {' '.join(src_vars)}".strip() if src_vars else tgt_name
        return _emit(f"({applied}).symm")

    if isinstance(step, _dsl.Inst):
        resolved = _resolve_ref(step.target, entries, prior_results, name)
        if resolved is None:
            return [f"  sorry  -- inst target not resolvable: {raw}"]
        _, tgt_name, src_vars = resolved
        subst = {v: t for v, t in step.substitutions}
        return _emit(apply_eq_with_subst(tgt_name, src_vars, subst))

    if isinstance(step, _dsl.Trans):
        a = _resolve_ref(step.left, entries, prior_results, name)
        b = _resolve_ref(step.right, entries, prior_results, name)
        if a is None or b is None:
            return [f"  sorry  -- trans operand not resolvable: {raw}"]
        a_eq, a_name, a_vars = a
        b_eq, b_name, b_vars = b
        a_apply = apply_eq_for_trans(a_name, a_vars, goal_vars)
        b_apply = apply_eq_for_trans(b_name, b_vars, goal_vars)
        if a_eq.rhs == b_eq.lhs:
            return _emit(f"({a_apply}).trans ({b_apply})")
        if a_eq.lhs == b_eq.lhs:
            return _emit(f"({a_apply}).symm.trans ({b_apply})")
        if a_eq.rhs == b_eq.rhs:
            return _emit(f"({a_apply}).trans ({b_apply}).symm")
        if a_eq.lhs == b_eq.rhs:
            return _emit(f"({a_apply}).symm.trans ({b_apply}).symm")
        return [f"  sorry  -- trans: no shared side detected (should not happen)"]

    if isinstance(step, _dsl.Rewrite):
        t = _resolve_ref(step.target, entries, prior_results, name)
        r = _resolve_ref(step.rule, entries, prior_results, name)
        if t is None or r is None:
            return [f"  sorry  -- rewrite operand not resolvable: {raw}"]
        _, t_name, t_vars = t
        _, r_name, _ = r
        t_apply = apply_eq_for_trans(t_name, t_vars, goal_vars)
        arrow = "← " if step.backwards else ""
        body: list[str] = []
        if intro_line is not None:
            body.append(intro_line)
        body.append(f"  -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the")
        body.append(f"  -- leftmost-outermost one. If the goal disagrees, replace `rw` with")
        body.append(f"  -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.")
        body.append(f"  have h_rw := {t_apply}")
        body.append(f"  rw [{arrow}{r_name}] at h_rw")
        body.append("  exact h_rw")
        return body

    primitive = PRIMITIVE_NAME.get(type(step), "<unknown>")
    return [
        f"  sorry  -- {primitive} not yet auto-translated; magmaexplorer definitions have no direct Lean counterpart"
    ]


# ---------------------------------------------------------------------------
# Implication-chain validation
# ---------------------------------------------------------------------------


def compute_implication_chain(
    entries: list[Entry], from_idx: int, to_idx: int
) -> list[int]:
    """Return sorted indices `[from_idx, ..., to_idx]` that form a single-
    hypothesis proof chain. Raises :class:`ImplicationChainError` when:

    - either index is out of range
    - `[from]` is not an ancestor of `[to]`
    - some ancestor of `[to]` is an axiom other than `[from]`
      (would need a second hypothesis we don't have)
    - some ancestor is a `Definition` (expand/fold not yet translated)
    - some ancestor cites a source outside the chain (corrupt save file)

    Indices come back in ascending order, which is a valid topological order
    because magmaexplorer enforces forward-reference-free derivations."""
    n = len(entries)
    if not (0 <= from_idx < n):
        raise ImplicationChainError(f"from index out of range: {from_idx}")
    if not (0 <= to_idx < n):
        raise ImplicationChainError(f"to index out of range: {to_idx}")

    if from_idx == to_idx:
        return [from_idx]

    ancestors = compute_ancestors(entries, to_idx)
    if from_idx not in ancestors:
        raise ImplicationChainError(
            f"[{from_idx}] is not an ancestor of [{to_idx}]; "
            f"cannot derive [{to_idx}] from [{from_idx}]"
        )

    for idx in sorted(ancestors):
        e = entries[idx]
        if isinstance(e.content, Definition):
            raise ImplicationChainError(
                f"[{idx}] is a definition; expand/fold are not yet "
                f"auto-translated, so this implication chain cannot be compiled"
            )
        if idx == from_idx:
            continue
        if not e.sources:
            raise ImplicationChainError(
                f"[{idx}] is an axiom but is not the hypothesis [{from_idx}]; "
                f"cannot prove the goal from `h` alone"
            )
        for src in e.sources:
            if src not in ancestors:
                raise ImplicationChainError(
                    f"[{idx}] cites [{src}] which is not on the chain from "
                    f"[{from_idx}] to [{to_idx}]"
                )

    return sorted(ancestors)


# ---------------------------------------------------------------------------
# Top-level file renderers
# ---------------------------------------------------------------------------


def render_standalone_file(entries: list[Entry], name: str) -> str:
    """Render the full standalone Lean file (`/lean` shape) as a string.

    One block per entry, in order:

    - Equation entry with no sources/steps → `axiom eq_<i> {G : Type _} [Mul G] : ...`
    - Derived equation → `theorem eq_<i> {G : Type _} [Mul G] : ... := by ...`
      with the DSL steps preserved as a `--` comment block above
    - Definition → comment only (syntactic abbreviation, no Lean counterpart)
    """
    count = len(entries)
    lines: list[str] = []
    lines.append(
        LEAN_STANDALONE_PREAMBLE.format(
            name=name,
            count=count,
            entries_word="entry" if count == 1 else "entries",
        )
    )

    for i, e in enumerate(entries):
        lines.append("")
        if isinstance(e.content, Definition):
            lines.append(f"-- [{i}] definition: {pretty_entry(e.content)}")
            lines.append(
                f"--     (syntactic abbreviation; "
                f"inline `{e.content.name}` as `{render_term(e.content.body)}` where needed)"
            )
            continue

        statement = render_forall(e.content)
        if not e.sources and not e.steps:
            lines.append(f"-- [{i}] axiom: {pretty_entry(e.content)}")
            lines.append(f"axiom eq_{i} {LEAN_BINDERS} : {statement}")
        else:
            srcs = ", ".join(f"[{s}]" for s in e.sources) if e.sources else "(none)"
            lines.append(f"-- [{i}] derived from {srcs}")
            for k, step in enumerate(e.steps, 1):
                lines.append(f"--     {k}. {step}")
            lines.append(f"theorem eq_{i} {LEAN_BINDERS} : {statement} := by")
            goal_vars = sorted(collect_vars(e.content.lhs) | collect_vars(e.content.rhs))
            for line in proof_body(e, entries, goal_vars):
                lines.append(line)

    return "\n".join(lines) + "\n"


def render_implication_file(
    entries: list[Entry], from_idx: int, to_idx: int, name: str
) -> str:
    """Render a single competition-shaped Lean theorem as a string.

    The hypothesis becomes the proof parameter `h` (no `axiom`); intermediate
    entries are inlined as universally-quantified `have h_<i>` blocks. Raises
    :class:`ImplicationChainError` if the requested chain cannot be packaged
    into a single proof (see :func:`compute_implication_chain`).

    The `name` argument is recorded in the file's header comment for
    provenance; the actual file path is the caller's responsibility."""
    chain = compute_implication_chain(entries, from_idx, to_idx)

    from_entry = entries[from_idx]
    to_entry = entries[to_idx]
    if not isinstance(from_entry.content, Equation):
        raise ImplicationChainError(
            f"[{from_idx}] is not an equation; cannot use as hypothesis"
        )
    if not isinstance(to_entry.content, Equation):
        raise ImplicationChainError(
            f"[{to_idx}] is not an equation; cannot use as goal"
        )

    def resolve(idx: int) -> str:
        return "h" if idx == from_idx else f"h_{idx}"

    from_stmt = render_forall(from_entry.content)
    to_stmt = render_forall(to_entry.content)
    to_vars = sorted(collect_vars(to_entry.content.lhs) | collect_vars(to_entry.content.rhs))

    lines: list[str] = []
    lines.append(
        LEAN_IMPLICATION_PREAMBLE.format(
            from_idx=from_idx,
            to_idx=to_idx,
            name=name,
            n=len(chain),
            from_stmt=pretty_entry(from_entry.content),
            to_stmt=pretty_entry(to_entry.content),
        )
    )
    lines.append(f"theorem implication {LEAN_BINDERS}")
    lines.append(f"    (h : {from_stmt}) :")
    lines.append(f"    {to_stmt} := by")

    if from_idx == to_idx:
        lines.append("  exact h")
    else:
        for idx in chain:
            if idx == from_idx or idx == to_idx:
                continue
            e = entries[idx]
            assert isinstance(e.content, Equation)
            stmt = render_forall(e.content)
            i_vars = sorted(collect_vars(e.content.lhs) | collect_vars(e.content.rhs))
            lines.append(f"  have h_{idx} : {stmt} := by")
            for ln in proof_body(e, entries, i_vars, name=resolve):
                lines.append("  " + ln)
        for ln in proof_body(to_entry, entries, to_vars, name=resolve):
            lines.append(ln)

    return "\n".join(lines) + "\n"
