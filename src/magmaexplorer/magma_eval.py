"""Finite-magma evaluator.

Evaluates terms / equations over a magma whose op is given as a row-major
table `table[a][b]` for `a, b ∈ {0, …, n-1}`. Used both by the SAIR
dataset validator and by the brute-force false-cert search.
"""

from __future__ import annotations

import itertools
from typing import Optional

from .term import Equation, Op, Term, Var


def collect_vars(term: Term) -> set[str]:
    if isinstance(term, Var):
        return {term.name}
    return collect_vars(term.left) | collect_vars(term.right)


def eval_term(term: Term, env: dict[str, int], table: list[list[int]]) -> int:
    if isinstance(term, Var):
        return env[term.name]
    return table[eval_term(term.left, env, table)][eval_term(term.right, env, table)]


def _all_envs(vars_: list[str], n: int):
    for assignment in itertools.product(range(n), repeat=len(vars_)):
        yield dict(zip(vars_, assignment))


def equation_holds(eq: Equation, table: list[list[int]]) -> bool:
    """Return True iff lhs == rhs for every assignment of free vars to Fin n."""
    n = len(table)
    vars_ = sorted(collect_vars(eq.lhs) | collect_vars(eq.rhs))
    for env in _all_envs(vars_, n):
        if eval_term(eq.lhs, env, table) != eval_term(eq.rhs, env, table):
            return False
    return True


def find_falsifying_assignment(
    eq: Equation, table: list[list[int]]
) -> Optional[dict[str, int]]:
    """Return the first env (in lex order) where eq fails on `table`, else None."""
    n = len(table)
    vars_ = sorted(collect_vars(eq.lhs) | collect_vars(eq.rhs))
    for env in _all_envs(vars_, n):
        if eval_term(eq.lhs, env, table) != eval_term(eq.rhs, env, table):
            return env
    return None
