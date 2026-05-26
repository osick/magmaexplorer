# SAIR Stage 2 (Solo track) — feature-complete roadmap

Each step below cites the SAIR requirement that motivates it. All work is
TDD: write the failing test first, implement the smallest thing that
makes it pass, then run the full suite. **Keep it simple** — no
abstractions, factories, or "future-proof" indirection.

Specs referenced:
- `docs/sair/competition.md` (overview + evaluation)
- `/tmp/sair_readme.md` (full protocol, banned tokens, false-cert shape)

---

## Status today

| Feature | State |
|---------|-------|
| stdin/stdout JSON protocol loop | ✅ done (`sair_solo.py`) |
| LLM → DSL → verify → render → judge cycle | ✅ done |
| True-cert rendering with `inst`/`sym`/`trans`/`rewrite` | ✅ done |
| `*` → `◇` operator swap at render time | ✅ done |
| PROMPT constant (minimal placeholders) | ✅ done |
| **Everything below** | ❌ not yet |

342 tests passing, 18 added in the last iteration.

---

## Test dataset — the foundation (Step 1)

**SAIR req:** competition.md §"Core Task" — *"If the implication is true:
a Lean 4 proof … If the implication is false: a Lean 4 proof
certificate."* Every later step needs equation pairs of both kinds to
test against.

**File:** `tests/data/sair_problems.json` — a JSON array, one entry per
pair. Schema:

```json
{
  "id":         "P001",
  "equation1":  "x*y = y*x",
  "equation2":  "x*(y*z) = (x*y)*z",
  "verdict":    "false",
  "witness":    {"size": 2, "table": "[[0,1],[1,0]]"},
  "comment":    "commutativity does not imply associativity"
}
```

`witness` is present for `verdict: "false"` entries only. `table` is the
exact string that `finOpTable` accepts in the false-cert Lean (so the
judge will accept a copy-paste of this field verbatim).

**Seed entries (8) for Step 1 PR.** Hand-checked, small, cover both
verdicts and the basic DSL primitives we already render:

| id | equation1 | equation2 | verdict | tactic / witness |
|----|-----------|-----------|---------|------------------|
| P001 | `x*y = y*x` | `x*y = y*x` | true | identity (trivial) |
| P002 | `x*y = y*x` | `b*a = a*b` | true | `inst [0] x:=a, y:=b; sym s1` |
| P003 | `(x*y)*z = x*(y*z)` | `(x*x)*x = x*(x*x)` | true | `inst [0] x:=x, y:=x, z:=x` |
| P004 | `x = x*y` | `x = x*x` | true | `inst [0] y:=x` *(ETP E_4 → E_3)* |
| P005 | `x*y = y*x` | `(x*y)*z = x*(y*z)` | false | Fin 2, `[[0,1],[1,0]]` (XOR — commutative, not associative) |
| P006 | `x*y = y*x` | `x*x = x` | false | Fin 2, `[[0,1],[1,0]]` (commutative, `1*1=0≠1`) |
| P007 | `x*x = x` | `x*y = y*x` | false | Fin 2, `[[0,0],[1,1]]` (left-proj — idempotent, not commutative) |
| P008 | `x*y = z*w` | `x = y` | false | Fin 2, `[[0,0],[0,0]]` (constant — lhs holds, rhs fails at `0=1`) |

**TDD for Step 1:**
- `tests/test_dataset.py::test_loads_and_validates_schema` — every entry has the required fields, verdicts are in `{"true","false"}`, every `"false"` has a `witness`.
- `tests/test_dataset.py::test_witnesses_are_actual_counterexamples` — for each `verdict: "false"` entry, brute-force-evaluate the witness table and assert eq1 holds, eq2 fails. (Reuses code from Step 3.)
- `tests/test_dataset.py::test_true_entries_verify` — for each `verdict: "true"` entry whose `tactic` field is filled, run `verify_derivation` and assert `ok`.

The dataset grows by ≥3 entries per later step (each step's
acceptance test pulls from it).

---

## Step 2 — Banned-token + code-size guards

**SAIR req:** readme §"Constraints":
*"Banned tokens: `sorry`, `admit`, `sorryAx`, `dbg_trace`, `dbgTrace`,
`run_tac`, `mkSorry`, `initialize`, `builtin_initialize`. Max code length
100,000 characters. Max false certificate code 20,000 bytes."*

**Why this first:** Cheap and high-leverage. Without it, any one slip
turns an `accepted` round into `incomplete_proof` / `malformed`. Also
catches our own `expand`/`fold` fallback that currently emits `sorry`.

