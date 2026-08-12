# Copyright 2026 Moritz E. Beber
"""Test the Register Student slice."""

import pytest
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import StudentRegistered
from course_subscriptions.course_subscriptions.register_student.slice import (
    RegisterStudentSlice,
)

_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"student:{_STUDENT_ID}"]


def _student_registered() -> TaggedEvent:
    return TaggedEvent(
        decision=StudentRegistered(
            student_id=_STUDENT_ID,
            name="Anna Müller",
            course_limit=2,
        ),
        tags=_TAGS,
    )


def _slice() -> RegisterStudentSlice:
    return RegisterStudentSlice(
        student_id=_STUDENT_ID,
        name="Anna Müller",
        course_limit=2,
    )


def test_register_student_emits_student_registered() -> None:
    """Happy path: registering a new student emits StudentRegistered."""
    given().when(_slice()).then(
        TaggedEvent(
            decision=StudentRegistered(
                student_id=_STUDENT_ID,
                name="Anna Müller",
                course_limit=2,
            ),
            tags=_TAGS,
        ),
    )


def test_register_student_raises_when_already_registered() -> None:
    """Invariant: cannot register the same student id twice."""
    with pytest.raises(ValueError, match="student_already_registered"):
        given(_student_registered()).when(_slice())
