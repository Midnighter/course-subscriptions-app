# Copyright 2026 Moritz E. Beber
"""Test the Student Course Subscriptions projection's own logic."""

from course_subscriptions.course_subscriptions.student_course_subscriptions.projection import (  # noqa: E501
    StudentCourseSubscriptionsView,
)


def test_tags_scope_to_the_queried_student() -> None:
    """The consistency boundary is keyed by the student id passed in."""
    view = StudentCourseSubscriptionsView(student_id="STU-2026-0042")
    assert view._tags() == ["student:STU-2026-0042"]  # noqa: SLF001
