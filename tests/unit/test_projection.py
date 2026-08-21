# Copyright 2026 Moritz E. Beber
"""Test the shared projection runtime and per-slice projection logic."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Self

from eventsourcing.domain import TaggedEvent
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import get_topic

from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.course_subscriptions.events import ExternalStudentRegistered
from course_subscriptions.course_subscriptions.slice.student_course_subscriptions.projection import (  # noqa: E501
    StudentCourseSubscriptionsView,
)
from course_subscriptions.projection import (
    ProjectionSupervisor,
    SharedAppProjectionRunner,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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


class _FakeRunner:
    """Stands in for a runner, recording entry and exit and dying on demand."""

    def __init__(self, *, dies: bool = True) -> None:
        self.dies = dies
        self.entered = False
        self.exited = False

    def __enter__(self) -> Self:
        self.entered = True
        return self

    def __exit__(self, *_: object) -> None:
        self.exited = True

    def run_forever(self, timeout: int = 0) -> None:  # noqa: ARG002
        """Re-raise a stored thread error, as the real runner does."""
        if self.dies:
            msg = "the processing thread died"
            raise RuntimeError(msg)


class _FlakyRecorder:
    """A tracking recorder whose position is unreadable for the first N reads."""

    def __init__(self, unreadable: int, position: int | None = None) -> None:
        self.unreadable = unreadable
        self.position = position
        self.reads = 0

    def max_tracking_id(self, context_name: str) -> int | None:  # noqa: ARG002
        """Raise while the store is 'down', then answer."""
        self.reads += 1
        if self.reads <= self.unreadable:
            msg = "connection refused"
            raise ConnectionError(msg)
        return self.position


def _supervise(
    recorder: _FlakyRecorder,
    runners: list[_FakeRunner],
    max_restarts: int = 3,
) -> ProjectionSupervisor:
    """Register one projection that hands out the given runners in order."""
    supervisor = ProjectionSupervisor(context_name="subscriptions", poll_interval=0.01)
    handed = iter(runners)

    def factory() -> SharedAppProjectionRunner:
        return next(handed)  # type: ignore[return-value]

    supervisor.register("flaky", recorder, factory, max_restarts=max_restarts)  # type: ignore[arg-type]
    return supervisor


def _until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Poll a predicate until it holds or the timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_watchdog_survives_an_unreadable_tracking_position() -> None:
    """
    A projection store that is down must not take the watchdog with it.

    The tracking ledger lives in the same store the runner reads through, so
    the read that counts a restart fails for exactly the same reason the
    runner did. Before this was guarded, that exception escaped `_watch` and
    killed the thread — and `failures()` then stayed empty forever, because
    nothing was left to observe a death. The rot was invisible to both probes:
    every projection silently frozen, every health check reporting fine.
    """
    recorder = _FlakyRecorder(unreadable=1_000)
    runners = [_FakeRunner() for _ in range(50)]
    supervisor = _supervise(recorder, runners)

    with supervisor:
        assert _until(lambda: recorder.reads >= 3)

        assert supervisor.is_watching(), "the watchdog died on its own bookkeeping"


def test_an_unreadable_position_does_not_burn_the_restart_budget() -> None:
    """
    An outage is not evidence that a projection is poisoned.

    Restarts are counted by tracking position precisely to tell progress from
    a stuck poison event — and with the position unreadable, neither can be
    established. Counting anyway would spend the budget on an outage and latch
    a terminal failure that no restart clears, so the projection would stay
    dead long after the store came back.
    """
    recorder = _FlakyRecorder(unreadable=1_000)
    runners = [_FakeRunner() for _ in range(50)]
    supervisor = _supervise(recorder, runners, max_restarts=1)

    with supervisor:
        assert _until(lambda: recorder.reads >= 5)

        assert supervisor.failures() == {}
        assert supervisor.is_watching()


def test_a_projection_resumes_once_its_store_answers_again() -> None:
    """
    Recovery is unattended: the watchdog keeps rebuilding until it takes.

    This is what the two tests above buy. The store returns, the position
    reads, and the next tick hands out a runner that stays up — with no
    restart of the process and no operator involved.
    """
    recorder = _FlakyRecorder(unreadable=3, position=7)
    runners = [*[_FakeRunner() for _ in range(4)], _FakeRunner(dies=False)]
    supervisor = _supervise(recorder, runners)

    with supervisor:
        assert _until(lambda: runners[-1].entered)

        assert supervisor.failures() == {}
        assert supervisor.is_watching()


def test_a_stuck_position_still_latches_a_terminal_failure() -> None:
    """
    A readable but unmoving position is a poison event, and still gives up.

    The guard above must not become a blanket retry. When the store answers,
    an unchanged position means the projection died on the same event it died
    on last time, so the restart budget applies exactly as before.
    """
    recorder = _FlakyRecorder(unreadable=0, position=7)
    runners = [_FakeRunner() for _ in range(50)]
    supervisor = _supervise(recorder, runners, max_restarts=2)

    with supervisor:
        assert _until(lambda: "flaky" in supervisor.failures())

        assert supervisor.is_watching()


def test_a_dead_runner_is_stopped_before_it_is_replaced() -> None:
    """
    Each rebuild releases the threads of the runner it replaces.

    An unreadable position retries indefinitely, so a long outage means one
    rebuild per tick. Dropping the reference without exiting would leak two
    threads each time and turn a recoverable outage into an exhausted process.
    """
    recorder = _FlakyRecorder(unreadable=1_000)
    runners = [_FakeRunner() for _ in range(50)]
    supervisor = _supervise(recorder, runners)

    with supervisor:
        assert _until(lambda: runners[1].entered)

    assert runners[0].exited, "the dead runner was dropped without being stopped"


def test_a_runner_that_cannot_be_rebuilt_is_retried() -> None:
    """
    A factory that raises is a failure to count, not one that escapes.

    Rebuilding happens inside `_watch`'s guard, so a store that is down when
    the runner is constructed — a pool that will not open, say — is handled by
    the same path as a runner that dies later, rather than killing the thread.
    """
    recorder = _FlakyRecorder(unreadable=1_000)
    attempts: list[int] = []
    supervisor = ProjectionSupervisor(context_name="subscriptions", poll_interval=0.01)

    def factory() -> SharedAppProjectionRunner:
        attempts.append(1)
        if len(attempts) < 3:
            msg = "cannot open a connection pool"
            raise ConnectionError(msg)
        return _FakeRunner(dies=False)  # type: ignore[return-value]

    supervisor.register("flaky", recorder, factory)  # type: ignore[arg-type]

    # Entered by hand: `__enter__` builds every runner eagerly and this one
    # raises on the first two calls, which is the failure under test.
    supervisor._registrations["flaky"].runner = None  # noqa: SLF001
    supervisor._watchdog_thread.start()  # noqa: SLF001
    try:
        assert _until(lambda: len(attempts) >= 3)
        assert supervisor.is_watching()
        assert supervisor.failures() == {}
    finally:
        supervisor._stop_event.set()  # noqa: SLF001
        supervisor._watchdog_thread.join()  # noqa: SLF001
