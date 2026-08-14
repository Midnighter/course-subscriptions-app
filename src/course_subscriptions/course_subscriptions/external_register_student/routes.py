# Copyright 2026 Moritz E. Beber
"""Provide the route for the External Register Student command."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.command import CommandResponse
from course_subscriptions.course_subscriptions.external_register_student.slice import (
    ExternalRegisterStudentSlice,
)

router = APIRouter(tags=["external_register_student"])


class RegisterStudentRequest(BaseModel):
    """Request body for the External Register Student command."""

    student_id: str
    name: str
    course_limit: int


@router.post(
    "/student-registrations",
    status_code=status.HTTP_201_CREATED,
    response_model=CommandResponse,
    operation_id="external_register_student",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def external_register_student(
    body: RegisterStudentRequest,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CommandResponse | Response:
    """Record an external student registration, triggering the automation."""
    slice_ = app.do(ExternalRegisterStudentSlice(**body.model_dump()))
    if slice_.outcome.position is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return CommandResponse(
        event_ids=list(slice_.outcome.event_ids),
        position=slice_.outcome.position,
    )
