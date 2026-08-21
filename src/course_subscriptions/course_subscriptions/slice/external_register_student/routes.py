# Copyright 2026 Moritz E. Beber
"""Provide the route for the External Register Student command."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.auth import require_registrar
from course_subscriptions.command import CommandResponse
from course_subscriptions.course_subscriptions.slice.external_register_student.slice import (
    ExternalRegisterStudentSlice,
)

router = APIRouter(tags=["webhooks"])


class StudentRegisteredWebhook(BaseModel):
    """Payload of a Student Registered notification from the registrar."""

    student_id: str
    name: str
    course_limit: int


# 202, not 201: this records the *external* event. The domain's own Register
# Student command is issued afterwards by the automation that follows it, so
# when this response goes out the student is not registered here yet.
@router.post(
    "/webhooks/student-registered",
    dependencies=[Depends(require_registrar)],
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandResponse,
    operation_id="external_register_student",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def external_register_student(
    body: StudentRegisteredWebhook,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CommandResponse | Response:
    """Record a student registration reported by the registrar."""
    slice_ = app.do(ExternalRegisterStudentSlice(**body.model_dump()))
    if slice_.outcome.position is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return CommandResponse(
        event_ids=list(slice_.outcome.event_ids),
        position=slice_.outcome.position,
    )
