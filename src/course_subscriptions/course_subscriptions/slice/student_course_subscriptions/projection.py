# Copyright 2026 Moritz E. Beber
"""Provide the Student Course Subscriptions view."""

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector, Slice

from course_subscriptions.course_subscriptions.events import (
    StudentRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)


class StudentCourseSubscriptionsView(Slice):
    """DCB read-model slice that projects a student's course subscriptions."""

    def __init__(self, student_id: str) -> None:
        self._student_id = student_id
        self.course_limit = 0
        self.courses: list[str] = []

    def _tags(self) -> list[str]:
        return [f"student:{self._student_id}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector that defines this view's consistency boundary."""
        return Selector(
            types=[StudentRegistered, StudentSubscribed, StudentUnsubscribed],
            tags=self._tags(),
        )

    @event(StudentRegistered)
    def _(self, course_limit: int) -> None:
        self.course_limit = course_limit

    @event(StudentSubscribed)
    def _(self, course_id: str) -> None:
        self.courses.append(course_id)

    @event(StudentUnsubscribed)
    def _(self, course_id: str) -> None:
        self.courses.remove(course_id)

    @property
    def subscription_count(self) -> int:
        """Return the number of courses the student is currently subscribed to."""
        return len(self.courses)

    def execute(self) -> None:
        """Read-only view: no command to run, no event to emit."""
