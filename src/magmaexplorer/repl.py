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


_PRIMITIVE_NAME = {
    _dsl.Sym: "sym",
    _dsl.Inst: "inst",
    _dsl.Trans: "trans",
    _dsl.Rewrite: "rewrite",
    _dsl.Expand: "expand",
    _dsl.Fold: "fold",
}


def _strip_verify_prefix(raw: str) -> str:
    """Strip the ✓/✗/? prefix that LLM-derived steps carry from verification.

    `_verify_llm_steps` annotates each step with `✓ `, `✗ `, or `? ` so the
    list display can show validity. For DSL re-parsing, those markers must
    be removed first; otherwise `parse_step("✓ sym [0]")` fails.
    """
    for marker in ("✓ ", "✗ ", "? "):
        if raw.startswith(marker):
            return raw[len(marker):]
    return raw


def _edge_rule_labels(steps: list[str]) -> dict[int, list[str]]:
    """For each entry-source index referenced across `steps`, return the
    ordered list of distinct DSL primitive names that referenced it.

    Steps that don't parse as DSL contribute no labels (the edge remains
    unlabeled). Steps using only `s<k>` refs (no `[i]`) also contribute none.
    """
    result: dict[int, list[str]] = {}
    for raw in steps:
        stripped = _strip_verify_prefix(raw)
        # Cut off any trailing `[...]` annotation appended by the verifier
        # (e.g. `"✗ inst [0] x:=y   [...explanation]"`).
        if "   [" in stripped:
            stripped = stripped.split("   [", 1)[0].rstrip()
        try:
            step = _dsl.parse_step(stripped)
        except DSLError:
            continue
        name = _PRIMITIVE_NAME.get(type(step))
        if name is None:
            continue
        for src_idx in _entry_source_indices(step):
            bucket = result.setdefault(src_idx, [])
            if name not in bucket:
                bucket.append(name)
    return result


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
        lines.append("Each node shows the entry's magma statement. An arrow `na --> nb` means entry `b` cites entry `a` as a source.")
        lines.append("Edge labels name the DSL primitive(s) that consumed the source while deriving the target.")
        lines.append("Definitions are drawn as stadiums; equations as rectangles.")
        lines.append("")
        lines.append("```mermaid")
        lines.append("graph TD")
        for i, e in enumerate(state.entries):
            label = pretty_entry(e.content)
            if isinstance(e.content, Definition):
                lines.append(f'    n{i}(["{label}"])')
            else:
                lines.append(f'    n{i}["{label}"]')
        for i, e in enumerate(state.entries):
            edge_labels = _edge_rule_labels(e.steps)
            for s in e.sources:
                if 0 <= s < len(state.entries):
                    primitives = edge_labels.get(s, [])
                    if primitives:
                        lbl = ", ".join(primitives)
                        lines.append(f"    n{s} -->|{lbl}| n{i}")
                    else:
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


def _term_to_lean(t) -> str:
    """Render a Term as a Lean 4 expression over `*` (`HMul.hMul`).

    Matches the magma pretty-printer's grouping: left children stay bare,
    right children get parens when they are themselves operators.
    """
    from .term import Op, Var
    if isinstance(t, Var):
        return t.name
    left = _term_to_lean(t.left)
    right = _term_to_lean(t.right)
    if isinstance(t.right, Op):
        right = f"({right})"
    return f"{left} * {right}"


def _collect_vars(t) -> set[str]:
    from .term import Op, Var
    if isinstance(t, Var):
        return {t.name}
    return _collect_vars(t.left) | _collect_vars(t.right)


def _lean_forall(eq: Equation) -> str:
    """Render a universally quantified Lean equation:

        ∀ x y : G, x * y = y * (x * x)

    Variables are sorted to give a deterministic, readable signature.
    """
    vars_set = _collect_vars(eq.lhs) | _collect_vars(eq.rhs)
    if vars_set:
        vars_str = " ".join(sorted(vars_set))
        return f"∀ {vars_str} : G, {_term_to_lean(eq.lhs)} = {_term_to_lean(eq.rhs)}"
    return f"{_term_to_lean(eq.lhs)} = {_term_to_lean(eq.rhs)}"


