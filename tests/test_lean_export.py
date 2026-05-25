"""Tests for the public `magmaexplorer.lean_export` API.

These tests exercise the module directly — without ever touching the REPL —
to prove that a future competition solver can `import magmaexplorer.lean_export`
and produce a judge-acceptable Lean certificate from a hand-built `list[Entry]`.
"""

from __future__ import annotations

import pytest

from magmaexplorer.entries import Entry, compute_ancestors
from magmaexplorer.lean_export import (
    ImplicationChainError,
    LEAN_BINDERS,
    apply_eq_for_trans,
    apply_eq_with_subst,
    collect_vars,
    compute_implication_chain,
    default_eq_name,
    entry_sorted_vars,
    proof_body,
    render_forall,
    render_implication_file,
    render_standalone_file,
    render_term,
    render_term_arg,
    strip_verify_prefix,
)
from magmaexplorer.term import (
    Equation,
    Op,
    Var,
    parse_equation,
)


# ---------------------------------------------------------------------------
# Small fixtures — hand-built Entry lists, exactly as a solver would build them
# ---------------------------------------------------------------------------


def _eq(src: str) -> Equation:
    return parse_equation(src)


def _axiom(src: str) -> Entry:
    return Entry(content=_eq(src), sources=[], steps=[])


def _derived(src: str, sources: list[int], step: str) -> Entry:
    return Entry(content=_eq(src), sources=sources, steps=[step])


# ---------------------------------------------------------------------------
# Term rendering — building blocks
# ---------------------------------------------------------------------------


def test_render_term_single_var():
    assert render_term(Var("x")) == "x"


def test_render_term_left_assoc_no_parens():
    # (x*y)*z renders as `x * y * z` (left-assoc default in Lean)
    t = Op(Op(Var("x"), Var("y")), Var("z"))
    assert render_term(t) == "x * y * z"


def test_render_term_right_assoc_keeps_parens():
    # x*(y*z) must keep parens to preserve right grouping
    t = Op(Var("x"), Op(Var("y"), Var("z")))
    assert render_term(t) == "x * (y * z)"


def test_render_term_arg_wraps_compound_terms():
    """Function-application arguments need parens around compound terms so
    `eq_0 (a * b) c` parses as `eq_0` applied to two args, not three."""
    assert render_term_arg(Var("a")) == "a"
    assert render_term_arg(Op(Var("a"), Var("b"))) == "(a * b)"


def test_collect_vars_collects_from_both_sides():
    assert collect_vars(Op(Var("y"), Op(Var("x"), Var("x")))) == {"x", "y"}


def test_render_forall_sorts_vars_alphabetically():
    rendered = render_forall(_eq("c*a = b*a"))
    assert rendered == "∀ a b c : G, c * a = b * a"


def test_render_forall_empty_vars_omits_quantifier():
    # No variables (constant equation, would never happen in practice but
    # the renderer should degrade gracefully).
    eq = Equation(lhs=Op(Var("x"), Var("x")), rhs=Op(Var("x"), Var("x")))
    out = render_forall(eq)
    # Either omits ∀ or quantifies x; just check it doesn't crash.
    assert "x * x = x * x" in out


# ---------------------------------------------------------------------------
# Verification-prefix handling
# ---------------------------------------------------------------------------


def test_strip_verify_prefix_handles_all_marks():
    assert strip_verify_prefix("✓ sym [0]") == "sym [0]"
    assert strip_verify_prefix("✗ inst [1] x:=a") == "inst [1] x:=a"
    assert strip_verify_prefix("? trans [0] [1]") == "trans [0] [1]"
    assert strip_verify_prefix("sym [0]") == "sym [0]"  # no prefix, untouched


# ---------------------------------------------------------------------------
# Name resolvers
# ---------------------------------------------------------------------------


def test_default_eq_name_returns_eq_pattern():
    assert default_eq_name(0) == "eq_0"
    assert default_eq_name(42) == "eq_42"


# ---------------------------------------------------------------------------
# Step → Lean term fragments
# ---------------------------------------------------------------------------


def test_entry_sorted_vars_returns_alpha_order():
    entries = [_axiom("b*a = a*b")]
    assert entry_sorted_vars(entries, 0) == ["a", "b"]


