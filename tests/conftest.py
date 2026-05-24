import pytest


class FakeAnthropicClient:
    """Stand-in for anthropic.Anthropic. Records calls; returns canned text."""

    def __init__(
        self,
        response_text: str = '{"equation": "x=x", "justification": "reflexivity"}',
        raise_on_create: Exception | None = None,
    ):
        self.response_text = response_text
        self.raise_on_create = raise_on_create
        self.calls: list[dict] = []
        self.messages = self  # so client.messages.create(...) works

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_create is not None:
            raise self.raise_on_create

        class _Block:
            def __init__(self, text): self.text = text

        class _Response:
            def __init__(self, text): self.content = [_Block(text)]

        return _Response(self.response_text)


@pytest.fixture
def fake_client():
    return FakeAnthropicClient()
