# Solver User Guide

`magmaexplorer.solver` is the LLM-driven proof-search loop for magma implications.
`magmaexplorer.solver_cli` is its JSON command-line front-end.

Together they form **Layer 4** of magmaexplorer's SAIR Stage 2 pipeline:

```
           ┌──────────────────────────────┐
problem  ─►│  solver_cli.main()           │
JSON       │   ├─ parse hypothesis/goal   │
           │   ├─ solver.solve_implication|
           │   │    ├─ ask LLM            │
           │   │    ├─ verify each step   │
           │   │    └─ retry on failure   │
           │   └─ lean_export.render_…    │
           └──────────────────────────────┘
                       │
                       ▼
            {"call":"judge","verdict":"true","code":"<Lean>"}
```

The "code" field is a self-contained Lean 4 source string that the
deterministic Stage 2 judge type-checks.

---

## 1. The pieces

| Module                              | Role                                                      | Has I/O? | Imports `anthropic`? |
| ----------------------------------- | --------------------------------------------------------- | -------- | --------------------- |
| `magmaexplorer.entries`             | Shared `Entry` dataclass                                  | no       | no                    |
| `magmaexplorer.dsl`                 | Six derivation primitives + executor                      | no       | no                    |
| `magmaexplorer.lean_export`         | Renders entry list → Lean 4 source                        | no       | no                    |
| `magmaexplorer.solver` ◄ this guide | Proof-search loop (LLM + verify)                          | no       | **lazy** (default arg)|
| `magmaexplorer.solver_cli` ◄ this   | stdin/stdout JSON driver                                  | yes      | lazy via solver       |

The solver / CLI never trust the LLM. Every step it proposes is replayed through
`dsl.execute_step`; only verified chains are emitted.

---

## 2. In-process API

### 2.1 `verify_derivation`

```python
from magmaexplorer import solver
from magmaexplorer.term import parse_equation

axiom = parse_equation("a*b = c")
v = solver.verify_derivation(["sym [0]"], [axiom])
print(v.ok, v.final_equation, v.annotated_steps)
# True, Equation(lhs=Var('c'), rhs=Op(Var('a'), Var('b'))), ['✓ sym [0]']
```

| Field              | Meaning                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `ok`               | `True` iff every step parsed *and* executed *and* (if `expected_final` was supplied) matched it. |
| `final_equation`   | Result of the last step, or `None` if no step ran.                   |
| `annotated_steps`  | One entry per input step, prefixed `✓` / `✗` / `?`. Bare DSL form — stored on `Entry.steps`, consumed by `lean_export`. |
| `narrated_steps`   | Same prefixes, but each `✓` row is suffixed with `   => <pretty equation>`. LLM-facing only — used to build the retry prompt. |
| `error`            | Human-readable failure summary, or `None`.                           |

### 2.2 `solve_implication`

```python
from magmaexplorer import solver, lean_export
from magmaexplorer.term import parse_equation

entries = solver.solve_implication(
    parse_equation("x*y = y*x"),
    parse_equation("b*a = a*b"),
    max_attempts=3,                    # default 3
    llm=None,                          # default → real Anthropic call
)
print(lean_export.render_implication_file(entries, 0, 1, "commute_ab"))
```

Returns a `list[Entry]` with exactly two elements on success:

| Index | Content       | Sources | Steps                            |
| ----- | ------------- | ------- | -------------------------------- |
| 0     | hypothesis    | `[]`    | `[]`                             |
| 1     | goal          | `[0]`   | verified DSL chain (annotated)   |

Raises `solver.SolverError` if no attempt within `max_attempts` produced a
verified derivation.

### 2.3 Injecting a stub LLM (offline / unit testing)

Anything matching `Callable[[list[Item], str], LLMResult]` works:

```python
from magmaexplorer.llm import LLMResult

def my_stub(items, command):
    return LLMResult(equation="c = a*b", steps=["sym [0]"], sources=[0])

entries = solver.solve_implication(
    parse_equation("a*b = c"),
    parse_equation("c = a*b"),
    llm=my_stub,
)
```

