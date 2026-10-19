from types import SimpleNamespace

import pytest

from copilot import mcp_server
from copilot.data import seed


@pytest.fixture(autouse=True)
def demo_db(tmp_path, monkeypatch):
    """Every test gets a fresh copy of the demo database."""
    path = seed(tmp_path / "backoffice.db")
    monkeypatch.setattr(mcp_server, "DB_PATH", path)
    return path


def text(t):
    return SimpleNamespace(type="text", text=t)


def tool_use(id, name, input):
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def reply(content, stop_reason=None):
    stop = stop_reason or ("tool_use" if any(b.type == "tool_use" for b in content) else "end_turn")
    usage = SimpleNamespace(input_tokens=1200, output_tokens=80, cache_read_input_tokens=900)
    return SimpleNamespace(content=content, stop_reason=stop, usage=usage)


class FakeClaude:
    """Replays scripted responses and records each request (with a snapshot of the messages)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)
