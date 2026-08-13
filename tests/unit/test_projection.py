# Copyright 2026 Moritz E. Beber
"""Test the shared projection runtime and per-slice projection logic."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eventsourcing.domain import TaggedEvent
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import get_topic

from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.course_subscriptions.events import ExternalStudentRegistered
from course_subscriptions.course_subscriptions.student_course_subscriptions.projection import (  # noqa: E501
    StudentCourseSubscriptionsView,
)
from course_subscriptions.projection import SharedAppProjectionRunner

if TYPE_CHECKING:
    from eventsourcing.persistence import Tracking


def test_tags_scope_to_the_queried_student() -> None:
    """The consistency boundary is keyed by the student id passed in."""
    view = StudentCourseSubscriptionsView(student_id="STU-2026-0042")
    assert view._tags() == ["student:STU-2026-0042"]  # noqa: SLF001


class _InertProjection(Projection[POPOTrackingRecorder, TaggedEvent[Decision]]):
    """A projection that records tracking and mutates nothing."""

    name = "inert_projection"
    topics = (get_topic(ExternalStudentRegistered),)

    def process_event(
        self,
        envelope: TaggedEvent[Decision],  # noqa: ARG002
        tracking: Tracking,
    ) -> None:
        """Persist the tracking position and do nothing else."""
        self.view.insert_tracking(tracking)


def test_runner_exit_leaves_the_shared_application_open() -> None:
    """
    Exiting a runner must not close the application it was handed.

    This is the upgrade tripwire for ``SharedAppProjectionRunner.__exit__``,
    which reproduces ``BaseProjectionRunner.__exit__`` minus its
    unconditional ``self.app.close()``. That override reaches into four
    private attributes of an alpha library, so a version bump can silently
    reintroduce the close.

    Under POPO ``close()`` is a no-op, which is exactly why the assertion
    cannot be "the app still works" alone — the regression would be
    invisible until the project moved to Postgres, where the closed
    connection pool cannot be reopened and every later request raises
    ``PoolClosed``. So the call itself is what is asserted against, and the
    still-usable check is kept as a second, weaker guard.
    """
    with CourseSubscriptionsApp() as app:
        closed: list[bool] = []
        original_close = app.close

        def _record_close() -> None:
            closed.append(True)
            original_close()

        app.close = _record_close  # type: ignore[method-assign]

        view = POPOTrackingRecorder()
        runner = SharedAppProjectionRunner(
            projection=_InertProjection(view=view),
            app=app,
            tracking_recorder=view,
            topics=_InertProjection.topics,
        )
        with runner:
            pass

        assert closed == [], "runner exit closed the shared application"

        app.close = original_close  # type: ignore[method-assign]
        position = app.events.append(
            events=[
                TaggedEvent(
                    decision=ExternalStudentRegistered(
                        student_id="STU-2026-0043",
                        name="Still Open",
                        course_limit=1,
                    ),
                    tags=["student:STU-2026-0043"],
                ),
            ],
        )
        assert position is not None
