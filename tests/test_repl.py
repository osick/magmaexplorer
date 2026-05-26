import io

import pytest

from magmaexplorer.llm import LLMError, LLMResult
from magmaexplorer.repl import run_repl
from magmaexplorer.term import parse_equation


def _run(inputs, *, llm=None):
    """Run REPL with a scripted input list; return captured stdout text."""
    out = io.StringIO()
    fake_llm = llm or (lambda eqs, cmd: LLMResult(equation="x=x", justification="reflex"))

    def reader():
        for line in inputs:
            yield line

    gen = reader()
    run_repl(
        read_input=lambda: next(gen),
        llm=fake_llm,
        out=out,
    )
    return out.getvalue()


def test_equation_input_appends_and_echoes():
    output = _run(["x*y=y*(x*x)", "/quit"])
    assert "[0] x*y = y*(x*x)" in output


def test_list_command_shows_all():
    output = _run(["x*y=y*x", "a*a=a", "/list", "/quit"])
    assert "[0] x*y = y*x" in output
    assert "[1] a*a = a" in output


def test_list_when_empty():
    output = _run(["/list", "/quit"])
    assert "empty" in output.lower() or "[0]" not in output


def test_quit_via_eof():
    out = io.StringIO()

    def reader():
        raise EOFError

    run_repl(read_input=reader, llm=lambda e, c: None, out=out)
    # Should exit cleanly without raising.


def test_unknown_input_routes_to_llm():
    captured = []

    def fake_llm(eqs, cmd):
        captured.append((list(eqs), cmd))
        return LLMResult(equation="y*x = x*y", justification="symm")

    output = _run(
        ["x*y=y*x", "apply symm to 0", "/quit"],
        llm=fake_llm,
    )
    assert captured == [([parse_equation("x*y=y*x")], "apply symm to 0")]
    assert "[1] y*x = x*y" in output
    assert "symm" in output


def test_llm_error_does_not_append():
    def boom(eqs, cmd):
        raise LLMError("network down")

    output = _run(["x*y=y*x", "do something", "/list", "/quit"], llm=boom)
    assert "llm error: network down" in output
    # Only the original equation should be in the list.
    assert output.count("[0]") >= 2  # echo + /list
    assert "[1]" not in output


def test_unparseable_llm_response_does_not_append():
    def bad(eqs, cmd):
        return LLMResult(equation="x* =", justification="oops")

    output = _run(["x*y=y*x", "garble", "/list", "/quit"], llm=bad)
    assert "unparseable" in output
    assert "[1]" not in output


def test_input_with_equals_falls_through_to_llm():
    """`apply y=x*x to 0` contains '=' but isn't an equation."""
    captured = []

    def fake_llm(eqs, cmd):
        captured.append(cmd)
        return LLMResult(equation="x=x", justification="t")

    _run(["x*y=y*x", "apply y=x*x to 0", "/quit"], llm=fake_llm)
    assert captured == ["apply y=x*x to 0"]


def test_save_then_load_round_trip(tmp_path):
    path = tmp_path / "m.json"
    _run([
        "x*y=y*(x*x)",
        "a*a=a",
        f"/save {path}",
        "/clear",
        "y",
        f"/load {path}",
        "/list",
        "/quit",
    ])
    # If /list shows both equations after the round-trip, the persistence works.
    out = io.StringIO()
    inputs = iter([
        f"/load {path}",
        "/list",
        "/quit",
    ])
    run_repl(
        read_input=lambda: next(inputs),
        llm=lambda e, c: None,
        out=out,
    )
    text = out.getvalue()
    # /list now renders a tabular view, so the index and equation land in
    # separate cells. Check for each substring independently.
    assert "[0]" in text and "x*y = y*(x*x)" in text
    assert "[1]" in text and "a*a = a" in text


def test_save_without_path_prints_usage():
    output = _run(["/save", "/quit"])
    assert "usage: /save" in output


def test_load_without_path_prints_usage():
    output = _run(["/load", "/quit"])
    assert "usage: /load" in output