Use this pattern in your own tests — none of the bundled tests requires an
Anthropic API key.

---

## 3. JSON CLI protocol

Runnable as:

```
python3 -m magmaexplorer.solver_cli [--mode single|jsonl] [--max-attempts N]
```

### 3.1 Input shape (per problem)

```json
{
  "problem_id":   "string-or-null (optional)",
  "hypothesis":   "lhs = rhs       (REQUIRED)",
  "goal":         "lhs = rhs       (REQUIRED)",
  "theorem_name": "ident           (optional, default 'implication')",
  "max_attempts": 3
}
```

* `hypothesis` and `goal` must be parseable by `magmaexplorer.term.parse_equation`:
  single-letter lowercase variables, `*` as the binary op, parentheses allowed.
* `theorem_name` is currently echoed in the Lean file's header comment only
  (the theorem itself is always called `implication` — see §6 *Limitations*).
* `max_attempts` overrides the `--max-attempts` flag for this one problem.

### 3.2 Output shape — success

```json
{
  "problem_id": "P1",
  "call": "judge",
  "verdict": "true",
  "code":  "<Lean 4 source string>"
}
```

The shape matches what the SAIR Stage 2 judge expects. The CLI's exit code
is `0`.

### 3.3 Output shape — failure

```json
{
  "problem_id": "P1",
  "status": "error",
  "error":  "..."
}
```

The CLI's exit code is non-zero so shell pipelines can detect failure without
parsing JSON.

### 3.4 Modes

| `--mode`  | Input on stdin                            | Output on stdout       |
| --------- | ----------------------------------------- | ---------------------- |
| `single`  | Exactly one JSON object                   | One JSON object        |
| `jsonl`   | One JSON object per line (blanks skipped) | One JSON object per line |

In `jsonl` mode the CLI keeps going after an error and reports a non-zero
exit code if any problem failed.

---

## 4. Worked examples

> All examples below assume you've installed the project in editable mode
> (`pip install -e .`) or are running from the repo root with `PYTHONPATH=src`.

### 4.1 Hello, sym!

The smallest interesting problem: deriving `c = a*b` from `a*b = c`.

```bash
echo '{"problem_id":"hello","hypothesis":"a*b = c","goal":"c = a*b"}' \
  | python3 -m magmaexplorer.solver_cli
```

Expected output (with the real Anthropic LLM):

```json
{"problem_id": "hello", "call": "judge", "verdict": "true",
 "code": "-- magmaexplorer implication: [0] => [1]  (implication)\n…\ntheorem implication {G : Type _} [Mul G]\n    (h : ∀ a b c : G, a * b = c) :\n    ∀ a b c : G, c = a * b := by\n  intro a b c\n  exact (h a b c).symm"}
```

Save the `code` field to a file and type-check it:

```bash
python3 -c "import json,sys; print(json.load(sys.stdin)['code'])" \
  < /tmp/answer.json > /tmp/hello.lean
~/.elan/bin/lean /tmp/hello.lean && echo OK
```

### 4.2 Commutativity → swapped commutativity

```bash
echo '{
  "problem_id": "commute_ab",
  "hypothesis": "x*y = y*x",
  "goal":       "b*a = a*b",
  "theorem_name": "commute_ab"
}' | python3 -m magmaexplorer.solver_cli
```

The solver should ask the LLM, get back something like
`["inst [0] x:=a, y:=b", "sym s1"]`, verify it produces `b*a = a*b`, and emit
the Lean theorem.

The emitted Lean uses `have h_s1 : ∀ a b : G, a * b = b * a := by …` for the
intermediate, then discharges the outer goal with `(h_s1 a b).symm`. The
judge accepts it: no `axiom`, no `sorry`, no Mathlib dependencies.

### 4.3 Batch mode (Marathon-style)

`problems.jsonl`:

```jsonl
{"problem_id":"P1","hypothesis":"a*b = c","goal":"c = a*b"}
{"problem_id":"P2","hypothesis":"x = y","goal":"y = x"}
{"problem_id":"P3","hypothesis":"a = b","goal":"a = c"}
```

