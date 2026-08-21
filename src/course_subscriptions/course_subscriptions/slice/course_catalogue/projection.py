# Copyright 2026 Moritz E. Beber
"""Provide the Course Catalogue view."""

from dataclasses import dataclass, field

from eventsourcing.domain import event
from eventsourcing.pydantic import Selector, Slice

from course_subscriptions.course_subscriptions.events import (
    CourseCapacityChanged,
    CourseRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)


@dataclass
class CourseCatalogueEntry:
    """The projected state of a single course in the catalogue."""

    title: str = ""
    capacity: int = 0
    subscribers: set[str] = field(default_factory=set)


class CourseCatalogueView(Slice):
    """DCB read-model slice that projects the catalogue of all registered courses."""

    def __init__(self) -> None:
        self.courses: dict[str, CourseCatalogueEntry] = {}

    def _tags(self) -> list[str]:
        # Global boundary: the catalogue lists every registered course, so there
        # is no single entity to scope the replay to.
        return []

    def consistency_boundary(self) -> Selector:
        """Return the selector that defines this view's consistency boundary."""
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
    def _(self, course_id: str, title: str, capacity: int) -> None:
        self.courses[course_id] = CourseCatalogueEntry(title=title, capacity=capacity)

    @event(CourseCapacityChanged)
    def _(self, course_id: str, capacity: int) -> None:
        self.courses[course_id].capacity = capacity

    @event(StudentSubscribed)
    def _(self, course_id: str, student_id: str) -> None:
        self.courses[course_id].subscribers.add(student_id)

    @event(StudentUnsubscribed)
    def _(self, course_id: str, student_id: str) -> None:
        self.courses[course_id].subscribers.discard(student_id)

    def execute(self) -> None:
        """Read-only view: no command to run, no event to emit."""
