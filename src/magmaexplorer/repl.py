"""Interactive REPL for magmaexplorer.

`read_input`, `llm`, and `critic` are injectable so the loop is testable
without a real terminal or network. The production entry point in `__main__.py`
wires these to prompt_toolkit and the Anthropic client respectively.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Callable, TextIO, Union

from rich.console import Console
from rich.table import Table

from .llm import LLMError, LLMResult, call_llm, critique_entry, format_user_message
from .term import (
    Definition,
    Equation,
    ParseError,
    parse,
    parse_entry,
    pretty,
    pretty_entry,
)

Item = Union[Equation, Definition]
LLMCallable = Callable[[list[Item], str], LLMResult]
CriticCallable = Callable[[list[Item], str], str]
InputReader = Callable[[], str]

QUIT_COMMANDS = {"/quit", "/exit"}


@dataclass
class Entry:
    content: Item
    sources: list[int] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)


@dataclass
class State:
    entries: list[Entry] = field(default_factory=list)
    debug: bool = False


def _kind(item: Item) -> str:
    return "definition" if isinstance(item, Definition) else "equation"


def _format_sources(sources: list[int]) -> str:
    return ", ".join(str(s) for s in sources) if sources else "-"


def _format_steps_cell(steps: list[str]) -> str:
    if not steps:
        return "-"
    return "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


def _format_add_line(i: int, entry: Entry) -> str:
    header_parts = [f"[{i}] {pretty_entry(entry.content)}"]
    if isinstance(entry.content, Definition):
        header_parts.append("[definition]")
    if entry.sources:
        header_parts.append(f"from [{_format_sources(entry.sources)}]")
    header = "   ".join(header_parts)
    if not entry.steps:
        return header
    lines = [header]
    for idx, step in enumerate(entry.steps, 1):
        lines.append(f"    {idx}. {step}")
    return "\n".join(lines)


def _print_list(entries: list[Entry], out: TextIO) -> None:
    if not entries:
        out.write("(list is empty)\n")
        return
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right")
    table.add_column("kind")
    table.add_column("statement")
    table.add_column("sources")
    table.add_column("steps")
    for i, e in enumerate(entries):
        table.add_row(
            f"[{i}]",
            _kind(e.content),
            pretty_entry(e.content),
            _format_sources(e.sources),
            _format_steps_cell(e.steps),
        )
    Console(file=out, color_system=None, width=140).print(table)


def _do_save(path: str, entries: list[Entry]) -> str:
    data = []
    for e in entries:
        if isinstance(e.content, Definition):
            row = {
                "kind": "definition",
                "name": e.content.name,
                "body": pretty(e.content.body),
            }
        else:
            row = {
                "kind": "equation",
                "lhs": pretty(e.content.lhs),
                "rhs": pretty(e.content.rhs),
            }
        row["sources"] = list(e.sources)
        row["steps"] = list(e.steps)
        data.append(row)
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return f"saved {len(entries)} item(s) to {path}\n"
    except OSError as exc:
        return f"save failed: {exc}\n"


def _do_load(path: str, entries: list[Entry]) -> str:
    try:
        with open(path) as f:
            data = json.load(f)
        new_entries: list[Entry] = []
        for item in data:
            kind = item.get("kind", "equation")
            if kind == "definition":
                content: Item = Definition(name=item["name"], body=parse(item["body"]))
            else:
                content = parse_entry(f"{item['lhs']}={item['rhs']}")
                if not isinstance(content, Equation):
                    raise ParseError("expected equation in legacy entry")
            if "steps" in item:
                raw = item["steps"]
                steps = [str(s) for s in raw] if isinstance(raw, list) else [str(raw)]
            elif item.get("justification"):
                steps = [str(item["justification"])]
            else:
                steps = []
            new_entries.append(
                Entry(
                    content=content,
                    sources=list(item.get("sources", [])),
                    steps=steps,
                )
            )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ParseError) as exc:
        return f"load failed: {exc}\n"
    entries.clear()
    entries.extend(new_entries)
    return f"loaded {len(new_entries)} item(s) from {path}\n"


def _compute_cascade(entries: list[Entry], doomed_root: int) -> set[int]:
    """Return the set of indices to delete if `doomed_root` is deleted:
    `doomed_root` itself plus every entry transitively derived from it."""
    doomed = {doomed_root}
    changed = True
    while changed:
        changed = False
        for j, e in enumerate(entries):
            if j in doomed:
                continue
            if any(s in doomed for s in e.sources):
                doomed.add(j)
                changed = True
    return doomed


def _do_clear(arg: str, state: State, out: TextIO, read_input: InputReader) -> None:
    if not arg:
        out.write("clear entire list? [y/N]: ")
        try:
            answer = read_input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer == "y":
            state.entries.clear()
            out.write("cleared.\n")
        else:
            out.write("cancelled.\n")
        return

    try:
        i = int(arg)
    except ValueError:
        out.write(f"invalid index: {arg!r}\n")
        return
    if not (0 <= i < len(state.entries)):
        out.write(f"index out of range: {i}\n")
        return

    doomed = _compute_cascade(state.entries, i)
    dependents = len(doomed) - 1
    if dependents == 0:
        prompt = f"delete [{i}]? [y/N]: "
    else:
        noun = "entry" if dependents == 1 else "entries"
        prompt = f"delete [{i}] and {dependents} dependent {noun}? [y/N]: "
    out.write(prompt)
    try:
        answer = read_input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "y":
        out.write("cancelled.\n")
        return

    old_to_new: dict[int, int] = {}
    new_entries: list[Entry] = []
    for j, e in enumerate(state.entries):
        if j in doomed:
            continue
        old_to_new[j] = len(new_entries)
        new_entries.append(
            Entry(
                content=e.content,
                sources=[old_to_new[s] for s in e.sources],
                steps=list(e.steps),
            )
        )
    state.entries.clear()
    state.entries.extend(new_entries)
    noun = "entry" if len(doomed) == 1 else "entries"
    out.write(f"deleted {len(doomed)} {noun}.\n")


def _do_verify(arg: str, state: State, critic: CriticCallable, out: TextIO) -> None:
    if not arg:
        out.write("usage: /verify <index>\n")
        return
    try:
        i = int(arg)
    except ValueError:
        out.write(f"invalid index: {arg!r}\n")
        return
    if not (0 <= i < len(state.entries)):
        out.write(f"index out of range: {i}\n")
        return

    entry = state.entries[i]
    if isinstance(entry.content, Definition):
        out.write(f"[{i}] is a definition; nothing to verify.\n")
        return
    if not entry.sources:
        out.write(f"[{i}] is an axiom (no sources); nothing to verify.\n")
        return

    source_items: list[Item] = []
    for s in entry.sources:
        if 0 <= s < len(state.entries):
            source_items.append(state.entries[s].content)
        else:
            out.write(f"warning: cited source [{s}] is out of range; skipping.\n")

    claim_text = pretty_entry(entry.content)
    try:
        verdict = critic(source_items, claim_text)
    except LLMError as exc:
        out.write(f"verify error: {exc}\n")
        return
    out.write(f"verdict for [{i}] {claim_text}:\n{verdict}\n")


_HELP_TEXT = """commands:
  <term>=<term>     add an equation (e.g. x*y=y*(x*x))
  <var>:=<term>     add a definition (a syntactic abbreviation)
  /list             show the numbered list (tabular)
  /verify <i>       ask a fresh LLM critic to re-check entry i's derivation
  /clear            empty the entire list (asks for confirmation)
  /clear <i>        delete entry i and every entry derived from it (cascading)
  /save <path>      write list to a JSON file
  /load <path>      replace list from a JSON file
  /debug            toggle printing of the exact payload sent to the LLM
  /help             this message
  /quit             exit (Ctrl-D also works)
