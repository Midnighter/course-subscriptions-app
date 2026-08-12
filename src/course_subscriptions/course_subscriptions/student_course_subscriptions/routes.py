# Copyright 2026 Moritz E. Beber
"""Provide the Student Course Subscriptions route."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.course_subscriptions.student_course_subscriptions.projection import (  # noqa: E501
    StudentCourseSubscriptionsView,
)

router = APIRouter(tags=["student_course_subscriptions"])


class StudentCourseSubscriptionsResponse(BaseModel):
    """Response body for the Student Course Subscriptions query."""

    student_id: str
    subscription_count: int
    course_limit: int
    courses: list[str]


@router.get(
    "/students/{student_id}/course-subscriptions",
    response_model=StudentCourseSubscriptionsResponse,
    operation_id="student_course_subscriptions",
)
async def student_course_subscriptions(
    student_id: str,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> StudentCourseSubscriptionsResponse:
    """Return a student's current course subscriptions."""
    view = app.do(StudentCourseSubscriptionsView(student_id=student_id))
    return StudentCourseSubscriptionsResponse(
        student_id=student_id,
        subscription_count=view.subscription_count,
        course_limit=view.course_limit,
        courses=view.courses,
    )
