# Copyright 2026 Moritz E. Beber
"""Test the Unsubscribe Student slice."""

import pytest
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)
from course_subscriptions.course_subscriptions.unsubscribe_student.slice import (
    UnsubscribeStudentSlice,
)

_COURSE_ID = "EM-2024-001"
_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"course:{_COURSE_ID}", f"student:{_STUDENT_ID}"]


def _slice() -> UnsubscribeStudentSlice:
    return UnsubscribeStudentSlice(course_id=_COURSE_ID, student_id=_STUDENT_ID)


def test_unsubscribe_student_emits_student_unsubscribed() -> None:
    """Happy path: an enrolled student is unsubscribed from their course."""
    course_registered = TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=10,
        ),
        tags=[f"course:{_COURSE_ID}"],
    )
    student_subscribed = TaggedEvent(
        decision=StudentSubscribed(course_id=_COURSE_ID, student_id=_STUDENT_ID),
        tags=_TAGS,
    )
    given(course_registered, student_subscribed).when(_slice()).then(
        TaggedEvent(
            decision=StudentUnsubscribed(
                course_id=_COURSE_ID,
                student_id=_STUDENT_ID,
            ),
            tags=_TAGS,
        ),
    )


def test_unsubscribe_student_raises_on_unknown_course() -> None:
    """Invariant: cannot unsubscribe from a course that was never registered."""
    with pytest.raises(ValueError, match="unknown_course"):
        given().when(_slice())


def test_unsubscribe_student_raises_when_not_subscribed() -> None:
    """
    Invariant: cannot unsubscribe a student who is not subscribed.

    The scenario in slice.json arranges another student's subscription to the
    same course to prove isolation between students. GWT rejects any given
    event outside the slice's own boundary tags, so that cross-entity case is
    covered in the integration suite instead; here we cover the same
    ``not_subscribed`` invariant with a boundary-compliant history.
    """
    course_registered = TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=10,
        ),
        tags=[f"course:{_COURSE_ID}"],
    )
    with pytest.raises(ValueError, match="not_subscribed"):
        given(course_registered).when(_slice())
