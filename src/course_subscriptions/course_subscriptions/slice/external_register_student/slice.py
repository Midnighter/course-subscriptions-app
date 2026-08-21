# Copyright 2026 Moritz E. Beber
"""Provide the External Register Student slice."""

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import ExternalStudentRegistered


class ExternalRegisterStudentSlice(CommandSlice):
    """DCB slice that records an external student registration fact."""

    def __init__(self, student_id: str, name: str, course_limit: int) -> None:
        self._student_id = student_id
        self._name = name
        self._course_limit = course_limit

    def _tags(self) -> list[str]:
        return [f"student:{self._student_id}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector scoping replay to this student's history."""
        return Selector(types=[ExternalStudentRegistered], tags=self._tags())

    @event(ExternalStudentRegistered)
    def _(self) -> None:
        """Ignore prior history — every submission is recorded unconditionally."""

    def execute(self) -> None:
        """Unconditionally emit an ExternalStudentRegistered event."""
        self.trigger_event(
            ExternalStudentRegistered,
            self._tags(),
            student_id=self._student_id,
            name=self._name,
            course_limit=self._course_limit,
        )
