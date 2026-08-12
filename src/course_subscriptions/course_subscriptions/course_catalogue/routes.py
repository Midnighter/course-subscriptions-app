# Copyright 2026 Moritz E. Beber
"""Provide the Course Catalogue route."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.course_subscriptions.course_catalogue.projection import (
    CourseCatalogueView,
)

router = APIRouter(tags=["course_catalogue"])


class CourseCatalogueEntryResponse(BaseModel):
    """Response body for a single course in the catalogue."""

    course_id: str
    title: str
    capacity: int
    number_of_subscriptions: int


class CourseCatalogueResponse(BaseModel):
    """Response body for the Course Catalogue query."""

    courses: list[CourseCatalogueEntryResponse]


@router.get(
    "/course-catalogue",
    response_model=CourseCatalogueResponse,
    operation_id="course_catalogue",
)
async def course_catalogue(
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
) -> CourseCatalogueResponse:
    """Return every registered course with its capacity and subscription count."""
    view = app.do(CourseCatalogueView())
    return CourseCatalogueResponse(
        courses=[
            CourseCatalogueEntryResponse(
                course_id=course_id,
                title=entry.title,
                capacity=entry.capacity,
                number_of_subscriptions=len(entry.subscribers),
            )
            for course_id, entry in view.courses.items()
        ],
    )