_LEAN_PREAMBLE = """-- magmaexplorer export: {name}
-- {count} {entries_word}
--
-- Each `axiom` is an input equation (no derivation in the REPL).
-- Each `theorem` carries a proof or a `sorry` placeholder; the comment block
-- above it records the DSL primitives the magmaexplorer REPL used to derive it.
-- Fill in the `by ...` blocks to produce a complete Lean proof script for
-- submission (e.g. to the equational-theories distillation challenge).
--
-- Each declaration carries its own `{{G : Type _}} [Mul G]` binders. `axiom`
-- does not pick up `variable` declarations in Lean 4 and `Type*` is a
-- Mathlib-only shorthand, so making the binders explicit keeps the file
-- compiling in both vanilla Lean 4 and Mathlib environments.
"""

_LEAN_BINDERS = "{G : Type _} [Mul G]"


def _term_to_lean_arg(t) -> str:
    """Render a term for use as an argument to a Lean function application.

    Same as `_term_to_lean` but parenthesises Op terms at the top level so
    `eq_0 (a * b) c` parses correctly (rather than `eq_0 a * b c`).
    """
    from .term import Op
    rendered = _term_to_lean(t)
    if isinstance(t, Op):
        return f"({rendered})"
    return rendered


def _entry_sorted_vars(entries: list[Entry], idx: int) -> list[str] | None:
    """Return the alpha-sorted bound vars of `entries[idx]` if it is an
    equation; None otherwise (definitions cannot be applied as Lean facts)."""
    if not (0 <= idx < len(entries)):
        return None
    e = entries[idx]
    if not isinstance(e.content, Equation):
        return None
    return sorted(_collect_vars(e.content.lhs) | _collect_vars(e.content.rhs))


def _default_eq_name(idx: int) -> str:
    """Default name resolver used by standalone `/lean`: entry `[i]` → `eq_i`."""
    return f"eq_{idx}"


def _apply_eq_with_subst(name: str, src_vars: list[str], subst: dict[str, "object"]) -> str:
    """Build `<name> arg1 arg2 ...` for Inst: each src var either maps to
    its substituted Lean term, or keeps its own name. `name` is the Lean
    identifier for the universally-quantified target equation — typically
    `eq_<i>` in standalone mode, `h` or `h_<i>` in implication mode."""
    args = []
    for v in src_vars:
        if v in subst:
            args.append(_term_to_lean_arg(subst[v]))
        else:
            args.append(v)
    return f"{name} {' '.join(args)}".strip() if args else name


def _apply_eq_for_trans(name: str, src_vars: list[str], goal_vars: list[str]) -> str:
    """Build `<name> arg1 arg2 ...` for Trans: each src var that is also a
    goal var passes through unchanged; any "orphan" var (in V_src but not in
    V_r) is filled with the first goal var (any G works because both sides
    use the same witness, so the chain still type-checks)."""
    if not src_vars:
        return name
    fallback = goal_vars[0] if goal_vars else "default"
    args = [v if v in goal_vars else fallback for v in src_vars]
    return f"{name} {' '.join(args)}".strip()