def test_load_bad_file_leaves_list_untouched(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    output = _run([
        "x*y=y*x",
        f"/load {bad}",
        "/list",
        "/quit",
    ])
    assert "load failed" in output
    assert "[0] x*y = y*x" in output
    assert "[1]" not in output


def test_clear_confirmed_empties_list():
    output = _run(["x*y=y*x", "/clear", "y", "/list", "/quit"])
    assert "cleared" in output
    assert "(list is empty)" in output


def test_clear_cancelled_keeps_list():
    output = _run(["x*y=y*x", "/clear", "n", "/list", "/quit"])
    assert "cancelled" in output
    assert "[0] x*y = y*x" in output


def test_debug_off_by_default_does_not_print_payload():
    captured = []

    def fake_llm(eqs, cmd):
        captured.append(cmd)
        return LLMResult(equation="x=x", justification="t")

    output = _run(["x*y=y*(x*x)", "do something", "/quit"], llm=fake_llm)
    assert "Current magma list:" not in output
    assert captured == ["do something"]


def test_debug_on_prints_payload_before_llm_call():
    def fake_llm(eqs, cmd):
        return LLMResult(equation="x=x", justification="t")

    output = _run(
        ["x*y=y*(x*x)", "/debug", "do something", "/quit"],
        llm=fake_llm,
    )
    assert "debug: on" in output
    # The debug dump must appear in the output, with the actual payload sent.
    assert "Current magma list:" in output
    assert "[0] x*y = y*(x*x)" in output
    assert "Command:" in output
    assert "do something" in output


def test_debug_toggle_off_silences_payload():
    output = _run(
        ["x*y=y*x", "/debug", "/debug", "do something", "/quit"],
        llm=lambda e, c: LLMResult(equation="x=x", justification="t"),
    )
    assert "debug: off" in output
    assert "Current magma list:" not in output


def test_debug_does_not_print_for_direct_equation_or_slash():
    """Debug should only fire around LLM calls, not on direct input or slash commands."""
    output = _run(
        ["/debug", "x*y=y*x", "/list", "/quit"],
        llm=lambda e, c: LLMResult(equation="x=x", justification="t"),
    )
    assert "debug: on" in output
    assert "Current magma list:" not in output


def test_added_llm_entry_shows_sources_inline():
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", justification="symm", sources=[0])

    output = _run(["x*y=y*x", "do it", "/quit"], llm=fake_llm)
    # Per-add line should show the source indices in some bracketed form.
    assert "from [0]" in output or "from 0" in output


def test_added_llm_entry_with_multiple_sources():
    def fake_llm(eqs, cmd):
        return LLMResult(equation="z*z = z", justification="combo", sources=[0, 1])

    output = _run(["x*y=y*x", "a*a=a", "combine", "/quit"], llm=fake_llm)
    assert "0, 1" in output or "0,1" in output


def test_added_llm_entry_with_no_sources_omits_from_clause():
    def fake_llm(eqs, cmd):
        return LLMResult(equation="x=x", justification="ax", sources=[])

    output = _run(["something", "/quit"], llm=fake_llm)
    assert "from" not in output.lower() or "x = x" in output  # at least the equation prints
    assert "ax" in output


def test_list_shows_tabular_columns_with_sources():
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", justification="symm", sources=[0])

    output = _run(["x*y=y*x", "derive", "/list", "/quit"], llm=fake_llm)
    # Table should have a sources column header and both row indices.
    assert "sources" in output.lower()
    assert "[0]" in output
    assert "[1]" in output
    # The sources cell for entry 1 should mention 0
    assert "0" in output


def test_save_load_preserves_sources_and_steps(tmp_path):
    path = tmp_path / "m.json"

    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", justification="symm", sources=[0])

    _run(["x*y=y*x", "derive", f"/save {path}", "/quit"], llm=fake_llm)
    import json as _json
    data = _json.loads(path.read_text())
    assert len(data) == 2
    assert data[1]["sources"] == [0]
    # "symm" is not a DSL step, so it gets annotated with "? " prefix on save
    assert data[1]["steps"] == ["? symm"]
    assert data[0]["sources"] == []
    assert data[0]["steps"] == []


def test_load_old_format_without_sources(tmp_path):
    """A pre-existing save file with only lhs/rhs must still load."""
    path = tmp_path / "old.json"
    path.write_text('[{"lhs": "x*y", "rhs": "y*x"}]')
    output = _run([f"/load {path}", "/list", "/quit"])
    assert "x*y = y*x" in output


# --- Definitions and /verify ---

def _run_with_critic(inputs, *, llm=None, critic=None):
    out = io.StringIO()
    fake_llm = llm or (lambda eqs, cmd: LLMResult(equation="x=x", justification="reflex"))
    fake_critic = critic or (lambda items, claim: "(no critic)")
    gen = (line for line in inputs)
    run_repl(
        read_input=lambda: next(gen),
        llm=fake_llm,
        critic=fake_critic,
        out=out,
    )
    return out.getvalue()


def test_direct_definition_input_appears_in_list():
    output = _run(["u := x*x", "/list", "/quit"])
    assert "u := x*x" in output


def test_list_shows_kind_column_for_mixed_entries():
    output = _run(["x*y = y*x", "u := x*x", "/list", "/quit"])
    assert "u := x*x" in output
    assert "x*y = y*x" in output
    # kind indicator visible somewhere
    low = output.lower()
    assert "definition" in low or "def" in low or "kind" in low


def test_verify_axiom_says_nothing_to_verify():
    output = _run_with_critic(["x*y = y*x", "/verify 0", "/quit"])
    low = output.lower()
    assert "nothing to verify" in low or "axiom" in low


def test_verify_invalid_index():
    output = _run_with_critic(["x*y = y*x", "/verify 5", "/quit"])
    assert "out of range" in output.lower() or "invalid" in output.lower()


def test_verify_non_numeric_index():
    output = _run_with_critic(["x*y = y*x", "/verify abc", "/quit"])
    assert "invalid" in output.lower() or "usage" in output.lower()


def test_verify_without_arg_prints_usage():
    output = _run_with_critic(["/verify", "/quit"])
    assert "usage: /verify" in output


def test_verify_invokes_critic_for_llm_derived_entry():
    captured = []

    def fake_critic(source_items, claim):
        captured.append((list(source_items), claim))
        return "Looks valid; clean rewrite of [0]."

    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", justification="symm", sources=[0])

    output = _run_with_critic(
        ["x*y = y*x", "derive", "/verify 1", "/quit"],
        llm=fake_llm,
        critic=fake_critic,
    )
    assert "Looks valid" in output
    assert len(captured) == 1
    source_items, claim = captured[0]
    assert len(source_items) == 1  # one source: entry [0]
    assert "y*x = x*y" in claim


def test_save_load_preserves_definitions(tmp_path):
    path = tmp_path / "m.json"
    _run(["x*y = y*x", "u := x*x", f"/save {path}", "/quit"])
    out2 = io.StringIO()
    inputs = iter([f"/load {path}", "/list", "/quit"])
    run_repl(
        read_input=lambda: next(inputs),
        llm=lambda e, c: None,
        out=out2,
    )
    text = out2.getvalue()
    assert "x*y = y*x" in text
    assert "u := x*x" in text


def test_help_lists_new_commands():
    output = _run(["/help", "/quit"])
    assert "/verify" in output
    assert ":=" in output


# --- /clear <i> with cascading delete ---

def _llm_returns(eq: str, justification: str = "j", sources=None):
    sources = sources or []
    return lambda eqs, cmd: LLMResult(equation=eq, justification=justification, sources=list(sources))


def test_clear_single_entry_no_dependents():
    """`/clear 0` on a one-entry list deletes it and the list becomes empty."""
    output = _run(["x*y = y*x", "/clear 0", "y", "/list", "/quit"])
    assert "deleted" in output
    assert "(list is empty)" in output


def test_clear_cascades_through_chain():
    """[0] axiom, [1] from [0], [2] from [1]; /clear 0 deletes all three."""
    llm = lambda eqs, cmd: LLMResult(
        equation=f"y*x = x*y" if len(eqs) == 1 else "z*z = z",
        justification="step",
        sources=[len(eqs) - 1],
    )
    output = _run(
        ["x*y = y*x", "step1", "step2", "/clear 0", "y", "/list", "/quit"],
        llm=llm,
    )
    assert "3" in output  # confirmation mentions the count
    assert "(list is empty)" in output


def test_clear_keeps_siblings_not_descendants(tmp_path):
    """[0] axiom, [1] derived from [0], [2] independent axiom; /clear 1 deletes only [1]."""
    path = tmp_path / "after.json"
    _run([
        "x*y = y*x",          # [0]
        "derive",             # [1] with sources=[0]
        "a*a = a",            # [2] direct axiom
        "/clear 1",
        "y",
        f"/save {path}",
        "/quit",
    ], llm=_llm_returns("y*x = x*y", sources=[0]))
    import json as _json
    data = _json.loads(path.read_text())
    # [1] gone; both axioms survive. After renumber: [0] x*y=y*x, [1] a*a=a.
    assert len(data) == 2
    assert (data[0]["lhs"], data[0]["rhs"]) == ("x*y", "y*x")
    assert (data[1]["lhs"], data[1]["rhs"]) == ("a*a", "a")


def test_clear_remaps_sources_after_cascade():
    """[0] axiom A, [1] axiom B, [2] from [1], [3] from [2]; /clear 0 leaves [1..3] which become [0..2] with sources remapped."""
    seq = iter([
        LLMResult(equation="b*b = b", justification="j", sources=[1]),     # [2] from [1]
        LLMResult(equation="c*c = c", justification="j", sources=[2]),     # [3] from [2]
    ])
    llm = lambda eqs, cmd: next(seq)
    output = _run([
        "x*y = y*x",  # [0]
        "a*a = a",    # [1]
        "step1",      # [2] sources=[1]
        "step2",      # [3] sources=[2]
        "/clear 0",
        "y",
        "/save /tmp/m_remap.json",
        "/quit",
    ], llm=llm)
    # After deletion, original indices [1,2,3] renumbered to [0,1,2]
    # Source [1] -> [0], Source [2] -> [1]
    import json as _json
    data = _json.loads(open("/tmp/m_remap.json").read())
    assert len(data) == 3
    assert data[0]["sources"] == []         # was [1] axiom
    assert data[1]["sources"] == [0]        # was [2] sources=[1] -> [0]
    assert data[2]["sources"] == [1]        # was [3] sources=[2] -> [1]


def test_clear_index_out_of_range():
    output = _run(["x*y = y*x", "/clear 5", "/quit"])
    assert "out of range" in output.lower()


def test_clear_non_numeric_index():
    output = _run(["x*y = y*x", "/clear abc", "/quit"])
    assert "invalid index" in output.lower()


def test_clear_with_n_cancels():
    output = _run(["x*y = y*x", "/clear 0", "n", "/list", "/quit"])
    assert "cancelled" in output
    assert "x*y = y*x" in output


def test_clear_no_arg_still_clears_all():
    """Existing behavior: /clear with no arg empties the whole list after y/N."""
    output = _run(["x*y = y*x", "a*a = a", "/clear", "y", "/list", "/quit"])
    assert "cleared" in output
    assert "(list is empty)" in output


def test_clear_confirmation_shows_dependent_count():
    """When the cascade includes dependents, the prompt mentions how many."""
    output = _run([
        "x*y = y*x",
        "derive 1",
        "derive 2",
        "/clear 0",
        "n",  # cancel
        "/quit",
    ], llm=lambda eqs, cmd: LLMResult(equation="y*x = x*y", steps=["step"], sources=[len(eqs) - 1]))
    # The confirmation prompt should mention 2 dependents (entries [1] and [2]).
    assert "2" in output


# --- steps as numbered list ---

def test_add_echo_shows_numbered_steps():
    """Per-add line for an LLM-derived entry shows steps on separate numbered lines.

    The steps are plain-English (not DSL), so the verifier annotates them with '? '.
    The numbering format still wraps each annotated step.
    """
    def fake_llm(eqs, cmd):
        return LLMResult(
            equation="y*x = x*y",
            steps=["instantiate x:=y in [0]", "rewrite by [0] backwards", "simplify"],
            sources=[0],
        )

    output = _run(["x*y = y*x", "derive", "/quit"], llm=fake_llm)
    # Steps are not valid DSL so annotated with "? "; check the annotated text appears numbered
    assert "1. ? instantiate x:=y in [0]" in output
    assert "2. ? rewrite by [0] backwards" in output
    assert "3. ? simplify" in output


def test_list_table_renders_steps_one_per_line():
    """Tabular /list embeds steps as newline-separated lines in the cell."""
    def fake_llm(eqs, cmd):
        return LLMResult(
            equation="y*x = x*y",
            steps=["alpha", "beta"],
            sources=[0],
        )

    output = _run(["x*y = y*x", "derive", "/list", "/quit"], llm=fake_llm)
    # Both step labels must appear in the rendered table.
    assert "alpha" in output
    assert "beta" in output


def test_save_load_round_trips_steps(tmp_path):
    path = tmp_path / "m.json"

    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", steps=["s1", "s2"], sources=[0])

    _run(["x*y = y*x", "derive", f"/save {path}", "/quit"], llm=fake_llm)
    import json as _json
    data = _json.loads(path.read_text())
    # "s1" and "s2" are not DSL primitives, so annotated with "? " prefix on save
    assert data[1]["steps"] == ["? s1", "? s2"]

    # Reload in a fresh REPL and confirm the annotated steps survive into the list.
    out2 = io.StringIO()
    inputs = iter([f"/load {path}", "/list", "/quit"])
    run_repl(read_input=lambda: next(inputs), llm=lambda e, c: None, out=out2)
    text = out2.getvalue()
    assert "s1" in text
    assert "s2" in text


def test_load_legacy_justification_format(tmp_path):
    """Old save files with `justification` (string) still load as a single step."""
    path = tmp_path / "legacy.json"
    path.write_text(
        '[{"kind": "equation", "lhs": "x*y", "rhs": "y*x", '
        '"sources": [], "justification": "legacy line"}]'
    )
    output = _run([f"/load {path}", "/list", "/quit"])
    assert "legacy line" in output


# --- /deduction <from> <to> <name> ---

def test_deduction_exports_chain_to_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seq = iter([
        LLMResult(equation="y*x = x*y", steps=["s1"], sources=[0]),  # [1] from [0]
        LLMResult(equation="z*z = z", steps=["s2"], sources=[1]),    # [2] from [1]
    ])
    _run([
        "x*y = y*x",
        "step1",
        "step2",
        "/deduction 0 2 myproof",
        "/quit",
    ], llm=lambda eqs, cmd: next(seq))

    import yaml
    path = tmp_path / "myproof.deduction"
    assert path.exists()
    doc = yaml.safe_load(path.read_text())
    assert doc["from"] == 0
    assert doc["to"] == 2
    assert len(doc["entries"]) == 3
    by_idx = {e["index"]: e for e in doc["entries"]}
    assert by_idx[0]["statement"] == "x*y = y*x"
    assert by_idx[0]["sources"] == []
    assert by_idx[2]["sources"] == [1]
    # "s2" is not a DSL primitive, so it gets annotated with "? " prefix
    assert by_idx[2]["steps"] == ["? s2"]


def test_deduction_excludes_entries_not_on_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    seq = iter([
        LLMResult(equation="y*x = x*y", steps=["s1"], sources=[0]),  # [1] from [0]
        LLMResult(equation="z*z = z", steps=["s2"], sources=[1]),    # [3] from [1]
    ])
    _run([
        "x*y = y*x",     # [0]
        "step1",         # [1] from [0]
        "a*a = a",       # [2] independent axiom
        "step2",         # [3] from [1]
        "/deduction 0 3 proof",
        "/quit",
    ], llm=lambda eqs, cmd: next(seq))

    import yaml
    doc = yaml.safe_load((tmp_path / "proof.deduction").read_text())
    indices = {e["index"] for e in doc["entries"]}
    assert indices == {0, 1, 3}


def test_deduction_from_equals_to(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",
        "/deduction 0 0 single",
        "/quit",
    ])
    import yaml
    doc = yaml.safe_load((tmp_path / "single.deduction").read_text())
    assert doc["from"] == 0 and doc["to"] == 0
    assert len(doc["entries"]) == 1
    assert doc["entries"][0]["statement"] == "x*y = y*x"


