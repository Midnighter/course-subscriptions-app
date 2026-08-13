# Copyright 2026 Moritz E. Beber
"""Test the /healthz operational route."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import get_topic

from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.course_subscriptions.events import ExternalStudentRegistered
from course_subscriptions.projection import (
    ProjectionSupervisor,
    SharedAppProjectionRunner,
)

if TYPE_CHECKING:
    from eventsourcing.persistence import Tracking
    from fastapi.testclient import TestClient


class _PoisonProjection(Projection[POPOTrackingRecorder, TaggedEvent[Decision]]):
    """Test double whose mutator raises on every event it is given."""

    name = "poison_projection"
    topics = (get_topic(ExternalStudentRegistered),)

    def process_event(
        self,
        envelope: TaggedEvent[Decision],  # noqa: ARG002
        tracking: Tracking,  # noqa: ARG002
    ) -> None:
        """Raise unconditionally, so the runner dies on the first event."""
        msg = "the poison projection always fails"
        raise RuntimeError(msg)


def test_healthz_reports_ok_when_every_projection_is_healthy(
    client: TestClient,
) -> None:
    """With no projection ever failing, /healthz answers 200."""
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_reports_503_once_a_projection_exceeds_max_restarts(
    client: TestClient,
) -> None:
    """
    /healthz answers 503 and names the projection once it has terminally failed.

    `main.py`'s own automation (Register Student) deliberately swallows every
    exception at its command port (see `RegisterStudentProjection._fire`), by
    design — an automation's targeted guard is there precisely so a poison
    *command* never kills the processing thread. That means the app's own
    wiring can never be driven into a terminal failure through the public
    API, and this test cannot exercise the 503 path through the supervisor
    `main.py` actually constructs.

    Instead, this builds a second, independent, and entirely real
    `ProjectionSupervisor` around a projection whose `process_event` raises
    unconditionally — nothing here is mocked: it is the real supervisor
    class, the real `SharedAppProjectionRunner`, and a real background
    watchdog thread applying the real restart-counting algorithm from
    `projection.py`. With `max_restarts=0` a single genuine exception is
    enough for the supervisor to give up for good.

    That real, failed supervisor is then substituted for the app's own via
    `client.app_state`, the same mutable dict the app's lifespan populates
    and that `conftest.py` already reads (`client.app_state["dcb_app"]`) —
    so the `/healthz` route under test is the genuine one from `main.py`,
    reading `request.state.projection_supervisor` exactly as it does in
    production.
    """
    with CourseSubscriptionsApp() as poison_app:
        view = POPOTrackingRecorder()
        supervisor = ProjectionSupervisor(
            context_name=poison_app.context_name,
            poll_interval=0.1,
        )

        def factory() -> SharedAppProjectionRunner:
            return SharedAppProjectionRunner(
                projection=_PoisonProjection(view=view),
                app=poison_app,
                tracking_recorder=view,
                topics=_PoisonProjection.topics,
            )

        supervisor.register("poison_projection", view, factory, max_restarts=0)

        with supervisor:
            poison_app.events.append(
                events=[
                    TaggedEvent(
                        decision=ExternalStudentRegistered(
                            student_id="poison",
                            name="Poison Student",
                            course_limit=1,
                        ),
                        tags=["student:poison"],
                    ),
                ],
            )

            # The poisoned view never inserts a tracking entry, so it never
            # advances — `wait()` is a blocking, non-busy synchronisation
            # primitive (not `time.sleep`) that gives the watchdog thread
            # real wall-clock time to detect the failure before we assert.
            with pytest.raises(TimeoutError):
                view.wait(
                    context_name=poison_app.context_name,
                    notification_id=1,
                    timeout=2,
                )

            assert "poison_projection" in supervisor.failures()

            client.app_state["projection_supervisor"] = supervisor

            response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert "poison_projection" in body["detail"]
