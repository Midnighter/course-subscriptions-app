# Copyright 2026 Moritz E. Beber
"""Test the Change Course Capacity route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseRegistered,
    StudentSubscribed,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from course_subscriptions.application import CourseSubscriptionsApp

_COURSE_ID = "EM-2024-001"
_STUDENT_ID = "STU-2026-0042"
_URL = f"/courses/{_COURSE_ID}/change-capacity"


@pytest.fixture
def course_registered(dcb_app: CourseSubscriptionsApp) -> CourseRegistered:
    """Seed the fact that the course was registered."""
    decision = CourseRegistered(
        course_id=_COURSE_ID,
        title="Intro to Event Modeling",
        capacity=10,
    )
    dcb_app.events.append(
        events=[TaggedEvent(decision=decision, tags=[f"course:{_COURSE_ID}"])],
    )
    return decision


def test_change_course_capacity_returns_201(
    client: TestClient,
    manager_auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Changing a registered course's capacity returns HTTP 201."""
    response = client.post(_URL, json={"capacity": 20}, headers=manager_auth)
    assert response.status_code == 201
    body = response.json()
    assert body["position"] is not None
    assert len(body["event_ids"]) == 1


def test_change_course_capacity_unknown_course_returns_422(
    client: TestClient,
    manager_auth: dict,
) -> None:
    """Changing the capacity of a never-registered course returns HTTP 422."""
    response = client.post(_URL, json={"capacity": 20}, headers=manager_auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown_course"


def test_change_course_capacity_same_capacity_returns_422(
    client: TestClient,
    manager_auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Requesting the current capacity again returns HTTP 422."""
    response = client.post(_URL, json={"capacity": 10}, headers=manager_auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "same_capacity"


def test_change_course_capacity_below_subscriptions_returns_422(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    manager_auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Reducing capacity below the current subscription count returns HTTP 422."""
    for student_id in ("s-1", "s-2", "s-3"):
        dcb_app.events.append(
            events=[
                TaggedEvent(
                    decision=StudentSubscribed(
                        course_id=_COURSE_ID,
                        student_id=student_id,
                    ),
                    tags=[f"course:{_COURSE_ID}", f"student:{student_id}"],
                ),
            ],
        )
    response = client.post(_URL, json={"capacity": 2}, headers=manager_auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "capacity_below_subscriptions"


def test_change_course_capacity_missing_field_returns_422(
    client: TestClient,
    manager_auth: dict,
) -> None:
    """A request missing the required capacity field returns HTTP 422."""
    response = client.post(_URL, json={}, headers=manager_auth)
    assert response.status_code == 422


def test_change_course_capacity_without_a_token_returns_401(
    client: TestClient,
) -> None:
    """An anonymous caller cannot change a course's capacity."""
    response = client.post(_URL, json={"capacity": 20})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_change_course_capacity_as_a_student_returns_403(
    client: TestClient,
    student_auth: Callable[[str], dict],
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """
    A student cannot change a course's capacity.

    Seeded with the course registered, so a 403 here cannot be mistaken for
    the 422 an unknown course would give: the rule is refusing the caller,
    not the request.
    """
    response = client.post(
        _URL,
        json={"capacity": 20},
        headers=student_auth(_STUDENT_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "missing_scope"
