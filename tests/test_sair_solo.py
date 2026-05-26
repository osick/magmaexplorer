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


# ---------------------------------------------------------------------------
# Emission guards — banned tokens + code size cap
#
# SAIR readme §Constraints:
#   Banned tokens: sorry, admit, sorryAx, dbg_trace, dbgTrace,
#                  run_tac, mkSorry, initialize, builtin_initialize
#   Max code length: 100,000 characters (true cert)
#   Max false certificate code: 20,000 bytes
#
# If the renderer hands us code that violates either, we MUST NOT send the
# judge call (it would map to incomplete_proof / malformed). Loop with a
# `last_attempt_summary` instead.
# ---------------------------------------------------------------------------


import pytest


class TestEmissionGuards:
    @pytest.mark.parametrize(
        "banned_token",
        ["sorry", "admit", "sorryAx", "dbg_trace", "dbgTrace",
         "run_tac", "mkSorry", "initialize", "builtin_initialize"],
    )
    def test_banned_token_in_code_skips_judge_call(self, monkeypatch, banned_token):
        # Force render to inject a banned token into otherwise-valid Lean.
        bad_code = (
            f"import JudgeProblem\n\ndef submission : Goal := by\n  {banned_token}\n"
        )
        monkeypatch.setattr(sair_solo, "render_sair_submission",
                            lambda *a, **kw: bad_code)
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            # No further proxy responses — stdin closes after the retry's llm call.
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)

        sent = _parse_stdout(stdout)
        # Expected sequence: llm (round 0), llm (round 1, retry); no judge call.
        assert [m["call"] for m in sent] == ["llm", "llm"], (
            f"banned token {banned_token!r} should suppress the judge call"
        )
        assert rc == 1  # exited because stdin closed before judge call
        # Retry summary mentions the violation.
        summary = sent[1]["context"]["last_attempt_summary"]
        assert "banned" in summary.lower() or banned_token in summary

    def test_oversized_true_cert_skips_judge_call(self, monkeypatch):
        # 200 KB of valid-looking Lean — over the 100,000-char limit.
        oversized = "import JudgeProblem\n" + ("-- pad line\n" * 18000)
        assert len(oversized) > 100_000
        monkeypatch.setattr(sair_solo, "render_sair_submission",
                            lambda *a, **kw: oversized)
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
        )
        stdout = io.StringIO()
        sair_solo.main(stdin=stdin, stdout=stdout)

        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "llm"]
        summary = sent[1]["context"]["last_attempt_summary"]
        assert "cap" in summary.lower() or "exceed" in summary.lower() or "too large" in summary.lower()

    def test_clean_code_still_passes_through(self):
        # Sanity: with no monkeypatching, the existing happy path still works.
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "judge"]


# ---------------------------------------------------------------------------
# Verdict dispatch — Step 4
#
# Before invoking the LLM, the solver runs a brute-force counterexample
# search.  If a witness exists at small Fin n, submit verdict="false"
# directly; otherwise fall through to the existing LLM-driven true-cert
# loop.  Spec: competition.md §"Core Task" — both directions need certs.
# ---------------------------------------------------------------------------


def _false_implication_start() -> dict:
    """Start message for a pair that does NOT imply (commutativity → idempotence).

    Witness: any non-idempotent commutative magma on Fin 2 (e.g. [[0,0],[0,0]])."""
    return {
        "problem": {
            "id": "false_test",
            "eq1_id": 43,
            "eq2_id": 3,
            "equation1": "x*y = y*x",
            "equation2": "x*x = x",
        },
        "budget": {"timeout_seconds": 3600, "max_code_length": 100000},
    }


