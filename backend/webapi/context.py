"""Request-scoped context: request id propagation for logs and responses."""

from __future__ import annotations

import contextvars
import uuid

_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

MAX_INCOMING_REQUEST_ID_LENGTH = 64


def new_request_id() -> str:
    return str(uuid.uuid4())


def sanitize_request_id(raw: str | None) -> str:
    """Accept an inbound request id for tracing, otherwise mint one.

    Only a bounded, printable value is accepted so a client cannot inject control
    characters or unbounded text into every log line.
    """
    if raw:
        candidate = raw.strip()
        if candidate and len(candidate) <= MAX_INCOMING_REQUEST_ID_LENGTH:
            if all(character.isprintable() and not character.isspace() for character in candidate):
                return candidate
    return new_request_id()


def set_request_id(request_id: str) -> contextvars.Token:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _REQUEST_ID.reset(token)


def get_request_id() -> str:
    return _REQUEST_ID.get()
