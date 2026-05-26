# sair_solo — user guide

`magmaexplorer.sair_solo` is the SAIR Stage 2 **Solo-track** driver. It
speaks the proxy's stdin/stdout JSON protocol, drives the LLM with a
built-in `PROMPT` template, verifies derivations locally with the DSL
executor, renders the certificate in the judge's required shape, and
loops on judge feedback until accepted (or the proxy kills the process at
the wall-clock deadline).

> Scope of this iteration: **true certificates only**. False-cert
> (brute-force counterexample) and single-file packaging are separate
> iterations — see "Known limitations" at the bottom.

---

## 1. Run it

```bash
PYTHONPATH=src python3 -m magmaexplorer.sair_solo
```

The process reads JSON line by line from stdin, writes JSON line by line
to stdout. In production the SAIR proxy is on both ends of the pipes;
locally you can drive it from a shell heredoc (see §4).

Exit codes:

| Code | Meaning                                                              |
|------|----------------------------------------------------------------------|
| `0`  | Judge returned `accepted`                                             |
| `1`  | Proxy closed stdin, returned `{"error": ...}`, or sent malformed start |

The process otherwise runs until accepted or the proxy SIGTERMs it at
budget exhaustion.

---

## 2. The protocol in one picture

```
Proxy  ──stdin──►  {"problem": {...}, "budget": {...}}        (start)
                                                               ↓
Solver ──stdout─►  {"call":"llm",  "context": {...}}           ┐
Proxy  ──stdin──►  {"response": "<LLM JSON>"}                  │
                                                               │  loop
Solver ──stdout─►  {"call":"judge","verdict":"true","code":...}│
Proxy  ──stdin──►  {"status":"accepted"}  OR  {"status":...}   ┘
```

Three message types the solver emits (`call: "llm"` and `call: "judge"`)
and the two response shapes the proxy returns (`{"response":...}` and
`{"status":..., "stderr":...}`). Nothing else.

---

## 3. Message shapes

### Start (proxy → solver)

```json
{
  "problem": {
    "id": "normal_0001",
    "eq1_id": 2,
    "eq2_id": 387,
    "equation1": "x*y = y*x",
    "equation2": "b*a = a*b"
  },
  "budget": {"timeout_seconds": 3600, "max_code_length": 100000}
}
```

### LLM call (solver → proxy)

```json
{"call": "llm", "context": {"round": "0", "last_attempt_summary": ""}}
```

The proxy reads the top-level `PROMPT = "..."` constant from this file,
substitutes `{problem.*}`, `{history.*}` (judge history), and
`{solver.*}` (the keys of our `context` dict), forwards to the LLM, and
returns:

```json
{"response": "{\"steps\": [...], \"equation\": \"...\"}"}
```

The solver expects the LLM's response to be a JSON object with at least
a `steps` field (a list of DSL primitives). Anything else triggers a
retry with a summary fed into `{solver.last_attempt_summary}`.

### Judge call (solver → proxy)

```json
{"call": "judge", "verdict": "true", "code": "import JudgeProblem\n..."}
```

Proxy returns one of:

```json
{"status": "accepted"}
{"status": "incorrect",         "stderr": "type mismatch ..."}
{"status": "incomplete_proof",  "stderr": "uses sorry ..."}
{"status": "malformed",         "stderr": "..."}
{"status": "unparsed",          "stderr": "..."}
```

On `accepted`, the solver exits 0. On anything else, it summarizes the
status + stderr into `{solver.last_attempt_summary}` for the next LLM
round.

---

## 4. Worked example: happy path

Pipe a scripted proxy conversation in:

```bash
cat <<'EOF' | PYTHONPATH=src python3 -m magmaexplorer.sair_solo
{"problem":{"id":"smoke","eq1_id":0,"eq2_id":1,"equation1":"x*y = y*x","equation2":"b*a = a*b"},"budget":{"timeout_seconds":3600,"max_code_length":100000}}
{"response":"{\"steps\":[\"inst [0] x:=a, y:=b\",\"sym s1\"],\"equation\":\"b*a = a*b\"}"}
{"status":"accepted"}
EOF
echo "exit: $?"
```

Output (formatted for readability):

