# Copyright 2026 Moritz E. Beber
"""Provide the Change Course Capacity slice."""

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import (
    CourseCapacityChanged,
    CourseRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)


class ChangeCourseCapacitySlice(CommandSlice):
    """DCB slice that processes the Change Course Capacity command."""

    def __init__(self, course_id: str, capacity: int) -> None:
        self.course_exists = False
        self.current_capacity = 0
        self.subscription_count = 0
        self._course_id = course_id
        self._capacity = capacity

    def _tags(self) -> list[str]:
        return [f"course:{self._course_id}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector scoping replay to this course's history."""
        return Selector(
            types=[
                CourseRegistered,
                CourseCapacityChanged,
                StudentSubscribed,
                StudentUnsubscribed,
            ],
            tags=self._tags(),
        )

    @event(CourseRegistered)
    def _(self, capacity: int) -> None:
        self.course_exists = True
        self.current_capacity = capacity

    @event(CourseCapacityChanged)
    def _(self, capacity: int) -> None:
        self.current_capacity = capacity

    @event(StudentSubscribed)
    def _(self) -> None:
        self.subscription_count += 1

    @event(StudentUnsubscribed)
    def _(self) -> None:
        self.subscription_count -= 1

    def execute(self) -> None:
        """Validate the new capacity and emit a CourseCapacityChanged event."""
        if not self.course_exists:
            msg = "unknown_course"
            raise ValueError(msg)
        if self._capacity == self.current_capacity:
            msg = "same_capacity"
            raise ValueError(msg)
        if self._capacity < self.subscription_count:
            msg = "capacity_below_subscriptions"
            raise ValueError(msg)

        self.trigger_event(
            CourseCapacityChanged,
            self._tags(),
            course_id=self._course_id,
            capacity=self._capacity,
        )