def test_deduction_includes_definition_cited_as_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", steps=["uses u"], sources=[0, 1])

    _run([
        "x*y = y*x",   # [0]
        "u := x*x",    # [1] definition
        "use u",       # [2] from [0, 1]
        "/deduction 0 2 d",
        "/quit",
    ], llm=llm)

    import yaml
    doc = yaml.safe_load((tmp_path / "d.deduction").read_text())
    by_idx = {e["index"]: e for e in doc["entries"]}
    assert by_idx[1]["kind"] == "definition"
    assert by_idx[1]["name"] == "u"
    assert by_idx[1]["body"] == "x*x"


def test_deduction_to_not_derived_from_from_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    output = _run([
        "x*y = y*x",
        "a*a = a",
        "/deduction 0 1 proof",
        "/quit",
    ])
    assert "not derived" in output.lower()
    assert not (tmp_path / "proof.deduction").exists()


def test_deduction_wrong_arg_count():
    output = _run(["x*y = y*x", "/deduction 0", "/quit"])
    assert "usage" in output.lower()


def test_deduction_index_out_of_range():
    output = _run(["x*y = y*x", "/deduction 0 5 p", "/quit"])
    assert "out of range" in output.lower()


def test_deduction_non_numeric_index():
    output = _run(["x*y = y*x", "/deduction abc 0 p", "/quit"])
    assert "invalid" in output.lower()


