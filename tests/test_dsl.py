"""Tests for term.py additions (substitute, rewrite_term) and dsl.py module.

TDD: these tests are written BEFORE implementation.
"""

from __future__ import annotations

import pytest

from magmaexplorer.term import (
    Definition,
    Equation,
    Op,
    Var,
    parse,
    parse_definition,
    parse_equation,
    parse_entry,
    substitute,
    rewrite_term,
)
from magmaexplorer.dsl import (
    DSLError,
    EntryRef,
    StepRef,
    Sym,
    Inst,
    Trans,
    Rewrite,
    Expand,
    Fold,
    parse_step,
    execute_step,
)


# ─── Part A: substitute ───────────────────────────────────────────────────────

def test_substitute_single_var():
    # substitute(Var("x"), {"x": parse("y*y")}) == parse("y*y")
    result = substitute(Var("x"), {"x": parse("y*y")})
    assert result == parse("y*y")


def test_substitute_op():
    # substitute(parse("x*y"), {"x": Var("a"), "y": Var("b")}) == parse("a*b")
    result = substitute(parse("x*y"), {"x": Var("a"), "y": Var("b")})
    assert result == parse("a*b")


def test_substitute_simultaneous():
    # Simultaneous semantics: {"x": Var("y"), "y": Var("x")} swaps x and y
    result = substitute(parse("x*y"), {"x": Var("y"), "y": Var("x")})
    assert result == parse("y*x")


def test_substitute_noop_when_no_match():
    # No variable in mapping matches; term unchanged
    result = substitute(parse("x*x"), {"y": Var("z")})
    assert result == parse("x*x")


# ─── Part A: rewrite_term ────────────────────────────────────────────────────

def test_rewrite_term_simple():
    # rewrite_term(parse("x*y"), parse("x"), parse("a")) == parse("a*y")
    result = rewrite_term(parse("x*y"), parse("x"), parse("a"))
    assert result == parse("a*y")


def test_rewrite_term_leftmost():
    # Leftmost: first x in x*x gets replaced
    result = rewrite_term(parse("x*x"), parse("x"), parse("a"))
    assert result == parse("a*x")


def test_rewrite_term_outermost():
    # Outermost: x*x matches the whole LHS subtree before descending into children
    result = rewrite_term(parse("(x*x)*y"), parse("x*x"), parse("a"))
    assert result == parse("a*y")


def test_rewrite_term_no_match():
    # Pattern not found → None
    result = rewrite_term(parse("x*y"), parse("z"), parse("a"))
    assert result is None


# ─── Part B: parse_step ──────────────────────────────────────────────────────

def test_parse_sym_entry_ref():
    assert parse_step("sym [0]") == Sym(EntryRef(0))


def test_parse_sym_step_ref():
    assert parse_step("sym s1") == Sym(StepRef(1))


def test_parse_inst_multiple_subst():
    expected = Inst(EntryRef(0), (("x", Var("y")), ("y", Var("x"))))
    assert parse_step("inst [0] x:=y, y:=x") == expected


def test_parse_inst_single_subst():
    result = parse_step("inst [3] y:=y*y")
    assert isinstance(result, Inst)
    assert result.target == EntryRef(3)
    assert len(result.substitutions) == 1
    assert result.substitutions[0][0] == "y"
    assert result.substitutions[0][1] == parse("y*y")


def test_parse_trans_entry_refs():
    assert parse_step("trans [0] [1]") == Trans(EntryRef(0), EntryRef(1))


def test_parse_trans_mixed_refs():
    assert parse_step("trans [0] s2") == Trans(EntryRef(0), StepRef(2))


def test_parse_rewrite_no_backwards():
    assert parse_step("rewrite [1] using [0]") == Rewrite(EntryRef(1), EntryRef(0), backwards=False)


def test_parse_rewrite_backwards():
    result = parse_step("rewrite [1] using [0] backwards")
    assert result == Rewrite(EntryRef(1), EntryRef(0), backwards=True)


def test_parse_expand():
    assert parse_step("expand [2] [3]") == Expand(EntryRef(2), EntryRef(3))


def test_parse_fold():
    assert parse_step("fold [2] [3]") == Fold(EntryRef(2), EntryRef(3))


@pytest.mark.parametrize("bad", [
    "",                   # empty
    "unknown [0]",        # unknown primitive
    "sym",                # missing ref
    "sym [0] extra",      # trailing junk
    "inst [0]",           # missing substitution list
    "inst [0] x=",        # bad substitution (= not :=)
    "rewrite [0]",        # missing 'using <ref>'
    "rewrite [0] using",  # missing ref after 'using'
    "trans [0]",          # missing second ref
    "sym foo",            # invalid ref format
    "sym [abc]",          # non-integer in brackets
    "sym s",              # 's' with no number
])
def test_parse_malformed_raises_dslerror(bad):
    with pytest.raises(DSLError):
        parse_step(bad)


# ─── Part B: execute_step ────────────────────────────────────────────────────

# --- sym ---

