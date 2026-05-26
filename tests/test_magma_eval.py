"""Tests for `magmaexplorer.magma_eval`.

This module is the shared finite-magma evaluator used by:
 * the SAIR dataset validator (`tests/test_dataset.py`)
 * the brute-force false-cert search (planned Step 3)

Tables are row-major lists of lists: `table[a][b]` is the magma product
of the elements `a` and `b` in `Fin n`.
"""

from __future__ import annotations

from magmaexplorer.magma_eval import (
    collect_vars,
    eval_term,
    equation_holds,
    find_falsifying_assignment,
)
from magmaexplorer.term import Op, Var, parse_equation


# ---------------------------------------------------------------------------
# collect_vars
# ---------------------------------------------------------------------------


class TestCollectVars:
    def test_single_variable(self):
        assert collect_vars(Var("x")) == {"x"}

    def test_nested_op(self):
        # x * (y * z)
        t = Op(Var("x"), Op(Var("y"), Var("z")))
        assert collect_vars(t) == {"x", "y", "z"}

    def test_repeated_variable_collapses(self):
        t = Op(Var("x"), Var("x"))
        assert collect_vars(t) == {"x"}


# ---------------------------------------------------------------------------
# eval_term
# ---------------------------------------------------------------------------


class TestEvalTerm:
    XOR = [[0, 1], [1, 0]]  # Fin 2, commutative non-idempotent

    def test_var_lookup(self):
        assert eval_term(Var("x"), {"x": 0}, self.XOR) == 0
        assert eval_term(Var("x"), {"x": 1}, self.XOR) == 1

    def test_op_uses_table(self):
        # x * y  at x=0,y=1 → table[0][1] = 1
        t = Op(Var("x"), Var("y"))
        assert eval_term(t, {"x": 0, "y": 1}, self.XOR) == 1
        # x * x  at x=1 → table[1][1] = 0
        t = Op(Var("x"), Var("x"))
        assert eval_term(t, {"x": 1}, self.XOR) == 0

    def test_nested_op_left_associates(self):
        # (x * y) * x  at x=1,y=0
        # inner: table[1][0] = 1; outer: table[1][1] = 0
        t = Op(Op(Var("x"), Var("y")), Var("x"))
        assert eval_term(t, {"x": 1, "y": 0}, self.XOR) == 0


# ---------------------------------------------------------------------------
# equation_holds — universal quantification over Fin n
# ---------------------------------------------------------------------------


class TestEquationHolds:
    def test_commutativity_holds_on_xor(self):
        # x*y = y*x  is true on XOR (it's commutative).
        eq = parse_equation("x*y = y*x")
        assert equation_holds(eq, [[0, 1], [1, 0]]) is True

    def test_idempotence_fails_on_xor(self):
        # x*x = x  is false on XOR: 1*1 = 0 ≠ 1.
        eq = parse_equation("x*x = x")
        assert equation_holds(eq, [[0, 1], [1, 0]]) is False

    def test_commutativity_fails_on_left_projection(self):
        # Left-proj  op a b = a   →   commutativity 0*1=0, 1*0=1, different.
        eq = parse_equation("x*y = y*x")
        assert equation_holds(eq, [[0, 0], [1, 1]]) is False

    def test_idempotence_holds_on_left_projection(self):
        # Left-proj is trivially idempotent: a*a = a.
        eq = parse_equation("x*x = x")
        assert equation_holds(eq, [[0, 0], [1, 1]]) is True

    def test_associativity_fails_on_xor(self):
        # (x*y)*z = x*(y*z) — XOR IS associative, this should hold.
        eq = parse_equation("(x*y)*z = x*(y*z)")
        assert equation_holds(eq, [[0, 1], [1, 0]]) is True

    def test_associativity_fails_on_subtraction_mod3(self):
        # Build subtraction mod 3 — commutative? No. Associative? No.
        # a-b mod 3 → table[a][b] = (a-b)%3
        sub3 = [[(a - b) % 3 for b in range(3)] for a in range(3)]
        assoc = parse_equation("(x*y)*z = x*(y*z)")
        assert equation_holds(assoc, sub3) is False

    def test_reflexive_equation_always_holds(self):
        eq = parse_equation("x = x")
        for table in ([[0, 0], [0, 0]], [[0, 1], [1, 0]], [[1, 1], [1, 1]]):
            assert equation_holds(eq, table) is True


# ---------------------------------------------------------------------------
# find_falsifying_assignment — for debugging / witness display
# ---------------------------------------------------------------------------


class TestFindFalsifyingAssignment:
    def test_returns_assignment_when_equation_fails(self):
        eq = parse_equation("x*x = x")
        env = find_falsifying_assignment(eq, [[0, 1], [1, 0]])
        # The first failing assignment in lexicographic order: x=1 (since x=0 gives 0=0).
        assert env is not None
        # Sanity: lhs at env != rhs at env on this magma.
        from magmaexplorer.magma_eval import eval_term as _eval
        assert _eval(eq.lhs, env, [[0, 1], [1, 0]]) != _eval(eq.rhs, env, [[0, 1], [1, 0]])

    def test_returns_none_when_equation_holds(self):
        eq = parse_equation("x*y = y*x")
        assert find_falsifying_assignment(eq, [[0, 1], [1, 0]]) is None
