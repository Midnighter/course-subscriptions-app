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


class StudentSubscribed(Decision):
    """A student subscribed to a course."""

    course_id: str
    student_id: str


class StudentUnsubscribed(Decision):
    """A student unsubscribed from a course."""

    course_id: str
    student_id: str
