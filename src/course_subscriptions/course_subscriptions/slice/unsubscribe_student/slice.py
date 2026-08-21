# Copyright 2026 Moritz E. Beber
"""Provide the Unsubscribe Student slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import (
    CourseRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class UnsubscribeStudentSlice(CommandSlice):
    """DCB slice that processes the Unsubscribe Student command."""

    def __init__(self, course_id: str, student_id: str) -> None:
        self.course_exists = False
        self.subscribed = False
        self._course_id = course_id
        self._student_id = student_id

    def _tags(self) -> list[str]:
        return [f"course:{self._course_id}", f"student:{self._student_id}"]

    def consistency_boundary(self) -> Sequence[Selector]:
        """Return selectors for the course's existence and this subscription."""
        return [
            Selector(types=[CourseRegistered], tags=[f"course:{self._course_id}"]),
            Selector(
                types=[StudentSubscribed, StudentUnsubscribed],
                tags=self._tags(),
            ),
        ]

    @event(CourseRegistered)
    def _(self) -> None:
        self.course_exists = True

    @event(StudentSubscribed)
    def _(self) -> None:
        self.subscribed = True

    @event(StudentUnsubscribed)
    def _(self) -> None:
        self.subscribed = False

    def execute(self) -> None:
        """Validate the subscription exists and emit a StudentUnsubscribed event."""
        if not self.course_exists:
            msg = "unknown_course"
            raise ValueError(msg)
        if not self.subscribed:
            msg = "not_subscribed"
            raise ValueError(msg)

        self.trigger_event(
            StudentUnsubscribed,
            self._tags(),
            course_id=self._course_id,
            student_id=self._student_id,
        )
