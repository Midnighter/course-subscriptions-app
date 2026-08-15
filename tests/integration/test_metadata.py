# Copyright 2026 Moritz E. Beber
"""Test that HTTP ingress seeds the metadata every event inherits."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from eventsourcing.domain import get_metadata_from_context
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from course_subscriptions.metadata import (
    CORRELATION_ID_HEADER,
    CORRELATION_ID_KEY,
    CREATED_AT_KEY,
    MetadataMiddleware,
)

if TYPE_CHECKING:
    from course_subscriptions.application import CourseSubscriptionsApp

_STUDENT_ID = "STU-2026-0042"


def test_response_echoes_a_minted_correlation_id(client: TestClient) -> None:
    """A request without the header still gets a flow, reported back."""
    response = client.get("/healthz")

    assert response.status_code == status.HTTP_200_OK
    assert UUID(response.headers[CORRELATION_ID_HEADER])


def test_response_echoes_a_supplied_correlation_id(client: TestClient) -> None:
    """A usable client id is adopted verbatim, so the caller can join on it."""
    response = client.get("/healthz", headers={CORRELATION_ID_HEADER: "corr-1"})

    assert response.headers[CORRELATION_ID_HEADER] == "corr-1"


def test_a_hostile_correlation_id_is_replaced(client: TestClient) -> None:
    """
    An unusable client id is replaced rather than echoed.

    `httpx` refuses to send a header containing a newline at all, so the case
    reachable over HTTP is the oversized one — still enough to prove the
    middleware sanitises rather than trusting what it is handed.
    """
    oversized = "x" * 200

    response = client.get("/healthz", headers={CORRELATION_ID_HEADER: oversized})

    assert response.headers[CORRELATION_ID_HEADER] != oversized
    assert UUID(response.headers[CORRELATION_ID_HEADER])


def test_the_correlation_id_reaches_the_route_handler() -> None:
    """
    The contextvar survives into the endpoint, not just the middleware.

    This is the assertion `BaseHTTPMiddleware` would fail: it runs the endpoint
    in a separate anyio task, so the metadata would arrive empty and every
    event would silently lose its flow. A standalone app keeps the claim about
    the middleware alone, with no application or projections in the way.
    """
    app = FastAPI()
    app.add_middleware(MetadataMiddleware)

    @app.get("/metadata")
    async def read_metadata() -> dict[str, str]:
        return get_metadata_from_context()

    with TestClient(app) as standalone:
        body = standalone.get(
            "/metadata",
            headers={CORRELATION_ID_HEADER: "corr-1"},
        ).json()

    assert body[CORRELATION_ID_KEY] == "corr-1"


def test_a_command_records_the_requests_correlation_id(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
) -> None:
    """The id seeded at ingress lands on the events the route's command writes."""
    response = client.post(
        "/webhooks/student-registered",
        json={"student_id": _STUDENT_ID, "name": "Anna Müller", "course_limit": 2},
        headers={CORRELATION_ID_HEADER: "corr-1"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    event_id = UUID(response.json()["event_ids"][0])
    recorded = next(env for env in dcb_app.events.read() if env.uuid == event_id)
    assert recorded.metadata[CORRELATION_ID_KEY] == "corr-1"
    assert recorded.metadata[CREATED_AT_KEY]
    # A root command has no causing *event*, and minting an id that resolves to
    # nothing would break the invariant that every causation_id names a real
    # event in our own log.
    assert "causation_id" not in recorded.metadata


def test_non_http_scopes_pass_straight_through() -> None:
    """A lifespan or websocket scope is forwarded untouched."""
    seen: list[str] = []

    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        seen.append(scope["type"])

    middleware = MetadataMiddleware(downstream)

    asyncio.run(middleware({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]