**Implementation (≤20 LOC):**
```python
# in sair_solo.py, before _send({"call": "judge", ...})
BANNED = ("sorry", "admit", "sorryAx", "dbg_trace", "dbgTrace",
          "run_tac", "mkSorry", "initialize", "builtin_initialize")

def _code_is_clean(code: str, verdict: str) -> Optional[str]:
    cap = 20_000 if verdict == "false" else 100_000
    if len(code.encode()) > cap:
        return f"code exceeds {cap} byte cap"
    for tok in BANNED:
        if tok in code:
            return f"banned token: {tok}"
    return None
```
If unclean → loop with a `last_attempt_summary` instead of sending.

**TDD:**
- `test_emit_with_sorry_is_skipped` — feed an LLM reply that produces a `sorry`-laden Lean blob; assert no judge call sent, retry happens.
- `test_oversized_code_is_skipped` — synthetic 200 KB code, same shape.
- Regression: existing happy-path tests still pass.

**Test data needed:** none new — synthesize in the test.

---

## Step 3 — Brute-force false-cert search + false-cert Lean rendering

**SAIR req:** competition.md §"Core Task" + readme §"False certificate":
*"`Goal` expands to `∃ (G : Type) (_ : Magma G), EquationLHS G ∧ ¬
EquationRHS G`."* with the `finOpTable` / `decideFin!` shape shown in the
readme.

**This is the biggest single missing feature.** ETP folklore says
roughly half of all implication pairs are false; without false certs we
can't even attempt those.

**New module: `src/magmaexplorer/false_cert.py`** (target ≤ 120 LOC).

Two pure functions:

```python
def search_counterexample(
    eq1: Equation, eq2: Equation, max_size: int = 3
) -> Optional[tuple[int, list[list[int]]]]:
    """Return (n, table) with eq1 satisfied & eq2 falsified on Fin n,
    or None. Enumerates magma tables of size 2, then 3 (n^(n*n) tables
    — 16 at n=2, 19683 at n=3, ~4M at n=4 — keep default at 3)."""

def render_false_cert(n: int, table: list[list[int]]) -> str:
    """Emit the exact 6-line Lean blob from readme §False certificate."""
```

The search itself: nest two loops — enumerate all `n^(n*n)` op tables;
for each, plug all `n^k` variable assignments (k = #vars in the
equation) and test eq1 holds for all & eq2 fails for at least one.
Straight Python — no SAT solver, no SymPy. n=2 finishes in ms; n=3 in a
few seconds.

**Rendering target (verbatim from readme):**
```
import JudgeProblem
import JudgeDecide.DecideBang
import JudgeFinOp.MemoFinOp
open MemoFinOp

def submission : Goal := by
  let m : Magma (Fin 2) := { op := finOpTable "[[0,1],[1,0]]" }
  refine ⟨Fin 2, m, ?_⟩
  decideFin!
