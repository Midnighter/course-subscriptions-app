# Copyright 2026 Moritz E. Beber
"""Test the External Register Student slice."""

from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import ExternalStudentRegistered
from course_subscriptions.course_subscriptions.slice.external_register_student.slice import (
    ExternalRegisterStudentSlice,
)

_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"student:{_STUDENT_ID}"]


def _external_student_registered() -> TaggedEvent:
    return TaggedEvent(
        decision=ExternalStudentRegistered(
            student_id=_STUDENT_ID,
            name="Anna Müller",
            course_limit=2,
        ),
        tags=_TAGS,
    )


def _slice() -> ExternalRegisterStudentSlice:
    return ExternalRegisterStudentSlice(
        student_id=_STUDENT_ID,
        name="Anna Müller",
        course_limit=2,
    )


def test_external_register_student_emits_external_student_registered() -> None:
    """Happy path: recording a registration emits ExternalStudentRegistered."""
    given().when(_slice()).then(_external_student_registered())


def test_external_register_student_is_unconditional() -> None:
    """A repeat submission for the same student still emits the event."""
    given(_external_student_registered()).when(_slice()).then(
        _external_student_registered(),
    )
