"""The provider layer: which provider answers, and what happens when the
network between here and it misbehaves.

No test in this file reaches the network — the OpenAI SDK rides httpx2, so
every call runs through an `httpx2.MockTransport` handed to the provider,
the same seam test_mcp_mount uses for the SDK's own client.
"""

import httpx2
import pytest

from app.assistant import provider
from app.config import get_settings


def _transport(status_code: int, body: object) -> httpx2.AsyncClient:
    """A provider pointed at a canned response instead of the network."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, json=body)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _failing_transport(exc: Exception) -> httpx2.AsyncClient:
    async def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _provider(http_client: httpx2.AsyncClient) -> provider.OpenAIProvider:
    return provider.OpenAIProvider(api_key="sk-test", model="test-model", timeout_seconds=5, http_client=http_client)


def _tool_completion(name: str = "get_shopping_list", arguments: str = "{}") -> dict:
    """A chat completion carrying one tool call, the shape the loop is built
    around."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}
                    ],
                },
            }
        ],
    }


def _plain_completion(content: str) -> dict:
    return {
        "id": "chatcmpl-2",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
    }


@pytest.fixture
def enabled(settings_override):
    settings_override(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-test", OPENAI_MODEL="test-model")
    provider._providers.clear()
    yield
    provider._providers.clear()


class TestFactory:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("LLM_PROVIDER", raising=False)
        get_settings.cache_clear()
        try:
            assert provider.get_provider() is None
        finally:
            get_settings.cache_clear()

    def test_enabled_returns_a_provider_and_reuses_it(self, enabled):
        first = provider.get_provider()
        assert first is not None
        assert provider.get_provider() is first  # one pool per config, not per turn

    def test_disabled_after_override_yields_none(self, enabled, settings_override):
        settings_override(LLM_PROVIDER="disabled")
        assert provider.get_provider() is None


class TestComplete:
    async def test_decodes_a_tool_call(self, enabled):
        llm = _provider(_transport(200, _tool_completion(arguments='{"include_staples": true}')))
        result = await llm.complete([{"role": "user", "content": "shop"}], [])
        assert result.content is None
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_shopping_list"
        assert result.tool_calls[0].arguments == {"include_staples": True}

    async def test_decodes_a_plain_answer(self, enabled):
        llm = _provider(_transport(200, _plain_completion("Hi!")))
        result = await llm.complete([{"role": "user", "content": "hi"}], [])
        assert result.content == "Hi!"
        assert result.tool_calls == []

    async def test_malformed_tool_arguments_raise_valueerror(self, enabled):
        llm = _provider(_transport(200, _tool_completion(arguments="not json")))
        with pytest.raises(ValueError, match="not valid JSON"):
            await llm.complete([{"role": "user", "content": "x"}], [])

    async def test_rate_limit_maps_to_its_own_error(self, enabled):
        llm = _provider(_transport(429, {"error": {"message": "slow down"}}))
        with pytest.raises(provider.ProviderRateLimited):
            await llm.complete([{"role": "user", "content": "x"}], [])

    async def test_bad_credentials_map_to_unavailable(self, enabled):
        # An auth failure is the operator's problem, not the user's: the
        # message must not repeat the provider's (which can quote the key).
        llm = _provider(_transport(401, {"error": {"message": "Incorrect API key provided: sk-test***"}}))
        with pytest.raises(provider.ProviderUnavailable):
            await llm.complete([{"role": "user", "content": "x"}], [])

    async def test_a_down_provider_maps_to_unavailable(self, enabled):
        llm = _provider(_failing_transport(httpx2.ConnectError("nope")))
        with pytest.raises(provider.ProviderUnavailable):
            await llm.complete([{"role": "user", "content": "x"}], [])
