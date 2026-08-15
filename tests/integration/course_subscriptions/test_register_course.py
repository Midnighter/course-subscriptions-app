# Copyright 2026 Moritz E. Beber
"""Test the Register Course route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import CourseRegistered

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from course_subscriptions.application import CourseSubscriptionsApp

_COURSE_ID = "EM-2024-001"


@pytest.fixture
def course_registered(dcb_app: CourseSubscriptionsApp) -> CourseRegistered:
    """Seed the fact that the course was already registered."""
    decision = CourseRegistered(
        course_id=_COURSE_ID,
        title="Intro to Event Modeling",
        capacity=10,
    )
    dcb_app.events.append(
        events=[TaggedEvent(decision=decision, tags=[f"course:{_COURSE_ID}"])],
    )
    return decision


def test_register_course_returns_201(client: TestClient) -> None:
    """Registering a new course returns HTTP 201."""
    response = client.post(
        "/courses/register",
        json={
            "course_id": _COURSE_ID,
            "title": "Intro to Event Modeling",
            "capacity": 10,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["position"] is not None
    assert len(body["event_ids"]) == 1


def test_register_course_already_registered_returns_422(
    client: TestClient,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Registering an already-registered course id returns HTTP 422."""
    response = client.post(
        "/courses/register",
        json={
            "course_id": _COURSE_ID,
            "title": "Intro to Event Modeling",
            "capacity": 10,
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "course_already_registered"


def test_register_course_missing_field_returns_422(client: TestClient) -> None:
    """A request missing a required field returns HTTP 422."""
    response = client.post("/courses/register", json={"course_id": _COURSE_ID})
    assert response.status_code == 422