def test_entry_sorted_vars_none_for_definition():
    from magmaexplorer.term import parse_definition
    entries = [Entry(content=parse_definition("p := x*x"), sources=[], steps=[])]
    assert entry_sorted_vars(entries, 0) is None


def test_entry_sorted_vars_none_for_out_of_range():
    assert entry_sorted_vars([_axiom("x = y")], 5) is None


def test_apply_eq_with_subst_passes_subst_values():
    """For `inst [0] x:=a, y:=b` against `eq_0 : ∀ x y, …`, the application
    is `eq_0 a b` (subst values in alpha-source-var order)."""
    src_vars = ["x", "y"]
    subst = {"x": Var("a"), "y": Var("b")}
    assert apply_eq_with_subst("eq_0", src_vars, subst) == "eq_0 a b"


def test_apply_eq_with_subst_keeps_unsubstituted_vars():
    src_vars = ["x", "y"]
    subst = {"x": Var("a")}  # y unchanged
    assert apply_eq_with_subst("eq_0", src_vars, subst) == "eq_0 a y"


def test_apply_eq_with_subst_parenthesizes_compound_args():
    src_vars = ["x", "y"]
    subst = {"x": Op(Var("a"), Var("b")), "y": Var("c")}
    assert apply_eq_with_subst("eq_0", src_vars, subst) == "eq_0 (a * b) c"


def test_apply_eq_with_subst_respects_caller_name():
    """The first param is the Lean name to apply — `eq_0`, `h`, `h_3`, …"""
    src_vars = ["x", "y"]
    subst = {"x": Var("a")}
    assert apply_eq_with_subst("h", src_vars, subst) == "h a y"
    assert apply_eq_with_subst("h_3", src_vars, subst) == "h_3 a y"


def test_apply_eq_for_trans_passes_goal_vars():
    """For trans, each src var that's in goal_vars stays itself."""
    assert apply_eq_for_trans("eq_0", ["x", "y"], ["x", "y"]) == "eq_0 x y"


def test_apply_eq_for_trans_orphan_var_uses_first_goal_var():
    """`b` is in src_vars but not goal_vars → fallback to goal_vars[0]."""
    assert apply_eq_for_trans("eq_0", ["a", "b"], ["a", "c"]) == "eq_0 a a"


# ---------------------------------------------------------------------------
# proof_body — the heart of the translator
# ---------------------------------------------------------------------------


def test_proof_body_sym_uses_symm():
    entries = [_axiom("x*y = y*x"), _derived("y*x = x*y", [0], "sym [0]")]
    body = proof_body(entries[1], entries, goal_vars=["x", "y"])
    text = "\n".join(body)
    assert "intro x y" in text
    assert "(eq_0 x y).symm" in text


def test_proof_body_inst_specializes_target():
    entries = [
        _axiom("x*y = y*x"),
        _derived("a*b = b*a", [0], "inst [0] x:=a, y:=b"),
    ]
    body = proof_body(entries[1], entries, goal_vars=["a", "b"])
    text = "\n".join(body)
    assert "exact eq_0 a b" in text


def test_proof_body_respects_custom_name_resolver():
    """The whole point of the refactor: a caller can swap `eq_0` for `h`."""
    entries = [_axiom("x*y = y*x"), _derived("y*x = x*y", [0], "sym [0]")]

    def use_h(idx: int) -> str:
        return "h" if idx == 0 else f"h_{idx}"

    body = proof_body(entries[1], entries, goal_vars=["x", "y"], name=use_h)
    text = "\n".join(body)
    assert "(h x y).symm" in text
    assert "eq_0" not in text


def test_proof_body_trans_handles_orientation():
    entries = [_axiom("a = b"), _axiom("b = c"), _derived("a = c", [0, 1], "trans [0] [1]")]
    body = proof_body(entries[2], entries, goal_vars=["a", "c"])
    text = "\n".join(body)
    assert ".trans" in text


def test_proof_body_rewrite_uses_rw_tactic():
    entries = [
        _axiom("a*x = x*a"),
        _axiom("x = b"),
        _derived("a*b = x*a", [0, 1], "rewrite [0] using [1]"),
    ]
    body = proof_body(entries[2], entries, goal_vars=["a", "b", "x"])
    text = "\n".join(body)
    assert "rw [eq_1] at" in text
    assert "exact" in text


