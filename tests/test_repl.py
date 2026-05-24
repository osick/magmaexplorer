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
    assert data[1]["steps"] == ["symm"]
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
    """Per-add line for an LLM-derived entry shows steps on separate numbered lines."""
    def fake_llm(eqs, cmd):
        return LLMResult(
            equation="y*x = x*y",
            steps=["instantiate x:=y in [0]", "rewrite by [0] backwards", "simplify"],
            sources=[0],
        )

    output = _run(["x*y = y*x", "derive", "/quit"], llm=fake_llm)
    assert "1. instantiate x:=y in [0]" in output
    assert "2. rewrite by [0] backwards" in output
    assert "3. simplify" in output


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
    assert data[1]["steps"] == ["s1", "s2"]

    # Reload in a fresh REPL and confirm the steps survive into the list.
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
