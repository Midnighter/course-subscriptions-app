# Copyright 2026 Moritz E. Beber
"""Test the /livez and /readyz operational routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import get_topic
from fastapi import status

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


def test_livez_reports_alive_when_the_watchdog_is_running(client: TestClient) -> None:
    """With the supervisor watching and no projection failed, /livez answers 200."""
    response = client.get("/livez")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "alive"}


def test_readyz_reports_ready_when_the_store_answers(client: TestClient) -> None:
    """
    With the store reachable and no projection failed, /readyz answers 200.

    The store behind a freshly built app holds nothing, so `head()` returns
    `None` here — which is the point: an empty store is reachable, and only an
    exception means unreachable. This pins that the two are not conflated.
    """
    assert client.app_state["dcb_app"].recorder.head() is None

    response = client.get("/readyz")

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {"status": "ready"}


def test_healthz_is_gone(client: TestClient) -> None:
    """The single conflated probe was removed, not merely superseded."""
    assert client.get("/healthz").status_code == status.HTTP_404_NOT_FOUND


def test_readyz_reports_503_when_the_event_store_becomes_unreachable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A store that goes away drains the instance but must not restart it.

    This is one half of the asymmetry the split exists for. Restarting a
    replica does not bring the store back, so /livez keeps answering 200 while
    /readyz takes this instance out of rotation.
    """

    def unreachable() -> int | None:
        msg = "connection refused"
        raise ConnectionError(msg)

    monkeypatch.setattr(client.app_state["dcb_app"].recorder, "head", unreachable)

    response = client.get("/readyz")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "connection refused" in response.json()["detail"]["event_store"]
    assert client.get("/livez").status_code == status.HTTP_200_OK


def _unreachable_recorder(name: str) -> POPOTrackingRecorder:
    """Return a real tracking recorder whose position cannot be read."""
    recorder = POPOTrackingRecorder()

    def unreachable(context_name: str) -> int | None:  # noqa: ARG001
        msg = f"{name}: connection refused"
        raise ConnectionError(msg)

    recorder.max_tracking_id = unreachable  # type: ignore[method-assign]
    return recorder


def _registering(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    **recorders: POPOTrackingRecorder,
) -> None:
    """
    Make the app's own supervisor report these recorders as its registrations.

    The live supervisor is kept rather than swapped for one built here, because
    its watchdog thread is running and a substitute's would not be — /livez
    would then 503 for a reason the test never intended, and the very asymmetry
    under test would be invisible. Only what readiness enumerates is replaced.
    """
    supervisor = client.app_state["projection_supervisor"]
    monkeypatch.setattr(supervisor, "tracking_recorders", lambda: dict(recorders))


def test_readyz_reports_503_when_a_projection_store_is_unreachable(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A projection's own store going away drains the instance, and only drains it.

    Each projection resolves its own infrastructure, so its store is a distinct
    dependency from the event store — and one the event store's `head()` cannot
    speak for. The instance is unready because a view it serves cannot be read;
    it is not unhealthy, because restarting the process would not bring that
    store back either. Same asymmetry as the event store, one dependency over.
    """
    _registering(
        client,
        monkeypatch,
        register_student=_unreachable_recorder("register_student"),
    )

    response = client.get("/readyz")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "connection refused" in response.json()["detail"]["view:register_student"]
    assert client.get("/livez").status_code == status.HTTP_200_OK


def test_readyz_names_every_unreachable_dependency(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    One 503 carries the whole list, not whichever probe happened to run first.

    An operator reading this response during an outage is trying to find out
    what is down. Short-circuiting on the first failure would name one
    dependency and stay silent about the rest, which reads as "only this one
    broke" — the most expensive thing a probe can imply while wrong.
    """

    def unreachable() -> int | None:
        msg = "event store is gone"
        raise ConnectionError(msg)

    monkeypatch.setattr(client.app_state["dcb_app"].recorder, "head", unreachable)
    _registering(
        client,
        monkeypatch,
        register_student=_unreachable_recorder("register_student"),
    )

    detail = client.get("/readyz").json()["detail"]

    assert "event store is gone" in detail["event_store"]
    assert "connection refused" in detail["view:register_student"]


def test_readyz_probes_each_distinct_store_once(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Two projections sharing a connection are one round trip, not two.

    Recorders built by one infrastructure factory share its datastore and pool,
    so probing both asks the same question twice — paid for in latency on a
    route an orchestrator polls every few seconds, and paid twice over when the
    store is down and each probe has to time out before it answers.
    """
    calls: list[str] = []
    shared = POPOTrackingRecorder()

    def counted(context_name: str) -> int | None:
        calls.append(context_name)
        return None

    shared.max_tracking_id = counted  # type: ignore[method-assign]

    _registering(client, monkeypatch, register_student=shared, some_later_view=shared)

    assert client.get("/readyz").status_code == status.HTTP_200_OK
    assert calls == ["subscriptions"]


def test_livez_reports_503_when_the_watchdog_thread_has_died(
    client: TestClient,
) -> None:
    """
    A supervisor that has stopped watching is unrecoverable in place.

    This is the other half of the asymmetry. Nothing will restart a projection
    that dies from here on, and `failures()` stays empty through the rot
    because nothing is left to observe a death — so only a restart clears it.
    The runners still alive are serving current views, so /readyz stays 200.

    Exiting a real supervisor joins its watchdog thread, which is exactly the
    state under test; no mock is involved.
    """
    stopped = ProjectionSupervisor(context_name="subscriptions", poll_interval=0.1)
    with stopped:
        pass  # leaving the block joins the watchdog for good
    assert not stopped.is_watching()

    client.app_state["projection_supervisor"] = stopped

    response = client.get("/livez")

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "watchdog" in response.json()["detail"]
    assert client.get("/readyz").status_code == status.HTTP_200_OK


def test_both_probes_report_503_once_a_projection_exceeds_max_restarts(
    client: TestClient,
) -> None:
    """
    A terminally failed projection fails both probes, and should.

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
    so the routes under test are the genuine ones from `health.py`, reading
    `request.state.projection_supervisor` exactly as they do in production.

    Both probes fail here on purpose: the view is frozen at a known position,
    so this replica must stop taking traffic, and only a fresh process will
    rebuild the runner from `max_tracking_id`.
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

            live = client.get("/livez")
            ready = client.get("/readyz")

    assert live.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "poison_projection" in live.json()["detail"]
    assert ready.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert "poison_projection" in ready.json()["detail"]