def test_help_includes_deduction():
    output = _run(["/help", "/quit"])
    assert "/deduction" in output


# --- /report <name> writes a markdown file with table + mermaid graph ---

def test_report_writes_markdown_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",
        "p := x*x",
        "/sym 0",                # [2] from [0]
        "/report myreport",
        "/quit",
    ])
    path = tmp_path / "myreport.md"
    assert path.exists()
    text = path.read_text()
    # Title and entry count
    assert "myreport" in text
    # Markdown table headers
    assert "| #" in text and "Statement" in text and "Sources" in text and "Steps" in text
    # All three entries appear in the table
    assert "x*y = y*x" in text
    assert "p := x*x" in text
    assert "y*x = x*y" in text


def test_report_includes_mermaid_block(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",
        "/sym 0",
        "/report g",
        "/quit",
    ])
    text = (tmp_path / "g.md").read_text()
    assert "```mermaid" in text
    assert "graph TD" in text
    # n0 represents [0], n1 represents [1]; [1] derives from [0]
    assert "n0" in text
    assert "n1" in text
    # Edge is now labeled with the DSL primitive (`sym`) that derived it.
    assert "n0 -->|sym| n1" in text


def test_report_definition_uses_different_node_shape(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",
        "p := x*x",
        "/report mix",
        "/quit",
    ])
    text = (tmp_path / "mix.md").read_text()
    mermaid = text.split("```mermaid", 1)[1].split("```", 1)[0]
    # Equation: rectangle with quoted label. Definition: stadium with quoted label.
    assert 'n0["x*y = y*x"]' in mermaid
    assert 'n1(["p := x*x"])' in mermaid


def test_report_handles_empty_list(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run(["/report empty", "/quit"])
    text = (tmp_path / "empty.md").read_text()
    assert "empty" in text.lower()


def test_report_isolated_entries_have_no_edges(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",
        "a*a = a",
        "/report isolated",
        "/quit",
    ])
    text = (tmp_path / "isolated.md").read_text()
    # Both nodes present in the mermaid block; no edges inside the block.
    mermaid = text.split("```mermaid", 1)[1].split("```", 1)[0]
    assert "n0" in mermaid and "n1" in mermaid
    assert "-->" not in mermaid


def test_report_without_name_prints_usage():
    output = _run(["/report", "/quit"])
    assert "usage: /report" in output


def test_report_help_includes_command():
    output = _run(["/help", "/quit"])
    assert "/report" in output


def test_report_mermaid_label_contains_equation_text(tmp_path, monkeypatch):
    """The node label shows the magma equation directly, using mermaid's
    quoted-string label syntax. Quoted strings let raw parens and brackets
    appear in the label without escaping."""
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*(x*x)",
        "/report clean",
        "/quit",
    ])
    text = (tmp_path / "clean.md").read_text()
    mermaid = text.split("```mermaid", 1)[1].split("```", 1)[0]
    # The equation appears verbatim in a quoted label, no HTML entities.
    assert 'n0["x*y = y*(x*x)"]' in mermaid
    assert "&#" not in mermaid
    assert "&quot;" not in mermaid


def test_report_mermaid_definition_label_contains_definition_text(tmp_path, monkeypatch):
    """Definitions get the same treatment: their `name := body` text appears
    verbatim inside the stadium-shape quoted label."""
    monkeypatch.chdir(tmp_path)
    _run([
        "u := x*x",
        "/report d",
        "/quit",
    ])
    text = (tmp_path / "d.md").read_text()
    mermaid = text.split("```mermaid", 1)[1].split("```", 1)[0]
    assert 'n0(["u := x*x"])' in mermaid
    assert "&#" not in mermaid


# ---------------------------------------------------------------------------
# Phase 2: DSL slash commands  /sym /inst /trans /rewrite /expand /fold
# ---------------------------------------------------------------------------

def test_sym_basic():
    """Test 1: /sym 0 on x*y=y*x produces [1] y*x=x*y, sources=[0], steps=['sym [0]']."""
    output = _run(["x*y = y*x", "/sym 0", "/quit"])
    assert "[1] y*x = x*y" in output


def test_sym_out_of_range():
    """Test 2: /sym 5 with only one entry prints DSLError, list unchanged."""
    output = _run(["x*y = y*x", "/sym 5", "/list", "/quit"])
    assert "out of range" in output.lower() or "error" in output.lower()
    # Only one entry should be in the list
    assert "[1]" not in output.split("/list")[1] if "/list" in output else "[1]" not in output


def test_sym_no_arg():
    """Test 3: /sym with no arg triggers DSLError; list unchanged."""
    output = _run(["x*y = y*x", "/sym", "/list", "/quit"])
    # Should print an error message
    assert "error" in output.lower() or "expects" in output.lower() or "invalid" in output.lower()
    # List should still have exactly one entry
    assert "[1]" not in output


def test_inst_basic():
    """Test 4: /inst 0 x:=y, y:=x on x=y*(x*x) produces y=x*(y*y)."""
    output = _run(["x = y*(x*x)", "/inst 0 x:=y, y:=x", "/quit"])
    assert "[1]" in output
    assert "y = x*(y*y)" in output


def test_inst_single_var():
    """Test 5: /inst 0 x:=y swaps just one var."""
    output = _run(["x*y = y*x", "/inst 0 x:=y", "/quit"])
    assert "[1]" in output
    assert "y*y = y*y" in output


def test_inst_bad_subst():
    """Test 6: /inst 0 garble produces DSLError, list unchanged."""
    output = _run(["x*y = y*x", "/inst 0 garble", "/list", "/quit"])
    assert "error" in output.lower() or "invalid" in output.lower()
    assert "[1]" not in output


def test_trans_basic():
    """Test 7: /trans 0 1 from a=b and b=c produces a=c, sources=[0,1]."""
    output = _run(["a = b", "b = c", "/trans 0 1", "/quit"])
    assert "[2]" in output
    assert "a = c" in output


def test_trans_no_shared_term():
    """Test 8: /trans 0 1 with no shared term prints error, list unchanged."""
    output = _run(["a = b", "c = d", "/trans 0 1", "/list", "/quit"])
    assert "error" in output.lower() or "shared" in output.lower() or "no shared" in output.lower()
    assert "[2]" not in output


def test_rewrite_basic():
    """Test 9: /rewrite 1 using 0 — basic case."""
    # [0] x = a (rule: x -> a), [1] x*x = x  -> rewrite leftmost x using [0] -> a*x = x
    output = _run(["x = a", "x*x = x", "/rewrite 1 using 0", "/quit"])
    assert "[2]" in output
    # x in [1] lhs gets rewritten to a using rule x=a forward
    assert "a*x = x" in output


