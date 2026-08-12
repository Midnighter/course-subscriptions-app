# Copyright 2026 Moritz E. Beber
"""Provide the route for the Unsubscribe Student command."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.command import CommandResponse
from course_subscriptions.course_subscriptions.unsubscribe_student.slice import (
    UnsubscribeStudentSlice,
)

router = APIRouter(tags=["unsubscribe_student"])


class UnsubscribeStudentRequest(BaseModel):
    """Request body for the Unsubscribe Student command."""

    course_id: str


@router.post(
    "/students/{student_id}/unsubscriptions",
    status_code=status.HTTP_201_CREATED,
    response_model=CommandResponse,
    operation_id="unsubscribe_student",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def unsubscribe_student(
    student_id: str,
    body: UnsubscribeStudentRequest,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CommandResponse | Response:
    """Unsubscribe a student from a course they are currently subscribed to."""
    try:
        slice_ = app.do(
            UnsubscribeStudentSlice(student_id=student_id, **body.model_dump()),
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
