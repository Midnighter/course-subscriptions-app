# Copyright 2026 Moritz E. Beber
"""Provide the general-purpose metadata that every recorded event carries."""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from eventsourcing.domain import get_metadata_from_context, put_metadata_in_context
from starlette.datastructures import Headers, MutableHeaders

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_ID_KEY = "correlation_id"
CAUSATION_ID_KEY = "causation_id"
CREATED_AT_KEY = "created_at"

# Who caused the write. Seeded by the auth dependency in `auth.py`, which is
# the only place that knows the caller - the same way `MetadataMiddleware`
# below is the only place that knows the request. Both keys are absent on
# events no principal authenticated: an automation's command has no request,
# and `causation_id` already leads a reader back to the one that did.
PRINCIPAL_ID_KEY = "principal_id"
PRINCIPAL_TYPE_KEY = "principal_type"

CORRELATION_ID_HEADER = "X-Correlation-ID"

# A correlation id is stored in a `jsonb` column and echoed into logs and a
# response header, so a client-supplied one has to be bounded and free of
# control characters before it is trusted with any of that.
_CORRELATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_correlation_id() -> str:
    """Mint an identifier for a flow that has none yet."""
    return str(uuid4())


def sanitise_correlation_id(raw: str | None) -> str:
    """
    Return the caller's correlation id when it is usable, else a fresh one.

    Rejecting rather than truncating keeps the invariant simple: every stored
    `correlation_id` is either exactly what the client sent or one we minted,
    never a mangled prefix of the two.

    Args:
        raw: The inbound header value, or None when the client sent none.

    Returns:
        A correlation id safe to store, log, and echo.

    """
    if raw is not None and _CORRELATION_ID_PATTERN.fullmatch(raw):
        return raw
    return new_correlation_id()


def created_at() -> str:
    """
    Return the current UTC time, ISO 8601 formatted.

    Deliberately not the library's `datetime_now_with_tzinfo()`: that honours
    `TZINFO_TOPIC`, and the timestamp on a permanent log record should not be
    reconfigurable by an environment variable set for unrelated reasons.

    Returns:
        The current time in UTC, as an ISO 8601 string.

    """
    return datetime.now(tz=UTC).isoformat()


@contextmanager
def command_metadata() -> Iterator[None]:
    """
    Seed the metadata every event recorded inside the block inherits.

    `created_at` is stamped on every call, since it describes *this* unit of
    work. `correlation_id` is seeded only when absent, so the id put in context
    by `MetadataMiddleware` — or inherited by an automation from its triggering
    event — survives untouched. `causation_id` is never seeded here: a command
    reaching this point directly has no causing event, and an automation has
    already supplied one.

    Yields:
        None, for the duration of the seeded metadata.

    """
    metadata = {CREATED_AT_KEY: created_at()}
    if CORRELATION_ID_KEY not in get_metadata_from_context():
        metadata[CORRELATION_ID_KEY] = new_correlation_id()
    with put_metadata_in_context(metadata):
        yield


class MetadataMiddleware:
    """
    Seed one correlation id per HTTP request, and echo it back to the caller.

    Pure ASGI rather than `BaseHTTPMiddleware`, and that is not a style
    preference: `BaseHTTPMiddleware` runs the endpoint in a separate anyio
    task, so a contextvar set in its `dispatch` never reaches the route. The
    metadata would silently arrive empty.

    One request yields one correlation id, however many commands the route
    issues, which is what makes the id name the *flow* rather than the write.

    Args:
        app: The ASGI application this middleware wraps.

    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Put a correlation id in context for the duration of one request.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = sanitise_correlation_id(
            Headers(scope=scope).get(CORRELATION_ID_HEADER),
        )

        async def send_with_correlation_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        with put_metadata_in_context({CORRELATION_ID_KEY: correlation_id}):
            await self.app(scope, receive, send_with_correlation_id)
