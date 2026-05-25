"""Tiny stub LLM used by the user-guide smoke recipes.

Looks up a canned response keyed by (hypothesis, goal). When nothing matches,
raises `LLMError` so the solver treats it as a failed attempt and (after
exhausting retries) cleanly raises `SolverError` — which the CLI turns into a
`{"status":"error"}` answer.
"""
from magmaexplorer.llm import LLMError, LLMResult
from magmaexplorer.term import pretty_entry

CANNED = {
    ("a*b = c", "c = a*b"): LLMResult(equation="c = a*b",
                                       steps=["sym [0]"], sources=[0]),
    ("a = b",   "b = a"):   LLMResult(equation="b = a",
                                       steps=["sym [0]"], sources=[0]),
    ("p = q",   "q = p"):   LLMResult(equation="q = p",
                                       steps=["sym [0]"], sources=[0]),
}

def stub(items, command):
    hyp = pretty_entry(items[0])
    for (h, g), r in CANNED.items():
        if h == hyp and g in command:
            return r
    raise LLMError(f"stub LLM has no canned response for hypothesis {hyp!r}")
