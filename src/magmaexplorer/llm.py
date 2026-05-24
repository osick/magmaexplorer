"""Anthropic-API wrapper for LLM-driven magma derivations.

The model is forced into a strict JSON shape:
    {"equation": "lhs = rhs", "justification": "...", "sources": [int, ...]}
"""

from __future__ import annotations

import json
from dataclasses import InitVar, dataclass, field
from typing import Union

from .term import Definition, Equation, pretty_entry

DEFAULT_MODEL = "claude-opus-4-7"
MAX_TOKENS = 2048

Item = Union[Equation, Definition]

SYSTEM_PROMPT = """You are a magma equational-reasoning assistant.

ALPHABET. Magma terms use single-letter lowercase variables (a-z), the binary
operator '*' (left-associative), and parentheses. Example: y*(x*x).

ITEMS. The user maintains a numbered list. Each item is either:
  - an EQUATION  `lhs = rhs`        - a magma identity (can be used as a rewrite rule).
  - a DEFINITION `name := body`     - a syntactic abbreviation; replace name by body
                                       (or vice versa). A definition is NOT an equation;
                                       you must never derive `name = body` as a magma
                                       equation from it.

LAWS. In a general magma there are NO laws beyond the equations the user has
provided. Do NOT use associativity, commutativity, cancellation, idempotence,
identity, inverses, distributivity, or any other algebraic law unless an
equation in the list directly says so.

DERIVATION DSL. Each derivation step you emit MUST be exactly one of the
following primitives. References take two forms: `[i]` for an entry in the
list, or `s<k>` for the intermediate result of an earlier step in this same
derivation (1-indexed, so s1 is the result of the first step, s2 the second).

  sym <ref>
      Swap the sides of an equation.

  inst <ref> <var>:=<term> [, <var>:=<term> ...]
      Simultaneous variable substitution in an equation.
      Example: `inst [0] x:=y, y:=x`

  trans <ref-a> <ref-b>
      Transitivity. Both refs must be equations. The two equations must share
      a side (lhs or rhs, in any of the 4 orientations). The result eliminates
      the shared side. Example: from [a=b] and [b=c], `trans [0] [1]` -> a=c.

  rewrite <ref-target> using <ref-rule> [backwards]
      Treat the rule equation as a left-to-right rewrite (lhs->rhs, or the
      reverse if `backwards`). Find one leftmost-outermost occurrence of the
      pattern inside the target equation (LHS first, then RHS) and substitute.

  expand <ref-target> <ref-def>
      Replace one leftmost-outermost occurrence of <def>.name inside the
      target equation by <def>.body.

  fold <ref-target> <ref-def>
      The reverse of expand: replace one leftmost-outermost occurrence of
      <def>.body inside the target equation by <def>.name.

A typical derivation: each `steps` entry is one DSL primitive invocation, in
order, each operating on entries from the list or on `s<k>` results from
earlier steps (s1, s2, ...). The FINAL step's result must equal the value you
give in `equation`.

OUTPUT. Respond with EXACTLY one JSON object, no prose around it, with these
keys:
  "equation": a single well-formed term=term over the alphabet.
  "steps":    JSON array of strings, each a DSL primitive invocation per above.
              If you genuinely cannot express a step in DSL, write a plain
              English fallback line; it will be marked unverifiable but kept.
  "sources":  JSON array of the integer indices the derivation cites from the
              list. Use [] if nothing is referenced.

Do not include code fences or any text outside the JSON object."""


CRITIC_SYSTEM_PROMPT = """You are an adversarial reviewer of magma derivations.

You will be shown a small set of source items (equations and/or definitions) over
a single binary operator '*' on single-letter variables, and a CLAIM equation.
Your job is to decide whether the CLAIM follows from the source items in a free
magma, where the only available laws are the equations themselves (no
associativity, commutativity, cancellation, etc.) and definitions expand
syntactically.

If valid: state so briefly and outline the rewrite steps.
If invalid: state so and point out the specific step or assumption that fails.

Reply in plain text, one paragraph, no JSON, no code fences."""


@dataclass
class LLMResult:
    equation: str
    steps: list[str] = field(default_factory=list)
    sources: list[int] = field(default_factory=list)
    justification: InitVar[str | None] = None

    def __post_init__(self, justification: str | None) -> None:
        # Back-compat: callers that still pass `justification="..."` get it
        # wrapped as a single step, provided they did not also pass steps.
        if justification is not None and not self.steps:
            self.steps = [justification]


class LLMError(RuntimeError):
    pass


def _format_list(items: list[Item]) -> str:
    if not items:
        return "(the list is empty)"
    lines = []
    for i, item in enumerate(items):
        if isinstance(item, Definition):
            lines.append(f"[{i}] {pretty_entry(item)}   [definition - NOT a magma equation]")
        else:
            lines.append(f"[{i}] {pretty_entry(item)}")
    return "\n".join(lines)


def format_user_message(items: list[Item], command: str) -> str:
    """Return the exact user-message string that gets sent to the LLM."""
    return (
        "Current magma list:\n"
        f"{_format_list(items)}\n\n"
        "Command:\n"
        f"{command}"
    )


def call_llm(
    items: list[Item],
    command: str,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
) -> LLMResult:
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": format_user_message(items, command)}],
        )
    except Exception as exc:  # network / API error
        raise LLMError(f"LLM call failed: {exc}") from exc

    text = response.content[0].text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMError(f"LLM response was not valid JSON: {text!r}") from exc

    if not isinstance(data, dict) or "equation" not in data:
        raise LLMError(f"LLM response missing required key 'equation': {data!r}")

    if "steps" in data:
        steps_raw = data["steps"]
        if isinstance(steps_raw, list):
            steps = [str(s) for s in steps_raw]
        else:
            steps = [str(steps_raw)]
    elif "justification" in data:
        steps = [str(data["justification"])]
    else:
        raise LLMError(f"LLM response missing both 'steps' and 'justification': {data!r}")

    sources_raw = data.get("sources", [])
    if isinstance(sources_raw, list):
        sources = [int(s) for s in sources_raw if isinstance(s, (int, float)) and not isinstance(s, bool)]
    else:
        sources = []

    return LLMResult(
        equation=str(data["equation"]),
        steps=steps,
        sources=sources,
    )


def _critic_user_message(source_items: list[Item], claim_text: str) -> str:
    if source_items:
        listing = "\n".join(f"[{i}] {pretty_entry(s)}" for i, s in enumerate(source_items))
    else:
        listing = "(no sources cited)"
    return (
        "Source items:\n"
        f"{listing}\n\n"
        "Claim:\n"
        f"{claim_text}"
    )


def critique_entry(
    source_items: list[Item],
    claim_text: str,
    *,
    client=None,
    model: str = DEFAULT_MODEL,
) -> str:
    """Ask a fresh stateless LLM call to critique a derivation.

    Returns the critic's plain-text verdict.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=CRITIC_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _critic_user_message(source_items, claim_text)}],
        )
    except Exception as exc:
        raise LLMError(f"critique call failed: {exc}") from exc

    return response.content[0].text.strip()
