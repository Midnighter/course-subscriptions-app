# Copyright 2026 Moritz E. Beber
"""Provide the route for the Subscribe Student command."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.auth import require_student_self
from course_subscriptions.command import CommandResponse
from course_subscriptions.course_subscriptions.slice.subscribe_student.slice import (
    SubscribeStudentSlice,
)

router = APIRouter(tags=["students"])


class SubscribeStudentRequest(BaseModel):
    """Request body for the Subscribe Student command."""

    course_id: str


@router.post(
    "/students/{student_id}/subscribe-to-course",
    dependencies=[Depends(require_student_self)],
    status_code=status.HTTP_201_CREATED,
    response_model=CommandResponse,
    operation_id="subscribe_student",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def subscribe_student(
    student_id: str,
    body: SubscribeStudentRequest,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CommandResponse | Response:
    """Subscribe a student to a course, subject to capacity and student limits."""
    try:
        slice_ = app.do(
            SubscribeStudentSlice(student_id=student_id, **body.model_dump()),
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
