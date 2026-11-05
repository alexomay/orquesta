import json

import pytest

from app.agents.orchestrator import ApprovalBroker, Decision
from app.agents.tools import REGISTRY, tool_definitions
from app.llm.anthropic_llm import AnthropicLLM
from app.llm.base import LLMRequest, Usage
from app.pricing import cost_usd


def test_tool_schemas_are_strict():
    defs = tool_definitions()
    assert [d["name"] for d in defs] == sorted(d["name"] for d in defs)
    for d in defs:
        schema = d["input_schema"]
        assert d["strict"] is True
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "title" not in json.dumps(schema)


def test_sensitive_tools_require_approval():
    risky = {name for name, t in REGISTRY.items() if t.requires_approval}
    assert risky == {"reset_password", "unlock_account"}


def test_cost_uses_model_prices():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost_usd("claude-opus-5-5", usage) == pytest.approx(24.0)
    assert cost_usd("claude-sonnet-5-5", usage) == pytest.approx(12.0)
    cached = Usage(cache_read_tokens=1_000_000, cache_write_tokens=1_000_000)
    assert cost_usd("claude-opus-5-5", cached) == pytest.approx(0.20 + 5.0)


async def test_broker_resolves_only_once():
    broker = ApprovalBroker()
    fut = broker.register(1)
    assert broker.resolve(1, Decision(True)) is True
    assert broker.resolve(1, Decision(False)) is False
    assert (await fut).approved is True


class _Block:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return self.data


class _Usage:
    input_tokens, output_tokens = 120, 40
    cache_read_input_tokens, cache_creation_input_tokens = 1000, 0


class _Resp:
    content = [
        _Block({"type": "thinking", "thinking": "resumen", "signature": "x"}),
        _Block({"type": "text", "text": "hola"}),
    ]
    stop_reason = "end_turn"
    stop_details = None
    usage = _Usage()
    model = "claude-opus-5-5"


class _FakeClient:
    def __init__(self):
        self.kwargs = None
        outer = self

        class _Messages:
            async def create(self, **kwargs):
                outer.kwargs = kwargs
                return _Resp()

        class _Beta:
            messages = _Messages()

        self.beta = _Beta()


async def test_anthropic_request_shape():
    client = _FakeClient()
    llm = AnthropicLLM(client=client)  # type: ignore[arg-type]
    result = await llm.complete(
        LLMRequest(
            agent="triage",
            system="s",
            messages=[{"role": "user", "content": "x"}],
            output_schema={"type": "object"},
            effort="low",
        )
    )

    k = client.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert k["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": {"type": "object"}},
    }
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]
    assert k["cache_control"] == {"type": "ephemeral"}
    assert "tools" not in k
    assert result.text() == "hola"
    assert result.thinking_summaries() == ["resumen"]
    assert result.usage.cache_read_tokens == 1000
