# Copyright 2026 Moritz E. Beber
"""Provide the Register Course slice."""

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import CourseRegistered


class RegisterCourseSlice(CommandSlice):
    """DCB slice that processes the Register Course command."""

    def __init__(self, course_id: str, title: str, capacity: int) -> None:
        self.registered = False
        self._course_id = course_id
        self._title = title
        self._capacity = capacity

    def _tags(self) -> list[str]:
        return [f"course:{self._course_id}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector scoping replay to this course's history."""
        return Selector(types=[CourseRegistered], tags=self._tags())

    @event(CourseRegistered)
    def _(self) -> None:
        self.registered = True

    def execute(self) -> None:
        """Validate the course id is unused and emit a CourseRegistered event."""
        if self.registered:
            msg = "course_already_registered"
            raise ValueError(msg)

        self.trigger_event(
            CourseRegistered,
            self._tags(),
            course_id=self._course_id,
            title=self._title,
            capacity=self._capacity,
        )
