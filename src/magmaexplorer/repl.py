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

from . import dsl as _dsl
from .dsl import DSLError, EntryRef
from .llm import LLMError, LLMResult, call_llm, critique_entry, format_user_message
from .term import (
    Definition,
    Equation,
    ParseError,
    parse,
    parse_entry,
    pretty,
    pretty_entry,
    pretty_equation,
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


def _compute_ancestors(entries: list[Entry], i: int) -> set[int]:
    """Indices in i's transitive source chain (including i)."""
    seen: set[int] = set()
    stack = [i]
    while stack:
        cur = stack.pop()
        if cur in seen or not (0 <= cur < len(entries)):
            continue
        seen.add(cur)
        stack.extend(entries[cur].sources)
    return seen


def _entry_to_yaml_row(idx: int, e: Entry) -> dict:
    row: dict = {"index": idx, "kind": _kind(e.content)}
    if isinstance(e.content, Definition):
        row["name"] = e.content.name
        row["body"] = pretty(e.content.body)
    else:
        row["statement"] = pretty_entry(e.content)
    row["sources"] = list(e.sources)
    row["steps"] = list(e.steps)
    return row


def _do_deduction(arg: str, state: State, out: TextIO) -> None:
    parts = arg.split()
    if len(parts) != 3:
        out.write("usage: /deduction <from> <to> <name>\n")
        return
    try:
        i_from = int(parts[0])
        i_to = int(parts[1])
    except ValueError:
        out.write(f"invalid index: {parts[0]!r} or {parts[1]!r}\n")
        return
    name = parts[2]
    n = len(state.entries)
    for label, idx in (("from", i_from), ("to", i_to)):
        if not (0 <= idx < n):
            out.write(f"{label} index out of range: {idx}\n")
            return

    ancestors = _compute_ancestors(state.entries, i_to)
    if i_from not in ancestors:
        out.write(f"[{i_to}] is not derived from [{i_from}]\n")
        return

    rows = [_entry_to_yaml_row(idx, state.entries[idx]) for idx in sorted(ancestors)]
    doc = {"from": i_from, "to": i_to, "entries": rows}

    path = f"{name}.deduction"
    try:
        import yaml
        with open(path, "w") as f:
            yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
    except OSError as exc:
        out.write(f"write failed: {exc}\n")
        return
    out.write(f"deduction written to {path} ({len(ancestors)} entries)\n")


def _md_escape(text: str) -> str:
    """Escape characters that would break a markdown table cell."""
    return text.replace("|", "\\|")


def _mermaid_label_escape(text: str) -> str:
    """Escape characters that confuse mermaid node-label parsers.

    Mermaid's label parser treats `(`, `)`, `[`, `]` as syntax even inside
    quoted strings on some renderers (GitHub in particular), which produces
    an empty canvas. HTML entities render correctly everywhere.
    """
    return (
        text.replace("(", "&#40;")
            .replace(")", "&#41;")
            .replace("[", "&#91;")
            .replace("]", "&#93;")
            .replace('"', "&quot;")
    )


def _do_report(arg: str, state: State, out: TextIO) -> None:
    name = arg.strip()
    if not name:
        out.write("usage: /report <name>\n")
        return

    lines: list[str] = []
    lines.append(f"# magmaexplorer report: {name}")
    lines.append("")
    lines.append(f"_{len(state.entries)} entries_")
    lines.append("")

    if not state.entries:
        lines.append("(list is empty)")
    else:
        lines.append("## Entries")
        lines.append("")
        lines.append("| # | Kind | Statement | Sources | Steps |")
        lines.append("|---|------|-----------|---------|-------|")
        for i, e in enumerate(state.entries):
            statement = _md_escape(pretty_entry(e.content))
            sources = _format_sources(e.sources)
            if e.steps:
                steps_md = "<br>".join(
                    _md_escape(f"{k}. {s}") for k, s in enumerate(e.steps, 1)
                )
            else:
                steps_md = "-"
            lines.append(
                f"| [{i}] | {_kind(e.content)} | `{statement}` | {sources} | {steps_md} |"
            )
        lines.append("")

        lines.append("## Deduction graph")
        lines.append("")
        lines.append("Each node is one entry. An arrow `[a] --> [b]` means `[b]` cites `[a]` as a source.")
        lines.append("Definitions are drawn with rounded corners; equations with rectangles.")
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph TD")
        for i, e in enumerate(state.entries):
            label = _mermaid_label_escape(f"[{i}] {pretty_entry(e.content)}")
            if isinstance(e.content, Definition):
                lines.append(f'    n{i}(["{label}"])')
            else:
                lines.append(f'    n{i}["{label}"]')
        for i, e in enumerate(state.entries):
            for s in e.sources:
                if 0 <= s < len(state.entries):
                    lines.append(f"    n{s} --> n{i}")
        lines.append("```")

    path = f"{name}.md"
    try:
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        out.write(f"report failed: {exc}\n")
        return
    out.write(f"report written to {path} ({len(state.entries)} entries)\n")


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
  /deduction <from> <to> <name>
                    export the proof subtree from <from> to <to> as a
                    structured YAML file <name>.deduction
  /report <name>    export the full list as a markdown file <name>.md with a
                    table and a mermaid graph of the deduction relations
  /clear            empty the entire list (asks for confirmation)
  /clear <i>        delete entry i and every entry derived from it (cascading)
  /save <path>      write list to a JSON file
  /load <path>      replace list from a JSON file
  /debug            toggle printing of the exact payload sent to the LLM
  /help             this message
  /quit             exit (Ctrl-D also works)
anything else is sent to the LLM as a derivation command.

derivation primitives (each adds a verified entry):
  /sym <i>                             swap [i]'s sides
  /inst <i> x:=t [, y:=u ...]          substitute variables in [i]
  /trans <i> <j>                       transitivity, auto-detects shared side
  /rewrite <i> using <j> [backwards]   rewrite one subterm in [i] using [j] as a rule
  /expand <i> <d>                      replace <d>.name by <d>.body in [i]
  /fold <i> <d>                        replace <d>.body by <d>.name in [i]
"""


def _entry_source_indices(step: "_dsl.Step") -> list[int]:
    """Collect EntryRef indices from any ref-typed field of `step`, in
    declaration order. Returns [] if there are none (e.g. all StepRef)."""
    refs: list[int] = []
    if isinstance(step, _dsl.Sym):
        if isinstance(step.target, EntryRef):
            refs.append(step.target.index)
    elif isinstance(step, _dsl.Inst):
        if isinstance(step.target, EntryRef):
            refs.append(step.target.index)
    elif isinstance(step, _dsl.Trans):
        if isinstance(step.left, EntryRef):
            refs.append(step.left.index)
        if isinstance(step.right, EntryRef):
            refs.append(step.right.index)
    elif isinstance(step, _dsl.Rewrite):
        if isinstance(step.target, EntryRef):
            refs.append(step.target.index)
        if isinstance(step.rule, EntryRef):
            refs.append(step.rule.index)
    elif isinstance(step, (_dsl.Expand, _dsl.Fold)):
        if isinstance(step.target, EntryRef):
            refs.append(step.target.index)
        if isinstance(step.definition, EntryRef):
            refs.append(step.definition.index)
    return refs


def _args_to_dsl(primitive: str, arg: str) -> str:
    """Wrap bare integer index tokens in [...] to form a full DSL step string.

    Rules:
      sym  <i>                        -> sym [i]
      inst <i> rest...                -> inst [i] rest...
      trans <i> <j>                   -> trans [i] [j]
      rewrite <i> using <j> [bkwd]    -> rewrite [i] using [j] [bkwd]
      expand <i> <j>                  -> expand [i] [j]
      fold  <i> <j>                   -> fold [i] [j]
    """
    tokens = arg.split()

    def _wrap(tok: str) -> str:
        return f"[{tok}]" if tok.lstrip("-").isdigit() else tok

    if primitive in ("sym", "inst"):
        if tokens:
            tokens[0] = _wrap(tokens[0])
    elif primitive in ("trans", "expand", "fold"):
        if len(tokens) >= 1:
            tokens[0] = _wrap(tokens[0])
        if len(tokens) >= 2:
            tokens[1] = _wrap(tokens[1])
    elif primitive == "rewrite":
        # rewrite <i> using <j> [backwards]
        if len(tokens) >= 1:
            tokens[0] = _wrap(tokens[0])
        # find "using" keyword and wrap the token after it
        for k, tok in enumerate(tokens):
            if tok == "using" and k + 1 < len(tokens):
                tokens[k + 1] = _wrap(tokens[k + 1])
                break

    wrapped_arg = " ".join(tokens)
    return f"{primitive} {wrapped_arg}".strip()


def _handle_dsl(primitive: str, arg: str, state: "State", out: "TextIO") -> None:
    """Parse-execute-append pipeline for a single DSL primitive slash command."""
    dsl_str = _args_to_dsl(primitive, arg)
    try:
        step = _dsl.parse_step(dsl_str)
    except DSLError as exc:
        out.write(f"dsl error: {exc}\n")
        return

    entries_content = [e.content for e in state.entries]
    try:
        result_eq = _dsl.execute_step(step, entries_content, [])
    except DSLError as exc:
        out.write(f"dsl error: {exc}\n")
        return

    sources = _entry_source_indices(step)
    entry = Entry(content=result_eq, sources=sources, steps=[dsl_str])
    state.entries.append(entry)
    out.write(_format_add_line(len(state.entries) - 1, entry) + "\n")


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
    elif cmd == "/deduction":
        _do_deduction(arg, state, out)
    elif cmd == "/report":
        _do_report(arg, state, out)
    elif cmd == "/clear":
        _do_clear(arg, state, out, read_input)
    elif cmd == "/save":
        out.write("usage: /save <path>\n" if not arg else _do_save(arg, state.entries))
    elif cmd == "/load":
        out.write("usage: /load <path>\n" if not arg else _do_load(arg, state.entries))
    elif cmd in ("/sym", "/inst", "/trans", "/rewrite", "/expand", "/fold"):
        _handle_dsl(cmd[1:], arg, state, out)
    else:
        out.write(f"unknown command: {cmd}\n")
    return True


def _verify_llm_steps(
    raw_steps: list[str],
    entries: list[Item],
    claim: Equation,
) -> tuple[list[str], bool]:
    """Parse and execute each DSL step in order, building a chain via prior_results.

    Returns (annotated_steps, fully_verified):
      - annotated_steps: same length as raw_steps; each prefixed with ``✓ ``, ``✗ ``, or ``? ``.
      - fully_verified: True iff EVERY step parsed and executed, AND the final
        intermediate equals ``claim``. Otherwise False.

    A step that didn't parse or didn't execute is treated as a "gap" — subsequent
    steps may still parse and execute against prior_results (no fabricated entry
    is added for the gap). The chain is considered NOT fully_verified if any gap
    exists OR if the final prior_results[-1] != claim.
    """
    annotated: list[str] = []
    prior_results: list[Equation] = []
    has_gap = False

    for raw in raw_steps:
        try:
            step = _dsl.parse_step(raw)
        except DSLError:
            annotated.append(f"? {raw}")
            has_gap = True
            continue
        try:
            result_eq = _dsl.execute_step(step, entries, prior_results)
        except DSLError as exc:
            annotated.append(f"✗ {raw}   [{exc}]")
            has_gap = True
            continue
        annotated.append(f"✓ {raw}")
        prior_results.append(result_eq)

    if has_gap or not prior_results:
        return annotated, False

    if prior_results[-1] != claim:
        # Replace the last ✓ with an explanatory ✗.
        last_raw = raw_steps[-1]
        annotated[-1] = (
            f"✗ {last_raw}   "
            f"[final {pretty_equation(prior_results[-1])} ≠ claim "
            f"{pretty_equation(claim)}]"
        )
        return annotated, False

    return annotated, True


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

    items_view: list[Item] = [e.content for e in state.entries]
    if isinstance(new_content, Equation):
        annotated_steps, verified = _verify_llm_steps(
            list(result.steps), items_view, new_content
        )
    else:
        # LLM returned a definition — skip chain verification
        annotated_steps, verified = list(result.steps), False

    entry = Entry(
        content=new_content,
        sources=list(result.sources),
        steps=annotated_steps,
    )
    state.entries.append(entry)
    out.write(_format_add_line(len(state.entries) - 1, entry) + "\n")
    out.write(f"    [{'verified' if verified else 'unverified'}]\n")
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