def test_execute_sym_basic():
    entries = [parse_equation("x*y = y*x")]
    result = execute_step(Sym(EntryRef(0)), entries, [])
    assert result == parse_equation("y*x = x*y")


def test_execute_sym_on_definition_raises():
    entries = [parse_definition("u := x*x")]
    with pytest.raises(DSLError):
        execute_step(Sym(EntryRef(0)), entries, [])


def test_execute_sym_out_of_range_raises():
    entries = [parse_equation("x = y")]
    with pytest.raises(DSLError):
        execute_step(Sym(EntryRef(5)), entries, [])


# --- inst ---

def test_execute_inst_basic():
    entries = [parse_equation("x = y*(x*x)")]
    step = Inst(EntryRef(0), (("x", parse("z")),))
    result = execute_step(step, entries, [])
    assert result == parse_equation("z = y*(z*z)")


def test_execute_inst_simultaneous():
    entries = [parse_equation("x = y")]
    step = Inst(EntryRef(0), (("x", Var("y")), ("y", Var("x"))))
    result = execute_step(step, entries, [])
    assert result == parse_equation("y = x")


# --- trans ---

def test_execute_trans_rhs_lhs():
    # a.rhs == b.lhs: [a=b] and [b=c] -> [a=c]
    entries = [parse_equation("a = b"), parse_equation("b = c")]
    result = execute_step(Trans(EntryRef(0), EntryRef(1)), entries, [])
    assert result == parse_equation("a = c")


def test_execute_trans_lhs_lhs():
    # a.lhs == b.lhs: [a=b] and [a=c] -> [b=c]
    entries = [parse_equation("a = b"), parse_equation("a = c")]
    result = execute_step(Trans(EntryRef(0), EntryRef(1)), entries, [])
    assert result == parse_equation("b = c")


def test_execute_trans_rhs_rhs():
    # a.rhs == b.rhs: [a=c] and [b=c] -> [a=b]
    entries = [parse_equation("a = c"), parse_equation("b = c")]
    result = execute_step(Trans(EntryRef(0), EntryRef(1)), entries, [])
    assert result == parse_equation("a = b")


def test_execute_trans_no_shared_raises():
    entries = [parse_equation("a = b"), parse_equation("c = d")]
    with pytest.raises(DSLError):
        execute_step(Trans(EntryRef(0), EntryRef(1)), entries, [])


# --- rewrite ---

def test_execute_rewrite_basic():
    # rule "x = a"; rewriting "x*y = y*x" -> "a*y = y*x"
    # rule at [0], target at [1]
    entries = [parse_equation("x = a"), parse_equation("x*y = y*x")]
    step = Rewrite(EntryRef(1), EntryRef(0), backwards=False)
    result = execute_step(step, entries, [])
    assert result == parse_equation("a*y = y*x")


def test_execute_rewrite_backwards():
    # rule "a = x" with backwards=True: rewrite x->a same as above
    entries = [parse_equation("a = x"), parse_equation("x*y = y*x")]
    step = Rewrite(EntryRef(1), EntryRef(0), backwards=True)
    result = execute_step(step, entries, [])
    assert result == parse_equation("a*y = y*x")


def test_execute_rewrite_not_found_raises():
    entries = [parse_equation("z = a"), parse_equation("x*y = y*x")]
    step = Rewrite(EntryRef(1), EntryRef(0), backwards=False)
    with pytest.raises(DSLError):
        execute_step(step, entries, [])


# --- expand ---

def test_execute_expand_basic():
    # definition u := x*x; target: "u*y = z*u"
    # expand [1] using [0] -> "(x*x)*y = z*u" (leftmost, LHS first)
    entries = [parse_definition("u := x*x"), parse_equation("u*y = z*u")]
    step = Expand(EntryRef(1), EntryRef(0))
    result = execute_step(step, entries, [])
    assert result == parse_equation("(x*x)*y = z*u")


# --- fold ---

def test_execute_fold_basic():
    # fold is inverse of expand
    # definition u := x*x; target "(x*x)*y = z*u"
    # fold [1] using [0] -> "u*y = z*u"
    entries = [parse_definition("u := x*x"), parse_equation("(x*x)*y = z*u")]
    step = Fold(EntryRef(1), EntryRef(0))
    result = execute_step(step, entries, [])
    assert result == parse_equation("u*y = z*u")


def test_execute_expand_not_found_raises():
    # variable u does not appear in equation
    entries = [parse_definition("u := x*x"), parse_equation("x*y = y*x")]
    step = Expand(EntryRef(1), EntryRef(0))
    with pytest.raises(DSLError):
        execute_step(step, entries, [])


# --- StepRef ---

def test_execute_stepref_basic():
    prior = [parse_equation("x*y = z")]
    result = execute_step(Sym(StepRef(1)), [], prior)
    assert result == parse_equation("z = x*y")


def test_execute_stepref_out_of_range_raises():
    prior = [parse_equation("x = y")]
    with pytest.raises(DSLError):
        execute_step(Sym(StepRef(5)), [], prior)