```
{"call": "llm",   "context": {"round": "0", "last_attempt_summary": ""}}
{"call": "judge", "verdict": "true", "code": "import JudgeProblem\n\ndef submission : Goal := by\n  intro G _ h\n  have h_s1 : ∀ a b : G, a ◇ b = b ◇ a := by\n    intro a b\n    exact h a b\n  intro a b\n  exact (h_s1 a b).symm\n"}
exit: 0
```

The `◇` in the actual JSON output is `◇` — the SAIR judge's magma
operator. The DSL/LLM/parser all use `*`; the swap happens at Lean-render
time only.

---

## 5. Worked example: retry path

DSL parse failure (round 0) → judge rejection (round 1) → finally
accepted (round 2):

```bash
cat <<'EOF' | PYTHONPATH=src python3 -m magmaexplorer.sair_solo
{"problem":{"id":"smoke","eq1_id":0,"eq2_id":1,"equation1":"x*y = y*x","equation2":"b*a = a*b"},"budget":{"timeout_seconds":3600,"max_code_length":100000}}
{"response":"{\"steps\":[\"garbage step\"],\"equation\":\"b*a = a*b\"}"}
{"response":"{\"steps\":[\"inst [0] x:=b, y:=a\"],\"equation\":\"b*a = a*b\"}"}
{"status":"incorrect","stderr":"type mismatch at have ... expected ◇"}
{"response":"{\"steps\":[\"inst [0] x:=a, y:=b\",\"sym s1\"],\"equation\":\"b*a = a*b\"}"}
{"status":"accepted"}
EOF
```

Resulting call sequence:

| Round | What we sent                                | `last_attempt_summary` carried in        |
|-------|----------------------------------------------|------------------------------------------|
| 0     | `llm`                                        | `""`                                      |
| 1     | `llm`                                        | `DSL verification failed: parse failed at step 1: unknown primitive 'garbage'\n  1. ? garbage step` |
| 1     | `judge`                                      | (sent after LLM came back with parseable DSL) |
| 2     | `llm`                                        | `Lean judge returned incorrect: type mismatch at have ... expected ◇` |
| 2     | `judge` → `accepted` → exit 0                | —                                         |

Note the asymmetry: DSL-failure rounds never reach the judge, so
`{history.attempts}` stays empty on those — that's why we carry our own
`{solver.last_attempt_summary}`.

---

## 6. The PROMPT template

```python
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
{"steps": ["...", "..."], "equation": "<goal as string>"}
"""
```

Placeholder namespaces (filled by the proxy):

| Namespace      | Filled by                              | Examples                                            |
|----------------|----------------------------------------|-----------------------------------------------------|
| `{problem.*}`  | Proxy, from the start message          | `equation1`, `equation2`, `id`, `eq1_id`, `eq2_id`  |
| `{history.*}`  | Proxy, from prior `judge` calls only   | `round`, `attempts`, `last_status`, `last_error`    |
| `{solver.*}`   | Solver, via `context` field on `llm` call | `round`, `last_attempt_summary` (our two)         |

> The `{}` braces in the JSON example at the bottom are escaped to `{{}}`
> in the source — Python's `.format`-style placeholder rules.

---

## 7. Testing without a real proxy

Two layers exist:

- **Unit tests** (`tests/test_sair_solo.py`) — pre-script the proxy as a
  `StringIO` of JSON lines, run `sair_solo.main(stdin=..., stdout=...)`,
  assert on what was written. 13 tests, ~200 lines, no LLM, no Lean.

- **CLI smoke tests** — heredoc the proxy conversation into stdin (the
  worked examples in §4 and §5). Useful as one-shot acceptance checks
  after a code change.

Neither requires the actual SAIR runner or judge installed locally.

---

## 8. Known limitations (deferred to later iterations)

| Limitation                                    | Status            |
|-----------------------------------------------|-------------------|
| False certificates (brute-force counterexample + `decideFin!` Lean) | Not started |
| Single-file `solver.py` packaging (~500 KB submission limit)         | Not started |
| `expand` / `fold` DSL primitives → Lean translation                  | Falls back to `sorry` (inherited from `lean_export`) |
| `rewrite` emits `rw` which rewrites all occurrences (not just leftmost-outermost) | Comment in code; needs `nth_rewrite` from Mathlib |
| Marathon-track protocol (file-based, HTTP LLM proxy)                  | Different track entirely |

When the LLM fails to find a working derivation in the wall-clock
budget, the proxy SIGTERMs the solver and the proxy reports the
absence of an `accepted` verdict to the judge — the solver itself does
not need to emit anything special.
