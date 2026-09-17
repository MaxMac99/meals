"""POST /assistant/chat — the built-in AI assistant.

The route is registered always and answers **404 while no LLM provider is
configured** — the same posture as the billing webhook and /metrics: a
self-hosted instance must not be able to acquire an LLM bill by accident,
and "off" has to mean the feature does not exist rather than a button that
errors. ``/client-config`` publishes the state as ``assistant_enabled`` so
clients show their disabled screen without asking.

This module is HTTP only: auth, the household throttle, error mapping and
one event line. The turn itself is ``app/assistant/runtime.run_turn``; the
tools it runs are the routers', in this request's own session.
"""

import time
import uuid
from collections import defaultdict, deque
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.assistant import provider as provider_module
from app.assistant.provider import ProviderRateLimited, ProviderUnavailable
from app.assistant.runtime import assistant_message, run_turn
from app.config import get_settings
from app.deps import CurrentUser, DbSession
from app.observability import log_event
from app.routers.skill import base_url
from app.schemas.assistant import AssistantChatIn, AssistantChatOut

router = APIRouter(tags=["assistant"])

OFF_DETAIL = (
    "this deployment has no AI assistant configured; it needs LLM_PROVIDER, OPENAI_API_KEY "
    "and OPENAI_MODEL set (see the server's README) — everything else works as normal"
)

# In-process per-household throttle, the same shape as the auth limiter in
# deps.py: good enough for a single-container deployment, and reset by a
# restart (which is the generous failure direction).
_attempts: dict[uuid.UUID, deque[float]] = defaultdict(deque)


def _rate_limit(household_id: uuid.UUID) -> None:
    limit = get_settings().assistant_rate_limit_per_minute
    if limit <= 0:
        return
    window = _attempts[household_id]
    now = time.monotonic()
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= limit:
        log_event("assistant.rate_limited", household_id=household_id)
        raise HTTPException(
            status_code=429,
            detail="too many assistant messages; wait a minute and try again",
        )
    window.append(now)


def _require_provider() -> None:
    """The gate, as a dependency declared *before* the auth one: a caller
    without a token hears "there is no assistant here" (404), not "you are
    not logged in" (401) — the feature does not exist on this deployment,
    and pretending otherwise would invite exactly the login it can't use."""
    if not get_settings().assistant_enabled:
        raise HTTPException(status_code=404, detail=OFF_DETAIL)


@router.post("/assistant/chat", response_model=AssistantChatOut)
async def chat(
    _: Annotated[None, Depends(_require_provider)],
    payload: AssistantChatIn,
    user: CurrentUser,
    db: DbSession,
    request: Request,
) -> AssistantChatOut:
    """One turn of the built-in assistant. Send the recent conversation
    (``messages``, oldest first) plus the new user message; the answer comes
    back as ``message``, with ``references`` naming the meals/recipes/plan
    it acted on and ``action`` set when a destructive step needs the
    household's confirmation — post that action back verbatim as
    ``confirmed_action`` to run it.

    Requires an LLM provider to be configured server-side; without one this
    endpoint does not exist (404), and ``/client-config`` says so up front.
    """
    settings = get_settings()
    if not settings.assistant_enabled:
        raise HTTPException(status_code=404, detail=OFF_DETAIL)
    llm = provider_module.get_provider()
    if llm is None:  # a race with a config change; same answer as above
        raise HTTPException(status_code=404, detail=OFF_DETAIL)
    _rate_limit(user.household_id)

    started = time.monotonic()
    try:
        result = await run_turn(llm, user, db, payload.messages, payload.confirmed_action, base_url=base_url(request))
    except ProviderRateLimited as exc:
        log_event("assistant.chat", outcome="rate_limited", household_id=user.household_id)
        raise HTTPException(
            status_code=429,
            detail="the LLM provider is rate limiting this server; try again in a moment",
        ) from exc
    except ProviderUnavailable as exc:
        # The provider's own text can name its endpoint or echo credentials;
        # only our sentence goes to the client.
        log_event("assistant.chat", outcome="provider_unreachable", household_id=user.household_id)
        raise HTTPException(
            status_code=502,
            detail="the LLM provider could not be reached; if this persists, check the server's LLM_* settings",
        ) from exc

    log_event(
        "assistant.chat",
        outcome=result.outcome,
        rounds=result.rounds,
        references=len(result.references),
        household_id=user.household_id,
        user_id=user.id,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return AssistantChatOut(message=assistant_message(result), references=result.references, action=result.action)
