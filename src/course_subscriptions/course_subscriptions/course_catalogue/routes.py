# Copyright 2026 Moritz E. Beber
"""Provide the Course Catalogue route."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel

from course_subscriptions.application import CourseSubscriptionsApp, get_application
from course_subscriptions.auth import require_authenticated
from course_subscriptions.course_subscriptions.course_catalogue.projection import (
    CourseCatalogueView,
)
from course_subscriptions.view import (
    VIEW_RESPONSES,
    PositionAtLeast,
    is_behind,
    too_early,
    view_headers,
)

router = APIRouter(tags=["courses"])


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
    dependencies=[Depends(require_authenticated)],
    response_model=CourseCatalogueResponse,
    operation_id="course_catalogue",
    responses=VIEW_RESPONSES,
)
async def course_catalogue(
    response: Response,
    app: Annotated[CourseSubscriptionsApp, Depends(get_application)],
    position_at_least: PositionAtLeast = None,
) -> CourseCatalogueResponse | Response:
    """Return every registered course with its capacity and subscription count."""
    view = app.do(CourseCatalogueView())
    position = view.last_known_position
    if is_behind(position, position_at_least):
        return too_early(position)
    response.headers.update(view_headers(position))
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