```

**TDD (`tests/test_false_cert.py`):**
- `test_finds_witness_commutativity_not_associativity` — uses P005.
- `test_returns_none_when_implication_is_true` — uses P002 (no counterexample exists).
- `test_render_produces_finOpTable_decideFin` — assert the literal substrings.
- `test_emitted_code_under_20kb` — sanity.
- `test_search_respects_max_size` — `max_size=2` on a pair that needs Fin 3 returns `None`.

**Dataset additions:** P005-P008 already serve. Add at least 2 more from
ETP examples to widen coverage.

---

## Step 4 — Verdict-decision dispatch in `sair_solo`

**SAIR req:** competition.md §"Core Task" — *"both directions require
machine-verifiable certificates"*. The solver must pick which to attempt.

**Strategy (cheapest first):**
1. Run `search_counterexample(eq1, eq2, max_size=3)` once before the LLM loop.
2. If found → render false cert → send judge call → exit on accepted.
3. Else → existing true-cert LLM loop unchanged.

This costs zero LLM tokens for half the problems and adds maybe 5
seconds wall-clock for the size-3 search.

**TDD additions in `test_sair_solo.py`:**
- `test_false_implication_emits_false_cert_without_calling_llm` — start message with eq1/eq2 from P005; assert *no* `{"call":"llm",...}` is sent, only a `{"call":"judge","verdict":"false",...}`.
- `test_true_implication_still_goes_through_llm_loop` — existing behavior on P002.
- `test_false_cert_path_handles_judge_rejection` — if the judge somehow rejects the false cert, fall back to the LLM loop with a summary.

**No new modules** — just wire `false_cert` into `sair_solo.main`.

---

## Step 5 — Close the Lean translation gaps

Two narrow issues both flagged in `sair-solo-userguide.md` §"Known
limitations". Pick the simplest fix for each.

### 5a — `expand` / `fold` currently emit `sorry`

**SAIR req:** the same banned-tokens line. `sorry` is fatal.

**Two options, in order of simplicity:**
1. **Ban them from the DSL response** — in the LLM loop, after parsing
   `steps`, reject any step whose primitive is `expand` or `fold` and
   loop with a `last_attempt_summary` telling the LLM to inline the
   definition instead. ~3 LOC, no Lean work needed. **Pick this.**
2. (Deferred) Actually translate them — requires understanding the def
   substitution & unfolding rules in Lean 4. Larger.

**TDD:** `test_expand_in_steps_triggers_retry`, `test_fold_in_steps_triggers_retry`.

### 5b — `rewrite` semantics (leftmost-outermost vs all occurrences)

**SAIR req:** soundness — a step the DSL says is valid but Lean rejects
maps to `incorrect`. Hits scoring.

**Fix:** emit `nth_rewrite 1 [...]` instead of `rw [...]` in
`lean_export.proof_body`. Available via Mathlib (which the judge allows
per readme §"Available Imports").

**TDD:** add a dataset entry P0xx whose DSL `rewrite` would target only
the leftmost occurrence; assert the rendered Lean uses `nth_rewrite 1`.

---

## Step 6 — `submission` budget awareness (soft)

**SAIR req:** readme §"Solver Budgets" — *"Wall-clock timeout 3600s …
pacing LLM/judge calls within this is the solver's responsibility."*

**Implementation:** record `start_time = time.monotonic()` at startup;
before each LLM call, if `time.monotonic() - start_time > 3500`, exit
cleanly with rc=1 instead of starting another round. ~5 LOC.

**TDD:** `test_aborts_near_wall_clock` with monkeypatched `time.monotonic`.

---

## Step 7 — Single-file `solver.py` packaging

**SAIR req:** competition.md §"Submission Format" — *"A Stage 2
submission is a single Python file … solver.py … 500 KB."*

**Implementation:** `scripts/build_solver.py` (≤80 LOC) — concatenate
the modules `sair_solo`, `false_cert`, `lean_export`, `solver`,
`entries`, `term`, `dsl` in dependency order, strip the `from
.module import X` relative imports (they all resolve to the same flat
file), drop unused `if __name__ == "__main__"` guards, write to
`build/solver.py`.

**TDD (`tests/test_packaging.py`):**
- `test_built_solver_under_500kb` — `os.path.getsize(build/solver.py) < 500_000`.
- `test_built_solver_is_self_contained` — run it as a subprocess via the §4 happy-path heredoc; assert exit 0.
- `test_built_solver_has_no_relative_imports` — grep the source.

**Run only on demand** — not in the default pytest sweep, since it
re-builds. Wire it as `pytest -k packaging`.

---

## Explicit non-goals (this roadmap)

| Item | Why deferred |
|------|--------------|
| Marathon track | Separate I/O contract + HTTP LLM proxy; different track entirely |
| PROMPT polishing beyond `{problem.eq1_name}` / `{problem.eq2_name}` | Marginal; pursue after scoring data lands |
| Larger-magma counterexample search (Fin ≥ 4) | Search-space explodes; saves <5% problems for huge complexity. Add only if scoring shows it pays off |
| Dependency-policy introspection (`#judge_report`) | Judge does this; we don't need to mirror it |
| LLM provider switching, retry-on-429, streaming | Proxy handles this — we just read JSON |

---

## Step ordering rationale

1. **Test dataset first** — every later step's acceptance test references it.
2. **Banned-token guard early** — cheap; prevents own-goals in steps 3-6.
3. **False certs (step 3+4) before Lean polish** — biggest score lift per LOC.
4. **Step 5 last among feature work** — incremental polish on something already shipping.
5. **Packaging at the end** — only needed for submission day; gated by all other steps being green.

---

## Acceptance per step

For each step:
1. New tests written first, run, **fail** → confirms test reaches the code.
2. Implementation added.
3. `PYTHONPATH=src python3 -m pytest -q` — **all** tests green, including all prior steps. No regression.
4. Heredoc smoke test on at least one dataset entry exercising the new step → exit 0 (or expected non-zero with the right `last_attempt_summary`).
5. Update this roadmap's "Status today" table.

---

## Test-dataset growth schedule

| After step | Min entries | Mix |
|------------|-------------|-----|
| Step 1 | 8 (P001-P008) | 4 true, 4 false |
| Step 3 | 12 | +2 false (size-3 witness), +2 true (multi-step) |
| Step 5a | 14 | +2 true that *would* tempt the LLM into `expand`/`fold` |
| Step 5b | 15 | +1 true with leftmost-outermost rewrite |

Anything beyond Step 5 stays at 15 unless real eval data motivates more.
