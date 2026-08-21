# Copyright 2026 Moritz E. Beber
"""Provide the Register Student slice."""

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import StudentRegistered


class RegisterStudentSlice(CommandSlice):
    """DCB slice that processes the Register Student command."""

    def __init__(self, student_id: str, name: str, course_limit: int) -> None:
        self.registered = False
        self._student_id = student_id
        self._name = name
        self._course_limit = course_limit

    def _tags(self) -> list[str]:
        return [f"student:{self._student_id}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector scoping replay to this student's history."""
        return Selector(types=[StudentRegistered], tags=self._tags())

    @event(StudentRegistered)
    def _(self) -> None:
        self.registered = True

    def execute(self) -> None:
        """Validate the student id is unused and emit a StudentRegistered event."""
        if self.registered:
            msg = "student_already_registered"
            raise ValueError(msg)

        self.trigger_event(
            StudentRegistered,
            self._tags(),
            student_id=self._student_id,
            name=self._name,
            course_limit=self._course_limit,
        )
