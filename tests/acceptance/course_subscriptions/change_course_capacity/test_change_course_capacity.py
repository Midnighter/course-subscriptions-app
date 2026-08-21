# Copyright 2026 Moritz E. Beber
"""Test the Change Course Capacity slice."""

import pytest
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseCapacityChanged,
    CourseRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)
from course_subscriptions.course_subscriptions.slice.change_course_capacity.slice import (
    ChangeCourseCapacitySlice,
)

_COURSE_ID = "EM-2024-001"
_TAGS = [f"course:{_COURSE_ID}"]


def _course_registered(capacity: int = 10) -> TaggedEvent:
    return TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=capacity,
        ),
        tags=_TAGS,
    )


def _subscribed(student_id: str) -> TaggedEvent:
    return TaggedEvent(
        decision=StudentSubscribed(course_id=_COURSE_ID, student_id=student_id),
        tags=[*_TAGS, f"student:{student_id}"],
    )


def _unsubscribed(student_id: str) -> TaggedEvent:
    return TaggedEvent(
        decision=StudentUnsubscribed(course_id=_COURSE_ID, student_id=student_id),
        tags=[*_TAGS, f"student:{student_id}"],
    )


def _slice(capacity: int) -> ChangeCourseCapacitySlice:
    return ChangeCourseCapacitySlice(course_id=_COURSE_ID, capacity=capacity)


def test_change_course_capacity_emits_course_capacity_changed() -> None:
    """Happy path: changing a registered course's capacity emits the event."""
    given(_course_registered()).when(_slice(20)).then(
        TaggedEvent(
            decision=CourseCapacityChanged(course_id=_COURSE_ID, capacity=20),
            tags=_TAGS,
        ),
    )


def test_change_course_capacity_raises_on_unknown_course() -> None:
    """Invariant: cannot change the capacity of a course that was never registered."""
    with pytest.raises(ValueError, match="unknown_course"):
        given().when(_slice(20))


def test_change_course_capacity_raises_on_same_capacity() -> None:
    """Invariant: the new capacity must differ from the current one."""
    with pytest.raises(ValueError, match="same_capacity"):
        given(_course_registered(capacity=10)).when(_slice(10))


def test_change_course_capacity_raises_below_current_subscriptions() -> None:
    """Invariant: capacity cannot drop below the current subscription count."""
    history = [
        _course_registered(capacity=10),
        _subscribed("s-1"),
        _subscribed("s-2"),
        _subscribed("s-3"),
        _subscribed("s-4"),
        _unsubscribed("s-4"),
    ]
    with pytest.raises(ValueError, match="capacity_below_subscriptions"):
        given(*history).when(_slice(2))
