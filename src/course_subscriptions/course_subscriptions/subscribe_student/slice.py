# Copyright 2026 Moritz E. Beber
"""Provide the Subscribe Student slice."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import (
    CourseCapacityChanged,
    CourseRegistered,
    StudentRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class SubscribeStudentSlice(CommandSlice):
    """DCB slice that processes the Subscribe Student command."""

    def __init__(self, course_id: str, student_id: str) -> None:
        self.course_exists = False
        self.capacity = 0
        self.course_subscription_count = 0
        self.course_limit = 0
        self.student_subscription_count = 0
        self.already_subscribed = False
        self._course_id = course_id
        self._student_id = student_id

    def _tags(self) -> list[str]:
        return [f"course:{self._course_id}", f"student:{self._student_id}"]

    def consistency_boundary(self) -> Sequence[Selector]:
        """Return selectors for the course's capacity and student's subscriptions."""
        return [
            Selector(
                types=[
                    CourseRegistered,
                    CourseCapacityChanged,
                    StudentSubscribed,
                    StudentUnsubscribed,
                ],
                tags=[f"course:{self._course_id}"],
            ),
            Selector(
                types=[StudentRegistered, StudentSubscribed, StudentUnsubscribed],
                tags=[f"student:{self._student_id}"],
            ),
        ]

    @event(CourseRegistered)
    def _(self, capacity: int) -> None:
        self.course_exists = True
        self.capacity = capacity

    @event(CourseCapacityChanged)
    def _(self, capacity: int) -> None:
        self.capacity = capacity

    @event(StudentRegistered)
    def _(self, course_limit: int) -> None:
        self.course_limit = course_limit

    @event(StudentSubscribed)
    def _(self, course_id: str, student_id: str) -> None:
        # This event may satisfy either selector on its own (this course, some
        # other student; or this student, some other course) or both at once
        # (this student's subscription to this course) - the three counters
        # are independent, not mutually exclusive.
        if course_id == self._course_id:
            self.course_subscription_count += 1
            if student_id == self._student_id:
                self.already_subscribed = True
        if student_id == self._student_id:
            self.student_subscription_count += 1

    @event(StudentUnsubscribed)
    def _(self, course_id: str, student_id: str) -> None:
        if course_id == self._course_id:
            self.course_subscription_count -= 1
            if student_id == self._student_id:
                self.already_subscribed = False
        if student_id == self._student_id:
            self.student_subscription_count -= 1

    def execute(self) -> None:
        """Validate the subscription and emit a StudentSubscribed event."""
        if not self.course_exists:
            msg = "unknown_course"
            raise ValueError(msg)
        if self.already_subscribed:
            msg = "already_subscribed"
            raise ValueError(msg)
        if self.course_subscription_count >= self.capacity:
            msg = "course_full"
            raise ValueError(msg)
        if self.student_subscription_count >= self.course_limit:
            msg = "subscription_limit_reached"
            raise ValueError(msg)

        self.trigger_event(
            StudentSubscribed,
            self._tags(),
            course_id=self._course_id,
            student_id=self._student_id,
        )
