# Copyright 2026 Moritz E. Beber
"""Provide the FastAPI application bootstrap."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI

from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.course_subscriptions.change_course_capacity import (
    routes as change_course_capacity_routes,
)
from course_subscriptions.course_subscriptions.course_catalogue import (
    routes as course_catalogue_routes,
)
from course_subscriptions.course_subscriptions.register_course import (
    routes as register_course_routes,
)
from course_subscriptions.course_subscriptions.student_course_subscriptions import (
    routes as student_course_subscriptions_routes,
)
from course_subscriptions.course_subscriptions.subscribe_student import (
    routes as subscribe_student_routes,
)
from course_subscriptions.course_subscriptions.unsubscribe_student import (
    routes as unsubscribe_student_routes,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[dict]:
    """Construct the process-wide application for the lifetime of the app."""
    with CourseSubscriptionsApp() as dcb_app:
        yield {"dcb_app": dcb_app}


def create_app() -> FastAPI:
    """Build the FastAPI application, wiring in every slice's router."""
    app = FastAPI(lifespan=lifespan)
    app.include_router(student_course_subscriptions_routes.router)
    app.include_router(subscribe_student_routes.router)
    app.include_router(unsubscribe_student_routes.router)
    app.include_router(change_course_capacity_routes.router)
    app.include_router(register_course_routes.router)
    app.include_router(course_catalogue_routes.router)
    return app
