# Copyright 2026 Moritz E. Beber
"""Test the Student Course Subscriptions route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import StudentSubscribed

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from course_subscriptions.application import CourseSubscriptionsApp

_STUDENT_ID = "STU-2026-0042"
_OTHER_STUDENT_ID = "STU-2026-9999"
_TAGS = [f"student:{_STUDENT_ID}"]
_URL = f"/students/{_STUDENT_ID}/course-subscriptions"


@pytest.fixture
def auth(student_auth: Callable[[str], dict]) -> dict:
    """Return the headers of the student these tests act for."""
    return student_auth(_STUDENT_ID)


def test_student_course_subscriptions_with_no_events(
    client: TestClient,
    auth: dict,
) -> None:
    """Querying a student with no events returns zero subscriptions."""
    response = client.get(_URL, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["student_id"] == _STUDENT_ID
    assert body["subscription_count"] == 0
    assert body["courses"] == []


@pytest.fixture
def subscribed_to_a_course(
    dcb_app: CourseSubscriptionsApp,
) -> StudentSubscribed:
    """Seed the fact that the student subscribed to one course."""
    decision = StudentSubscribed(course_id="EM-2024-001", student_id=_STUDENT_ID)
    dcb_app.events.append(events=[TaggedEvent(decision=decision, tags=_TAGS)])
    return decision


def test_student_course_subscriptions_returns_projected_state(
    client: TestClient,
    auth: dict,
    subscribed_to_a_course: StudentSubscribed,
) -> None:
    """Querying a subscribed student returns the projected course."""
    response = client.get(_URL, headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_count"] == 1
    assert body["courses"] == [subscribed_to_a_course.course_id]


def test_student_course_subscriptions_isolates_other_students(
    client: TestClient,
    student_auth: Callable[[str], dict],
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """
    Another student's subscriptions do not leak into this student's view.

    The other student queries their *own* account, so this proves the view is
    scoped by student id rather than merely that the ownership rule refuses
    cross-account reads - which the 403 case below covers separately.
    """
    response = client.get(
        f"/students/{_OTHER_STUDENT_ID}/course-subscriptions",
        headers=student_auth(_OTHER_STUDENT_ID),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_count"] == 0
    assert body["courses"] == []


def test_student_course_subscriptions_reports_its_position(
    client: TestClient,
    auth: dict,
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """A successful query reports the position the view reflects."""
    response = client.get(_URL, headers=auth)
    assert response.status_code == 200
    assert int(response.headers["X-Current-Position"]) >= 1


def test_student_course_subscriptions_reports_too_early_when_behind(
    client: TestClient,
    auth: dict,
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """A precondition the view cannot meet is answered with 425, not stale data."""
    response = client.get(
        _URL,
        headers={**auth, "X-Position-AtLeast": "1000000"},
    )
    assert response.status_code == 425
    assert response.content == b""


def test_student_course_subscriptions_without_a_token_returns_401(
    client: TestClient,
) -> None:
    """An anonymous caller cannot read anyone's subscriptions."""
    response = client.get(_URL)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_student_course_subscriptions_of_another_student_returns_403(
    client: TestClient,
    student_auth: Callable[[str], dict],
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """
    A student cannot read another student's subscriptions.

    The ownership rule guards reads as well as writes: which courses somebody
    is enrolled in is theirs to know.
    """
    response = client.get(_URL, headers=student_auth(_OTHER_STUDENT_ID))
    assert response.status_code == 403
    assert response.json()["detail"] == "not_your_account"


def test_student_course_subscriptions_as_a_manager_returns_403(
    client: TestClient,
    manager_auth: dict,
) -> None:
    """
    A course manager has no route to a student's subscriptions either.

    The board draws this screen only in the Student lane. Should the business
    ever want a manager to see it, that is a new slice with its own screen,
    not a widened rule here.
    """
    response = client.get(_URL, headers=manager_auth)
    assert response.status_code == 403
    assert response.json()["detail"] == "not_your_account"
