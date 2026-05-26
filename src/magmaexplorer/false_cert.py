"""Brute-force false-certificate search + Lean rendering.

When E_A does NOT imply E_B, there's a finite magma satisfying E_A but
falsifying E_B. `search_counterexample` finds one by enumerating every
op-table over Fin n in turn (n = 2, 3, …, up to `max_size`).

`render_false_cert(n, table)` emits the Lean shape required by the SAIR
judge (readme §False certificate):

    import JudgeProblem
    import JudgeDecide.DecideBang
    import JudgeFinOp.MemoFinOp
    open MemoFinOp

    def submission : Goal := by
      let m : Magma (Fin 2) := { op := finOpTable "[[0,1],[1,0]]" }
      refine ⟨Fin 2, m, ?_⟩
      decideFin!
"""

from __future__ import annotations

import itertools
import json
from typing import Optional

from .magma_eval import equation_holds
from .term import Equation


def _enumerate_tables(n: int):
    """Yield every n×n op table over Fin n in row-major lex order.

    There are n^(n*n) such tables — 16 at n=2, 19_683 at n=3."""
    for flat in itertools.product(range(n), repeat=n * n):
        yield [list(flat[i * n:(i + 1) * n]) for i in range(n)]


def search_counterexample(
    eq1: Equation, eq2: Equation, max_size: int = 3
) -> Optional[tuple[int, list[list[int]]]]:
    """Return (n, table) of a magma satisfying eq1 and falsifying eq2.

    Tries n = 2, 3, …, max_size, returning the first witness. Fin 1 is
    skipped: it's the trivial one-element magma where every equation
    holds, so no implication can be falsified there.

    Returns None when no witness exists up to `max_size`.
    """
    for n in range(2, max_size + 1):
        for table in _enumerate_tables(n):
            if equation_holds(eq1, table) and not equation_holds(eq2, table):
                return (n, table)
    return None


def render_false_cert(n: int, table: list[list[int]]) -> str:
    """Emit the SAIR judge's false-certificate Lean blob for this magma."""
    table_str = json.dumps(table, separators=(",", ":"))
    return (
        "import JudgeProblem\n"
        "import JudgeDecide.DecideBang\n"
        "import JudgeFinOp.MemoFinOp\n"
        "open MemoFinOp\n"
        "\n"
        "def submission : Goal := by\n"
        f"  let m : Magma (Fin {n}) := {{ op := finOpTable \"{table_str}\" }}\n"
        f"  refine ⟨Fin {n}, m, ?_⟩\n"
        "  decideFin!\n"
    )