def _lean_proof_body(
    entry: Entry,
    entries: list[Entry],
    goal_vars: list[str],
    name=_default_eq_name,
) -> list[str]:
    """Build the lines that go inside the `by` block of a derived theorem.

    Returns `["  intro ...", "  exact ..."]` for translatable single-step
    entries, or `["  sorry"]` (possibly with a hint comment) for everything
    we can't yet auto-translate.

    `name` is a `Callable[[int], str]` that maps an entry index to the Lean
    identifier the proof should use for that entry's universally-quantified
    equation. Standalone `/lean` uses the default (`eq_<i>`); implication
    mode passes a resolver that returns `h` for the hypothesis index and
    `h_<i>` for inlined intermediates.
    """
    # Multi-step is out of scope for the MVP — fall back.
    if len(entry.steps) != 1:
        return ["  sorry  -- multi-step derivation; translate the chain manually"]

    raw = entry.steps[0]
    clean = _strip_verify_prefix(raw)
    if "   [" in clean:
        clean = clean.split("   [", 1)[0].rstrip()

    try:
        step = _dsl.parse_step(clean)
    except DSLError:
        return [f"  sorry  -- step did not parse as DSL: {raw}"]

    intro_line = f"  intro {' '.join(goal_vars)}" if goal_vars else None

    def _emit(exact_term: str) -> list[str]:
        body = []
        if intro_line is not None:
            body.append(intro_line)
        body.append(f"  exact {exact_term}")
        return body

    # --- sym -----------------------------------------------------------------
    if isinstance(step, _dsl.Sym):
        if not isinstance(step.target, EntryRef):
            return [f"  sorry  -- sym on step-ref not auto-translated: {raw}"]
        src_vars = _entry_sorted_vars(entries, step.target.index)
        if src_vars is None:
            return [f"  sorry  -- sym target is not an equation: {raw}"]
        tgt_name = name(step.target.index)
        applied = f"{tgt_name} {' '.join(src_vars)}".strip() if src_vars else tgt_name
        return _emit(f"({applied}).symm")

    # --- inst ----------------------------------------------------------------
    if isinstance(step, _dsl.Inst):
        if not isinstance(step.target, EntryRef):
            return [f"  sorry  -- inst on step-ref not auto-translated: {raw}"]
        src_vars = _entry_sorted_vars(entries, step.target.index)
        if src_vars is None:
            return [f"  sorry  -- inst target is not an equation: {raw}"]
        subst = {v: t for v, t in step.substitutions}
        return _emit(_apply_eq_with_subst(name(step.target.index), src_vars, subst))

    # --- trans ---------------------------------------------------------------
    if isinstance(step, _dsl.Trans):
        if not (isinstance(step.left, EntryRef) and isinstance(step.right, EntryRef)):
            return [f"  sorry  -- trans on step-ref not auto-translated: {raw}"]
        a_idx = step.left.index
        b_idx = step.right.index
        a_vars = _entry_sorted_vars(entries, a_idx)
        b_vars = _entry_sorted_vars(entries, b_idx)
        if a_vars is None or b_vars is None:
            return [f"  sorry  -- trans operand is not an equation: {raw}"]
        a_eq = entries[a_idx].content
        b_eq = entries[b_idx].content
        a_apply = _apply_eq_for_trans(name(a_idx), a_vars, goal_vars)
        b_apply = _apply_eq_for_trans(name(b_idx), b_vars, goal_vars)
        if a_eq.rhs == b_eq.lhs:
            return _emit(f"({a_apply}).trans ({b_apply})")
        if a_eq.lhs == b_eq.lhs:
            return _emit(f"({a_apply}).symm.trans ({b_apply})")
        if a_eq.rhs == b_eq.rhs:
            return _emit(f"({a_apply}).trans ({b_apply}).symm")
        if a_eq.lhs == b_eq.rhs:
            return _emit(f"({a_apply}).symm.trans ({b_apply}).symm")
        return [f"  sorry  -- trans: no shared side detected (should not happen)"]

    # --- rewrite -------------------------------------------------------------
    if isinstance(step, _dsl.Rewrite):
        if not (isinstance(step.target, EntryRef) and isinstance(step.rule, EntryRef)):
            return [f"  sorry  -- rewrite on step-ref not auto-translated: {raw}"]
        t_idx = step.target.index
        r_idx = step.rule.index
        t_vars = _entry_sorted_vars(entries, t_idx)
        if t_vars is None or _entry_sorted_vars(entries, r_idx) is None:
            return [f"  sorry  -- rewrite operand is not an equation: {raw}"]
        t_apply = _apply_eq_for_trans(name(t_idx), t_vars, goal_vars)
        arrow = "← " if step.backwards else ""
        # Local hypothesis name `h_rw` avoids clashing with the outer `h`
        # the implication-mode wrapper introduces.
        body: list[str] = []
        if intro_line is not None:
            body.append(intro_line)
        body.append(f"  -- NOTE: `rw` rewrites ALL occurrences; the DSL only rewrites the")
        body.append(f"  -- leftmost-outermost one. If the goal disagrees, replace `rw` with")
        body.append(f"  -- `nth_rewrite 1` (from Mathlib) to target a single occurrence.")
        body.append(f"  have h_rw := {t_apply}")
        body.append(f"  rw [{arrow}{name(r_idx)}] at h_rw")
        body.append("  exact h_rw")
        return body

    # expand / fold — not yet auto-translated
    primitive_name = _PRIMITIVE_NAME.get(type(step), "<unknown>")
    return [f"  sorry  -- {primitive_name} not yet auto-translated; magmaexplorer definitions have no direct Lean counterpart"]