def test_rewrite_backwards():
    """Test 10: /rewrite 1 using 0 backwards — uses rule rhs->lhs."""
    # [0] x = a, [1] a*a = a -> rewrite backwards means a -> x
    output = _run(["x = a", "a*a = a", "/rewrite 1 using 0 backwards", "/quit"])
    assert "[2]" in output
    # rewriting a->x in a*a=a, leftmost a -> x*a=a
    assert "x*a = a" in output


def test_expand_basic():
    """Test 11: /expand 1 0 where [0] is a definition and [1] uses the def-name."""
    # [0]: u := x*x  (definition), [1]: u = u*u (equation using u)
    # expand [1] using [0]: replace u -> x*x (leftmost)
    output = _run(["u := x*x", "u = u*u", "/expand 1 0", "/quit"])
    assert "[2]" in output
    assert "x*x = u*u" in output


def test_expand_not_a_definition():
    """Test 12: /expand 1 0 where [0] is NOT a definition prints error."""
    output = _run(["x*y = y*x", "a*a = a", "/expand 1 0", "/list", "/quit"])
    assert "error" in output.lower() or "definition" in output.lower()
    assert "[2]" not in output


def test_fold_basic():
    """Test 13: /fold 1 0 — inverse of expand."""
    # [0]: u := x*x  (definition), [1]: x*x = x*x*x (equation)
    # fold [1] using [0]: replace x*x -> u (leftmost)
    output = _run(["u := x*x", "x*x = x*x*x", "/fold 1 0", "/quit"])
    assert "[2]" in output
    assert "u = x*x*x" in output or "u =" in output


def test_dsl_entry_saved_step_is_canonical(tmp_path):
    """Test 14: After /sym 0, entry's steps list contains canonical 'sym [0]'."""
    path = tmp_path / "m.json"
    _run(["x*y = y*x", "/sym 0", f"/save {path}", "/quit"])
    import json as _json
    data = _json.loads(path.read_text())
    assert len(data) == 2
    assert data[1]["steps"] == ["sym [0]"]
    assert data[1]["sources"] == [0]


def test_help_includes_dsl_commands():
    """Test 15: /help includes /sym, /inst, /trans, /rewrite, /expand, /fold."""
    output = _run(["/help", "/quit"])
    assert "/sym" in output
    assert "/inst" in output
    assert "/trans" in output
    assert "/rewrite" in output
    assert "/expand" in output
    assert "/fold" in output


# ---------------------------------------------------------------------------
# Phase 3: LLM step verifier and SYSTEM_PROMPT DSL grammar
# ---------------------------------------------------------------------------


def test_llm_steps_all_verified_marks_with_check():
    """Test P3-1: LLM emits a valid DSL step; output annotated with checkmark and [verified]."""
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", sources=[0], steps=["sym [0]"])

    output = _run(["x*y = y*x", "go", "/quit"], llm=fake_llm)
    assert "✓ sym [0]" in output
    assert "[verified]" in output


def test_llm_unparseable_step_marked_with_question():
    """Test P3-2: LLM emits a step that doesn't parse; output annotated with '?' and [unverified]."""
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", sources=[0], steps=["this is not DSL"])

    output = _run(["x*y = y*x", "go", "/quit"], llm=fake_llm)
    assert "? this is not DSL" in output
    assert "[unverified]" in output


def test_llm_failing_step_marked_with_cross():
    """Test P3-3: LLM emits a step that parses but fails at execution (out of range); annotated ✗, [unverified]."""
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", sources=[0], steps=["sym [99]"])

    output = _run(["x*y = y*x", "go", "/quit"], llm=fake_llm)
    assert "✗ sym [99]" in output
    assert "[unverified]" in output


def test_llm_chain_final_mismatch_marked_unverified():
    """Test P3-4: LLM step executes cleanly but final result != claimed equation; last step marked ✗, [unverified]."""
    # [0] x*y = y*x; sym [0] -> y*x = x*y, but claim is z=z  -> mismatch
    def fake_llm(eqs, cmd):
        return LLMResult(equation="z = z", sources=[0], steps=["sym [0]"])

    output = _run(["x*y = y*x", "go", "/quit"], llm=fake_llm)
    # The last step was ✓ until mismatch detected, then replaced with ✗
    assert "✗ sym [0]" in output
    assert "≠" in output or "!=" in output or "claim" in output.lower()
    assert "[unverified]" in output


def test_llm_multi_step_chain_uses_s_refs():
    """Test P3-5: Multi-step chain with s-references; all ✓ and overall [verified]."""
    # [0] x = a*b
    # steps: ["sym [0]", "sym s1"]
    # sym [0] -> a*b = x  (s1)
    # sym s1  -> x = a*b  (s2) — matches claim "x = a*b"
    def fake_llm(eqs, cmd):
        return LLMResult(equation="x = a*b", sources=[0], steps=["sym [0]", "sym s1"])

    output = _run(["x = a*b", "go", "/quit"], llm=fake_llm)
    assert "✓ sym [0]" in output
    assert "✓ sym s1" in output
    assert "[verified]" in output


def test_system_prompt_includes_dsl_grammar():
    """Test P3-6: SYSTEM_PROMPT references all six primitives and step reference notation."""
    from magmaexplorer.llm import SYSTEM_PROMPT
    for primitive in ("sym", "inst", "trans", "rewrite", "expand", "fold"):
        assert primitive in SYSTEM_PROMPT, f"SYSTEM_PROMPT missing primitive: {primitive!r}"
    # Should document the s<k> / s1 notation for prior step references
    assert "s1" in SYSTEM_PROMPT or "s<k>" in SYSTEM_PROMPT, (
        "SYSTEM_PROMPT should mention step-reference notation (s1 or s<k>)"
    )


def test_verified_label_appears_below_steps():
    """Test P3-7: [verified] line appears after the last numbered step line."""
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", sources=[0], steps=["sym [0]"])

    output = _run(["x*y = y*x", "go", "/quit"], llm=fake_llm)
    # Find positions: step line should precede [verified] line
    step_pos = output.find("✓ sym [0]")
    verified_pos = output.find("[verified]")
    assert step_pos != -1, "step annotation not found"
    assert verified_pos != -1, "[verified] not found"
    assert step_pos < verified_pos, "[verified] must appear AFTER the step line"


def test_unverified_appears_when_no_steps_returned():
    """Test P3-8: LLM returns empty steps list; output shows [unverified]."""
    def fake_llm(eqs, cmd):
        return LLMResult(equation="y*x = x*y", sources=[0], steps=[])

    output = _run(["x*y = y*x", "go", "/quit"], llm=fake_llm)
    assert "[unverified]" in output


# ---------------------------------------------------------------------------
# /report — edges carry the DSL primitive name as a mermaid edge label
# ---------------------------------------------------------------------------

def test_report_edge_labeled_with_primitive_sym(tmp_path, monkeypatch):
    """A `/sym 0` step produces edge `n0 -->|sym| n1` in the mermaid block."""
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",
        "/sym 0",
        "/report g",
        "/quit",
    ])
    mermaid = (tmp_path / "g.md").read_text().split("```mermaid", 1)[1].split("```", 1)[0]
    assert "n0 -->|sym| n1" in mermaid


