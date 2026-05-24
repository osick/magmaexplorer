"""Shared data model for derivation lists.

`Entry` is the unit of state in a magmaexplorer derivation list. It is the
sole `list[Entry]` data type that flows between the three frontends:

- the interactive REPL (`repl.py`) — mutates a list of `Entry` from slash
  commands as the user/LLM builds a derivation;
- the Lean renderer (`lean_export.py`) — consumes a list of `Entry` and
  produces Lean 4 source code;
- the (future) competition solver (`solver.py`) — constructs a list of
  `Entry` from an automated proof-search loop and hands it to the renderer.

Keeping `Entry` here (not in `repl.py`) prevents an artificial REPL → solver
dependency: the solver can import `Entry` and `lean_export` without pulling
in `prompt_toolkit`, the Anthropic SDK, or any REPL plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union

from .term import Definition, Equation

Item = Union[Equation, Definition]


@dataclass
class Entry:
    """One row of the derivation list.

    - `content`: the equation or definition itself (parsed `Item`)
    - `sources`: indices of earlier entries cited by this one's derivation
                 (`[]` for axioms / user-provided inputs)
    - `steps`:   the DSL steps used to derive `content`. Empty for axioms;
                 length-1 for slash-command derivations (`/sym`, `/inst`, …);
                 may be longer for LLM-supplied multi-step derivations.
                 Each entry is a DSL string optionally prefixed with a
                 verification marker (`✓ `, `✗ `, `? `) added by the REPL.
    """

    content: Item
    sources: list[int] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


def compute_ancestors(entries: list[Entry], idx: int) -> set[int]:
    """Return `entries[idx]`'s transitive source-graph ancestors, including
    `idx` itself.

    Walks `sources` links recursively. Indices out of range are silently
    skipped (safety net against corrupted save files).
    """
    seen: set[int] = set()
    stack = [idx]
    while stack:
        cur = stack.pop()
        if cur in seen or not (0 <= cur < len(entries)):
            continue
        seen.add(cur)
        stack.extend(entries[cur].sources)
    return seen