```bash
python3 -m magmaexplorer.solver_cli --mode jsonl --max-attempts 2 < problems.jsonl
```

Output (one line per problem):

```
{"problem_id":"P1","call":"judge","verdict":"true","code":"..."}
{"problem_id":"P2","call":"judge","verdict":"true","code":"..."}
{"problem_id":"P3","status":"error","error":"failed to derive a = c …"}
```

Exit code: 1 (because P3 failed).

### 4.4 Error case: unparseable equation

```bash
echo '{"hypothesis":"a = =","goal":"a = b"}' \
  | python3 -m magmaexplorer.solver_cli
```

```json
{"problem_id": null, "status": "error",
 "error": "could not parse hypothesis 'a = =': expected variable or '(' at column 2, got '='"}
```

Exit code: 1. The LLM is never called for problems that fail input
validation.

### 4.5 Error case: solver budget exhausted

```bash
echo '{"problem_id":"impossible","hypothesis":"a = b","goal":"a = c"}' \
  | python3 -m magmaexplorer.solver_cli --max-attempts 3
```

```json
{"problem_id": "impossible", "status": "error",
 "error": "failed to derive a = c from a = b after 3 attempts: …"}
```

Exit code: 1.

---

## 5. Offline testing recipes

### 5.1 Stub LLM smoke pipe

A ready-to-use stub lives at [`examples/stub_llm.py`](../examples/stub_llm.py).
It maps a few `(hypothesis, goal)` pairs to canned `LLMResult` objects and
raises `LLMError` when no entry matches (so the solver retries / fails
cleanly instead of crashing).

One-liner that drives the CLI with the stub — no API key needed:

```bash
PYTHONPATH=src:examples python3 -c "
import io
from magmaexplorer import solver_cli
from stub_llm import stub
stdin = io.StringIO('{\"hypothesis\":\"a*b = c\",\"goal\":\"c = a*b\"}')
stdout = io.StringIO()
rc = solver_cli.main([], stdin=stdin, stdout=stdout, llm=stub)
print('exit', rc)
print(stdout.getvalue())
"
```

Add your own entries to `CANNED` to script richer end-to-end scenarios.

No API key needed. The stub returns deterministic responses, the solver
verifies them, the renderer emits Lean — exactly the same code paths as
the real Anthropic backend.

### 5.2 Round-trip through Lean

```bash
# 1. Solve (with stub or real LLM)
echo '{"problem_id":"hello","hypothesis":"a*b = c","goal":"c = a*b"}' \
  | python3 -m magmaexplorer.solver_cli > /tmp/answer.json

# 2. Extract Lean code
python3 -c "import json,sys; print(json.load(open('/tmp/answer.json'))['code'])" \
  > /tmp/hello.lean

# 3. Type-check
~/.elan/bin/lean /tmp/hello.lean && echo OK || echo FAIL
```

### 5.3 Running the test suite

```bash
PYTHONPATH=src python3 -m pytest tests/test_solver.py tests/test_solver_cli.py -v
```

All 36 solver/CLI tests are stub-driven; no Anthropic key required.

---

## 5bis. When the solver fails

Not every implication is reachable in this setting. The bundled solver is
*minimal* — single-shot prompting, three retries by default, no search. On
non-trivial problems you should expect failures, and the error messages tell
you where the brittleness sits. Three failure shapes to recognise:

| Error fragment | What happened | Cheapest next move |
| -------------- | ------------- | ------------------ |
| `exec failed at step N: rewrite: pattern not found in equation` | The LLM proposed syntactically valid DSL whose Nth step doesn't fire on the term tree that step N-1 actually produced. Often a wrong `s<k>` reference or a substitution that landed in the wrong subtree. | `--max-attempts 10`; consider whether the proof needs an explicit waypoint. |
| `final <X> ≠ goal <Y>` | The LLM ran a valid chain but ended on the wrong equation — it drifted away from the goal. | Same; or try the REPL interactively to find a working chain. |
| `LLM call failed: …` | Anthropic API error, rate-limit, or (with the stub) no canned response matched. | Inspect; retry. |