anything else is sent to the LLM as a derivation command.
"""


def _handle_slash(
    line: str,
    state: State,
    out: TextIO,
    read_input: InputReader,
    critic: CriticCallable,
) -> bool:
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in QUIT_COMMANDS:
        return False
    if cmd == "/list":
        _print_list(state.entries, out)
    elif cmd == "/help":
        out.write(_HELP_TEXT)
    elif cmd == "/debug":
        state.debug = not state.debug
        out.write(f"debug: {'on' if state.debug else 'off'}\n")
    elif cmd == "/verify":
        _do_verify(arg, state, critic, out)
    elif cmd == "/clear":
        _do_clear(arg, state, out, read_input)
    elif cmd == "/save":
        out.write("usage: /save <path>\n" if not arg else _do_save(arg, state.entries))
    elif cmd == "/load":
        out.write("usage: /load <path>\n" if not arg else _do_load(arg, state.entries))
    else:
        out.write(f"unknown command: {cmd}\n")
    return True


def _handle_line(
    line: str,
    state: State,
    llm: LLMCallable,
    critic: CriticCallable,
    out: TextIO,
    read_input: InputReader,
) -> bool:
    line = line.strip()
    if not line:
        return True
    if line.startswith("/"):
        return _handle_slash(line, state, out, read_input, critic)

    # Try as a direct equation or definition.
    try:
        content: Item | None = parse_entry(line)
    except ParseError:
        content = None

    if content is not None:
        entry = Entry(content=content)
        state.entries.append(entry)
        out.write(_format_add_line(len(state.entries) - 1, entry) + "\n")
        return True

    # Otherwise route to the LLM. Only the bare items are sent — never
    # sources or justifications from prior entries.
    items: list[Item] = [e.content for e in state.entries]
    if state.debug:
        out.write("--- LLM input ---\n")
        out.write(format_user_message(items, line) + "\n")
        out.write("--- end ---\n")

    try:
        result = llm(items, line)
    except LLMError as exc:
        out.write(f"llm error: {exc}\n")
        return True

    try:
        new_content = parse_entry(result.equation)
    except ParseError as exc:
        out.write(f"llm returned unparseable equation {result.equation!r}: {exc}\n")
        return True

    entry = Entry(
        content=new_content,
        sources=list(result.sources),
        steps=list(result.steps),
    )
    state.entries.append(entry)
    out.write(_format_add_line(len(state.entries) - 1, entry) + "\n")
    return True


def run_repl(
    *,
    read_input: InputReader | None = None,
    llm: LLMCallable = call_llm,
    critic: CriticCallable = critique_entry,
    out: TextIO | None = None,
    initial: str | None = None,
) -> None:
    out = out or sys.stdout
    if read_input is None:
        read_input = _default_input

    state = State()

    if initial is not None:
        _handle_line(initial, state, llm, critic, out, read_input)

    while True:
        try:
            line = read_input()
        except (EOFError, StopIteration, KeyboardInterrupt):
            out.write("\nbye.\n")
            return
        if not _handle_line(line, state, llm, critic, out, read_input):
            out.write("bye.\n")
            return


def _default_input() -> str:
    from prompt_toolkit import PromptSession
    session = PromptSession()
    return session.prompt("magma> ")
