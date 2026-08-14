# Copyright 2026 Moritz E. Beber
"""Test the Course Catalogue route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseCapacityChanged,
    CourseRegistered,
    StudentSubscribed,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from course_subscriptions.application import CourseSubscriptionsApp

_FIRST_COURSE_ID = "EM-2024-001"
_SECOND_COURSE_ID = "EX-2025-011"


@pytest.fixture
def first_course_registered(dcb_app: CourseSubscriptionsApp) -> CourseRegistered:
    """Seed the fact that the first course was registered."""
    decision = CourseRegistered(
        course_id=_FIRST_COURSE_ID,
        title="Intro to Event Modeling",
        capacity=10,
    )
    dcb_app.events.append(
        events=[TaggedEvent(decision=decision, tags=[f"course:{_FIRST_COURSE_ID}"])],
    )
    return decision


@pytest.fixture
def second_course_registered(dcb_app: CourseSubscriptionsApp) -> CourseRegistered:
    """Seed the fact that a second, independent course was registered."""
    decision = CourseRegistered(
        course_id=_SECOND_COURSE_ID,
        title="Advanced Event Modeling",
        capacity=5,
    )
    dcb_app.events.append(
        events=[TaggedEvent(decision=decision, tags=[f"course:{_SECOND_COURSE_ID}"])],
    )
    return decision


def test_course_catalogue_empty_returns_empty_list(client: TestClient) -> None:
    """With no courses registered, the catalogue is an empty list, not a 404."""
    response = client.get("/course-catalogue")
    assert response.status_code == 200
    assert response.json() == {"courses": []}


def test_course_catalogue_shows_title_and_capacity(
    client: TestClient,
    first_course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """A registered course appears with its title and capacity."""
    response = client.get("/course-catalogue")
    assert response.status_code == 200
    assert response.json() == {
        "courses": [
            {
                "course_id": _FIRST_COURSE_ID,
                "title": "Intro to Event Modeling",
                "capacity": 10,
                "number_of_subscriptions": 0,
            },
        ],
    }


def test_course_catalogue_accounts_for_capacity_changes(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    first_course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """A capacity change is reflected in the catalogue entry."""
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=CourseCapacityChanged(
                    course_id=_FIRST_COURSE_ID,
                    capacity=2,
                ),
                tags=[f"course:{_FIRST_COURSE_ID}"],
            ),
        ],
    )
    response = client.get("/course-catalogue")
    assert response.status_code == 200
    assert response.json()["courses"][0]["capacity"] == 2


def test_course_catalogue_counts_subscriptions(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    first_course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """The subscription count reflects currently subscribed students."""
    for student_id in ("s-1", "s-2"):
        dcb_app.events.append(
            events=[
                TaggedEvent(
                    decision=StudentSubscribed(
                        course_id=_FIRST_COURSE_ID,
                        student_id=student_id,
                    ),
                    tags=[f"course:{_FIRST_COURSE_ID}", f"student:{student_id}"],
                ),
            ],
        )
    response = client.get("/course-catalogue")
    assert response.status_code == 200
    assert response.json()["courses"][0]["number_of_subscriptions"] == 2


def test_course_catalogue_lists_multiple_independent_courses(
    client: TestClient,
    first_course_registered: CourseRegistered,  # noqa: ARG001
    second_course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Each registered course appears as its own catalogue entry."""
    response = client.get("/course-catalogue")
    assert response.status_code == 200
    course_ids = {entry["course_id"] for entry in response.json()["courses"]}
    assert course_ids == {_FIRST_COURSE_ID, _SECOND_COURSE_ID}


def test_course_catalogue_reports_its_position(
    client: TestClient,
    first_course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """A successful query reports the position the view reflects."""
    response = client.get("/course-catalogue")
    assert response.status_code == 200
    assert int(response.headers["X-Current-Position"]) >= 1


def test_course_catalogue_reports_too_early_when_behind(
    client: TestClient,
    first_course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """A precondition the view cannot meet is answered with 425, not stale data."""
    response = client.get(
        "/course-catalogue",
        headers={"X-Position-AtLeast": "1000000"},
    )
    assert response.status_code == 425
    assert response.content == b""
