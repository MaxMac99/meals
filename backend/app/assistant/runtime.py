"""The tool-calling agent loop.

One chat turn runs here. The shape is deliberately boring:

1. the client's messages (trimmed) plus the system prompt go to the LLM;
2. tool calls the model asks for are validated against the MCP-published
   schemas, then executed against the routers in the caller's session;
3. every tool result — success or failure — goes back to the model as a
   tool message, so one broken tool is feedback, never a crashed turn;
4. a call against a destructive tool is *not* executed: the turn returns a
   structured ``action`` for the app's native confirmation dialog instead,
   and a later request carries it back as ``confirmed_action`` (stateless —
   the server re-validates the envelope rather than remembering it);
5. the loop is bounded: MAX_TOOL_ROUNDS, after which the turn ends with an
   honest sentence instead of spinning.

Everything the model produces is treated as untrusted input in the usual
way: arguments are validated before tools run, and no tool exists here that
is not already reachable through the REST API with the same scoping.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.assistant import tools as tools_module
from app.assistant.prompts import build_system_prompt
from app.assistant.provider import CompletionResult, LLMProvider
from app.assistant.tools import ToolSpec
from app.models import User
from app.observability import log_event
from app.schemas.assistant import (
    ActionOut,
    AssistantMessageOut,
    ChatMessageIn,
    ConfirmedActionIn,
    ReferenceOut,
)

#: The ceiling on tool rounds per turn. A plan edit across five meals is
#: normal work and fits; a request that needs more is a request to split.
MAX_TOOL_ROUNDS = 8

#: The server-side cap on the history the client replays — against both a
#: runaway context window and a client padding messages. Oldest first out.
MAX_HISTORY_MESSAGES = 24
MAX_HISTORY_CHARS = 20_000

#: One tool result's budget. Recipe instructions and image URLs are the
#: bulky fields; everything else survives intact unless it is enormous.
MAX_RESULT_CHARS = 8000

_DROP_FIELDS = {"instructions", "image_url", "raw", "created_at", "updated_at"}


@dataclass
class TurnResult:
    """What one chat turn produced, for the endpoint to shape into a
    response and one event line."""

    content: str
    references: list[ReferenceOut] = field(default_factory=list)
    action: ActionOut | None = None
    rounds: int = 0
    #: answered | confirmation_required | max_rounds — the event log's enum.
    outcome: str = "answered"


def _trim_history(history: list[ChatMessageIn]) -> list[dict[str, str]]:
    """The last N messages within a character budget, with at least the
    newest one kept. The client owns the full transcript; this only stops
    it from being replayed at the model in full."""
    messages = [{"role": message.role, "content": message.content} for message in history]
    while len(messages) > 1 and (
        len(messages) > MAX_HISTORY_MESSAGES or sum(len(m["content"]) for m in messages) > MAX_HISTORY_CHARS
    ):
        messages.pop(0)
    return messages


def _strip_bulky(value: Any) -> Any:
    """Drop the fields a planner doesn't need (instructions, image URLs,
    raw parser lines, row timestamps), recursively; cap long strings."""
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in _DROP_FIELDS:
                continue
            if isinstance(item, str) and len(item) > 200:
                item = item[:200] + "…"
            cleaned[key] = _strip_bulky(item)
        return cleaned
    if isinstance(value, list):
        return [_strip_bulky(item) for item in value]
    return value


def _tool_result_text(value: Any) -> str:
    """The model's view of a tool result: the response model, slimmed and
    capped."""
    plain = _strip_bulky(_to_plain(value))
    text = json.dumps(plain, default=str, ensure_ascii=False)
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + "…(truncated)"
    return text


def _to_plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value


def _references(spec: ToolSpec, value: Any) -> list[ReferenceOut]:
    """Ids the answer is about, for the app's navigation chips. Only
    single-object results are tagged — a list tool's rows would flood."""
    if spec.ref_type is None or value is None:
        return []
    plain = _to_plain(value)
    if not isinstance(plain, dict) or "id" not in plain:
        return []
    title = plain.get("title") or plain.get("name") or plain.get("label")
    return [ReferenceOut(type=spec.ref_type, id=plain["id"], title=title)]