def _do_lean(arg: str, state: State, out: TextIO) -> None:
    name = arg.strip()
    if not name:
        out.write("usage: /lean <filename>\n")
        return

    path = name if name.endswith(".lean") else f"{name}.lean"

    count = len(state.entries)
    lines: list[str] = []
    lines.append(_LEAN_PREAMBLE.format(
        name=name,
        count=count,
        entries_word="entry" if count == 1 else "entries",
    ))

    for i, e in enumerate(state.entries):
        lines.append("")
        if isinstance(e.content, Definition):
            lines.append(f"-- [{i}] definition: {pretty_entry(e.content)}")
            lines.append(f"--     (syntactic abbreviation; inline `{e.content.name}` as `{_term_to_lean(e.content.body)}` where needed)")
            continue

        statement = _lean_forall(e.content)
        if not e.sources and not e.steps:
            lines.append(f"-- [{i}] axiom: {pretty_entry(e.content)}")
            lines.append(f"axiom eq_{i} {_LEAN_BINDERS} : {statement}")
        else:
            srcs = ", ".join(f"[{s}]" for s in e.sources) if e.sources else "(none)"
            lines.append(f"-- [{i}] derived from {srcs}")
            for k, step in enumerate(e.steps, 1):
                lines.append(f"--     {k}. {step}")
            lines.append(f"theorem eq_{i} {_LEAN_BINDERS} : {statement} := by")
            goal_vars = sorted(_collect_vars(e.content.lhs) | _collect_vars(e.content.rhs))
            for line in _lean_proof_body(e, state.entries, goal_vars):
                lines.append(line)

    try:
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        out.write(f"lean export failed: {exc}\n")
        return
    out.write(f"lean script written to {path} ({count} entries)\n")


def _compute_implication_chain(
    entries: list[Entry], from_idx: int, to_idx: int
) -> list[int]:
    """Return sorted indices `[from_idx, ..., to_idx]` forming the proof chain
    of `[to]` from `[from]`. Raises `ValueError` when the chain can't be built:
      - either index is out of range
      - `[from]` is not an ancestor of `[to]`
      - some ancestor of `[to]` is an axiom other than `[from]`
        (would require a second hypothesis we don't have)
      - some ancestor of `[to]` is a Definition
        (expand/fold are not yet auto-translated)
      - some ancestor cites a source outside the chain
        (would indicate a broken sources field)

    Entries are sorted by index, which is a valid topological order because
    magmaexplorer enforces forward-reference-free derivations.
    """
    n = len(entries)
    if not (0 <= from_idx < n):
        raise ValueError(f"from index out of range: {from_idx}")
    if not (0 <= to_idx < n):
        raise ValueError(f"to index out of range: {to_idx}")

    if from_idx == to_idx:
        return [from_idx]

    ancestors = _compute_ancestors(entries, to_idx)
    if from_idx not in ancestors:
        raise ValueError(
            f"[{from_idx}] is not an ancestor of [{to_idx}]; cannot derive [{to_idx}] from [{from_idx}]"
        )

    for idx in sorted(ancestors):
        e = entries[idx]
        if isinstance(e.content, Definition):
            raise ValueError(
                f"[{idx}] is a definition; expand/fold are not yet auto-translated, "
                f"so this implication chain cannot be compiled"
            )
        if idx == from_idx:
            continue
        if not e.sources:
            raise ValueError(
                f"[{idx}] is an axiom but is not the hypothesis [{from_idx}]; "
                f"cannot prove the goal from `h` alone"
            )
        for src in e.sources:
            if src not in ancestors:
                raise ValueError(
                    f"[{idx}] cites [{src}] which is not on the chain from "
                    f"[{from_idx}] to [{to_idx}]"
                )

    return sorted(ancestors)