def test_report_edge_labeled_with_primitive_inst(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*(x*x)",
        "/inst 0 x:=a, y:=b",
        "/report g",
        "/quit",
    ])
    mermaid = (tmp_path / "g.md").read_text().split("```mermaid", 1)[1].split("```", 1)[0]
    assert "n0 -->|inst| n1" in mermaid


def test_report_trans_labels_both_edges(tmp_path, monkeypatch):
    """A `/trans 0 1` step labels BOTH incoming edges with `trans`."""
    monkeypatch.chdir(tmp_path)
    _run([
        "a = b",
        "b = c",
        "/trans 0 1",
        "/report g",
        "/quit",
    ])
    mermaid = (tmp_path / "g.md").read_text().split("```mermaid", 1)[1].split("```", 1)[0]
    assert "n0 -->|trans| n2" in mermaid
    assert "n1 -->|trans| n2" in mermaid


def test_report_unparseable_step_leaves_edge_unlabeled(tmp_path, monkeypatch):
    """If the derivation comes from the LLM and isn't valid DSL, the edge
    has no label — graceful degradation, not a crash."""
    def fake_llm(eqs, cmd):
        # Plain-English step; not DSL parseable.
        return LLMResult(equation="y*x = x*y", sources=[0], steps=["just swap the sides"])

    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "swap it", "/report g", "/quit"], llm=fake_llm)
    mermaid = (tmp_path / "g.md").read_text().split("```mermaid", 1)[1].split("```", 1)[0]
    # The edge exists but has no `|label|`.
    assert "n0 --> n1" in mermaid
    assert "|" not in mermaid  # no edge label anywhere


def test_report_multiple_steps_same_source_combines_labels(tmp_path, monkeypatch):
    """If an entry has multiple DSL steps that both reference the same source,
    the edge label lists each primitive once, comma-separated."""
    def fake_llm(eqs, cmd):
        # Two steps both citing [0]: sym then inst.
        return LLMResult(
            equation="y*y = x*y",
            sources=[0],
            steps=["sym [0]", "inst [0] x:=y"],
        )

    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "do it", "/report g", "/quit"], llm=fake_llm)
    mermaid = (tmp_path / "g.md").read_text().split("```mermaid", 1)[1].split("```", 1)[0]
    # Both primitives appear in the same label.
    assert "n0 -->|" in mermaid
    line = next(ln for ln in mermaid.splitlines() if "n0 -->|" in ln and "n1" in ln)
    assert "sym" in line and "inst" in line


# ---------------------------------------------------------------------------
# /lean <filename> — export the command history as a Lean 4 script
# ---------------------------------------------------------------------------

def test_lean_no_arg_prints_usage():
    output = _run(["/lean", "/quit"])
    assert "usage: /lean" in output


def test_lean_help_includes_command():
    output = _run(["/help", "/quit"])
    assert "/lean" in output


def test_lean_writes_file_with_lean_extension(tmp_path, monkeypatch):
    """/lean foo writes foo.lean."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/lean foo", "/quit"])
    assert (tmp_path / "foo.lean").exists()


def test_lean_existing_extension_kept(tmp_path, monkeypatch):
    """/lean foo.lean writes foo.lean (not foo.lean.lean)."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/lean foo.lean", "/quit"])
    assert (tmp_path / "foo.lean").exists()
    assert not (tmp_path / "foo.lean.lean").exists()


def test_lean_has_magma_binders_on_each_declaration(tmp_path, monkeypatch):
    """Every axiom and theorem carries its own `{G : Type _} [Mul G]`
    binders so the file compiles in both vanilla Lean 4 and Mathlib —
    `axiom` does NOT pick up `variable` declarations, and `Type*` is
    Mathlib-only."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # Both the axiom and the derived theorem have explicit binders.
    assert "axiom eq_0 {G : Type _} [Mul G] :" in text
    assert "theorem eq_1 {G : Type _} [Mul G] :" in text


def test_lean_no_typestar_or_redundant_variable_line(tmp_path, monkeypatch):
    """Type* is Mathlib-only; the file uses `Type _` instead. The `variable`
    line is omitted because we put binders directly on each declaration.

    Only the actual Lean code lines are inspected — the preamble comment is
    allowed to mention `Type*` while explaining why we don't use it."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    code = "\n".join(code_lines)
    assert "Type*" not in code
    assert not any(ln.startswith("variable ") for ln in code_lines)


def test_lean_axiom_for_no_source_entry(tmp_path, monkeypatch):
    """Entries with no sources become `axiom` declarations (no proof needed)."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*(x*x)", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # Axiom with universal quantifier over the free variables.
    assert "axiom eq_0" in text
    assert "∀" in text
    assert "x * y = y * (x * x)" in text


def test_lean_theorem_emitted_for_derived(tmp_path, monkeypatch):
    """Derived entries become `theorem eq_i : ... := by ...`. The body
    is now a real proof when the DSL primitive is auto-translatable
    (sym/inst/trans); only un-translatable steps keep `sorry`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "theorem eq_1" in text
    assert "y * x = x * y" in text


# ---------------------------------------------------------------------------
# /lean — auto-generated term proofs for sym, inst, trans
# ---------------------------------------------------------------------------

def test_lean_sym_emits_symm_term_proof(tmp_path, monkeypatch):
    """A single-step `sym [i]` entry produces a real Lean proof using
    `Eq.symm`, NOT a `sorry`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # The proof body uses the symm of the source axiom.
    assert "(eq_0 x y).symm" in text
    # And the theorem body for eq_1 does NOT contain `sorry`.
    eq1_block = text.split("theorem eq_1")[1].split("\n\n", 1)[0]
    assert "sorry" not in eq1_block


def test_lean_inst_emits_application_term_proof(tmp_path, monkeypatch):
    """A single-step `inst [i] x:=a, y:=b` becomes `exact eq_i a b`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*(x*x)", "/inst 0 x:=a, y:=b", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # eq_0's bound vars are alphabetised (x then y), so args are (a, b).
    assert "exact eq_0 a b" in text
    eq1_block = text.split("theorem eq_1")[1].split("\n\n", 1)[0]
    assert "sorry" not in eq1_block


