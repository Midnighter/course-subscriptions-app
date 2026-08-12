# Copyright 2026 Moritz E. Beber
"""Provide the domain events shared across the Course Subscriptions context."""

from eventsourcing.pydantic import Decision


class CourseRegistered(Decision):
    """A course was registered with a capacity limit on concurrent subscriptions."""

    course_id: str
    title: str
    capacity: int


class StudentRegistered(Decision):
    """A student registered with a limit on concurrent course subscriptions."""

    student_id: str
    name: str
    course_limit: int


class ExternalStudentRegistered(Decision):
    """
    A student was registered with the external registrar system.

    This is the trigger for the "Register Student" automation. It is a
    distinct event type from `StudentRegistered` (a different id on the
    board, under `EXTERNAL` context) even though both currently carry the
    same fields — one names an external fact, the other this context's own
    outcome.
    """

    student_id: str
    name: str
    course_limit: int


class StudentSubscribed(Decision):
    """A student subscribed to a course."""

    course_id: str
    student_id: str


class StudentUnsubscribed(Decision):
    """A student unsubscribed from a course."""

    course_id: str
    student_id: str


class CourseCapacityChanged(Decision):
    """A course's capacity limit on concurrent subscriptions was changed."""

    course_id: str
    capacity: int