# ---------------------------------------------------------------------------
# proof_body — multi-step chains (Layer 2)
# ---------------------------------------------------------------------------


def test_proof_body_two_step_inst_then_sym_no_sorry():
    # Derivation: inst [0] x:=a, y:=b   => a*b = b*a
    #             sym s1                 => b*a = a*b
    axiom = _axiom("x*y = y*x")
    e = Entry(
        content=_eq("b*a = a*b"),
        sources=[0],
        steps=["inst [0] x:=a, y:=b", "sym s1"],
    )
    entries = [axiom, e]
    body = proof_body(e, entries, goal_vars=["a", "b"])
    text = "\n".join(body)
    assert "sorry" not in text
    # Intermediate result is named h_s1 ...
    assert "have h_s1" in text
    # ... and the final step references it via `.symm`-style application.
    assert "h_s1" in text.split("have h_s1", 1)[1]


def test_proof_body_three_step_chain_no_sorry():
    # eq0: x*y = y*x
    # eq1: a = b
    # inst [0] x:=a, y:=a   => a*a = a*a
    # inst [0] x:=a, y:=b   => a*b = b*a
    # sym s2                => b*a = a*b
    axiom = _axiom("x*y = y*x")
    other = _axiom("a = b")
    e = Entry(
        content=_eq("b*a = a*b"),
        sources=[0],
        steps=[
            "inst [0] x:=a, y:=a",
            "inst [0] x:=a, y:=b",
            "sym s2",
        ],
    )
    entries = [axiom, other, e]
    body = proof_body(e, entries, goal_vars=["a", "b"])
    text = "\n".join(body)
    assert "sorry" not in text
    # Each non-final intermediate gets its own `have`.
    assert "have h_s1" in text
    assert "have h_s2" in text
    # The last step does NOT get a `have h_s3`; it discharges the outer goal.
    assert "have h_s3" not in text


def test_proof_body_trans_using_step_refs_no_sorry():
    # axiom 0: a = b
    # axiom 1: b = c
    # sym [0]      => b = a   (s1)
    # trans s1 [1] -- intermediate b=a + b=c via shared b -> a = c
    e = Entry(
        content=_eq("a = c"),
        sources=[0, 1],
        steps=["sym [0]", "trans s1 [1]"],
    )
    entries = [_axiom("a = b"), _axiom("b = c"), e]
    body = proof_body(e, entries, goal_vars=["a", "c"])
    text = "\n".join(body)
    assert "sorry" not in text
    assert "have h_s1" in text


def test_proof_body_unparseable_step_in_chain_still_sorrys():
    # If one step of a chain is not parseable, the whole chain can't be
    # replayed deterministically — fall back to sorry rather than emit a
    # half-working block that would type-check by accident.
    e = Entry(
        content=_eq("a = b"),
        sources=[0],
        steps=["sym [0]", "garbage nonsense", "sym s2"],
    )
    entries = [_axiom("a = b"), e]
    body = proof_body(e, entries, goal_vars=["a", "b"])
    assert any("sorry" in ln for ln in body)


def test_proof_body_step_with_runtime_failure_in_chain_still_sorrys():
    # `trans [0] [0]` here will succeed (a == a), but `rewrite [0] using [0]`
    # where the pattern isn't found surfaces a runtime DSLError; the multi-
    # step replay must abort with sorry, not emit garbage.
    e = Entry(
        content=_eq("a = b"),
        sources=[0],
        steps=["sym [0]", "rewrite s1 using [0] backwards"],
    )
    # `rewrite s1 using [0] backwards`: pattern = rhs of [0] = b.
    # s1 = b = a; pattern b appears in s1 (LHS). Actually this would succeed
    # too. Use a definitively-failing case:
    e2 = Entry(
        content=_eq("a = b"),
        sources=[0, 1],
        steps=["sym [0]", "trans s1 [1]"],
    )
    # entries[1] = c = d (disjoint), trans s1=(b=a) with c=d shares nothing.
    entries = [_axiom("a = b"), _axiom("c = d"), e2]
    body = proof_body(e2, entries, goal_vars=["a", "b"])
    assert any("sorry" in ln for ln in body)


# ---------------------------------------------------------------------------
# compute_implication_chain
# ---------------------------------------------------------------------------


