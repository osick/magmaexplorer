# How the Solver Works

End-to-end mechanics of `magmaexplorer.solver` + `magmaexplorer.solver_cli`
when an actual LLM (Anthropic) is in the loop.

## Who does what

**The LLM is a *planner*, not a prover.** It never runs anything. It only
proposes a sequence of DSL primitives. The harness does the proving (it
re-executes every step) and the Lean translation. The "intelligence" is
just: pattern-matching from training data ("this looks like commutativity →
try `inst` + `sym`") plus following the strict output format we impose.

```
solver_cli ── problem ──► solve_implication
                              │
                              │  (build prompt)
                              ▼
                          ┌─ call_llm ─► Anthropic API
                          │                  │
                          │   ◄── JSON ──────┘
                          ▼
                  verify_derivation
                     (re-runs every step via dsl.execute_step)
                              │
                ┌─────────────┴─────────────┐
                │                           │
              ok? yes                     ok? no
                │                           │
                ▼                           ▼
       lean_export.render_…       _build_command with narrated trace ──► retry (loop)
                │
                ▼
          {call:judge, verdict:true, code:<Lean>}
```

## What the LLM sees

Two messages, every call.

**System prompt** (`llm.py::SYSTEM_PROMPT`, ~60 lines): defines the alphabet
(single-letter vars, `*` operator), the item-list semantics (equations vs
definitions), the rule that **no algebraic laws are free** in a magma, the
full DSL grammar (six primitives — see below), and the required JSON output
shape (`{equation, steps, sources}`).

**User message** (built fresh each call). For the
`x*y = y*(x*x)` → `x*y = y*x` problem:

```
Current magma list:
[0] x*y = y*(x*x)

Command:
Derive the equation `x*y = y*x` from entry [0] using the DSL.
Your final DSL step MUST produce exactly `x*y = y*x`.
Set the JSON 'equation' field to `x*y = y*x` as well.
```

On a retry, the command grows a trace of the previous attempt (Option 1
work):

```
Command:
Derive the equation `x*y = y*x` from entry [0] using the DSL.
...

The previous attempt failed: final y*(x*x) = x*x*(y*y) ≠ goal x*y = y*x
Here is exactly what each step produced:
  1. ✓ inst [0] x:=y, y:=x        => y*x = x*(y*y)
  2. ✓ rewrite s1 using [0]       => y*(x*x) = x*x*(y*y)
Use this trace to plan a different derivation that actually ends at the goal.
```

## What the LLM returns

A single JSON object, e.g.:

```json
{
  "equation": "b*a = a*b",
  "steps": ["inst [0] x:=a, y:=b", "sym s1"],
  "sources": [0]
}
```

That's it. The LLM picked two DSL primitives. It didn't compute
`a*b = b*a`; it just claimed the chain works.

## What the harness does with that

`verify_derivation` in `solver.py` walks the `steps` list, parses each via
`dsl.parse_step`, and *re-executes* via `dsl.execute_step`:

```python
prior_results = []
for raw in steps:
    step = parse_step(raw)
    result_eq = execute_step(step, items=[from_eq], prior_results=prior_results)
    prior_results.append(result_eq)

if prior_results[-1] != goal:
    # narrate the trace, retry
```

`dsl.execute_step` is *deterministic Python*. For `inst [0] x:=a, y:=b` it
pattern-matches `x*y = y*x`, substitutes simultaneously, returns
`a*b = b*a`. For `sym s1` it flips the prior result to `b*a = a*b`. If the
LLM had lied — wrong substitution, bad step ref, primitive misapplied — the
executor raises `DSLError` and the attempt is thrown away.

This is the core trust boundary: **the LLM gets to say anything, but only
mathematically-real DSL steps make it through.**

## What Lean ever sees

The LLM's role ends at the JSON. `lean_export.render_implication_file` then
walks the verified entries and emits Lean source using fixed templates per
primitive — no LLM involvement. For the 2-step example:

| DSL step             | Lean fragment emitted by `lean_export` |
| -------------------- | -------------------------------------- |
| `inst [0] x:=a, y:=b` | `exact h a b`                          |
| `sym s1`             | `exact (h_s1 a b).symm`                |

…wrapped in a `have h_s1 : … := by` block for the intermediate. The full
output for this problem:

```lean
theorem implication {G : Type _} [Mul G]
    (h : ∀ x y : G, x * y = y * x) :
    ∀ a b : G, b * a = a * b := by
  have h_s1 : ∀ a b : G, a * b = b * a := by
    intro a b
    exact h a b
  intro a b
  exact (h_s1 a b).symm
```

Lean then type-checks this file. **Lean never sees a word the LLM wrote.**
The LLM's chain has been translated through deterministic code two layers
deep before reaching the judge.

## Why this matters

- **Hallucinations are bounded.** The worst the LLM can do is propose
  unprovable chains; those fail verification, never reach Lean.
- **The judge's "valid Lean axioms" check is enforced by construction.**
  Templates emit only `intro`, `exact`, `have`, `rw`, `.symm`, `.trans` —
  none of which is on the disallowed list. No `sorry` (after Layer 2), no
  `axiom`.
- **The LLM does the part computers are bad at**: pattern-recognising
  "this looks like commutativity with a substitution then a flip". The
  harness does the parts LLMs are bad at: faithful execution and rigorous
  translation.

## Why hard problems still fail

Because the LLM is asked to *plan the whole proof in one shot* — no search,
no exploration, no partial credit. Once the chain gets longer than a few
primitives, the joint probability of "every step correct AND last step
equals goal" collapses. Three improvements lift different parts of the
ceiling (see `solver-userguide.md` §5bis):

1. ✅ **Richer retry context** — narrated trace forwarded to the LLM. *Done.*
2. **`intermediate_hints` JSON field** — let the caller seed waypoints.
3. **Planner / executor split** — two LLM calls per problem (waypoint
   selection, then step-by-step derivation). The one that actually attacks
   the depth problem.

## The DSL primitives — origin & meaning

The six primitives are standard equational-reasoning moves. Their names
match conventions used in proof assistants (Lean, Coq, Isabelle) and in
the term-rewriting literature; the magma-specific bit is the concrete syntax
(`[i]` for entries, `s<k>` for step references, `:=` for substitutions).

| Primitive | Standard name              | What it is                                         |
| --------- | -------------------------- | -------------------------------------------------- |
| `sym`     | symmetry of `=`            | `a = b  ⟹  b = a`. Birkhoff's *Sym* rule.          |
| `inst`    | universal instantiation, *subst* | Simultaneous variable substitution in a universally quantified equation. Birkhoff's *Subst* rule. |
| `trans`   | transitivity of `=`        | From `a = b` and `b = c` derive `a = c`. Birkhoff's *Trans* rule. |
| `rewrite` | term rewriting             | Use one equation as a directed `lhs → rhs` rule and apply it once inside another equation. Standard in TRS / Knuth–Bendix completion. |
| `expand`  | definition unfolding (`δ`-reduction) | Replace a defined symbol by its body. Lean's `unfold`; Coq's `unfold`; Isabelle's `unfolding`. |
| `fold`    | definition folding         | The inverse: replace a body occurrence by the defined symbol. Lean / Coq `fold`. |

Why this *particular set of six*, rather than e.g. Birkhoff's minimal three
(`Refl`, `Sym`, `Trans`, plus *Subst* and *Cong*)?

- **Ergonomics over minimality.** Birkhoff's congruence rule, applied
  step-by-step, is unbearably verbose for working math; `rewrite` packages
  the typical congruence-then-substitute pattern into one move.
- **Definitions matter in practice.** Many real magma proofs introduce
  abbreviations like `m := x*x`. `expand` / `fold` make those usable
  without inflating the equation list.
- **One-occurrence semantics.** Both `rewrite` and `expand` / `fold` operate
  on the *leftmost-outermost single occurrence*, not all occurrences — this
  matches the granularity at which humans reason about rewrites and keeps
  derivations predictable. (Lean's `rw` rewrites all occurrences, which is
  why the rendered Lean for `rewrite` carries a `nth_rewrite` comment.)

So: **the underlying operations are textbook; the specific DSL is bespoke.**
There is no single "standard" magma-reasoning DSL — each project picks an
ergonomic working set. Magmaexplorer's six are designed to be (a) easy for
LLMs to produce reliably and (b) faithfully translatable into Lean tactics.
