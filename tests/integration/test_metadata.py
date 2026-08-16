# Copyright 2026 Moritz E. Beber
"""Test that HTTP ingress seeds the metadata every event inherits."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from eventsourcing.domain import TaggedEvent, get_metadata_from_context
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from course_subscriptions.course_subscriptions.events import (
    CourseRegistered,
    StudentRegistered,
)
from course_subscriptions.metadata import (
    CAUSATION_ID_KEY,
    CORRELATION_ID_HEADER,
    CORRELATION_ID_KEY,
    CREATED_AT_KEY,
    PRINCIPAL_ID_KEY,
    PRINCIPAL_TYPE_KEY,
    MetadataMiddleware,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from course_subscriptions.application import CourseSubscriptionsApp

_STUDENT_ID = "STU-2026-0042"
_COURSE_ID = "EM-2024-001"
_WEBHOOK_BODY = {
    "student_id": _STUDENT_ID,
    "name": "Anna Müller",
    "course_limit": 2,
}


def test_response_echoes_a_minted_correlation_id(client: TestClient) -> None:
    """A request without the header still gets a flow, reported back."""
    response = client.get("/livez")

    assert response.status_code == status.HTTP_200_OK
    assert UUID(response.headers[CORRELATION_ID_HEADER])


def test_response_echoes_a_supplied_correlation_id(client: TestClient) -> None:
    """A usable client id is adopted verbatim, so the caller can join on it."""
    response = client.get("/livez", headers={CORRELATION_ID_HEADER: "corr-1"})

    assert response.headers[CORRELATION_ID_HEADER] == "corr-1"


def test_a_hostile_correlation_id_is_replaced(client: TestClient) -> None:
    """
    An unusable client id is replaced rather than echoed.

    `httpx` refuses to send a header containing a newline at all, so the case
    reachable over HTTP is the oversized one — still enough to prove the
    middleware sanitises rather than trusting what it is handed.
    """
    oversized = "x" * 200

    response = client.get("/livez", headers={CORRELATION_ID_HEADER: oversized})

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
    registrar_auth: dict,
) -> None:
    """The id seeded at ingress lands on the events the route's command writes."""
    response = client.post(
        "/webhooks/student-registered",
        json=_WEBHOOK_BODY,
        headers={**registrar_auth, CORRELATION_ID_HEADER: "corr-1"},
    )

    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    event_id = UUID(response.json()["event_ids"][0])
    recorded = next(env for env in dcb_app.events.read() if env.uuid == event_id)
    assert recorded.metadata[CORRELATION_ID_KEY] == "corr-1"
    assert recorded.metadata[CREATED_AT_KEY]
    # A root command has no causing *event*, and minting an id that resolves to
    # nothing would break the invariant that every causation_id names a real
    # event in our own log.
    assert CAUSATION_ID_KEY not in recorded.metadata


def test_a_command_records_the_caller(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    student_auth: Callable[[str], dict],
) -> None:
    """
    A student's command names that student on the event it writes.

    This is the point of the whole auth dependency, and the assertion that
    would fail were `get_principal` ever turned into a sync `def`: FastAPI
    would run it in the threadpool on a copied context, the contextvar would
    never reach the route, and the event would be recorded with no principal
    at all - silently, with the request still answered 201.
    """
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=CourseRegistered(
                    course_id=_COURSE_ID,
                    title="Intro to Event Modeling",
                    capacity=10,
                ),
                tags=[f"course:{_COURSE_ID}"],
            ),
            TaggedEvent(
                decision=StudentRegistered(
                    student_id=_STUDENT_ID,
                    name="Anna Müller",
                    course_limit=2,
                ),
                tags=[f"student:{_STUDENT_ID}"],
            ),
        ],
    )

    response = client.post(
        f"/students/{_STUDENT_ID}/subscribe-to-course",
        json={"course_id": _COURSE_ID},
        headers=student_auth(_STUDENT_ID),
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    event_id = UUID(response.json()["event_ids"][0])
    recorded = next(env for env in dcb_app.events.read() if env.uuid == event_id)
    assert recorded.metadata[PRINCIPAL_ID_KEY] == _STUDENT_ID
    assert recorded.metadata[PRINCIPAL_TYPE_KEY] == "user"


def test_a_webhook_records_the_machine_that_called_it(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    registrar_auth: dict,
    registrar_id: str,
) -> None:
    """
    The registrar's own event names the registrar, as a service.

    Why the key is `principal_id` and not `user_id`: no person caused this
    write, and a permanent log should not say one did.
    """
    response = client.post(
        "/webhooks/student-registered",
        json=_WEBHOOK_BODY,
        headers=registrar_auth,
    )

    event_id = UUID(response.json()["event_ids"][0])
    recorded = next(env for env in dcb_app.events.read() if env.uuid == event_id)
    assert recorded.metadata[PRINCIPAL_ID_KEY] == registrar_id
    assert recorded.metadata[PRINCIPAL_TYPE_KEY] == "service"


def test_an_automations_command_records_no_principal(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    registrar_auth: dict,
) -> None:
    """
    The automation's own event carries no principal, deliberately.

    `register_student` fires from a projection thread with no request behind
    it, so no principal authenticated it. Absence is honest and recoverable
    here rather than lossy: the `causation_id` leads a reader straight to the
    `ExternalStudentRegistered` event, which does name the registrar. Copying
    the registrar onto this event would instead assert an authentication that
    never took place.
    """
    view = client.app_state["register_student_view"]
    response = client.post(
        "/webhooks/student-registered",
        json=_WEBHOOK_BODY,
        headers=registrar_auth,
    )
    trigger_id = UUID(response.json()["event_ids"][0])
    view.wait(
        context_name=dcb_app.context_name,
        notification_id=response.json()["position"] + 1,
        timeout=5,
    )

    emitted = next(
        env
        for env in dcb_app.events.read()
        if isinstance(env.decision, StudentRegistered)
    )
    assert PRINCIPAL_ID_KEY not in emitted.metadata
    assert PRINCIPAL_TYPE_KEY not in emitted.metadata
    assert emitted.metadata[CAUSATION_ID_KEY] == str(trigger_id)


def test_non_http_scopes_pass_straight_through() -> None:
    """A lifespan or websocket scope is forwarded untouched."""
    seen: list[str] = []

    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        seen.append(scope["type"])

    middleware = MetadataMiddleware(downstream)

    asyncio.run(middleware({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]
