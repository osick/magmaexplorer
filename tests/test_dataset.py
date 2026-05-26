"""Validator tests for `tests/data/sair_problems.json`.

Three guarantees:
 1. Schema: every entry has the required fields; verdicts are well-formed.
 2. Soundness of `verdict: "false"` entries: the witness magma really
    satisfies eq1 and falsifies eq2.
 3. Optional `tactic` fields on `verdict: "true"` entries actually verify
    via `verify_derivation`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from magmaexplorer.magma_eval import equation_holds
from magmaexplorer.solver import verify_derivation
from magmaexplorer.term import parse_equation


DATASET_PATH = Path(__file__).parent / "data" / "sair_problems.json"


def _load() -> list[dict]:
    assert DATASET_PATH.exists(), f"missing dataset file: {DATASET_PATH}"
    return json.loads(DATASET_PATH.read_text())


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    def test_dataset_loads(self):
        entries = _load()
        assert isinstance(entries, list)
        assert len(entries) >= 15, "dataset should have at least 15 entries"

    def test_entries_have_required_fields(self):
        for entry in _load():
            for field in ("id", "equation1", "equation2", "verdict"):
                assert field in entry, f"{entry.get('id', '?')} missing {field}"
            assert entry["verdict"] in ("true", "false"), entry["id"]

    def test_ids_are_unique(self):
        ids = [e["id"] for e in _load()]
        assert len(ids) == len(set(ids)), "duplicate ids in dataset"

    def test_false_entries_have_witness(self):
        for entry in _load():
            if entry["verdict"] == "false":
                w = entry.get("witness")
                assert w is not None, f"{entry['id']} (false) missing witness"
                assert "size" in w and "table" in w, entry["id"]
                # `table` is the JSON-string finOpTable accepts ("[[0,1],[1,0]]").
                table = json.loads(w["table"])
                assert len(table) == w["size"]
                for row in table:
                    assert len(row) == w["size"]
                    assert all(0 <= x < w["size"] for x in row)

    def test_equations_parse(self):
        for entry in _load():
            parse_equation(entry["equation1"])  # raises ParseError on failure
            parse_equation(entry["equation2"])

    def test_dataset_has_both_verdicts(self):
        verdicts = {e["verdict"] for e in _load()}
        assert verdicts == {"true", "false"}, (
            "dataset must contain at least one true and one false entry"
        )


# ---------------------------------------------------------------------------
# Witness soundness — false entries
# ---------------------------------------------------------------------------


class TestFalseWitnesses:
    @pytest.mark.parametrize(
        "entry",
        [e for e in _load() if e["verdict"] == "false"],
        ids=lambda e: e["id"],
    )
    def test_witness_satisfies_equation1(self, entry):
        eq1 = parse_equation(entry["equation1"])
        table = json.loads(entry["witness"]["table"])
        assert equation_holds(eq1, table), (
            f"{entry['id']}: witness should satisfy eq1 {entry['equation1']}"
        )

    @pytest.mark.parametrize(
        "entry",
        [e for e in _load() if e["verdict"] == "false"],
        ids=lambda e: e["id"],
    )
    def test_witness_falsifies_equation2(self, entry):
        eq2 = parse_equation(entry["equation2"])
        table = json.loads(entry["witness"]["table"])
        assert not equation_holds(eq2, table), (
            f"{entry['id']}: witness should falsify eq2 {entry['equation2']}"
        )


# ---------------------------------------------------------------------------
# Optional tactics — true entries
# ---------------------------------------------------------------------------


class TestTacticsVerify:
    @pytest.mark.parametrize(
        "entry",
        [e for e in _load() if e["verdict"] == "true" and e.get("tactic")],
        ids=lambda e: e["id"],
    )
    def test_tactic_verifies(self, entry):
        eq1 = parse_equation(entry["equation1"])
        eq2 = parse_equation(entry["equation2"])
        result = verify_derivation(entry["tactic"], [eq1], expected_final=eq2)
        assert result.ok, (
            f"{entry['id']}: tactic failed — {result.error}\n"
            + "\n".join(f"  {n}" for n in result.narrated_steps)
        )
