# Copyright 2026 Moritz E. Beber
"""Test the Subscribe Student slice."""

import pytest
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseRegistered,
    StudentRegistered,
    StudentSubscribed,
)
from course_subscriptions.course_subscriptions.slice.subscribe_student.slice import (
    SubscribeStudentSlice,
)

_COURSE_ID = "EM-2024-001"
_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"course:{_COURSE_ID}", f"student:{_STUDENT_ID}"]


def _course_registered(course_id: str = _COURSE_ID, capacity: int = 10) -> TaggedEvent:
    return TaggedEvent(
        decision=CourseRegistered(
            course_id=course_id,
            title="Intro to Event Modeling",
            capacity=capacity,
        ),
        tags=[f"course:{course_id}"],
    )


def _student_registered(course_limit: int = 10) -> TaggedEvent:
    return TaggedEvent(
        decision=StudentRegistered(
            student_id=_STUDENT_ID,
            name="Anna Müller",
            course_limit=course_limit,
        ),
        tags=[f"student:{_STUDENT_ID}"],
    )


def _subscribed(course_id: str, student_id: str) -> TaggedEvent:
    return TaggedEvent(
        decision=StudentSubscribed(course_id=course_id, student_id=student_id),
        tags=[f"course:{course_id}", f"student:{student_id}"],
    )


def _slice() -> SubscribeStudentSlice:
    return SubscribeStudentSlice(course_id=_COURSE_ID, student_id=_STUDENT_ID)


def test_subscribe_student_emits_student_subscribed() -> None:
    """Happy path: a registered student subscribes to a registered course."""
    given(_course_registered(), _student_registered()).when(_slice()).then(
        TaggedEvent(
            decision=StudentSubscribed(course_id=_COURSE_ID, student_id=_STUDENT_ID),
            tags=_TAGS,
        ),
    )


def test_subscribe_student_raises_on_unknown_course() -> None:
    """Invariant: cannot subscribe to a course that was never registered."""
    with pytest.raises(ValueError, match="unknown_course"):
        given(_student_registered()).when(_slice())


def test_subscribe_student_raises_when_course_full() -> None:
    """Invariant: cannot subscribe once the course has reached capacity."""
    history = [
        _course_registered(capacity=2),
        _student_registered(),
        _subscribed(_COURSE_ID, "s-1"),
        _subscribed(_COURSE_ID, "s-2"),
    ]
    with pytest.raises(ValueError, match="course_full"):
        given(*history).when(_slice())


def test_subscribe_student_raises_when_already_subscribed() -> None:
    """Invariant: cannot subscribe to the same course twice."""
    history = [
        _course_registered(),
        _student_registered(),
        _subscribed(_COURSE_ID, _STUDENT_ID),
    ]
    with pytest.raises(ValueError, match="already_subscribed"):
        given(*history).when(_slice())


def test_subscribe_student_raises_at_subscription_limit() -> None:
    """Invariant: cannot subscribe beyond the student's course limit."""
    history = [
        _course_registered(),
        _student_registered(course_limit=2),
        _subscribed("EM-2024-002", _STUDENT_ID),
        _subscribed("EM-2024-003", _STUDENT_ID),
    ]
    with pytest.raises(ValueError, match="subscription_limit_reached"):
        given(*history).when(_slice())
