# Copyright 2026 Moritz E. Beber
"""Test the Subscribe Student route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseRegistered,
    StudentRegistered,
    StudentSubscribed,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

    from course_subscriptions.application import CourseSubscriptionsApp

_COURSE_ID = "EM-2024-001"
_STUDENT_ID = "STU-2026-0042"
_OTHER_STUDENT_ID = "STU-2026-0099"
_URL = f"/students/{_STUDENT_ID}/subscribe-to-course"


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
def student_registered(dcb_app: CourseSubscriptionsApp) -> StudentRegistered:
    """Seed the fact that the student was registered with a course limit of 10."""
    decision = StudentRegistered(
        student_id=_STUDENT_ID,
        name="Anna Müller",
        course_limit=10,
    )
    dcb_app.events.append(
        events=[TaggedEvent(decision=decision, tags=[f"student:{_STUDENT_ID}"])],
    )
    return decision


@pytest.fixture
def subscribed_to_the_course(
    dcb_app: CourseSubscriptionsApp,
    course_registered: CourseRegistered,  # noqa: ARG001
    student_registered: StudentRegistered,  # noqa: ARG001
) -> StudentSubscribed:
    """Seed the fact that the student already subscribed to the course."""
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


def test_subscribe_student_returns_201(
    client: TestClient,
    auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
    student_registered: StudentRegistered,  # noqa: ARG001
) -> None:
    """Subscribing a registered student to a registered course returns HTTP 201."""
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 201
    body = response.json()
    assert body["position"] is not None
    assert len(body["event_ids"]) == 1


def test_subscribe_student_unknown_course_returns_422(
    client: TestClient,
    auth: dict,
    student_registered: StudentRegistered,  # noqa: ARG001
) -> None:
    """Subscribing to a never-registered course returns HTTP 422."""
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "unknown_course"


def test_subscribe_student_already_subscribed_returns_422(
    client: TestClient,
    auth: dict,
    subscribed_to_the_course: StudentSubscribed,  # noqa: ARG001
) -> None:
    """Subscribing twice to the same course returns HTTP 422."""
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "already_subscribed"


def test_subscribe_student_course_full_returns_422(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    auth: dict,
    student_registered: StudentRegistered,  # noqa: ARG001
) -> None:
    """Subscribing once the course has reached capacity returns HTTP 422."""
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=CourseRegistered(
                    course_id=_COURSE_ID,
                    title="Intro to Event Modeling",
                    capacity=2,
                ),
                tags=[f"course:{_COURSE_ID}"],
            ),
            TaggedEvent(
                decision=StudentSubscribed(course_id=_COURSE_ID, student_id="s-1"),
                tags=[f"course:{_COURSE_ID}", "student:s-1"],
            ),
            TaggedEvent(
                decision=StudentSubscribed(course_id=_COURSE_ID, student_id="s-2"),
                tags=[f"course:{_COURSE_ID}", "student:s-2"],
            ),
        ],
    )
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "course_full"


def test_subscribe_student_at_subscription_limit_returns_422(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    auth: dict,
    course_registered: CourseRegistered,  # noqa: ARG001
) -> None:
    """Subscribing beyond the student's course limit returns HTTP 422."""
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=StudentRegistered(
                    student_id=_STUDENT_ID,
                    name="Anna Müller",
                    course_limit=2,
                ),
                tags=[f"student:{_STUDENT_ID}"],
            ),
            TaggedEvent(
                decision=StudentSubscribed(
                    course_id="EM-2024-002",
                    student_id=_STUDENT_ID,
                ),
                tags=["course:EM-2024-002", f"student:{_STUDENT_ID}"],
            ),
            TaggedEvent(
                decision=StudentSubscribed(
                    course_id="EM-2024-003",
                    student_id=_STUDENT_ID,
                ),
                tags=["course:EM-2024-003", f"student:{_STUDENT_ID}"],
            ),
        ],
    )
    response = client.post(_URL, json={"course_id": _COURSE_ID}, headers=auth)
    assert response.status_code == 422
    assert response.json()["detail"] == "subscription_limit_reached"


def test_subscribe_student_missing_field_returns_422(
    client: TestClient,
    auth: dict,
) -> None:
    """A request missing the required course_id field returns HTTP 422."""
    response = client.post(_URL, json={}, headers=auth)
    assert response.status_code == 422


def test_subscribe_student_without_a_token_returns_401(client: TestClient) -> None:
    """An anonymous caller cannot subscribe anyone."""
    response = client.post(_URL, json={"course_id": _COURSE_ID})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_subscribe_student_as_another_student_returns_403(
    client: TestClient,
    student_auth: Callable[[str], dict],
    course_registered: CourseRegistered,  # noqa: ARG001
    student_registered: StudentRegistered,  # noqa: ARG001
) -> None:
    """
    A student cannot subscribe somebody else, however well-formed the request.

    The id in the path and the id in the token are both `STU-2026-…` strings
    and the route body cannot tell them apart, so this rule is the only thing
    standing between one student and another's subscriptions. Seeded so the
    command would otherwise have succeeded.
    """
    response = client.post(
        _URL,
        json={"course_id": _COURSE_ID},
        headers=student_auth(_OTHER_STUDENT_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "not_your_account"


def test_subscribe_student_as_another_student_records_nothing(
    client: TestClient,
    dcb_app: CourseSubscriptionsApp,
    student_auth: Callable[[str], dict],
    course_registered: CourseRegistered,  # noqa: ARG001
    student_registered: StudentRegistered,  # noqa: ARG001
) -> None:
    """
    The refused command leaves no trace in the log.

    A 403 that still wrote would be worse than no rule at all: the event log
    is permanent, so a write the caller was not entitled to make cannot be
    taken back.
    """
    before = len(list(dcb_app.events.read()))

    client.post(
        _URL,
        json={"course_id": _COURSE_ID},
        headers=student_auth(_OTHER_STUDENT_ID),
    )

    assert len(list(dcb_app.events.read())) == before