_LEAN_IMPLICATION_PREAMBLE = """-- magmaexplorer implication: [{from_idx}] => [{to_idx}]
-- Chain length: {n} entries.
-- Hypothesis (h): {from_stmt}
-- Goal:           {to_stmt}
--
-- The hypothesis appears as the proof parameter `h` (no `axiom` declarations,
-- which the equational-theories Stage 2 judge would reject as `incomplete_proof`).
-- Intermediate derivation steps are inlined as universally-quantified `have`
-- blocks; the final tactic block discharges the goal.
--
-- For the equational-theories Lean project, swap `[Mul G]` for `[Magma G]` and
-- `*` for the project's `◇` notation as needed.
"""


def _do_lean_implication(arg: str, state: State, out: TextIO) -> None:
    parts = arg.split()
    if len(parts) < 3:
        out.write("usage: /lean-implication <from> <to> <name>\n")
        return
    try:
        from_idx = int(parts[0])
        to_idx = int(parts[1])
    except ValueError:
        out.write(f"invalid indices: {parts[0]!r} {parts[1]!r}\n")
        return
    name = " ".join(parts[2:]).strip()
    if not name:
        out.write("usage: /lean-implication <from> <to> <name>\n")
        return

    try:
        chain = _compute_implication_chain(state.entries, from_idx, to_idx)
    except ValueError as exc:
        out.write(f"implication error: {exc}\n")
        return

    from_entry = state.entries[from_idx]
    to_entry = state.entries[to_idx]
    if not isinstance(from_entry.content, Equation):
        out.write(f"[{from_idx}] is not an equation; cannot use as hypothesis\n")
        return
    if not isinstance(to_entry.content, Equation):
        out.write(f"[{to_idx}] is not an equation; cannot use as goal\n")
        return

    def resolve(idx: int) -> str:
        return "h" if idx == from_idx else f"h_{idx}"

    from_stmt = _lean_forall(from_entry.content)
    to_stmt = _lean_forall(to_entry.content)
    to_vars = sorted(_collect_vars(to_entry.content.lhs) | _collect_vars(to_entry.content.rhs))

    lines: list[str] = []
    lines.append(_LEAN_IMPLICATION_PREAMBLE.format(
        from_idx=from_idx,
        to_idx=to_idx,
        n=len(chain),
        from_stmt=pretty_entry(from_entry.content),
        to_stmt=pretty_entry(to_entry.content),
    ))
    lines.append(f"theorem implication {{G : Type _}} [Mul G]")
    lines.append(f"    (h : {from_stmt}) :")
    lines.append(f"    {to_stmt} := by")

    if from_idx == to_idx:
        # Trivial reflexive — `h` already has the goal's type.
        lines.append("  exact h")
    else:
        # Inline every intermediate as a universally-quantified `have` so
        # subsequent sym/inst/trans/rewrite can apply it like an axiom.
        for idx in chain:
            if idx == from_idx or idx == to_idx:
                continue
            e = state.entries[idx]
            stmt = _lean_forall(e.content)
            i_vars = sorted(_collect_vars(e.content.lhs) | _collect_vars(e.content.rhs))
            lines.append(f"  have h_{idx} : {stmt} := by")
            for ln in _lean_proof_body(e, state.entries, i_vars, name=resolve):
                # Nested-by bodies need an extra 2 spaces of indent.
                lines.append("  " + ln)
        # Final goal body at the outer indent level (`_lean_proof_body`
        # already prefixes its lines with 2 spaces).
        for ln in _lean_proof_body(to_entry, state.entries, to_vars, name=resolve):
            lines.append(ln)

    path = name if name.endswith(".lean") else f"{name}.lean"
    try:
        with open(path, "w") as f:
            f.write("\n".join(lines) + "\n")
    except OSError as exc:
        out.write(f"lean-implication failed: {exc}\n")
        return
    out.write(f"implication script written to {path} ({len(chain)} entries on chain)\n")


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
  /lean <name>      export the full list as a Lean 4 script <name>.lean
                    (axioms for inputs, theorem-with-sorry for derived entries,
                    DSL steps preserved as comments)
  /lean-implication <from> <to> <name>
                    export a single competition-shaped Lean theorem proving
                    [to] from [from] using the hypothesis as a proof parameter
                    `h` (no `axiom`); intermediates inlined as `have` blocks
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
    elif cmd == "/lean":
        _do_lean(arg, state, out)
    elif cmd == "/lean-implication":
        _do_lean_implication(arg, state, out)
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
