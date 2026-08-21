# Copyright 2026 Moritz E. Beber
"""Test the Course Catalogue slice."""

from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from course_subscriptions.course_subscriptions.events import (
    CourseCapacityChanged,
    CourseRegistered,
    StudentSubscribed,
    StudentUnsubscribed,
)
from course_subscriptions.course_subscriptions.slice.course_catalogue.projection import (
    CourseCatalogueView,
)

_COURSE_ID = "EM-2024-001"
_TAGS = [f"course:{_COURSE_ID}"]


def _view() -> CourseCatalogueView:
    return CourseCatalogueView()


def test_course_catalogue_with_no_events_is_empty() -> None:
    """Empty catalogue: no courses have been registered."""
    view = _view()
    given().when(view)
    assert view.courses == {}


def test_course_catalogue_shows_title_and_capacity() -> None:
    """Courses are shown with their title and capacity."""
    registered = TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=10,
        ),
        tags=_TAGS,
    )
    view = _view()
    given(registered).when(view)
    entry = view.courses[_COURSE_ID]
    assert entry.title == "Intro to Event Modeling"
    assert entry.capacity == 10
    assert entry.subscribers == set()


def test_course_catalogue_accounts_for_capacity_changes() -> None:
    """A capacity change is reflected in the catalogue."""
    registered = TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=10,
        ),
        tags=_TAGS,
    )
    capacity_changed = TaggedEvent(
        decision=CourseCapacityChanged(course_id=_COURSE_ID, capacity=2),
        tags=_TAGS,
    )
    view = _view()
    given(registered, capacity_changed).when(view)
    assert view.courses[_COURSE_ID].capacity == 2


def test_course_catalogue_counts_subscriptions() -> None:
    """The subscription count reflects currently subscribed students."""
    registered = TaggedEvent(
        decision=CourseRegistered(
            course_id=_COURSE_ID,
            title="Intro to Event Modeling",
            capacity=5,
        ),
        tags=_TAGS,
    )
    first_subscribed = TaggedEvent(
        decision=StudentSubscribed(course_id=_COURSE_ID, student_id="STU-2026-0042"),
        tags=_TAGS,
    )
    unsubscribed = TaggedEvent(
        decision=StudentUnsubscribed(
            course_id=_COURSE_ID,
            student_id="STU-2026-0042",
        ),
        tags=_TAGS,
    )
    second_subscribed = TaggedEvent(
        decision=StudentSubscribed(course_id=_COURSE_ID, student_id="STU-2026-0043"),
        tags=_TAGS,
    )
    third_subscribed = TaggedEvent(
        decision=StudentSubscribed(course_id=_COURSE_ID, student_id="STU-2026-0044"),
        tags=_TAGS,
    )
    view = _view()
    given(
        registered,
        first_subscribed,
        unsubscribed,
        second_subscribed,
        third_subscribed,
    ).when(view)
    assert len(view.courses[_COURSE_ID].subscribers) == 2
