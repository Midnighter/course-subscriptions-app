# Copyright 2026 Moritz E. Beber
"""Provide the operational probes an orchestrator polls: liveness and readiness."""

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application

# A runtime import, not a `TYPE_CHECKING` one: this module has no
# `from __future__ import annotations`, and FastAPI resolves the `Annotated`
# parameter annotations below eagerly. Hidden behind the guard it would raise
# `NameError` at import time.
from course_subscriptions.projection import ProjectionSupervisor  # noqa: TC001
from course_subscriptions.view import materialized_position

if TYPE_CHECKING:
    from eventsourcing.persistence import TrackingRecorder

router = APIRouter(tags=["infrastructure"])

_UNAVAILABLE_RESPONSE: dict[int | str, dict[str, Any]] = {
    status.HTTP_503_SERVICE_UNAVAILABLE: {
        "description": "A probed dependency is unavailable; `detail` names each one.",
    },
}


class HealthResponse(BaseModel):
    """Response body for an operational probe."""

    # Closed rather than a bare `str`, so the published spec names the only two
    # answers and a generated client can switch on them. Both routes share the
    # model, so each documents one value it never returns — the alternative is
    # two single-valued schemas for one field, which buys precision nobody
    # polling these routes can spend.
    status: Literal["alive", "ready"]


def get_supervisor(request: Request) -> ProjectionSupervisor:
    """Return the process-wide projection supervisor from FastAPI request state."""
    return request.state.projection_supervisor


def _terminal_failures(supervisor: ProjectionSupervisor) -> dict[str, str] | None:
    """Return every projection that has stopped for good, or None if all run."""
    failures = supervisor.failures()
    if not failures:
        return None
    return {name: str(error) for name, error in failures.items()}


def _connection_key(recorder: TrackingRecorder) -> int:
    """
    Return an identity for the connection a tracking recorder reads through.

    Every Postgres recorder holds a `datastore`, and recorders built by one
    infrastructure factory share it — and so share its pool. Two recorders
    with the same datastore therefore answer the same question, and probing
    both only doubles the cost of finding out. Recorders without one, such as
    the in-memory backend's, stand for themselves.
    """
    return id(getattr(recorder, "datastore", recorder))


def _unreachable_stores(
    app: CourseSubscriptionsApp,
    supervisor: ProjectionSupervisor,
) -> dict[str, str]:
    """
    Probe the event store and every projection's store, naming those that fail.

    Every dependency is probed even once one has failed. An operator reading a
    503 during an outage wants the whole list, not whichever happened to be
    checked first, and each probe is a round trip that has to happen anyway
    before this instance can be called ready.
    """
    unreachable: dict[str, str] = {}
    try:
        # `head()` returning None means the store answered and is empty, which
        # is ready. Only an exception says unreachable.
        app.recorder.head()
    except Exception as exc:  # noqa: BLE001
        unreachable["event_store"] = str(exc)

    seen: set[int] = set()
    for name, recorder in supervisor.tracking_recorders().items():
        key = _connection_key(recorder)
        if key in seen:
            continue
        seen.add(key)
        try:
            # `None` is success here too: the projection has processed nothing
            # yet, which is a lag question and not a readiness one.
            materialized_position(recorder)
        except Exception as exc:  # noqa: BLE001
            # Namespaced, because `_terminal_failures` keys by the bare
            # projection name and one 503 body can carry both kinds.
            unreachable[f"view:{name}"] = str(exc)
    return unreachable


@router.get(
    "/livez",
    response_model=HealthResponse,
    operation_id="livez",
    responses=_UNAVAILABLE_RESPONSE,
)
async def livez(
    supervisor: Annotated[ProjectionSupervisor, Depends(get_supervisor)],
) -> HealthResponse:
    """Report whether this process can still recover on its own."""
    failures = _terminal_failures(supervisor)
    if failures:
        # A runner past `max_restarts` stays dead for the life of the process,
        # and a fresh one resumes from `max_tracking_id` — so a restart is the
        # recovery, not a blunt instrument applied for want of a better idea.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=failures,
        )
    if not supervisor.is_watching():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"watchdog": "the projection watchdog thread is no longer running"},
        )
    return HealthResponse(status="alive")


# Declared `def` rather than `async def` on purpose: every probe below is
# blocking socket I/O, and on the event loop a hung socket would stall every
# request in the process — the same reasoning that makes the supervisor's
# watchdog a thread rather than a task. FastAPI runs a sync handler in the
# threadpool, so a hung probe degrades one worker instead. That is load-bearing
# rather than tidy here: a Postgres tracking read retries internally before it
# gives up, so an unreachable projection store makes this route slow, not
# instantaneous. A docstring would put all this in the published OpenAPI
# description, where it is noise.
@router.get(
    "/readyz",
    response_model=HealthResponse,
    operation_id="readyz",
    responses=_UNAVAILABLE_RESPONSE,
)
def readyz(
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
    supervisor: Annotated[ProjectionSupervisor, Depends(get_supervisor)],
) -> HealthResponse:
    """Report whether this instance can serve correct traffic right now."""
    failures = _terminal_failures(supervisor)
    if failures:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=failures,
        )
    unreachable = _unreachable_stores(app, supervisor)
    if unreachable:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=unreachable,
        )
    return HealthResponse(status="ready")
