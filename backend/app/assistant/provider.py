"""The LLM provider behind the built-in assistant, and nothing else.

Two rules shape this module:

- **Server-side only.** The API key lives in the environment and this
  process — it is never logged, never serialised into a response, and the
  iOS app learns only ``assistant_enabled`` from ``/client-config``. There
  is no provider code on any client.
- **One call shape.** A chat completion that may ask for tools; the
  assistant loop (``app/assistant/runtime.py``) owns everything above that:
  rounds, retries and confirmations. Keeping the provider dumb is what
  makes swapping providers a local change rather than a behavioural one.

Errors are folded into two exceptions the endpoint maps to honest status
codes: :class:`ProviderRateLimited` → 429, :class:`ProviderUnavailable` →
502. Authentication failures count as *unavailable* rather than their own
case on purpose — the client did nothing wrong and cannot fix it; the
operator must.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2
from openai import APIConnectionError, APIStatusError, AsyncOpenAI, AuthenticationError, RateLimitError

from app.config import get_settings


class ProviderUnavailable(Exception):
    """The provider could not be asked — down, unreachable, timed out, or
    refusing this server's credentials. The endpoint answers 502 with a
    sentence, never the exception text (which may name the endpoint or key)."""


class ProviderRateLimited(Exception):
    """The provider throttled this server. The endpoint answers 429 and the
    client retries later; nothing here retries on its own, because a chat
    answer that arrives a minute late is worse than one asked for again."""


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool the model asked for. ``arguments`` has been decoded from the
    wire's JSON string; a payload that is not JSON raises before this exists
    (the runtime turns it into feedback the model can act on)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class CompletionResult:
    """What the model said on one turn: either a final message, a request
    for tools, or both."""

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)


class LLMProvider(Protocol):
    """The one method the agent loop needs. Messages and tools are the
    provider's own wire format — building them is the caller's job, which is
    what keeps a provider swap from touching the loop."""

    async def complete(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> CompletionResult: ...


def _tool_call_requests(message_tool_calls: object) -> list[ToolCallRequest]:
    """Decode the response's tool calls; malformed argument JSON raises
    ``ValueError`` so the runtime can show the model its own mistake."""
    calls: list[ToolCallRequest] = []
    for call in message_tool_calls or []:  # type: ignore[union-attr]
        raw = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"model sent tool arguments that are not valid JSON: {exc}") from exc
        if not isinstance(arguments, dict):
            raise ValueError("model sent tool arguments that are not a JSON object")
        calls.append(ToolCallRequest(id=call.id or str(uuid.uuid4()), name=call.function.name, arguments=arguments))
    return calls


class OpenAIProvider:
    """Chat Completions over the official SDK — which is also the wire any
    OpenAI-compatible endpoint speaks, so OPENAI_BASE_URL reaches a local
    server, a gateway or OpenRouter without a second provider class."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        # A seam for tests: a pre-built transport means no test ever reaches
        # the network, the same promise respx makes for the REST-side httpx
        # calls. The SDK already rides httpx2 (it and the MCP SDK moved to it
        # together), so the type is httpx2's.
        http_client: httpx2.AsyncClient | None = None,
    ):
        self._model = model
        client_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "base_url": base_url,
            "timeout": timeout_seconds,
            "max_retries": 0,
        }
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        self._client = AsyncOpenAI(**client_kwargs)

    async def complete(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> CompletionResult:
        request: dict[str, Any] = {"model": self._model, "messages": list(messages)}
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"
        try:
            response = await self._client.chat.completions.create(**request)
        except RateLimitError as exc:
            raise ProviderRateLimited("the LLM provider is rate limiting this server") from exc
        except (AuthenticationError, APIStatusError, APIConnectionError) as exc:
            # The text of these can quote the endpoint or (worse) echo the
            # auth header; only the type survives, the detail is ours.
            raise ProviderUnavailable("the LLM provider could not be reached or refused the request") from exc
        choice = response.choices[0]
        return CompletionResult(
            content=choice.message.content, tool_calls=_tool_call_requests(choice.message.tool_calls)
        )


# One provider per configuration, built lazily and reused: a chat turn makes
# several completions, and standing up a fresh connection pool per turn would
# add a handshake to every one. The key is the config tuple, so changed
# settings (or a test override) build a fresh client rather than finding a
# stale one. Keys hold the configured secret in process memory only — the
# same place `get_settings()` already keeps it — and are never logged.
_providers: dict[tuple[str, str, str | None, float], OpenAIProvider] = {}


def get_provider() -> LLMProvider | None:
    """The configured provider for this deployment, or None while the
    assistant is off. Tests replace this function rather than the network."""
    settings = get_settings()
    if not settings.assistant_enabled:
        return None
    key = (
        settings.openai_api_key or "",
        settings.openai_model or "",
        settings.openai_base_url,
        settings.llm_timeout_seconds,
    )
    provider = _providers.get(key)
    if provider is None:
        provider = OpenAIProvider(api_key=key[0], model=key[1], base_url=key[2], timeout_seconds=key[3])
        _providers[key] = provider
    return provider
