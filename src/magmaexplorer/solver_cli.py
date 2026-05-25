"""JSON-driven command-line driver for `magmaexplorer.solver`.

This is Layer 4 step 3 of the SAIR competition pipeline: a thin shell that
reads problem JSON from stdin, calls `solver.solve_implication`, renders the
result with `lean_export.render_implication_file`, and writes a Stage 2
Solo-track answer object to stdout::

    {"problem_id": "...", "call": "judge", "verdict": "true",
     "code": "<Lean 4 source>"}

The "code" string is verified by the deterministic Lean judge. A solver
that fails to find a proof within `--max-attempts` emits an error answer::

    {"problem_id": "...", "status": "error", "error": "..."}

This module is runnable as ``python3 -m magmaexplorer.solver_cli`` (see
``__main__`` block at the bottom) and is also importable for tests, which
inject a stub LLM and in-memory stdin/stdout via the ``main()`` parameters.

Input shape — local "convenience" protocol::

    {
      "problem_id":    "str | null",   (optional)
      "hypothesis":    "lhs = rhs",    (required)
      "goal":          "lhs = rhs",    (required)
      "theorem_name":  "ident",        (optional, default "implication")
      "max_attempts":  int             (optional, overrides --max-attempts)
    }

Two stdin modes are supported:

* ``--mode single`` (default): one JSON object on stdin, one answer object on
  stdout. Matches the Solo track's stdin/stdout-JSON style.
* ``--mode jsonl``: one JSON object per line of stdin, one answer per line of
  stdout. Matches the Marathon track's manifest-in / JSONL-out style.

This driver does NOT yet speak the *exact* SAIR pipeline protocol (the
problem schema in the upstream repo may differ) — it is a local protocol
adequate for offline development and easy to wrap or adapt later.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import IO, Any, Optional

from . import lean_export, solver
from .solver import LLMCallable, SolverError
from .term import ParseError, parse_equation


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="magmaexplorer.solver_cli",
        description=(
            "JSON-driven solver for magma implication problems. "
            "Reads problems from stdin, writes Lean-judge answers to stdout."
        ),
    )
    p.add_argument(
        "--mode",
        choices=("single", "jsonl"),
        default="single",
        help=(
            "single (default): one JSON object on stdin → one on stdout. "
            "jsonl: one JSON object per line in/out."
        ),
    )
    p.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Default per-problem retry budget (problem JSON may override).",
    )
    return p


# ---------------------------------------------------------------------------
# Per-problem solve
# ---------------------------------------------------------------------------


def _solve_one(
    problem: Any,
    default_max_attempts: int,
    llm: Optional[LLMCallable],
) -> dict:
    """Solve a single problem dict; return the answer dict to emit."""
    if not isinstance(problem, dict):
        return {
            "problem_id": None,
            "status": "error",
            "error": f"problem must be a JSON object, got {type(problem).__name__}",
        }

    problem_id = problem.get("problem_id")

    hypothesis_src = problem.get("hypothesis")
    goal_src = problem.get("goal")
    if not isinstance(hypothesis_src, str):
        return {
            "problem_id": problem_id,
            "status": "error",
            "error": "missing or non-string 'hypothesis' field",
        }
    if not isinstance(goal_src, str):
        return {
            "problem_id": problem_id,
            "status": "error",
            "error": "missing or non-string 'goal' field",
        }

    try:
        from_eq = parse_equation(hypothesis_src)
    except ParseError as exc:
        return {
            "problem_id": problem_id,
            "status": "error",
            "error": f"could not parse hypothesis {hypothesis_src!r}: {exc}",
        }
    try:
        to_eq = parse_equation(goal_src)
    except ParseError as exc:
        return {
            "problem_id": problem_id,
            "status": "error",
            "error": f"could not parse goal {goal_src!r}: {exc}",
        }

    max_attempts = problem.get("max_attempts", default_max_attempts)
    if not isinstance(max_attempts, int) or max_attempts < 1:
        return {
            "problem_id": problem_id,
            "status": "error",
            "error": f"max_attempts must be a positive int, got {max_attempts!r}",
        }

    name = problem.get("theorem_name", "implication")
    if not isinstance(name, str) or not name:
        name = "implication"

    try:
        entries = solver.solve_implication(
            from_eq, to_eq, max_attempts=max_attempts, llm=llm
        )
    except SolverError as exc:
        return {
            "problem_id": problem_id,
            "status": "error",
            "error": str(exc),
        }

    code = lean_export.render_implication_file(
        entries, from_idx=0, to_idx=len(entries) - 1, name=name
    )
    return {
        "problem_id": problem_id,
        "call": "judge",
        "verdict": "true",
        "code": code,
    }


def _is_error(answer: dict) -> bool:
    return answer.get("status") == "error"


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


def main(
    argv: Optional[list[str]] = None,
    *,
    stdin: Optional[IO[str]] = None,
    stdout: Optional[IO[str]] = None,
    llm: Optional[LLMCallable] = None,
) -> int:
    """Run the CLI. Returns process exit code (0 = success, 1 = some error)."""
    args = _build_parser().parse_args(argv)
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout

    any_error = False

    if args.mode == "single":
        raw = stdin.read()
        try:
            problem = json.loads(raw)
        except json.JSONDecodeError as exc:
            answer: dict = {
                "problem_id": None,
                "status": "error",
                "error": f"could not parse input JSON: {exc}",
            }
            stdout.write(json.dumps(answer) + "\n")
            return 1

        answer = _solve_one(problem, args.max_attempts, llm)
        stdout.write(json.dumps(answer) + "\n")
        return 1 if _is_error(answer) else 0

    # --mode jsonl
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            problem = json.loads(line)
        except json.JSONDecodeError as exc:
            answer = {
                "problem_id": None,
                "status": "error",
                "error": f"could not parse JSONL line: {exc}",
            }
            any_error = True
        else:
            answer = _solve_one(problem, args.max_attempts, llm)
            if _is_error(answer):
                any_error = True
        stdout.write(json.dumps(answer) + "\n")
        stdout.flush()

    return 1 if any_error else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