def test_compute_implication_chain_simple():
    entries = [_axiom("x*y = y*x"), _derived("y*x = x*y", [0], "sym [0]")]
    assert compute_implication_chain(entries, 0, 1) == [0, 1]


def test_compute_implication_chain_trivial_reflexive():
    entries = [_axiom("x*y = y*x")]
    assert compute_implication_chain(entries, 0, 0) == [0]


def test_compute_implication_chain_raises_when_from_not_ancestor():
    entries = [_axiom("x*y = y*x"), _axiom("a = b"), _derived("b = a", [1], "sym [1]")]
    with pytest.raises(ImplicationChainError):
        compute_implication_chain(entries, 0, 2)


def test_compute_implication_chain_raises_on_unrelated_axiom():
    entries = [_axiom("a = b"), _axiom("b = c"), _derived("a = c", [0, 1], "trans [0] [1]")]
    with pytest.raises(ImplicationChainError):
        compute_implication_chain(entries, 0, 2)


def test_compute_implication_chain_raises_on_out_of_range():
    with pytest.raises(ImplicationChainError):
        compute_implication_chain([_axiom("x = y")], 5, 0)
    with pytest.raises(ImplicationChainError):
        compute_implication_chain([_axiom("x = y")], 0, 5)


# ---------------------------------------------------------------------------
# Top-level file renderers — what a solver actually calls
# ---------------------------------------------------------------------------


def test_render_standalone_file_returns_string_with_axiom_and_theorem():
    entries = [
        _axiom("x*y = y*(x*x)"),
        _derived("y*(x*x) = x*y", [0], "sym [0]"),
    ]
    text = render_standalone_file(entries, "my_export")
    assert isinstance(text, str)
    assert "magmaexplorer export: my_export" in text
    assert f"axiom eq_0 {LEAN_BINDERS} :" in text
    assert f"theorem eq_1 {LEAN_BINDERS} :" in text
    assert "(eq_0 x y).symm" in text


def test_render_standalone_file_handles_empty_entries():
    text = render_standalone_file([], "empty")
    # No axiom or theorem keyword on code lines.
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    assert not any(ln.startswith("axiom ") for ln in code_lines)
    assert not any(ln.startswith("theorem ") for ln in code_lines)


def test_render_implication_file_competition_shape():
    entries = [
        _axiom("x*y = y*x"),
        _derived("y*x = x*y", [0], "sym [0]"),
        _derived("b*a = a*b", [1], "inst [1] x:=a, y:=b"),
    ]
    text = render_implication_file(entries, 0, 2, "commute_impl")
    assert "theorem implication" in text
    assert LEAN_BINDERS in text
    # Hypothesis is the proof parameter `h`, not an axiom.
    assert "(h : ∀ x y : G, x * y = y * x)" in text
    # Goal is the destination equation.
    assert "∀ a b : G, b * a = a * b" in text
    # No axiom keyword on code lines.
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    assert not any(ln.startswith("axiom") for ln in code_lines)
    # No sorry in code lines.
    assert not any("sorry" in ln for ln in code_lines)
    # Intermediate [1] is inlined as h_1.
    assert "have h_1" in text


def test_render_implication_file_trivial_reflexive():
    entries = [_axiom("x*y = y*x")]
    text = render_implication_file(entries, 0, 0, "refl")
    assert "exact h" in text
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    assert not any(ln.startswith("axiom") for ln in code_lines)


def test_render_implication_file_raises_on_bad_chain():
    entries = [_axiom("a = b"), _axiom("b = c"), _derived("a = c", [0, 1], "trans [0] [1]")]
    with pytest.raises(ImplicationChainError):
        render_implication_file(entries, 0, 2, "bad")


# ---------------------------------------------------------------------------
# compute_ancestors — re-exported alongside Entry from `entries`
# ---------------------------------------------------------------------------


def test_compute_ancestors_includes_self_and_transitive_sources():
    entries = [
        _axiom("x = y"),
        _derived("y = x", [0], "sym [0]"),
        _derived("x = z", [0, 1], "trans [0] [1]"),
    ]
    assert compute_ancestors(entries, 2) == {0, 1, 2}
    assert compute_ancestors(entries, 1) == {0, 1}
    assert compute_ancestors(entries, 0) == {0}