Concrete example: deriving `x*y = y*x` from `x*y = y*(x*x)` is a deep result
from the equational-theories project — not a "the LLM should have tried
harder" case. The minimal solver simply cannot plan that far ahead in one
shot.

What lifts the ceiling:

1. **Richer retry context.** ✅ *implemented.* On a failed attempt the solver
   now forwards the full per-step trace back to the LLM, including the
   intermediate equation each successful step produced. A retry prompt looks
   like:

   ```
   The previous attempt failed: final a*b = b*a ≠ goal b*a = a*b
   Here is exactly what each step produced:
     1. ✓ inst [0] x:=a, y:=b   => a*b = b*a
   Use this trace to plan a different derivation that actually ends at the goal.
   ```

   The LLM can now see *where* it landed and adjust, instead of replanning
   blindly. Inspect what the solver sent on attempt N via
   `solver.verify_derivation(...).narrated_steps` in your own scripts.
2. **`intermediate_hints` JSON field.** Let the caller seed waypoint
   equations. The solver derives each waypoint, then chains them. *(not yet)*
3. **Planner + executor split.** First LLM call returns waypoint equations;
   second derives each waypoint from the previous. Removes the "plan + execute
   in one shot" coupling. *(not yet)*
4. **Layer 2 multi-step renderer.** ✅ *implemented.* Multi-step DSL chains
   are now translated into nested `have h_s<k> : ∀ vars : G, <eq> := by …`
   blocks per intermediate, with the final step discharging the outer goal.
   Single-step proofs work as before; chains that contain an unparseable or
   runtime-failing step still fall back to `sorry` with a diagnostic comment.
   This is the gate for end-to-end judge-acceptable certificates on
   non-trivial proofs.

For now: prefer single-step problems for end-to-end demos, and use the REPL
interactively to build longer chains (`/lean-implication` for export).

---

## 6. Current limitations

| # | Limitation                                                                                          | Layer |
| - | --------------------------------------------------------------------------------------------------- | ----- |
| 1 | `theorem_name` only appears in the file header comment, not as the actual Lean theorem identifier.  | (cosmetic) |
| 2 | ~~Multi-step DSL derivations emit `sorry`.~~ ✅ Fixed in Layer 2 — multi-step chains now render as nested `have h_s<k>` blocks (see §5bis item 4). Falls back to `sorry` only when a step is unparseable or fails at replay. | 2 |
| 3 | No counterexample track — the CLI always emits `verdict: "true"`. False implications are out of scope. | 3 |
| 4 | The local input schema (`{hypothesis, goal, ...}`) is **not** the exact SAIR pipeline schema. Wrap or adapt when integrating with `pipeline.runner`. | (integration) |
| 5 | The driver does not yet implement the `{"call":"llm",…}` proxy protocol — it just calls the Anthropic API directly. Real Stage 2 submissions must route through the local proxy. | (integration) |
| 6 | No proof-search strategy beyond "ask LLM, retry on failure". No backtracking, no caching, no triage. | 2/3 |

These are tracked as future work, not bugs in the current scope.

---

## 7. Reference: full module relationships

```
magmaexplorer.solver_cli       (JSON I/O)
        │
        ▼
magmaexplorer.solver           (search loop)
        │
        ├──► magmaexplorer.dsl        (verify each step)
        │
        ├──► magmaexplorer.llm        (ask Anthropic – lazy import)
        │
        ▼
magmaexplorer.lean_export      (render entries → Lean 4 source)
        │
        ▼
magmaexplorer.entries          (shared Entry dataclass)
        │
        ▼
magmaexplorer.term             (AST, parser, pretty-printer)
```

None of `solver`, `solver_cli`, `lean_export`, `entries`, or `dsl` imports
`prompt_toolkit`, `rich`, or the REPL. The CLI is suitable for shipping as
a competition `solver.py` after the integration items in §6 are addressed.