def _schema_for(definitions: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for definition in definitions:
        if definition["function"]["name"] == name:
            return definition["function"]["parameters"]
    return {}


def _assistant_tool_message(content: str | None, calls: list[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in calls
        ],
    }


def _tool_reply(call_id: str, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _validation_error_text(exc: ValidationError) -> str:
    bits = []
    for error in exc.errors()[:5]:
        loc = ".".join(str(part) for part in error.get("loc", ()))
        bits.append(f"{error['msg']} ({loc})" if loc else error["msg"])
    return "; ".join(bits)


async def _execute(spec: ToolSpec, user: User, db: AsyncSession, arguments: dict[str, Any]) -> Any:
    """Run one tool against the routers, in the chat request's own session.
    A seam kept trivial so tests can prove the loop around it, not around a
    mock of it."""
    return await spec.handler(user, db, arguments)


async def run_turn(
    llm: LLMProvider,
    user: User,
    db: AsyncSession,
    history: list[ChatMessageIn],
    confirmed_action: ConfirmedActionIn | None,
    today: date | None = None,
    base_url: str = "",
) -> TurnResult:
    """One assistant turn, bounded and stateless. Raises the provider's own
    errors (ProviderUnavailable / ProviderRateLimited) for the endpoint to
    map; everything tool-shaped is absorbed into the loop."""
    specs = tools_module.TOOLS
    definitions = await tools_module.tool_definitions()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(today or datetime.now(UTC).date(), base_url)}
    ]
    messages.extend(_trim_history(history))
    references: list[ReferenceOut] = []

    if confirmed_action is not None:
        spec = specs.get(confirmed_action.tool)
        if spec is None or not (spec.destructive or spec.destructive_for is not None):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{confirmed_action.tool}' is not a confirmation-required action; ask for it in the chat instead"
                ),
            )
        problem = tools_module.validate_arguments(_schema_for(definitions, spec.name), confirmed_action.arguments)
        if problem is not None:
            raise HTTPException(status_code=422, detail=f"the confirmed action is not valid: {problem}")
        try:
            value = await _execute(spec, user, db, confirmed_action.arguments)
        except HTTPException:
            raise  # the router's own refusal, wording intact
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"the confirmed action is not valid: {_validation_error_text(exc)}"
            ) from exc
        except LookupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        references.extend(_references(spec, value))
        # Reconstruct the exchange the model would have had, so its next
        # completion reads as the follow-up it is, not a cold start.
        synthetic_call = SimpleNamespace(id="confirmed", name=spec.name, arguments=confirmed_action.arguments)
        messages.append(_assistant_tool_message(None, [synthetic_call]))
        messages.append(_tool_reply("confirmed", _tool_result_text(value)))

    outcome = TurnResult(content="", references=references)
    for round_index in range(1, MAX_TOOL_ROUNDS + 1):
        completion: CompletionResult = await llm.complete(messages, definitions)
        outcome.rounds = round_index
        if not completion.tool_calls:
            outcome.content = completion.content or ""
            outcome.outcome = "answered"
            return outcome
        messages.append(_assistant_tool_message(completion.content, completion.tool_calls))
        for call in completion.tool_calls:
            spec = specs.get(call.name)
            if spec is None:
                messages.append(
                    _tool_reply(
                        call.id,
                        f"unknown tool '{call.name}'. Available tools: {', '.join(sorted(specs))}",
                    )
                )
                continue
            problem = tools_module.validate_arguments(_schema_for(definitions, spec.name), call.arguments)
            if problem is not None:
                messages.append(
                    _tool_reply(call.id, f"invalid arguments for {spec.name}: {problem}. Fix them and call again.")
                )
                continue
            if spec.requires_confirmation(call.arguments):
                # Destructive: stop here and ask the household. The ask text
                # is the model's own sentence when it wrote one.
                summary = tools_module.confirmation_summary(call.name, call.arguments)
                outcome.content = completion.content or summary
                outcome.action = ActionOut(tool=call.name, arguments=call.arguments, summary=summary)
                outcome.outcome = "confirmation_required"
                return outcome
            try:
                value = await _execute(spec, user, db, call.arguments)
            except HTTPException as exc:
                # The routers' 4xx sentences are written for assistants; the
                # model gets them verbatim and can change course.
                messages.append(_tool_reply(call.id, f"API error {exc.status_code}: {exc.detail}"))
                continue
            except ValidationError as exc:
                messages.append(
                    _tool_reply(call.id, f"invalid arguments for {spec.name}: {_validation_error_text(exc)}")
                )
                continue
            except LookupError as exc:
                messages.append(_tool_reply(call.id, str(exc)))
                continue
            except Exception:
                # A tool that failed unexpectedly must not take the turn
                # with it — and the model must not be told server internals
                # it cannot act on.
                log_event("assistant.tool_failed", tool=spec.name, household_id=user.household_id)
                messages.append(
                    _tool_reply(
                        call.id,
                        f"{spec.name} failed internally; its action may not have completed. "
                        "Verify the result before relying on it.",
                    )
                )
                continue
            messages.append(_tool_reply(call.id, _tool_result_text(value)))
            references.extend(_references(spec, value))

    outcome.content = (
        f"I stopped after {MAX_TOOL_ROUNDS} tool calls without finishing — the request may be too broad. "
        "Try a narrower one."
    )
    outcome.outcome = "max_rounds"
    return outcome


def assistant_message(result: TurnResult) -> AssistantMessageOut:
    return AssistantMessageOut(role="assistant", content=result.content)
