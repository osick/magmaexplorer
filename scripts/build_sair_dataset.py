"""Build `tests/data/sair_problems.json` from the Equational Theories Project.

Sources (raw GitHub from teorth/equational_theories):
  equations.txt                — 4694 equations, line N is E_N (`◇` operator)
  smallest_magma_examples.txt  — for many E_N, a smallest magma that satisfies it
  Austin_implications.txt      — known TRUE implications "EquationA → EquationB"

Output: a JSON list of entries with `id`, `equation1`, `equation2`, `verdict`,
optional `witness` (false), optional `tactic` (true), `source`, `comment`.

Run:  PYTHONPATH=src python3 scripts/build_sair_dataset.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

# Make `magmaexplorer` importable when run from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from magmaexplorer.magma_eval import equation_holds  # noqa: E402
from magmaexplorer.term import parse_equation  # noqa: E402


RAW = "https://raw.githubusercontent.com/teorth/equational_theories/main/data"
OUT = Path(__file__).resolve().parents[1] / "tests" / "data" / "sair_problems.json"

# Pick equations that are short and easy to read: ≤ 3 vars, ≤ MAX_LEN chars.
MAX_EQ_LEN = 28
MAX_VARS = 3

# How many of each kind we want in the seed dataset.
TARGET_TRUE = 12
TARGET_FALSE = 12


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as r:
        return r.read().decode("utf-8")


def _load_equations() -> dict[int, str]:
    """Map equation number → equation string with `*` (our internal op)."""
    raw = _http_get(f"{RAW}/equations.txt")
    out: dict[int, str] = {}
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        # Convert ETP's `◇` to our `*`; also tighten whitespace around it.
        eq = line.replace("◇", "*")
        eq = re.sub(r"\s*\*\s*", "*", eq)  # `x * y` → `x*y`
        eq = re.sub(r"\s*=\s*", " = ", eq)  # normalize spaces around `=`
        out[i] = eq
    return out


def _load_smallest_magmas() -> dict[int, tuple[int, list[list[int]]]]:
    """Map equation number → (size, table) of a smallest magma satisfying it."""
    raw = _http_get(f"{RAW}/smallest_magma_examples.txt")
    out: dict[int, tuple[int, list[list[int]]]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        # Each line: `<n> [[...],[...]]`
        n_str, table_str = line.split(" ", 1)
        table = json.loads(table_str)
        out[int(n_str)] = (len(table), table)
    return out


def _load_austin_implications() -> list[tuple[int, int]]:
    """List of (a, b) pairs where Austin_implications.txt says E_a → E_b."""
    raw = _http_get(f"{RAW}/Austin_implications.txt")
    pairs: list[tuple[int, int]] = []
    pat = re.compile(r"Equation(\d+)\s*→\s*Equation(\d+)")
    for line in raw.splitlines():
        m = pat.search(line)
        if m:
            pairs.append((int(m.group(1)), int(m.group(2))))
    return pairs


def _var_count(eq_str: str) -> int:
    """Number of distinct lowercase ASCII variables in an equation string."""
    return len({c for c in eq_str if c.islower() and c.isalpha()})


def _is_short(eq_str: str) -> bool:
    return len(eq_str) <= MAX_EQ_LEN and _var_count(eq_str) <= MAX_VARS


def _entry_true(eq_id_a: int, eq_id_b: int, eq_a: str, eq_b: str,
                source: str, tactic: list[str] | None = None) -> dict:
    out = {
        "id": f"ETP_T_{eq_id_a}_{eq_id_b}",
        "equation1": eq_a,
        "equation2": eq_b,
        "verdict": "true",
        "source": source,
    }
    if tactic is not None:
        out["tactic"] = tactic
    return out


def _entry_false(eq_id_a: int, eq_id_b: int, eq_a: str, eq_b: str,
                 size: int, table: list[list[int]], source: str) -> dict:
    return {
        "id": f"ETP_F_{eq_id_a}_{eq_id_b}",
        "equation1": eq_a,
        "equation2": eq_b,
        "verdict": "false",
        "witness": {"size": size, "table": json.dumps(table)},
        "source": source,
    }


# A small hand-curated set of `tactic` annotations for true entries we know
# how to derive with the current DSL primitives. Keyed by (a, b).
HAND_TACTICS: dict[tuple[int, int], list[str]] = {
    # E_4: x = x*y  → E_3: x = x*x  (substitute y := x)
    (4, 3): ["inst [0] y:=x"],
    # E_43: x*y = y*x  → E_43: x*y = y*x  (trivial identity — same equation)
    # (Not actually in Austin, used as identity sanity)
}


def build() -> list[dict]:
    print("Fetching equations.txt ...", file=sys.stderr)
    equations = _load_equations()
    print(f"  loaded {len(equations)} equations", file=sys.stderr)

    print("Fetching smallest_magma_examples.txt ...", file=sys.stderr)
    smallest = _load_smallest_magmas()
    print(f"  loaded {len(smallest)} witness magmas", file=sys.stderr)

    print("Fetching Austin_implications.txt ...", file=sys.stderr)
    implications = _load_austin_implications()
    print(f"  loaded {len(implications)} implication pairs", file=sys.stderr)

    short_ids = {i for i, eq in equations.items() if _is_short(eq)}
    print(f"  {len(short_ids)} equations are 'short' (≤{MAX_EQ_LEN} chars, ≤{MAX_VARS} vars)", file=sys.stderr)

    # ----------------- TRUE entries from Austin -----------------
    true_entries: list[dict] = []
    for a, b in implications:
        if a == b:
            continue
        if a not in short_ids or b not in short_ids:
            continue
        eq_a, eq_b = equations[a], equations[b]
        tac = HAND_TACTICS.get((a, b))
        true_entries.append(_entry_true(a, b, eq_a, eq_b,
                                        source=f"Austin_implications.txt E{a}→E{b}",
                                        tactic=tac))
        if len(true_entries) >= TARGET_TRUE:
            break

    # Ensure the hand-curated entries are present even if Austin order skipped them.
    austin_pairs = set(implications)
    for (a, b), tac in HAND_TACTICS.items():
        if (a, b) in {(e_a, e_b) for e_a, e_b in [(int(e["id"].split("_")[2]),
                                                   int(e["id"].split("_")[3]))
                                                   for e in true_entries]}:
            continue
        if a not in equations or b not in equations:
            continue
        # Even if not in Austin, it's a true implication if our DSL can derive it.
        source = ("Austin_implications.txt E%d→E%d" % (a, b)
                  if (a, b) in austin_pairs else "hand-curated (DSL-derivable)")
        true_entries.append(_entry_true(a, b, equations[a], equations[b],
                                        source=source, tactic=tac))

    # ----------------- FALSE entries via smallest_magma_examples -----------------
    # If equation A is satisfied by magma M_A (from smallest_magma_examples),
    # and M_A does NOT satisfy equation B, then `E_A → E_B` is FALSE,
    # with M_A as the counter-witness.
    #
    # To get diversity, take at most ONE false entry per hypothesis a — this
    # spreads the dataset over many witness magmas instead of stacking them all
    # under E_1.
    false_entries: list[dict] = []
    used_as: set[int] = set()
    seen_pairs: set[tuple[int, int]] = set()

    # Skip the two degenerate equations: E_1 (`x = x`, holds in every magma)
    # and E_2 (`x = y`, fails in every magma with ≥ 2 elements).  As goal
    # they're trivially falsifiable; as hypothesis they're trivially true.
    DEGENERATE = {1, 2}

    for a in sorted(short_ids):
        if a in used_as or a not in smallest or a in DEGENERATE:
            continue
        size, table = smallest[a]
        try:
            eq_a_parsed = parse_equation(equations[a])
        except Exception:
            continue
        # Sanity: ETP says M_A satisfies E_A. Verify.
        if not equation_holds(eq_a_parsed, table):
            continue  # corrupted entry — skip
        # Take the THIRD falsifying b we encounter (rather than the first) —
        # this skips trivially-falsified goals (typically near the top of the
        # equation list) and gives the dataset more variety in eq2 shape.
        skip_remaining = 2
        for b in sorted(short_ids):
            if a == b or b in DEGENERATE or (a, b) in seen_pairs:
                continue
            try:
                eq_b_parsed = parse_equation(equations[b])
            except Exception:
                continue
            if not equation_holds(eq_b_parsed, table):
                if skip_remaining > 0:
                    skip_remaining -= 1
                    continue
                seen_pairs.add((a, b))
                used_as.add(a)
                false_entries.append(_entry_false(
                    a, b, equations[a], equations[b],
                    size=size, table=table,
                    source=f"smallest_magma E{a} falsifies E{b}",
                ))
                break  # next hypothesis a — diversify
        if len(false_entries) >= TARGET_FALSE:
            break

    entries = true_entries + false_entries
    print(f"\n  → {len(true_entries)} true, {len(false_entries)} false "
          f"({len(entries)} total)", file=sys.stderr)
    return entries


def main() -> int:
    entries = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(entries, indent=2) + "\n")
    print(f"\nWrote {OUT.relative_to(Path.cwd())} ({len(entries)} entries)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
