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
from .dsl import DSLError, EntryRef
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


LEAN_IMPLICATION_PREAMBLE = """-- magmaexplorer implication: [{from_idx}] => [{to_idx}]
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

    Returns `["  sorry  -- <reason>"]` for everything that can't yet be
    auto-translated: multi-step entries, steps using `s<k>` step references,
    `expand` / `fold` (definitions have no direct Lean counterpart), or steps
    that fail to parse as DSL.

    The `name` resolver maps an entry index to the Lean identifier the proof
    should use for that entry's universally-quantified equation. The default
    (`eq_<i>`) is right for standalone files; implication mode passes a
    resolver returning `h` for the hypothesis index and `h_<i>` for inlined
    intermediates."""
    # Multi-step is out of scope for the MVP — fall back.
    if len(entry.steps) != 1:
        return ["  sorry  -- multi-step derivation; translate the chain manually"]

    raw = entry.steps[0]
    clean = strip_verify_prefix(raw)

    try:
        step = _dsl.parse_step(clean)
    except DSLError:
        return [f"  sorry  -- step did not parse as DSL: {raw}"]

    intro_line = f"  intro {' '.join(goal_vars)}" if goal_vars else None

    def _emit(exact_term: str) -> list[str]:
        body = []
        if intro_line is not None:
            body.append(intro_line)
        body.append(f"  exact {exact_term}")
        return body

    if isinstance(step, _dsl.Sym):
        if not isinstance(step.target, EntryRef):
            return [f"  sorry  -- sym on step-ref not auto-translated: {raw}"]
        src_vars = entry_sorted_vars(entries, step.target.index)
        if src_vars is None:
            return [f"  sorry  -- sym target is not an equation: {raw}"]
        tgt_name = name(step.target.index)
        applied = f"{tgt_name} {' '.join(src_vars)}".strip() if src_vars else tgt_name
        return _emit(f"({applied}).symm")

    if isinstance(step, _dsl.Inst):
        if not isinstance(step.target, EntryRef):
            return [f"  sorry  -- inst on step-ref not auto-translated: {raw}"]
        src_vars = entry_sorted_vars(entries, step.target.index)
        if src_vars is None:
            return [f"  sorry  -- inst target is not an equation: {raw}"]
        subst = {v: t for v, t in step.substitutions}
        return _emit(apply_eq_with_subst(name(step.target.index), src_vars, subst))

    if isinstance(step, _dsl.Trans):
        if not (isinstance(step.left, EntryRef) and isinstance(step.right, EntryRef)):
            return [f"  sorry  -- trans on step-ref not auto-translated: {raw}"]
        a_idx = step.left.index
        b_idx = step.right.index
        a_vars = entry_sorted_vars(entries, a_idx)
        b_vars = entry_sorted_vars(entries, b_idx)
        if a_vars is None or b_vars is None:
            return [f"  sorry  -- trans operand is not an equation: {raw}"]
        a_eq = entries[a_idx].content
        b_eq = entries[b_idx].content
        a_apply = apply_eq_for_trans(name(a_idx), a_vars, goal_vars)
        b_apply = apply_eq_for_trans(name(b_idx), b_vars, goal_vars)
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
        if not (isinstance(step.target, EntryRef) and isinstance(step.rule, EntryRef)):
            return [f"  sorry  -- rewrite on step-ref not auto-translated: {raw}"]
        t_idx = step.target.index
        r_idx = step.rule.index
        t_vars = entry_sorted_vars(entries, t_idx)
        if t_vars is None or entry_sorted_vars(entries, r_idx) is None:
            return [f"  sorry  -- rewrite operand is not an equation: {raw}"]
        t_apply = apply_eq_for_trans(name(t_idx), t_vars, goal_vars)
        arrow = "← " if step.backwards else ""
        body: list[str] = []
        if intro_line is not None:
            body.append(intro_line)
        body.append(f"  -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the")
        body.append(f"  -- leftmost-outermost one. If the goal disagrees, replace `rw` with")
        body.append(f"  -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.")
        body.append(f"  have h_rw := {t_apply}")
        body.append(f"  rw [{arrow}{name(r_idx)}] at h_rw")
        body.append("  exact h_rw")
        return body

    # expand / fold — not yet auto-translated
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
