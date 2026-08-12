# Copyright 2026 Moritz E. Beber
"""Test the Student Course Subscriptions slice."""

from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    StudentRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)
from course_subscriptions.course_subscriptions.student_course_subscriptions.projection import (  # noqa: E501
    StudentCourseSubscriptionsView,
)

_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"student:{_STUDENT_ID}"]


def _view() -> StudentCourseSubscriptionsView:
    return StudentCourseSubscriptionsView(student_id=_STUDENT_ID)


def test_student_course_subscriptions_with_no_events() -> None:
    """No prior events: the student has no course subscriptions."""
    view = _view()
    given().when(view)
    assert view.subscription_count == 0


def test_student_course_subscriptions_with_one_subscription() -> None:
    """One subscription: the count reflects the single course."""
    prior = TaggedEvent(
        decision=StudentSubscribed(course_id="EM-2024-001", student_id=_STUDENT_ID),
        tags=_TAGS,
    )
    view = _view()
    given(prior).when(view)
    assert view.subscription_count == 1
    assert view.courses == ["EM-2024-001"]


def test_student_course_subscriptions_with_two_subscriptions() -> None:
    """Two subscriptions: the count reflects both courses."""
    first = TaggedEvent(
        decision=StudentSubscribed(course_id="EM-2024-001", student_id=_STUDENT_ID),
        tags=_TAGS,
    )
    second = TaggedEvent(
        decision=StudentSubscribed(course_id="EX-2025-011", student_id=_STUDENT_ID),
        tags=_TAGS,
    )
    view = _view()
    given(first, second).when(view)
    assert view.subscription_count == 2
    assert view.courses == ["EM-2024-001", "EX-2025-011"]


def test_student_course_subscriptions_after_unsubscribing() -> None:
    """Unsubscribing removes the course from the count."""
    subscribed = TaggedEvent(
        decision=StudentSubscribed(course_id="EM-2024-001", student_id=_STUDENT_ID),
        tags=_TAGS,
    )
    unsubscribed = TaggedEvent(
        decision=StudentUnsubscribed(course_id="EM-2024-001", student_id=_STUDENT_ID),
        tags=_TAGS,
    )
    view = _view()
    given(subscribed, unsubscribed).when(view)
    assert view.subscription_count == 0
    assert view.courses == []


def test_student_course_subscriptions_projects_course_limit() -> None:
    """Registration sets the student's course limit."""
    registered = TaggedEvent(
        decision=StudentRegistered(
            student_id=_STUDENT_ID,
            name="Ada Lovelace",
            course_limit=2,
        ),
        tags=_TAGS,
    )
    view = _view()
    given(registered).when(view)
    assert view.course_limit == 2
