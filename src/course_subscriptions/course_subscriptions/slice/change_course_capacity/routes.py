# Copyright 2026 Moritz E. Beber
"""Provide the route for the Change Course Capacity command."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.auth import require_course_manager
from course_subscriptions.command import CommandResponse
from course_subscriptions.course_subscriptions.slice.change_course_capacity.slice import (  # noqa: E501
    ChangeCourseCapacitySlice,
)

router = APIRouter(tags=["courses"])


class ChangeCourseCapacityRequest(BaseModel):
    """Request body for the Change Course Capacity command."""

    capacity: int


@router.post(
    "/courses/{course_id}/change-capacity",
    dependencies=[Depends(require_course_manager)],
    status_code=status.HTTP_201_CREATED,
    response_model=CommandResponse,
    operation_id="change_course_capacity",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def change_course_capacity(
    course_id: str,
    body: ChangeCourseCapacityRequest,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CommandResponse | Response:
    """Change the capacity limit on concurrent subscriptions for a course."""
    try:
        slice_ = app.do(
            ChangeCourseCapacitySlice(course_id=course_id, **body.model_dump()),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if slice_.outcome.position is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return CommandResponse(
        event_ids=list(slice_.outcome.event_ids),
        position=slice_.outcome.position,
    )
