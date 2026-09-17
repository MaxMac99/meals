"""Request/response shapes for the built-in assistant.

The conversation history is the client's: the app holds the messages and
sends the recent ones with every request, and the server trims them against
an oversized context window before the model sees them (see
``app/assistant/runtime.py``). The server therefore accepts only plain
``user``/``assistant`` text here — tool traffic never crosses this endpoint,
because the tool loop happens entirely server-side.
"""

import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field


class ChatMessageIn(BaseModel):
    """One turn the client is replaying. ``assistant`` messages are the
    assistant's *visible* answers only — a client has no tool transcripts to
    send, and none are accepted."""

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class ConfirmedActionIn(BaseModel):
    """The destructive action the household just confirmed in the app's
    dialog, sent back verbatim from the earlier response's ``action``.

    Trusting a client-named action would be trusting whoever can hold a
    token, so the runtime re-validates the whole envelope: the tool must be
    one of the destructive ones (an allow-list, not a flag), and its
    arguments must survive the same validation any direct call would."""

    tool: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantChatIn(BaseModel):
    messages: list[ChatMessageIn] = Field(min_length=1, max_length=40)
    confirmed_action: ConfirmedActionIn | None = None


class AssistantMessageOut(BaseModel):
    """The assistant's answer — what the app appends to its local history."""

    role: Literal["assistant"] = "assistant"
    content: str


class ReferenceOut(BaseModel):
    """A concrete Meal object the answer is about, so the app can offer the
    matching screen later. Optional payload — a reference-less answer is
    ordinary."""

    type: Literal["recipe", "meal", "plan", "plan_meal", "list_item", "freezer_item", "ingredient", "supermarket"]
    id: uuid.UUID
    title: str | None = None


class ActionOut(BaseModel):
    """A destructive action waiting for the household's yes.

    The app shows ``summary`` in a native dialog; on confirmation it posts
    this exact object back as ``confirmed_action``. The server treats it as
    a claim, not a command: the tool must be on the destructive allow-list
    and the arguments re-validated before anything runs."""

    tool: str
    arguments: dict[str, Any]
    summary: str


class AssistantChatOut(BaseModel):
    """One assistant turn. Exactly one of the three states holds:

    - plain answer: ``message`` set, ``action`` null;
    - needs confirmation: ``action`` set (``message`` carries the
      ask-first sentence to show above the dialog);
    - both are set for a normal answer that also mentions what it could do
      next — the app renders the message and only prompts when ``action``
      is present."""

    message: AssistantMessageOut
    references: list[ReferenceOut] = Field(default_factory=list)
    action: ActionOut | None = None
