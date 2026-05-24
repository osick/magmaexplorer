import json

import pytest

from magmaexplorer.llm import LLMError, LLMResult, call_llm
from magmaexplorer.term import Equation, Op, Var, parse_equation


@pytest.fixture
def two_equations():
    return [
        parse_equation("x*y = y*(x*x)"),
        parse_equation("a*a = a"),
    ]


def test_call_llm_sends_numbered_list(fake_client, two_equations):
    call_llm(two_equations, "apply y=x*x to 0", client=fake_client)
    assert len(fake_client.calls) == 1
    sent = fake_client.calls[0]
    user_text = sent["messages"][0]["content"]
    assert "[0] x*y = y*(x*x)" in user_text
    assert "[1] a*a = a" in user_text
    assert "apply y=x*x to 0" in user_text


def test_call_llm_includes_system_prompt(fake_client):
    call_llm([], "noop", client=fake_client)
    sent = fake_client.calls[0]
    assert "system" in sent
    assert "*" in sent["system"]  # mentions the operator
    assert "JSON" in sent["system"] or "json" in sent["system"]


def test_call_llm_uses_specified_model(fake_client):
    call_llm([], "noop", client=fake_client, model="claude-sonnet-4-6")
    assert fake_client.calls[0]["model"] == "claude-sonnet-4-6"


def test_call_llm_default_model(fake_client):
    call_llm([], "noop", client=fake_client)
    assert fake_client.calls[0]["model"] == "claude-opus-4-7"


from tests.conftest import FakeAnthropicClient


def test_call_llm_parses_valid_json():
    client = FakeAnthropicClient(
        response_text='{"equation": "x*y = y*x", "justification": "commutativity"}'
    )
    result = call_llm([], "commute", client=client)
    assert isinstance(result, LLMResult)
    assert result.equation == "x*y = y*x"
    # Legacy `justification` is converted to a single-step list.
    assert result.steps == ["commutativity"]


def test_call_llm_raises_on_non_json():
    client = FakeAnthropicClient(response_text="not json at all")
    with pytest.raises(LLMError, match="not valid JSON"):
        call_llm([], "noop", client=client)


def test_call_llm_raises_on_missing_keys():
    client = FakeAnthropicClient(response_text='{"equation": "x=x"}')
    with pytest.raises(LLMError, match="missing both 'steps' and 'justification'"):
        call_llm([], "noop", client=client)


def test_call_llm_raises_on_non_dict():
    client = FakeAnthropicClient(response_text='["x=x", "just"]')
    with pytest.raises(LLMError, match="missing required key 'equation'"):
        call_llm([], "noop", client=client)


def test_call_llm_wraps_network_error():
    client = FakeAnthropicClient(raise_on_create=ConnectionError("boom"))
    with pytest.raises(LLMError, match="LLM call failed"):
        call_llm([], "noop", client=client)


def test_call_llm_strips_whitespace_around_json():
    client = FakeAnthropicClient(
        response_text='  \n{"equation": "x=x", "justification": "ok"}\n  '
    )
    result = call_llm([], "noop", client=client)
    assert result.equation == "x=x"


def test_call_llm_parses_sources_field():
    client = FakeAnthropicClient(
        response_text='{"equation": "x=x", "justification": "ok", "sources": [0, 1]}'
    )
    result = call_llm([], "noop", client=client)
    assert result.sources == [0, 1]


def test_call_llm_sources_default_empty_when_missing():
    client = FakeAnthropicClient(
        response_text='{"equation": "x=x", "justification": "ok"}'
    )
    result = call_llm([], "noop", client=client)
    assert result.sources == []


def test_call_llm_sources_default_empty_when_not_list():
    client = FakeAnthropicClient(
        response_text='{"equation": "x=x", "justification": "ok", "sources": "not-a-list"}'
    )
    result = call_llm([], "noop", client=client)
    assert result.sources == []


