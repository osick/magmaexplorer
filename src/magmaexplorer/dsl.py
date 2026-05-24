"""DSL primitives for magma equational derivations.

Six pure-functional derivation primitives:
    sym, inst, trans, rewrite, expand, fold

Each is represented as a frozen dataclass (Step).  ``parse_step`` turns a
string into a Step; ``execute_step`` runs a Step against the entry list and
prior intermediate results, returning a new Equation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

from .term import (
    Definition,
    Equation,
    Op,
    Term,
    Var,
    parse as parse_term,
    rewrite_term,
    substitute,
)

# ---------------------------------------------------------------------------
# Public error type
# ---------------------------------------------------------------------------


class DSLError(ValueError):
    """Raised when a DSL step is malformed or cannot be executed."""


# ---------------------------------------------------------------------------
# Reference types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryRef:
    """Reference to ``entries[index]``."""
    index: int


@dataclass(frozen=True)
class StepRef:
    """Reference to the (1-based) intermediate result of an earlier step."""
    index: int


Ref = Union[EntryRef, StepRef]

# ---------------------------------------------------------------------------
# Step dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sym:
    target: Ref


@dataclass(frozen=True)
class Inst:
    target: Ref
    substitutions: tuple[tuple[str, Term], ...]  # ordered, applied simultaneously


@dataclass(frozen=True)
class Trans:
    left: Ref
    right: Ref


@dataclass(frozen=True)
class Rewrite:
    target: Ref
    rule: Ref
    backwards: bool  # if True use rule.rhs -> rule.lhs


@dataclass(frozen=True)
class Expand:
    target: Ref
    definition: Ref


@dataclass(frozen=True)
class Fold:
    target: Ref
    definition: Ref


Step = Union[Sym, Inst, Trans, Rewrite, Expand, Fold]

# Type alias shared with llm.py
Item = Union[Equation, Definition]

# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

_REF_ENTRY_RE = re.compile(r'^\[(\d+)\]$')
_REF_STEP_RE = re.compile(r'^s(\d+)$')


def _parse_ref(token: str) -> Ref:
    """Parse a single reference token: ``[N]`` or ``sN``."""
    m = _REF_ENTRY_RE.match(token)
    if m:
        return EntryRef(int(m.group(1)))
    m = _REF_STEP_RE.match(token)
    if m:
        return StepRef(int(m.group(1)))
    raise DSLError(f"invalid ref {token!r}: expected '[N]' or 'sN'")


def _tokenize(src: str) -> list[str]:
    """Split on whitespace; handle comma-separated subst lists by also splitting
    on ',' but keeping ':=' intact.  We return all non-empty tokens."""
    return src.split()


def _parse_subst_list(text: str) -> tuple[tuple[str, Term], ...]:
    """Parse a comma-separated list of ``var:=term`` substitutions.

    ``text`` is everything after the ref in an ``inst`` command.
    """
    parts = [p.strip() for p in text.split(",")]
    if not parts or parts == [""]:
        raise DSLError("inst requires at least one substitution")
    result: list[tuple[str, Term]] = []
    for part in parts:
        if ":=" not in part:
            raise DSLError(f"invalid substitution {part!r}: expected 'var:=term'")
        idx = part.index(":=")
        var_name = part[:idx].strip()
        term_src = part[idx + 2:].strip()
        if len(var_name) != 1 or not var_name.isalpha() or not var_name.islower():
            raise DSLError(f"substitution variable must be a single lowercase letter, got {var_name!r}")
        if not term_src:
            raise DSLError(f"substitution for {var_name!r} has empty term")
        try:
            term = parse_term(term_src)
        except Exception as exc:
            raise DSLError(f"failed to parse substitution term {term_src!r}: {exc}") from exc
        result.append((var_name, term))
    return tuple(result)


def parse_step(src: str) -> Step:
    """Parse a single DSL step string.

    Grammar::

        step       := primitive args
        primitive  := "sym" | "inst" | "trans" | "rewrite" | "expand" | "fold"
        ref        := "[" INTEGER "]"  |  "s" INTEGER

        sym     <ref>
        inst    <ref> <subst-list>
        trans   <ref> <ref>
        rewrite <ref> "using" <ref> ["backwards"]
        expand  <ref> <ref>
        fold    <ref> <ref>

    Raises :class:`DSLError` on malformed input.
    """
    src = src.strip()
    if not src:
        raise DSLError("empty step string")

    tokens = src.split()
    primitive = tokens[0]
    rest_tokens = tokens[1:]

    if primitive == "sym":
        if len(rest_tokens) != 1:
            raise DSLError(f"sym expects exactly one ref, got {len(rest_tokens)} token(s)")
        return Sym(_parse_ref(rest_tokens[0]))

    if primitive == "inst":
        if not rest_tokens:
            raise DSLError("inst requires a ref and substitution list")
        ref = _parse_ref(rest_tokens[0])
        # Everything after the first token is the substitution list
        # Rejoin on spaces, split on commas
        rest_src = src[len(primitive):].strip()
        # strip the ref token from the front
        ref_token = rest_tokens[0]
        after_ref = rest_src[len(ref_token):].strip()
        if not after_ref:
            raise DSLError("inst requires at least one substitution after the ref")
        substs = _parse_subst_list(after_ref)
        return Inst(ref, substs)

    if primitive == "trans":
        if len(rest_tokens) != 2:
            raise DSLError(f"trans expects exactly two refs, got {len(rest_tokens)} token(s)")
        return Trans(_parse_ref(rest_tokens[0]), _parse_ref(rest_tokens[1]))

    if primitive == "rewrite":
        # rewrite <ref> using <ref> [backwards]
        if len(rest_tokens) < 3:
            raise DSLError("rewrite expects: rewrite <ref> using <ref> [backwards]")
        target_ref = _parse_ref(rest_tokens[0])
        if rest_tokens[1] != "using":
            raise DSLError(f"rewrite: expected 'using', got {rest_tokens[1]!r}")
        rule_ref = _parse_ref(rest_tokens[2])
        backwards = False
        if len(rest_tokens) == 4:
            if rest_tokens[3] == "backwards":
                backwards = True
            else:
                raise DSLError(f"rewrite: unexpected token {rest_tokens[3]!r} (expected 'backwards')")
        elif len(rest_tokens) > 4:
            raise DSLError(f"rewrite: too many tokens after ref")
        return Rewrite(target_ref, rule_ref, backwards)

    if primitive == "expand":
        if len(rest_tokens) != 2:
            raise DSLError(f"expand expects exactly two refs, got {len(rest_tokens)} token(s)")
        return Expand(_parse_ref(rest_tokens[0]), _parse_ref(rest_tokens[1]))

    if primitive == "fold":
        if len(rest_tokens) != 2:
            raise DSLError(f"fold expects exactly two refs, got {len(rest_tokens)} token(s)")
        return Fold(_parse_ref(rest_tokens[0]), _parse_ref(rest_tokens[1]))

    raise DSLError(f"unknown primitive {primitive!r}")


# ---------------------------------------------------------------------------
# Executor helpers
# ---------------------------------------------------------------------------


def _resolve_ref(ref: Ref, entries: list[Item], prior_results: list[Equation]) -> Item:
    """Resolve a Ref to an Item (Equation or Definition)."""
    if isinstance(ref, EntryRef):
        if ref.index < 0 or ref.index >= len(entries):
            raise DSLError(
                f"EntryRef({ref.index}) out of range; entries has {len(entries)} item(s)"
            )
        return entries[ref.index]
    # StepRef — 1-based
    idx = ref.index - 1
    if idx < 0 or idx >= len(prior_results):
        raise DSLError(
            f"StepRef({ref.index}) out of range; {len(prior_results)} prior step(s) available"
        )
    return prior_results[idx]


def _require_equation(item: Item, label: str) -> Equation:
    if not isinstance(item, Equation):
        raise DSLError(f"{label} must be an Equation, got {type(item).__name__}")
    return item


def _require_definition(item: Item, label: str) -> Definition:
    if not isinstance(item, Definition):
        raise DSLError(f"{label} must be a Definition, got {type(item).__name__}")
    return item


def _rewrite_term_in_equation(
    eq: Equation, pattern: Term, replacement: Term
) -> Equation | None:
    """Try to rewrite one occurrence (leftmost-outermost) in the equation.

    LHS is tried first; if successful returns Equation(new_lhs, rhs).
    Then RHS; if successful returns Equation(lhs, new_rhs).
    Returns None if no match found.
    """
    new_lhs = rewrite_term(eq.lhs, pattern, replacement)
    if new_lhs is not None:
        return Equation(new_lhs, eq.rhs)
    new_rhs = rewrite_term(eq.rhs, pattern, replacement)
    if new_rhs is not None:
        return Equation(eq.lhs, new_rhs)
    return None


# ---------------------------------------------------------------------------
# Main executor
# ---------------------------------------------------------------------------


def execute_step(
    step: Step,
    entries: list[Item],
    prior_results: list[Equation],
) -> Equation:
    """Execute one DSL step; return a new Equation.

    Raises :class:`DSLError` when the operation is invalid.
    """
    if isinstance(step, Sym):
        item = _resolve_ref(step.target, entries, prior_results)
        eq = _require_equation(item, "sym target")
        return Equation(eq.rhs, eq.lhs)

    if isinstance(step, Inst):
        item = _resolve_ref(step.target, entries, prior_results)
        eq = _require_equation(item, "inst target")
        mapping = {name: term for name, term in step.substitutions}
        new_lhs = substitute(eq.lhs, mapping)
        new_rhs = substitute(eq.rhs, mapping)
        return Equation(new_lhs, new_rhs)

    if isinstance(step, Trans):
        a_item = _resolve_ref(step.left, entries, prior_results)
        b_item = _resolve_ref(step.right, entries, prior_results)
        a = _require_equation(a_item, "trans left")
        b = _require_equation(b_item, "trans right")
        # Try 4 orientations; prefer first match
        # 1. a.rhs == b.lhs  -> a.lhs = b.rhs
        if a.rhs == b.lhs:
            return Equation(a.lhs, b.rhs)
        # 2. a.lhs == b.lhs  -> a.rhs = b.rhs
        if a.lhs == b.lhs:
            return Equation(a.rhs, b.rhs)
        # 3. a.rhs == b.rhs  -> a.lhs = b.lhs
        if a.rhs == b.rhs:
            return Equation(a.lhs, b.lhs)
        # 4. a.lhs == b.rhs  -> a.rhs = b.lhs
        if a.lhs == b.rhs:
            return Equation(a.rhs, b.lhs)
        raise DSLError("trans: no shared term found between the two equations")

    if isinstance(step, Rewrite):
        target_item = _resolve_ref(step.target, entries, prior_results)
        rule_item = _resolve_ref(step.rule, entries, prior_results)
        eq = _require_equation(target_item, "rewrite target")
        rule = _require_equation(rule_item, "rewrite rule")
        if step.backwards:
            pattern, replacement = rule.rhs, rule.lhs
        else:
            pattern, replacement = rule.lhs, rule.rhs
        result = _rewrite_term_in_equation(eq, pattern, replacement)
        if result is None:
            raise DSLError(
                f"rewrite: pattern not found in equation"
            )
        return result

    if isinstance(step, Expand):
        target_item = _resolve_ref(step.target, entries, prior_results)
        def_item = _resolve_ref(step.definition, entries, prior_results)
        eq = _require_equation(target_item, "expand target")
        defn = _require_definition(def_item, "expand definition")
        # Replace one leftmost-outermost occurrence of Var(defn.name) with defn.body
        pattern: Term = Var(defn.name)
        replacement: Term = defn.body
        result = _rewrite_term_in_equation(eq, pattern, replacement)
        if result is None:
            raise DSLError(
                f"expand: variable '{defn.name}' not found in equation"
            )
        return result

    if isinstance(step, Fold):
        target_item = _resolve_ref(step.target, entries, prior_results)
        def_item = _resolve_ref(step.definition, entries, prior_results)
        eq = _require_equation(target_item, "fold target")
        defn = _require_definition(def_item, "fold definition")
        # Replace one leftmost-outermost occurrence of defn.body with Var(defn.name)
        pattern = defn.body
        replacement = Var(defn.name)
        result = _rewrite_term_in_equation(eq, pattern, replacement)
        if result is None:
            raise DSLError(
                f"fold: definition body not found in equation"
            )
        return result

    raise DSLError(f"unknown step type: {type(step).__name__}")
