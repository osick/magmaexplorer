"""Tests for `magmaexplorer.false_cert` — Step 3 of the SAIR roadmap.

Two pure functions under test:
 * `search_counterexample(eq1, eq2, max_size)` — brute-force enumerate
   finite-magma tables, return one satisfying eq1 and falsifying eq2.
 * `render_false_cert(n, table)` — emit the Lean blob from the readme.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magmaexplorer.false_cert import render_false_cert, search_counterexample
from magmaexplorer.magma_eval import equation_holds
from magmaexplorer.term import parse_equation


# ---------------------------------------------------------------------------
# search_counterexample
# ---------------------------------------------------------------------------


class TestSearchCounterexample:
    def test_finds_witness_for_commutativity_not_idempotence(self):
        # x*y = y*x  does NOT imply  x*x = x.
        # XOR on Fin 2 is commutative but 1*1=0 ≠ 1.
        eq1 = parse_equation("x*y = y*x")
        eq2 = parse_equation("x*x = x")
        result = search_counterexample(eq1, eq2, max_size=2)
        assert result is not None
        n, table = result
        assert equation_holds(eq1, table)
        assert not equation_holds(eq2, table)

    def test_returns_none_when_implication_holds(self):
        # x*y = y*x  implies  b*a = a*b  — same equation alpha-renamed.
        eq1 = parse_equation("x*y = y*x")
        eq2 = parse_equation("b*a = a*b")
        assert search_counterexample(eq1, eq2, max_size=2) is None

    def test_returns_none_for_reflexive_goal(self):
        # Anything implies x = x — no counterexample exists at any size.
        eq1 = parse_equation("x*y = y*x")
        eq2 = parse_equation("x = x")
        assert search_counterexample(eq1, eq2, max_size=2) is None

    def test_max_size_one_returns_none(self):
        # Search only meaningfully starts at Fin 2 (Fin 1 is the trivial
        # one-element magma where every equation holds).
        eq1 = parse_equation("x*y = y*x")
        eq2 = parse_equation("x*x = x")
        assert search_counterexample(eq1, eq2, max_size=1) is None

    def test_returns_smallest_n_first(self):
        # The commutativity / idempotence pair has a Fin 2 witness; the
        # search must prefer it over any Fin 3 witness.
        eq1 = parse_equation("x*y = y*x")
        eq2 = parse_equation("x*x = x")
        result = search_counterexample(eq1, eq2, max_size=3)
        assert result is not None
        n, _ = result
        assert n == 2


# ---------------------------------------------------------------------------
# render_false_cert
# ---------------------------------------------------------------------------


class TestRenderFalseCert:
    def test_emits_required_imports(self):
        # readme §Available Imports + §False certificate.
        code = render_false_cert(2, [[0, 1], [1, 0]])
        assert "import JudgeProblem" in code
        assert "import JudgeDecide.DecideBang" in code
        assert "import JudgeFinOp.MemoFinOp" in code
        assert "open MemoFinOp" in code

    def test_uses_finOpTable_and_decideFin(self):
        code = render_false_cert(2, [[0, 1], [1, 0]])
        assert "finOpTable" in code
        assert "decideFin!" in code

    def test_table_appears_verbatim_in_lean_string_literal(self):
        # finOpTable parses its arg as JSON; the literal must be compact.
        code = render_false_cert(2, [[0, 1], [1, 0]])
        assert '"[[0,1],[1,0]]"' in code

    def test_n_appears_in_lean_type(self):
        code = render_false_cert(3, [[0, 0, 0], [0, 0, 0], [0, 0, 0]])
        assert "Fin 3" in code

    def test_emitted_code_under_20kb(self):
        # SAIR readme §Constraints: false certificates are capped at 20,000 bytes.
        for n, table in [
            (2, [[0, 1], [1, 0]]),
            (3, [[0, 1, 2], [1, 2, 0], [2, 0, 1]]),
        ]:
            code = render_false_cert(n, table)
            assert len(code.encode("utf-8")) < 20_000

    def test_no_banned_tokens(self):
        # Must pass through the sair_solo emission guard.
        from magmaexplorer.sair_solo import BANNED_TOKENS
        code = render_false_cert(2, [[0, 1], [1, 0]])
        for tok in BANNED_TOKENS:
            assert tok not in code, f"emitted false cert contains banned {tok!r}"


# ---------------------------------------------------------------------------
# Regression — every false dataset entry must have a discoverable witness
# at max_size=2 (all current ETP witnesses are Fin 2).
# ---------------------------------------------------------------------------


DATASET = json.loads(
    (Path(__file__).parent / "data" / "sair_problems.json").read_text()
)


class TestDatasetCoverage:
    @pytest.mark.parametrize(
        "entry",
        [e for e in DATASET if e["verdict"] == "false"],
        ids=lambda e: e["id"],
    )
    def test_search_finds_a_witness(self, entry):
        eq1 = parse_equation(entry["equation1"])
        eq2 = parse_equation(entry["equation2"])
        result = search_counterexample(eq1, eq2, max_size=2)
        assert result is not None, f"no witness found for {entry['id']}"
        n, table = result
        # Sanity: the found witness must satisfy eq1 and falsify eq2.
        assert equation_holds(eq1, table), entry["id"]
        assert not equation_holds(eq2, table), entry["id"]