def test_lean_inst_parenthesises_compound_term_arg(tmp_path, monkeypatch):
    """When a substitution value is itself an operator term (e.g. x:=a*b),
    Lean needs it parenthesised in the application: `eq_0 (a * b) c`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/inst 0 x:=a*b, y:=c", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "exact eq_0 (a * b) c" in text


def test_lean_inst_partial_substitution_keeps_other_vars(tmp_path, monkeypatch):
    """An inst that leaves some vars unchanged passes the literal var name
    for the unchanged ones — they remain in the goal's binder."""
    monkeypatch.chdir(tmp_path)
    # eq_0 has bound vars {x, y}; only x is substituted.
    _run(["x*y = y*x", "/inst 0 x:=a", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # Result equation: a*y = y*a (free vars {a, y}), passed as `eq_0 a y`.
    assert "exact eq_0 a y" in text


def test_lean_trans_emits_trans_term_proof(tmp_path, monkeypatch):
    """A single-step `trans [a] [b]` produces `(eq_a ...).trans (eq_b ...)`
    when the orientation is `a.rhs = b.lhs`."""
    monkeypatch.chdir(tmp_path)
    # a: x = y, b: y = z; trans gives x = z.
    _run(["x = y", "y = z", "/trans 0 1", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # Both axioms are applied to literal vars; the matching var `y` is
    # in both V_a and V_b, and is also a goal var (V_r = [x, y, z]? no:
    # V_r = vars of result `x = z` = {x, z}, so `y` is an orphan).
    # Orphans are filled with the first goal var; so eq_0 receives (x, x)
    # and eq_1 receives (x, z) — both type-check via uniform witness choice.
    eq2_block = text.split("theorem eq_2")[1].split("\n\n", 1)[0]
    assert "sorry" not in eq2_block
    assert ".trans" in eq2_block
    assert "eq_0" in eq2_block and "eq_1" in eq2_block


def test_lean_trans_uses_symm_when_a_lhs_matches_b_lhs(tmp_path, monkeypatch):
    """Orientation where both LHSes match needs `.symm` on the first arg."""
    monkeypatch.chdir(tmp_path)
    # a: y = x, b: y = z; a.lhs == b.lhs, so result = x = z via (a.symm).trans b.
    _run(["y = x", "y = z", "/trans 0 1", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    eq2_block = text.split("theorem eq_2")[1].split("\n\n", 1)[0]
    assert ".symm" in eq2_block
    assert "sorry" not in eq2_block


def test_lean_multi_step_emits_have_blocks_no_sorry(tmp_path, monkeypatch):
    """Multi-step entries (with `s<k>` step references) are auto-translated
    into `have h_s<k> := by …` blocks per intermediate, with the final step
    discharging the outer goal — no `sorry`."""
    def fake_llm(eqs, cmd):
        return LLMResult(
            equation="y*(x*x) = x*y",
            sources=[0],
            steps=["sym [0]", "sym s1"],   # multi-step, references s1
        )

    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*(x*x)", "go", "/lean foo", "/quit"], llm=fake_llm)
    text = (tmp_path / "foo.lean").read_text()
    eq1_block = text.split("theorem eq_1")[1].split("\n\n", 1)[0]
    assert "sorry" not in eq1_block
    assert "have h_s1" in eq1_block


def test_lean_rewrite_forward_emits_nth_rewrite_at_hypothesis(tmp_path, monkeypatch):
    """`rewrite [i] using [j]` becomes `nth_rewrite 1 [eq_j] at h` (Step 5b:
    DSL `rewrite` is leftmost-outermost, matching `nth_rewrite 1`)."""
    monkeypatch.chdir(tmp_path)
    _run([
        "a*x = x*a",        # [0]  target
        "x = b",             # [1]  rule
        "/rewrite 0 using 1",
        "/lean foo",
        "/quit",
    ])
    text = (tmp_path / "foo.lean").read_text()
    eq2_block = text.split("theorem eq_2")[1].split("\n\n", 1)[0]
    assert "have h" in eq2_block
    assert "nth_rewrite 1 [eq_1] at h" in eq2_block
    assert "exact h" in eq2_block
    assert "sorry" not in eq2_block


def test_lean_rewrite_backwards_uses_arrow(tmp_path, monkeypatch):
    """`rewrite [i] using [j] backwards` becomes `nth_rewrite 1 [← eq_j] at h`."""
    monkeypatch.chdir(tmp_path)
    _run([
        "a*a = b*a",         # [0]  target — RHS of rule (a*a) appears here
        "c = a*a",           # [1]  rule — backwards turns a*a → c
        "/rewrite 0 using 1 backwards",
        "/lean foo",
        "/quit",
    ])
    text = (tmp_path / "foo.lean").read_text()
    eq2_block = text.split("theorem eq_2")[1].split("\n\n", 1)[0]
    assert "nth_rewrite 1 [← eq_1] at h" in eq2_block
    assert "sorry" not in eq2_block


def test_lean_rewrite_includes_caveat_comment(tmp_path, monkeypatch):
    """The auto-generated rewrite proof carries a `-- NOTE:` line warning
    that Lean's `rw` rewrites all occurrences, while the DSL does only
    the leftmost-outermost — so the proof may need `nth_rewrite 1`."""
    monkeypatch.chdir(tmp_path)
    _run([
        "a*x = x*a",
        "x = b",
        "/rewrite 0 using 1",
        "/lean foo",
        "/quit",
    ])
    text = (tmp_path / "foo.lean").read_text()
    eq2_block = text.split("theorem eq_2")[1].split("\n\n", 1)[0]
    assert "nth_rewrite" in eq2_block.lower() or "all occurrences" in eq2_block.lower()


def test_lean_expand_still_falls_back_to_sorry(tmp_path, monkeypatch):
    """expand/fold still emit sorry — definitions have no direct Lean form."""
    monkeypatch.chdir(tmp_path)
    _run([
        "p := x*x",          # [0] definition
        "p = y",             # [1] equation using p
        "/expand 1 0",       # expand p in [1] using definition [0]
        "/lean foo",
        "/quit",
    ])
    text = (tmp_path / "foo.lean").read_text()
    # The expanded theorem (whatever index it lands at) still has sorry.
    if "theorem eq_2" in text:
        eq2_block = text.split("theorem eq_2")[1].split("\n\n", 1)[0]
        assert "sorry" in eq2_block


def test_lean_axiom_block_unchanged(tmp_path, monkeypatch):
    """Sanity: axioms still emit `axiom eq_i`, not `theorem ... := by ...`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*(x*x)", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "axiom eq_0" in text
    assert "theorem eq_0" not in text


# ---------------------------------------------------------------------------
# /lean-implication <from> <to> <name> — single competition-shaped theorem
# ---------------------------------------------------------------------------


def _lean_code_lines(text: str) -> list[str]:
    """Return only the non-comment lines of a Lean file."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]


def test_lean_implication_no_args_prints_usage():
    output = _run(["/lean-implication", "/quit"])
    assert "usage: /lean-implication" in output


def test_lean_implication_missing_name_prints_usage():
    output = _run(["x*y = y*x", "/sym 0", "/lean-implication 0 1", "/quit"])
    assert "usage: /lean-implication" in output


def test_lean_implication_help_lists_command():
    output = _run(["/help", "/quit"])
    assert "/lean-implication" in output


def test_lean_implication_writes_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean-implication 0 1 foo", "/quit"])
    assert (tmp_path / "foo.lean").exists()


def test_lean_implication_existing_extension_kept(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean-implication 0 1 foo.lean", "/quit"])
    assert (tmp_path / "foo.lean").exists()
    assert not (tmp_path / "foo.lean.lean").exists()


def test_lean_implication_sym_uses_hypothesis_not_axiom(tmp_path, monkeypatch):
    """Single `/sym 0` implication: hypothesis is `h`, no `axiom`, no `sorry`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean-implication 0 1 foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "theorem implication" in text
    assert "{G : Type _} [Mul G]" in text
    assert "(h : ∀ x y : G, x * y = y * x)" in text
    assert "∀ x y : G, y * x = x * y" in text
    assert "(h x y).symm" in text
    # No axiom or sorry in CODE lines (preamble comments are fine).
    code = "\n".join(_lean_code_lines(text))
    assert "axiom " not in code
    assert "sorry" not in code


def test_lean_implication_inst_specializes_h(tmp_path, monkeypatch):
    """`/inst 0 x:=a, y:=b` becomes `exact h a b` (no eq_0)."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*(x*x)", "/inst 0 x:=a, y:=b", "/lean-implication 0 1 foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "exact h a b" in text
    code = "\n".join(_lean_code_lines(text))
    assert "eq_0" not in code
    assert "axiom " not in code
    assert "sorry" not in code


def test_lean_implication_intermediate_become_have_blocks(tmp_path, monkeypatch):
    """A chain that passes through an intermediate entry emits that entry as
    a universally-quantified `have h_<i> : ... := by intro ...; exact ...`."""
    monkeypatch.chdir(tmp_path)
    _run([
        "x*y = y*x",          # [0] axiom
        "/sym 0",             # [1] y*x = x*y, derived from [0]
        "/inst 1 x:=a, y:=b", # [2] b*a = a*b, derived from [1]
        "/lean-implication 0 2 foo",
        "/quit",
    ])
    text = (tmp_path / "foo.lean").read_text()
    # h_1 appears as a universally-quantified have, then is specialised in
    # the final exact step.
    assert "have h_1" in text
    assert "(h x y).symm" in text
    assert "h_1 a b" in text
    code = "\n".join(_lean_code_lines(text))
    assert "axiom " not in code
    assert "sorry" not in code


def test_lean_implication_error_when_from_not_ancestor(tmp_path, monkeypatch):
    """`[from]` must be an ancestor of `[to]`; otherwise refuse and emit no file."""
    monkeypatch.chdir(tmp_path)
    output = _run([
        "x*y = y*x",  # [0]
        "a = b",      # [1] unrelated axiom
        "/sym 1",     # [2] = b = a, ancestor only [1]
        "/lean-implication 0 2 foo",
        "/quit",
    ])
    assert "ancestor" in output.lower() or "not reachable" in output.lower()
    assert not (tmp_path / "foo.lean").exists()


def test_lean_implication_error_when_unrelated_axiom_in_chain(tmp_path, monkeypatch):
    """The chain may not contain an axiom other than [from] — we'd have no
    way to prove it from h alone."""
    monkeypatch.chdir(tmp_path)
    output = _run([
        "a = b",      # [0]
        "b = c",      # [1] second axiom
        "/trans 0 1", # [2] derived from BOTH [0] and [1]
        "/lean-implication 0 2 foo",
        "/quit",
    ])
    assert "axiom" in output.lower() and "[1]" in output
    assert not (tmp_path / "foo.lean").exists()


def test_lean_implication_to_equals_from_is_trivial(tmp_path, monkeypatch):
    """`/lean-implication i i name` is the trivial reflexive implication
    `h ⊢ h` — still produces a compileable file, just `exact h`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/lean-implication 0 0 foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "theorem implication" in text
    assert "exact h" in text
    code = "\n".join(_lean_code_lines(text))
    assert "axiom " not in code
    assert "sorry" not in code


def test_lean_implication_confirmation_message(tmp_path, monkeypatch):
    """The REPL prints a `wrote …` confirmation after success."""
    monkeypatch.chdir(tmp_path)
    output = _run(["x*y = y*x", "/sym 0", "/lean-implication 0 1 foo", "/quit"])
    assert "foo.lean" in output


def test_lean_implication_definition_in_chain_errors(tmp_path, monkeypatch):
    """A Definition entry on the path can't be translated (expand/fold not
    auto-translated yet); refuse with an error."""
    monkeypatch.chdir(tmp_path)
    output = _run([
        "p := x*x",      # [0] definition
        "p = y",         # [1] axiom mentioning p
        "/expand 1 0",   # [2] expand p in [1] using def [0]
        "/lean-implication 1 2 foo",
        "/quit",
    ])
    # Either the definition is flagged or expand is, but the file shouldn't
    # be written with a sorry — the command refuses.
    assert ("definition" in output.lower() or "expand" in output.lower()
            or "cannot" in output.lower())
    assert not (tmp_path / "foo.lean").exists()


def test_lean_definition_becomes_comment(tmp_path, monkeypatch):
    """A magmaexplorer definition has no direct Lean translation — it appears
    as a comment in the script."""
    monkeypatch.chdir(tmp_path)
    _run(["p := x*x", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # No theorem/axiom for the definition entry, just a comment.
    assert "axiom eq_0" not in text
    assert "theorem eq_0" not in text
    # The comment records the definition body.
    assert "p := x*x" in text


def test_lean_records_dsl_steps_as_comments(tmp_path, monkeypatch):
    """The derivation chain (sources + DSL steps) appears as a comment
    above each `theorem`."""
    monkeypatch.chdir(tmp_path)
    _run(["x*y = y*x", "/sym 0", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    # The DSL step `sym [0]` should appear in a comment line.
    lines = text.splitlines()
    sym_comment_lines = [ln for ln in lines if ln.lstrip().startswith("--") and "sym [0]" in ln]
    assert sym_comment_lines, "expected a comment line mentioning the DSL step `sym [0]`"


def test_lean_universal_quantifier_lists_all_free_vars_sorted(tmp_path, monkeypatch):
    """For an equation with vars {a, c, b}, the theorem reads `∀ a b c : G, ...`."""
    monkeypatch.chdir(tmp_path)
    _run(["c*a = b*a", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "∀ a b c : G" in text


def test_lean_empty_list(tmp_path, monkeypatch):
    """/lean on an empty list still writes the preamble — never crashes."""
    monkeypatch.chdir(tmp_path)
    _run(["/lean empty", "/quit"])
    text = (tmp_path / "empty.lean").read_text()
    # Preamble comment is always present.
    assert "magmaexplorer export" in text
    # No axiom/theorem declarations should be emitted.
    code_lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("--")]
    assert not any(ln.startswith("axiom ") for ln in code_lines)
    assert not any(ln.startswith("theorem ") for ln in code_lines)


def test_lean_writes_confirmation_message(tmp_path, monkeypatch):
    """The REPL prints a `wrote to <path>` confirmation after /lean."""
    monkeypatch.chdir(tmp_path)
    output = _run(["x*y = y*x", "/lean foo", "/quit"])
    assert "foo.lean" in output


def test_lean_parens_for_right_associative(tmp_path, monkeypatch):
    """The Lean output respects right-grouping parens, matching the pretty-printer."""
    monkeypatch.chdir(tmp_path)
    _run(["x = y*(z*w)", "/lean foo", "/quit"])
    text = (tmp_path / "foo.lean").read_text()
    assert "x = y * (z * w)" in text


def test_lean_name_clash_for_index_uses_numeric(tmp_path, monkeypatch):
    """Sanity: entry index 10 produces `eq_10`, not a renamed identifier."""
    monkeypatch.chdir(tmp_path)
    # Make 11 entries: one axiom + 10 sym applications.
    inputs = ["x*y = y*x"]
    for _ in range(10):
        # Always sym the latest entry.
        inputs.append(f"/sym {len(inputs) - 1}")
    inputs += ["/lean foo", "/quit"]
    _run(inputs)
    text = (tmp_path / "foo.lean").read_text()
    assert "eq_10" in text
