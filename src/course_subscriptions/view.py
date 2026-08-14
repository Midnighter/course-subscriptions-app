# Copyright 2026 Moritz E. Beber
"""Provide the read side's counterpart to `command.py`: view position reporting."""

from typing import Annotated, Any

from eventsourcing.persistence import TrackingRecorder  # noqa: TC002
from fastapi import Header, Response, status

from course_subscriptions.application import CourseSubscriptionsApp

CURRENT_POSITION_HEADER = "X-Current-Position"
POSITION_AT_LEAST_HEADER = "X-Position-AtLeast"

PositionAtLeast = Annotated[
    int | None,
    Header(
        alias=POSITION_AT_LEAST_HEADER,
        ge=0,
        description="Answer only once the view has reached at least this position.",
    ),
]

_POSITION_HEADER_SPEC: dict[str, Any] = {
    CURRENT_POSITION_HEADER: {
        "description": (
            "Position of the last event this view reflects. Absent "
            "when the view has processed nothing."
        ),
        "schema": {"type": "integer"},
    },
}

VIEW_RESPONSES: dict[int | str, dict[str, Any]] = {
    status.HTTP_200_OK: {"headers": _POSITION_HEADER_SPEC},
    status.HTTP_425_TOO_EARLY: {
        "description": f"The view has not reached {POSITION_AT_LEAST_HEADER}.",
        "headers": _POSITION_HEADER_SPEC,
    },
}

NOT_FOUND_RESPONSE: dict[str, Any] = {
    "description": "This view holds no such entity.",
    "headers": _POSITION_HEADER_SPEC,
}


def materialized_position(view: TrackingRecorder) -> int | None:
    """Return the position of the last event a materialized view processed."""
    return view.max_tracking_id(CourseSubscriptionsApp.context_name)


def is_behind(current: int | None, at_least: int | None) -> bool:
    """Return whether a caller's required position has not been reached."""
    if at_least is None:
        return False
    return current is None or current < at_least


def view_headers(current: int | None) -> dict[str, str]:
    """Return the headers every view response carries, position included."""
    headers = {"Cache-Control": "no-store"}
    if current is not None:
        headers[CURRENT_POSITION_HEADER] = str(current)
    return headers


def too_early(current: int | None) -> Response:
    """Return an empty 425 telling the caller the view has not caught up."""
    return Response(
        status_code=status.HTTP_425_TOO_EARLY,
        headers=view_headers(current),
    )
