# ADR 001 - magmaexplorer tool stack

**Date:** 2026-05-24
**Status:** Accepted

## Context

We need a small interactive tool to explore magma equations from equational
theories, with derivations driven by an LLM.

## Decisions

1. **Python 3.10+**, single small package under `src/magmaexplorer/`.
2. **Anthropic API** via the official `anthropic` SDK; default model
   `claude-opus-4-7`, overridable via `--model`. `ANTHROPIC_API_KEY` from env.
3. **`prompt_toolkit`** for the REPL - gives line editing and history with
   minimal code. Rejected: plain `input()` (no history), Textual (overkill).
4. **Hand-written recursive-descent parser** for the term grammar - ~40 lines,
   no external dependency. Rejected: `lark` (overkill for one production),
   `sympy` (treats `*` as commutative/associative, which is wrong for magmas).
5. **Syntactic-only verification** of LLM output: parse the returned equation,
   reject if malformed; do not attempt to prove the derivation. The word
   problem for free magmas is undecidable in general; correctness is the
   user's responsibility.
6. **Left-associative `*`** as the canonical reading (`x*y*z = (x*y)*z`).
   Pretty-printer emits minimal parentheses to recover the tree.
7. **In-memory state** with explicit `/save` and `/load` to JSON. Rejected:
   auto-persistence (state leaks between unrelated sessions).
8. **Strict JSON response contract** from the LLM (`{"equation", "justification"}`)
   instead of free-text parsing.

## Consequences

- The tool is fully unit-testable: parser is pure, LLM client is injectable,
  REPL takes an injectable input reader and output stream.
- Derivations that are mathematically wrong but syntactically well-formed will
  be appended without warning. Documented in README.
- Adding a second operator or non-letter variables would require parser
  changes and a new system prompt; intentionally out of scope.
