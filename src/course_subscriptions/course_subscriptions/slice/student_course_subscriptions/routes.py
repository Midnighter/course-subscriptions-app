# Copyright 2026 Moritz E. Beber
"""Provide the Student Course Subscriptions route."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.auth import require_student_self
from course_subscriptions.course_subscriptions.slice.student_course_subscriptions.projection import (  # noqa: E501
    StudentCourseSubscriptionsView,
)
from course_subscriptions.view import (
    VIEW_RESPONSES,
    PositionAtLeast,
    is_behind,
    too_early,
    view_headers,
)

router = APIRouter(tags=["students"])


class StudentCourseSubscriptionsResponse(BaseModel):
    """Response body for the Student Course Subscriptions query."""

    student_id: str
    subscription_count: int
    course_limit: int
    courses: list[str]


@router.get(
    "/students/{student_id}/course-subscriptions",
    dependencies=[Depends(require_student_self)],
    response_model=StudentCourseSubscriptionsResponse,
    operation_id="student_course_subscriptions",
    responses=VIEW_RESPONSES,
)
async def student_course_subscriptions(
    student_id: str,
    response: Response,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
    position_at_least: PositionAtLeast = None,
) -> StudentCourseSubscriptionsResponse | Response:
    """Return a student's current course subscriptions."""
    view = app.do(StudentCourseSubscriptionsView(student_id=student_id))
    position = view.last_known_position
    if is_behind(position, position_at_least):
        return too_early(position)
    response.headers.update(view_headers(position))
    return StudentCourseSubscriptionsResponse(
        student_id=student_id,
        subscription_count=view.subscription_count,
        course_limit=view.course_limit,
        courses=view.courses,
    )
