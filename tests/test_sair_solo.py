"""Tests for `magmaexplorer.sair_solo` — the Stage 2 Solo protocol loop.

The loop reads JSON messages line-by-line from stdin and writes them to
stdout. Tests pre-script the proxy's responses into a `StringIO` stdin,
run `main()`, then parse the StringIO it wrote to.
"""

from __future__ import annotations

import io
import json

from magmaexplorer import sair_solo


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _proxy_script(start: dict, *responses: dict) -> io.StringIO:
    """Build a stdin StringIO from a start message + ordered proxy replies."""
    lines = [json.dumps(start)] + [json.dumps(r) for r in responses]
    return io.StringIO("\n".join(lines) + "\n")


def _parse_stdout(stdout: io.StringIO) -> list[dict]:
    return [json.loads(ln) for ln in stdout.getvalue().splitlines() if ln.strip()]


def _start(eq1: str = "x*y = y*x", eq2: str = "b*a = a*b") -> dict:
    return {
        "problem": {
            "id": "test_001",
            "eq1_id": 0,
            "eq2_id": 1,
            "equation1": eq1,
            "equation2": eq2,
        },
        "budget": {"timeout_seconds": 3600, "max_code_length": 100000},
    }


def _llm_reply(steps: list[str], equation: str = "b*a = a*b") -> dict:
    """A well-formed LLM proxy response containing parseable solver JSON."""
    return {"response": json.dumps({"steps": steps, "equation": equation})}


# ---------------------------------------------------------------------------
# PROMPT constant
# ---------------------------------------------------------------------------


class TestPromptConstant:
    def test_prompt_is_str(self):
        assert isinstance(sair_solo.PROMPT, str)
        assert len(sair_solo.PROMPT) > 0

    def test_prompt_has_required_placeholders(self):
        # The proxy auto-fills {problem.*} and {history.*}; we fill {solver.*}.
        assert "{problem.equation1}" in sair_solo.PROMPT
        assert "{problem.equation2}" in sair_solo.PROMPT
        assert "{solver.last_attempt_summary}" in sair_solo.PROMPT


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_one_round_accepted(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert len(sent) == 2
        assert sent[0]["call"] == "llm"
        assert sent[1]["call"] == "judge"
        assert sent[1]["verdict"] == "true"
        # Lean code uses the SAIR submission shape with ◇.
        assert "import JudgeProblem" in sent[1]["code"]
        assert "def submission : Goal := by" in sent[1]["code"]
        assert " * " not in sent[1]["code"].replace("--", "")

    def test_first_llm_context_has_round_zero(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        sair_solo.main(stdin=stdin, stdout=stdout)
        sent = _parse_stdout(stdout)
        assert sent[0]["context"]["round"] == "0"
        # On the first round there's no prior summary.
        assert sent[0]["context"]["last_attempt_summary"] == ""


# ---------------------------------------------------------------------------
# Retry on judge rejection
# ---------------------------------------------------------------------------


class TestJudgeRejectionRetry:
    def test_judge_incorrect_triggers_retry(self):
        # Round 0: LLM returns a verifying derivation; judge rejects (incorrect).
        # Round 1: LLM returns the same derivation; judge accepts.
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "incorrect", "stderr": "type mismatch foo bar"},
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        sent = _parse_stdout(stdout)
        # llm, judge(rejected), llm, judge(accepted)
        assert [m["call"] for m in sent] == ["llm", "judge", "llm", "judge"]
        # Round-1 context carries the judge error summary.
        round1_ctx = sent[2]["context"]
        assert round1_ctx["round"] == "1"
        assert "incorrect" in round1_ctx["last_attempt_summary"]
        assert "type mismatch" in round1_ctx["last_attempt_summary"]


# ---------------------------------------------------------------------------
# Retry on local DSL verification failure
# ---------------------------------------------------------------------------


class TestDslVerificationRetry:
    def test_unparseable_steps_trigger_retry_without_judge_call(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["garbage step", "more garbage"]),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        sent = _parse_stdout(stdout)
        # No judge call after the bad round.
        assert [m["call"] for m in sent] == ["llm", "llm", "judge"]
        # Round-1 carries a DSL-failure summary.
        assert "DSL" in sent[1]["context"]["last_attempt_summary"]

    def test_wrong_final_equation_triggers_retry(self):
        # `sym [0]` of `x*y = y*x` gives `y*x = x*y`, NOT `b*a = a*b`.
        stdin = _proxy_script(
            _start(),
            _llm_reply(["sym [0]"]),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "llm", "judge"]

    def test_malformed_llm_response_triggers_retry(self):
        stdin = _proxy_script(
            _start(),
            {"response": "not json at all"},
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "llm", "judge"]


# ---------------------------------------------------------------------------
# Graceful exit conditions
# ---------------------------------------------------------------------------


class TestGracefulExit:
    def test_proxy_closes_stdin_after_start(self):
        # Proxy sends start, then closes stdin (simulating budget exhaustion
        # right before the first LLM reply).
        stdin = io.StringIO(json.dumps(_start()) + "\n")
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        # The solver should not crash; it sends one LLM call then exits.
        assert rc != 0
        sent = _parse_stdout(stdout)
        assert len(sent) == 1 and sent[0]["call"] == "llm"

    def test_llm_error_response_aborts(self):
        # Proxy responds with {"error": "..."} on the LLM call (budget gone).
        stdin = _proxy_script(
            _start(),
            {"error": "token budget exhausted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc != 0


# ---------------------------------------------------------------------------
# Code shape: no banned constructs
# ---------------------------------------------------------------------------


class TestEmittedCodeShape:
    def test_emitted_code_has_no_star_operator(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        sair_solo.main(stdin=stdin, stdout=stdout)
        sent = _parse_stdout(stdout)
        code = sent[1]["code"]
        non_comment = [ln for ln in code.splitlines() if not ln.lstrip().startswith("--")]
        assert " * " not in "\n".join(non_comment)

    def test_emitted_code_has_no_sorry(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=a, y:=b", "sym s1"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        sair_solo.main(stdin=stdin, stdout=stdout)
        sent = _parse_stdout(stdout)
        assert "sorry" not in sent[1]["code"]
