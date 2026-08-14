# Copyright 2026 Moritz E. Beber
"""Test the Student Course Subscriptions route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import StudentSubscribed

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from course_subscriptions.application import CourseSubscriptionsApp

_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"student:{_STUDENT_ID}"]


def test_student_course_subscriptions_with_no_events(client: TestClient) -> None:
    """Querying a student with no events returns zero subscriptions."""
    response = client.get(f"/students/{_STUDENT_ID}/course-subscriptions")
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
    subscribed_to_a_course: StudentSubscribed,
) -> None:
    """Querying a subscribed student returns the projected course."""
    response = client.get(f"/students/{_STUDENT_ID}/course-subscriptions")
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_count"] == 1
    assert body["courses"] == [subscribed_to_a_course.course_id]


def test_student_course_subscriptions_isolates_other_students(
    client: TestClient,
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """Another student's subscriptions do not leak into this student's view."""
    response = client.get("/students/STU-2026-9999/course-subscriptions")
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_count"] == 0
    assert body["courses"] == []


def test_student_course_subscriptions_reports_its_position(
    client: TestClient,
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """A successful query reports the position the view reflects."""
    response = client.get(f"/students/{_STUDENT_ID}/course-subscriptions")
    assert response.status_code == 200
    assert int(response.headers["X-Current-Position"]) >= 1


def test_student_course_subscriptions_reports_too_early_when_behind(
    client: TestClient,
    subscribed_to_a_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """A precondition the view cannot meet is answered with 425, not stale data."""
    response = client.get(
        f"/students/{_STUDENT_ID}/course-subscriptions",
        headers={"X-Position-AtLeast": "1000000"},
    )
    assert response.status_code == 425
    assert response.content == b""
