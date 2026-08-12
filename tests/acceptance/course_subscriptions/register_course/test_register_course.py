# Copyright 2026 Moritz E. Beber
"""Test the Register Course slice."""

import pytest
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import CourseRegistered
from course_subscriptions.course_subscriptions.register_course.slice import (
    RegisterCourseSlice,
)

_COURSE_ID = "EM-2024-001"
_TAGS = [f"course:{_COURSE_ID}"]


def _course_registered() -> TaggedEvent:
    return TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=10,
        ),
        tags=_TAGS,
    )


def _slice() -> RegisterCourseSlice:
    return RegisterCourseSlice(
        course_id=_COURSE_ID,
        title="Intro to Event Modeling",
        capacity=10,
    )


def test_register_course_emits_course_registered() -> None:
    """Happy path: registering a new course emits CourseRegistered."""
    given().when(_slice()).then(
        TaggedEvent(
            decision=CourseRegistered(
                course_id=_COURSE_ID,
                title="Intro to Event Modeling",
                capacity=10,
            ),
            tags=_TAGS,
        ),
    )


def test_register_course_raises_when_already_registered() -> None:
    """Invariant: cannot register the same course id twice."""
    with pytest.raises(ValueError, match="course_already_registered"):
        given(_course_registered()).when(_slice())