def test_system_prompt_mentions_sources():
    from magmaexplorer.llm import SYSTEM_PROMPT
    assert "sources" in SYSTEM_PROMPT


# --- Definition formatting and critic ---

from magmaexplorer.llm import critique_entry, format_user_message
from magmaexplorer.term import Definition, Op, Var, parse_equation


def test_format_user_message_marks_definitions():
    items = [
        parse_equation("x*y = y*x"),
        Definition(name="u", body=Op(Var("x"), Var("x"))),
    ]
    msg = format_user_message(items, "command")
    assert "[0] x*y = y*x" in msg
    assert "u := x*x" in msg
    # The marker that distinguishes a definition from an equation must appear.
    assert "definition" in msg.lower() or "NOT a magma equation" in msg


def test_system_prompt_forbids_unfounded_laws():
    from magmaexplorer.llm import SYSTEM_PROMPT
    low = SYSTEM_PROMPT.lower()
    assert "cancel" in low
    assert "associat" in low
    assert "commut" in low


def test_system_prompt_explains_definition_distinction():
    from magmaexplorer.llm import SYSTEM_PROMPT
    assert ":=" in SYSTEM_PROMPT
    assert "definition" in SYSTEM_PROMPT.lower()


def test_critique_entry_sends_sources_and_claim():
    client = FakeAnthropicClient(response_text="Looks valid.")
    result = critique_entry(
        source_items=[parse_equation("x*y = y*x")],
        claim_text="y*x = x*y",
        client=client,
    )
    assert "Looks valid" in result
    sent = client.calls[0]
    body = sent["messages"][0]["content"]
    assert "[0] x*y = y*x" in body
    assert "y*x = x*y" in body


def test_critique_entry_returns_critic_text_verbatim():
    client = FakeAnthropicClient(response_text="Step 3 silently uses cancellation, invalid.")
    result = critique_entry(
        source_items=[parse_equation("x*y = y*(x*x)")],
        claim_text="x*x = x",
        client=client,
    )
    assert result == "Step 3 silently uses cancellation, invalid."


def test_critique_entry_wraps_network_error():
    client = FakeAnthropicClient(raise_on_create=ConnectionError("offline"))
    with pytest.raises(LLMError, match="critique call failed"):
        critique_entry(source_items=[], claim_text="x = x", client=client)


# --- steps as a list ---

def test_llm_parses_steps_field():
    client = FakeAnthropicClient(
        response_text=json.dumps({
            "equation": "x = x",
            "steps": ["step A", "step B", "step C"],
            "sources": [],
        })
    )
    result = call_llm([], "noop", client=client)
    assert result.steps == ["step A", "step B", "step C"]


def test_llm_falls_back_to_justification_as_single_step():
    """Backward compat: if the response uses the old `justification` field, wrap it."""
    client = FakeAnthropicClient(
        response_text=json.dumps({
            "equation": "x = x",
            "justification": "single line of reasoning",
            "sources": [],
        })
    )
    result = call_llm([], "noop", client=client)
    assert result.steps == ["single line of reasoning"]


def test_llm_raises_when_neither_steps_nor_justification():
    client = FakeAnthropicClient(
        response_text=json.dumps({"equation": "x = x", "sources": []})
    )
    with pytest.raises(LLMError, match="missing"):
        call_llm([], "noop", client=client)


def test_llm_ignores_non_list_steps():
    """If steps comes back as a string by mistake, treat it as a single step."""
    client = FakeAnthropicClient(
        response_text=json.dumps({
            "equation": "x = x",
            "steps": "single-string instead of list",
            "sources": [],
        })
    )
    result = call_llm([], "noop", client=client)
    assert result.steps == ["single-string instead of list"]


def test_system_prompt_asks_for_steps_list():
    from magmaexplorer.llm import SYSTEM_PROMPT
    assert "steps" in SYSTEM_PROMPT.lower()
