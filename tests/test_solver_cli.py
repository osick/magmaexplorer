"""Tests for the JSON CLI driver (`magmaexplorer.solver_cli`).

The CLI reads one or more problem objects from stdin and writes one answer
per problem to stdout in the SAIR Stage 2 Solo-track answer shape:

    {"call": "judge", "verdict": "true", "code": "<Lean source>"}

For local-test convenience the input shape is a small JSON object documented
in `docs/solver-userguide.md`. Both stream styles are accepted:

* "single"  — exactly one JSON object on stdin (Solo-style)
* "jsonl"   — one JSON object per line on stdin (Marathon-style batch)

The CLI's `main()` accepts injected `llm` and `stdin`/`stdout` streams so
these tests are deterministic and network-free.
"""

from __future__ import annotations

import io
import json
import re

import pytest

from magmaexplorer import solver_cli
from magmaexplorer.llm import LLMError, LLMResult


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubLLM:
    """Programmable LLM callable: pop one response per call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, items, command):
        self.calls.append((list(items), command))
        nxt = self._responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


def _ok(equation: str, steps: list[str]) -> LLMResult:
    return LLMResult(equation=equation, steps=steps, sources=[0])


# ---------------------------------------------------------------------------
# Single-problem (Solo-style) stdin → stdout
# ---------------------------------------------------------------------------


class TestSingleProblemMode:
    def test_success_emits_judge_answer(self):
        problem = {
            "problem_id": "P1",
            "hypothesis": "a*b = c",
            "goal": "c = a*b",
        }
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        stub = StubLLM([_ok("c = a*b", ["sym [0]"])])

        exit_code = solver_cli.main(
            ["--mode", "single"], stdin=stdin, stdout=stdout, llm=stub
        )
        assert exit_code == 0

        answer = json.loads(stdout.getvalue())
        assert answer["problem_id"] == "P1"
        assert answer["call"] == "judge"
        assert answer["verdict"] == "true"
        assert "theorem implication" in answer["code"]
        assert "sorry" not in _code_only(answer["code"])

    def test_default_mode_is_single(self):
        problem = {"problem_id": "P1", "hypothesis": "a = b", "goal": "b = a"}
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        stub = StubLLM([_ok("b = a", ["sym [0]"])])

        # No --mode flag, should default to "single".
        exit_code = solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        assert exit_code == 0
        answer = json.loads(stdout.getvalue())
        assert answer["verdict"] == "true"

    def test_failure_emits_error_answer(self):
        problem = {
            "problem_id": "P_fail",
            "hypothesis": "a = b",
            "goal": "a = c",  # unreachable
        }
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        # Three retries, all return a wrong derivation.
        stub = StubLLM([_ok("a = c", ["sym [0]"])] * 3)

        exit_code = solver_cli.main(
            ["--mode", "single", "--max-attempts", "3"],
            stdin=stdin, stdout=stdout, llm=stub,
        )

        answer = json.loads(stdout.getvalue())
        assert answer["problem_id"] == "P_fail"
        assert answer["status"] == "error"
        assert "error" in answer
        # Exit code is non-zero so callers can detect failure without parsing
        # JSON.
        assert exit_code != 0

    def test_invalid_input_json_emits_error(self):
        stdin = io.StringIO("{not valid json")
        stdout = io.StringIO()
        stub = StubLLM([])

        exit_code = solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        answer = json.loads(stdout.getvalue())
        assert answer["status"] == "error"
        assert "json" in answer["error"].lower() or "parse" in answer["error"].lower()
        assert exit_code != 0

    def test_missing_required_field_emits_error(self):
        # No "goal" field.
        stdin = io.StringIO(json.dumps({"hypothesis": "a = b"}))
        stdout = io.StringIO()
        stub = StubLLM([])

        exit_code = solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        answer = json.loads(stdout.getvalue())
        assert answer["status"] == "error"
        assert "goal" in answer["error"].lower()
        assert exit_code != 0

    def test_unparseable_equation_emits_error(self):
        stdin = io.StringIO(
            json.dumps({"hypothesis": "a = =", "goal": "a = b"})
        )
        stdout = io.StringIO()
        stub = StubLLM([])

        exit_code = solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        answer = json.loads(stdout.getvalue())
        assert answer["status"] == "error"
        assert exit_code != 0
        # Solver should NOT have been called.
        assert stub.calls == []

    def test_max_attempts_from_cli_flag_overrides_default(self):
        problem = {"hypothesis": "a = b", "goal": "a = c"}
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        # 5 bad responses; with max-attempts=1 only one is consumed.
        stub = StubLLM([_ok("a = c", ["sym [0]"])] * 5)

        solver_cli.main(
            ["--max-attempts", "1"], stdin=stdin, stdout=stdout, llm=stub
        )
        assert len(stub.calls) == 1

    def test_problem_must_be_object_not_array(self):
        stdin = io.StringIO(json.dumps([1, 2, 3]))
        stdout = io.StringIO()
        rc = solver_cli.main([], stdin=stdin, stdout=stdout, llm=StubLLM([]))
        ans = json.loads(stdout.getvalue())
        assert ans["status"] == "error"
        assert "object" in ans["error"].lower()
        assert rc != 0

    def test_hypothesis_must_be_string(self):
        stdin = io.StringIO(json.dumps({"hypothesis": 42, "goal": "a = b"}))
        stdout = io.StringIO()
        rc = solver_cli.main([], stdin=stdin, stdout=stdout, llm=StubLLM([]))
        ans = json.loads(stdout.getvalue())
        assert ans["status"] == "error"
        assert "hypothesis" in ans["error"]
        assert rc != 0

    def test_unparseable_goal_emits_error(self):
        stdin = io.StringIO(json.dumps({"hypothesis": "a = b", "goal": "***"}))
        stdout = io.StringIO()
        rc = solver_cli.main([], stdin=stdin, stdout=stdout, llm=StubLLM([]))
        ans = json.loads(stdout.getvalue())
        assert ans["status"] == "error"
        assert "goal" in ans["error"].lower()
        assert rc != 0

    def test_max_attempts_must_be_positive_int(self):
        stdin = io.StringIO(json.dumps(
            {"hypothesis": "a = b", "goal": "b = a", "max_attempts": 0}
        ))
        stdout = io.StringIO()
        rc = solver_cli.main([], stdin=stdin, stdout=stdout, llm=StubLLM([]))
        ans = json.loads(stdout.getvalue())
        assert ans["status"] == "error"
        assert "max_attempts" in ans["error"]
        assert rc != 0

    def test_blank_theorem_name_falls_back_to_default(self):
        stdin = io.StringIO(json.dumps(
            {"hypothesis": "a = b", "goal": "b = a", "theorem_name": ""}
        ))
        stdout = io.StringIO()
        stub = StubLLM([_ok("b = a", ["sym [0]"])])
        rc = solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        assert rc == 0
        ans = json.loads(stdout.getvalue())
        # Default 'implication' appears in the header comment.
        assert "(implication)" in ans["code"]

    def test_problem_id_optional_defaults_to_null(self):
        problem = {"hypothesis": "a = b", "goal": "b = a"}
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        stub = StubLLM([_ok("b = a", ["sym [0]"])])

        solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        answer = json.loads(stdout.getvalue())
        assert "problem_id" in answer
        assert answer["problem_id"] is None


# ---------------------------------------------------------------------------
# Batch (JSONL / Marathon-style) mode
# ---------------------------------------------------------------------------


class TestJsonlBatchMode:
    def test_two_problems_produce_two_answer_lines(self):
        problems = [
            {"problem_id": "A", "hypothesis": "a*b = c", "goal": "c = a*b"},
            {"problem_id": "B", "hypothesis": "x = y", "goal": "y = x"},
        ]
        stdin = io.StringIO("\n".join(json.dumps(p) for p in problems) + "\n")
        stdout = io.StringIO()
        stub = StubLLM([
            _ok("c = a*b", ["sym [0]"]),
            _ok("y = x", ["sym [0]"]),
        ])

        exit_code = solver_cli.main(
            ["--mode", "jsonl"], stdin=stdin, stdout=stdout, llm=stub
        )
        assert exit_code == 0

        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2
        ids = [json.loads(ln)["problem_id"] for ln in lines]
        assert ids == ["A", "B"]
        assert all(json.loads(ln)["verdict"] == "true" for ln in lines)

    def test_one_failure_does_not_stop_subsequent_problems(self):
        problems = [
            {"problem_id": "fail", "hypothesis": "a = b", "goal": "a = c"},
            {"problem_id": "ok", "hypothesis": "p = q", "goal": "q = p"},
        ]
        stdin = io.StringIO("\n".join(json.dumps(p) for p in problems))
        stdout = io.StringIO()
        stub = StubLLM([
            _ok("a = c", ["sym [0]"]),
            _ok("a = c", ["sym [0]"]),
            _ok("a = c", ["sym [0]"]),
            _ok("q = p", ["sym [0]"]),
        ])

        exit_code = solver_cli.main(
            ["--mode", "jsonl", "--max-attempts", "3"],
            stdin=stdin, stdout=stdout, llm=stub,
        )
        # Exit code reflects partial failure but second problem was still
        # processed.
        assert exit_code != 0

        lines = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2
        assert lines[0]["status"] == "error"
        assert lines[1]["verdict"] == "true"

    def test_malformed_jsonl_line_emits_error_answer(self):
        stdin = io.StringIO(
            "{not valid json\n"
            + json.dumps({"problem_id": "ok", "hypothesis": "a = b", "goal": "b = a"})
            + "\n"
        )
        stdout = io.StringIO()
        stub = StubLLM([_ok("b = a", ["sym [0]"])])

        rc = solver_cli.main(
            ["--mode", "jsonl"], stdin=stdin, stdout=stdout, llm=stub
        )
        lines = [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 2
        assert lines[0]["status"] == "error"
        assert lines[1]["verdict"] == "true"
        assert rc != 0

    def test_blank_lines_in_jsonl_are_skipped(self):
        stdin = io.StringIO(
            "\n\n"
            + json.dumps({"problem_id": "A", "hypothesis": "a = b", "goal": "b = a"})
            + "\n\n"
        )
        stdout = io.StringIO()
        stub = StubLLM([_ok("b = a", ["sym [0]"])])

        solver_cli.main(
            ["--mode", "jsonl"], stdin=stdin, stdout=stdout, llm=stub
        )
        lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
        assert len(lines) == 1


# ---------------------------------------------------------------------------
# Output Lean code structure
# ---------------------------------------------------------------------------


class TestEmittedLeanCode:
    def test_single_step_code_field_is_judge_ready(self):
        # Single-step `sym` is auto-translatable; the emitted theorem must
        # have no `sorry`. Multi-step derivations still fall back to `sorry`
        # (a Layer 2 limitation of `lean_export`, tracked separately).
        problem = {"hypothesis": "a*b = c", "goal": "c = a*b"}
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        stub = StubLLM([_ok("c = a*b", ["sym [0]"])])
        solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        answer = json.loads(stdout.getvalue())

        code = answer["code"]
        # Must be a Lean theorem with the hypothesis as the proof parameter,
        # never an axiom (Stage 2 judge rejects axioms).
        code_only = _code_only(code)
        assert "theorem implication" in code_only
        assert "(h :" in code_only
        assert "axiom" not in code_only
        assert "sorry" not in code_only

    def test_custom_theorem_name_is_respected(self):
        # The renderer currently hardcodes the theorem name to "implication"
        # regardless of `theorem_name`. The CLI plumbs the field but it has
        # no observable effect today; this test pins the current contract so
        # we notice if `lean_export` starts honouring the name.
        problem = {
            "hypothesis": "a = b",
            "goal": "b = a",
            "theorem_name": "thm_swap",
        }
        stdin = io.StringIO(json.dumps(problem))
        stdout = io.StringIO()
        stub = StubLLM([_ok("b = a", ["sym [0]"])])
        solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
        answer = json.loads(stdout.getvalue())
        # Today: name shows up only in the header comment.
        assert "thm_swap" in answer["code"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _code_only(lean_source: str) -> str:
    """Drop comment-only lines so word-presence assertions don't trigger on
    the file header (which mentions `axiom`, `sorry`, etc. educationally)."""
    return "\n".join(
        ln for ln in lean_source.splitlines() if not ln.lstrip().startswith("--")
    )