class TestVerdictDispatch:
    def test_false_implication_submits_false_cert_without_llm_call(self):
        stdin = _proxy_script(
            _false_implication_start(),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout, max_size=2)
        assert rc == 0
        sent = _parse_stdout(stdout)
        # Exactly one judge call, no LLM call.
        assert [m["call"] for m in sent] == ["judge"]
        assert sent[0]["verdict"] == "false"
        assert "finOpTable" in sent[0]["code"]
        assert "decideFin!" in sent[0]["code"]

    def test_true_implication_still_uses_llm_loop(self):
        # The existing happy path: x*y=y*x → b*a=a*b (true, no counterexample).
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout, max_size=2)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "judge"]
        assert sent[1]["verdict"] == "true"

    def test_false_cert_rejection_falls_back_to_llm_loop(self):
        # Judge rejects the false cert — solver should still emit an LLM
        # call as a fallback (the LLM might find a different angle).
        stdin = _proxy_script(
            _false_implication_start(),
            {"status": "incorrect", "stderr": "synthetic failure"},
            # Stdin closes — solver exits 1 after the fallback LLM call.
        )
        stdout = io.StringIO()
        sair_solo.main(stdin=stdin, stdout=stdout, max_size=2)
        sent = _parse_stdout(stdout)
        # Sequence: judge(false) → llm(fallback).
        assert sent[0]["call"] == "judge" and sent[0]["verdict"] == "false"
        assert sent[1]["call"] == "llm"
        # Fallback summary should mention the rejection.
        summary = sent[1]["context"]["last_attempt_summary"]
        assert "incorrect" in summary or "judge" in summary.lower()

    def test_search_skipped_when_max_size_is_one(self):
        # max_size=1 disables the search (Fin 1 is trivial). The solver
        # should skip the false-cert path entirely and go straight to LLM.
        stdin = _proxy_script(
            _false_implication_start(),
            _llm_reply(["inst [0] x:=b, y:=a"]),  # will fail to reach goal, but loop continues
            # Stdin closes — solver exits.
        )
        stdout = io.StringIO()
        sair_solo.main(stdin=stdin, stdout=stdout, max_size=1)
        sent = _parse_stdout(stdout)
        # First message must be LLM, not judge.
        assert sent[0]["call"] == "llm"


# ---------------------------------------------------------------------------
# Step 5a — ban `expand` / `fold` DSL primitives in the LLM reply.
#
# `lean_export.proof_body` emits `sorry` for these (they reference DSL
# definitions that have no direct Lean counterpart). `sorry` is on the
# SAIR banned-token list → would always be rejected by the judge. Reject
# the LLM reply at the solver layer so the LLM gets a chance to rewrite
# its derivation without these primitives.
# ---------------------------------------------------------------------------


class TestExpandFoldBan:
    def test_expand_in_steps_triggers_retry(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["expand s1 using d_1"]),     # would render to sorry
            _llm_reply(["inst [0] x:=b, y:=a"]),     # clean fallback
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout, max_size=2)
        assert rc == 0
        sent = _parse_stdout(stdout)
        # No judge call after the banned step.
        assert [m["call"] for m in sent] == ["llm", "llm", "judge"]
        # Round-1 carries an explanation that mentions the banned primitive.
        summary = sent[1]["context"]["last_attempt_summary"]
        assert "expand" in summary.lower()

    def test_fold_in_steps_triggers_retry(self):
        stdin = _proxy_script(
            _start(),
            _llm_reply(["fold s1 using d_2"]),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout, max_size=2)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "llm", "judge"]
        summary = sent[1]["context"]["last_attempt_summary"]
        assert "fold" in summary.lower()

    def test_mixed_clean_and_banned_steps_still_triggers_retry(self):
        # If ANY step uses expand/fold, reject the whole reply (don't try
        # to verify and let the bad step blow up downstream).
        stdin = _proxy_script(
            _start(),
            _llm_reply(["inst [0] x:=b, y:=a", "expand s1 using d_1"]),
            _llm_reply(["inst [0] x:=b, y:=a"]),
            {"status": "accepted"},
        )
        stdout = io.StringIO()
        rc = sair_solo.main(stdin=stdin, stdout=stdout, max_size=2)
        assert rc == 0
        sent = _parse_stdout(stdout)
        assert [m["call"] for m in sent] == ["llm", "llm", "judge"]
