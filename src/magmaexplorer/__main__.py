"""CLI entrypoint: `python -m magmaexplorer` or the `magmaexplorer` script."""

from __future__ import annotations

import argparse
import sys

from .llm import DEFAULT_MODEL, call_llm
from .repl import run_repl


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="magmaexplorer")
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Anthropic model to use (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "initial",
        nargs="?",
        default=None,
        help="optional initial equation, e.g. 'x*y=y*(x*x)'",
    )
    args = parser.parse_args(argv)

    llm = lambda eqs, cmd: call_llm(eqs, cmd, model=args.model)
    run_repl(llm=llm, initial=args.initial)
    return 0


if __name__ == "__main__":
    sys.exit(main())
