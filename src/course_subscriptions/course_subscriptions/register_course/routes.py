# Copyright 2026 Moritz E. Beber
"""Provide the route for the Register Course command."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.command import CommandResponse
from course_subscriptions.course_subscriptions.register_course.slice import (
    RegisterCourseSlice,
)

router = APIRouter(tags=["register_course"])


class RegisterCourseRequest(BaseModel):
    """Request body for the Register Course command."""

    course_id: str
    title: str
    capacity: int


@router.post(
    "/course-registrations",
    status_code=status.HTTP_201_CREATED,
    response_model=CommandResponse,
    operation_id="register_course",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def register_course(
    body: RegisterCourseRequest,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CommandResponse | Response:
    """Register a new course with a capacity limit on concurrent subscriptions."""
    try:
        slice_ = app.do(RegisterCourseSlice(**body.model_dump()))
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
