"""SAIR Stage 2 Solo-track driver — the stdin/stdout JSON protocol loop.

Runnable as `python3 -m magmaexplorer.sair_solo`. The proxy launches this
process, writes a `{"problem": ..., "budget": ...}` start message to its
stdin, and then mediates `{"call": "llm", ...}` / `{"call": "judge", ...}`
requests until the judge returns `accepted` or the wall-clock runs out.

PROMPT is a top-level string constant so the proxy can extract it via static
AST parsing (no import / no execution on the proxy host).

This iteration covers verdict="true" only. False certificates (brute-force
counterexample search + `decideFin!` Lean) are a separate iteration.
"""

from __future__ import annotations

import io
import json
import re
import sys
from typing import IO, Optional

from .entries import Entry
from .lean_export import render_sair_submission
from .solver import VerificationResult, verify_derivation
from .term import ParseError, parse_equation


PROMPT = """You are deriving an equational implication over magmas.

Hypothesis: {problem.equation1}
Goal:       {problem.equation2}

Express the derivation as DSL steps using these primitives:
  sym <ref>
  inst <ref> v1:=t1, v2:=t2, ...
  trans <ref> <ref>
  rewrite <ref> using <ref>
  expand <ref> using <def-ref>
  fold   <ref> using <def-ref>

<ref> is [0] (the hypothesis) or s1, s2, ... (earlier step results).
The final step MUST produce exactly: {problem.equation2}

Round: {history.round}
Previous attempts (judge feedback): {history.attempts}
Most recent local error: {solver.last_attempt_summary}

Respond with ONLY a JSON object, no markdown fences:
{{"steps": ["...", "..."], "equation": "<goal as string>"}}
"""


# ---------------------------------------------------------------------------
# Protocol I/O
# ---------------------------------------------------------------------------


def _read(stdin: IO[str]) -> Optional[dict]:
    line = stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line.strip())
    except json.JSONDecodeError:
        return None


def _send(stdout: IO[str], msg: dict) -> None:
    stdout.write(json.dumps(msg) + "\n")
    stdout.flush()


# ---------------------------------------------------------------------------
# LLM response parsing
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> Optional[dict]:
    """Lift a JSON object out of the LLM's free-text response.

    Strips `<think>...</think>` blocks and ``` fences, then tries the whole
    string and falls back to the first `{...}` blob."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ---------------------------------------------------------------------------
# Failure summarizers (fed into the {solver.last_attempt_summary} slot)
# ---------------------------------------------------------------------------


def _summarize_dsl_failure(v: VerificationResult) -> str:
    body = "\n".join(f"  {i}. {n}" for i, n in enumerate(v.narrated_steps, 1))
    return f"DSL verification failed: {v.error}\n{body}"


def _summarize_judge_failure(status: str, stderr: str) -> str:
    return f"Lean judge returned {status}: {stderr[:500]}"


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def main(
    *,
    stdin: Optional[IO[str]] = None,
    stdout: Optional[IO[str]] = None,
) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    start = _read(stdin)
    if start is None or "problem" not in start:
        return 1
    problem = start["problem"]

    try:
        from_eq = parse_equation(problem["equation1"])
        to_eq = parse_equation(problem["equation2"])
    except (ParseError, KeyError):
        return 1

    last_summary = ""
    round_num = 0

    while True:
        _send(stdout, {
            "call": "llm",
            "context": {
                "round": str(round_num),
                "last_attempt_summary": last_summary,
            },
        })
        round_num += 1

        llm_resp = _read(stdin)
        if llm_resp is None or "error" in llm_resp:
            return 1

        parsed = _extract_json(llm_resp.get("response", ""))
        if not isinstance(parsed, dict) or "steps" not in parsed:
            last_summary = "LLM response was not valid JSON or missing 'steps'."
            continue
        steps = parsed["steps"]
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            last_summary = "LLM 'steps' field must be a list of strings."
            continue

        verdict = verify_derivation(steps, [from_eq], expected_final=to_eq)
        if not verdict.ok:
            last_summary = _summarize_dsl_failure(verdict)
            continue

        entries = [
            Entry(content=from_eq, sources=[], steps=[]),
            Entry(content=to_eq, sources=[0], steps=verdict.annotated_steps),
        ]
        try:
            code = render_sair_submission(entries, 0, 1)
        except Exception as exc:  # ImplicationChainError or other render error
            last_summary = f"Lean rendering failed: {exc}"
            continue

        _send(stdout, {"call": "judge", "verdict": "true", "code": code})
        judge_resp = _read(stdin)
        if judge_resp is None:
            return 1
        if judge_resp.get("status") == "accepted":
            return 0
        last_summary = _summarize_judge_failure(
            judge_resp.get("status", "unknown"),
            judge_resp.get("stderr", ""),
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
