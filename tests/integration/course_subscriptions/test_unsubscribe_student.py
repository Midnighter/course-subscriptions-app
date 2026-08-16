# Copyright 2026 Moritz E. Beber
"""Test the Unsubscribe Student route."""

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
_OTHER_STUDENT_ID = "STU-2026-0041"
_URL = f"/students/{_STUDENT_ID}/unsubscribe-from-course"


@pytest.fixture
def auth(student_auth: Callable[[str], dict]) -> dict:
    """Return the headers of the student these tests act for."""
    return student_auth(_STUDENT_ID)


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


@pytest.fixture
def subscribed_to_the_course(
    dcb_app: CourseSubscriptionsApp,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> StudentSubscribed:
    """Seed the fact that the student subscribed to the course."""
    decision = StudentSubscribed(course_id=_COURSE_ID, student_id=_STUDENT_ID)
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=decision,
                tags=[f"course:{_COURSE_ID}", f"student:{_STUDENT_ID}"],
            ),
        ],
    )
    return decision


def test_unsubscribe_student_returns_201(
    client: TestClient,
    auth: dict,
    subscribed_to_the_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """Unsubscribing an enrolled student returns HTTP 201."""
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 201
    body = response.json()
    assert body["position"] is not None
    assert len(body["event_ids"]) == 1


def test_unsubscribe_student_unknown_course_returns_422(
    client: TestClient,
    auth: dict,
) -> None:
    """Unsubscribing from a never-registered course returns HTTP 422."""
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown_course"


def test_unsubscribe_student_not_subscribed_returns_422(
    client: TestClient,
    auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Unsubscribing a student who never subscribed returns HTTP 422."""
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "not_subscribed"


def test_unsubscribe_student_missing_field_returns_422(
    client: TestClient,
    auth: dict,
) -> None:
    """A request missing the required course_id field returns HTTP 422."""
    response = client.post(_URL, json={}, headers=auth)
    assert response.status_code == 422


def test_unsubscribe_student_isolates_other_students(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """One student's subscription to a course does not cover another student."""
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=StudentSubscribed(
                    course_id=_COURSE_ID,
                    student_id=_OTHER_STUDENT_ID,
                ),
                tags=[f"course:{_COURSE_ID}", f"student:{_OTHER_STUDENT_ID}"],
            ),
        ],
    )
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "not_subscribed"


def test_unsubscribe_student_without_a_token_returns_401(client: TestClient) -> None:
    """An anonymous caller cannot unsubscribe anyone."""
    response = client.post(_URL, json={"course_id": _COURSE_ID})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unsubscribe_student_as_another_student_returns_403(
    client: TestClient,
    student_auth: Callable[[str], dict],
    subscribed_to_the_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """
    A student cannot unsubscribe somebody else from a course.

    Seeded with the subscription in place, so the command would have succeeded
    had the rule not refused it - which is what makes this the destructive
    case the ownership rule exists to prevent.
    """
    response = client.post(
        _URL,
        json={"course_id": _COURSE_ID},
        headers=student_auth(_OTHER_STUDENT_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not_your_account"
