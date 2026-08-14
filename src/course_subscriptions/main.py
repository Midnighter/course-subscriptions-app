# Copyright 2026 Moritz E. Beber
"""Provide the FastAPI application bootstrap."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, Request, status

from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.course_subscriptions.change_course_capacity import (
    routes as change_course_capacity_routes,
)
from course_subscriptions.course_subscriptions.course_catalogue import (
    routes as course_catalogue_routes,
)
from course_subscriptions.course_subscriptions.external_register_student import (
    routes as external_register_student_routes,
)
from course_subscriptions.course_subscriptions.register_course import (
    routes as register_course_routes,
)
from course_subscriptions.course_subscriptions.register_student.projection import (
    create_runner as create_register_student_runner,
)
from course_subscriptions.course_subscriptions.register_student.projection import (
    create_view as create_register_student_view,
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
from course_subscriptions.metadata import MetadataMiddleware
from course_subscriptions.projection import ProjectionSupervisor
from course_subscriptions.telemetry import (
    configure_telemetry,
    instrument_app,
    instrument_recorder,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[dict]:
    """Construct the process-wide application and its projections."""
    async with AsyncExitStack() as stack:
        dcb_app = stack.enter_context(CourseSubscriptionsApp())  # entered FIRST
        logger.info("%r", dcb_app.__class__.__name__)
        logger.info("Context name: %r", dcb_app.context_name)
        logger.info("Recorder type: %r", type(dcb_app.recorder))
        instrument_recorder(dcb_app)  # AFTER construction — recorder is set in __init__
        supervisor = ProjectionSupervisor(context_name=dcb_app.context_name)

        register_student_view = create_register_student_view()
        # Registered BEFORE the supervisor is entered, so it runs AFTER the
        # supervisor stops: closing a connection pool out from under a live
        # projection thread would fail. A no-op on the in-memory backend.
        stack.callback(register_student_view.close)
        supervisor.register(
            "register_student",
            register_student_view,
            partial(create_register_student_runner, dcb_app, register_student_view),
        )

        stack.enter_context(supervisor)  # exits BEFORE the app
        yield {
            "dcb_app": dcb_app,
            "register_student_view": register_student_view,
            "projection_supervisor": supervisor,
        }


def create_app() -> FastAPI:
    """Build the FastAPI application, wiring in every slice's router."""
    configure_telemetry()  # FIRST — `instrument_app` reads the state it sets
    app = FastAPI(lifespan=lifespan)
    instrument_app(app)
    app.add_middleware(MetadataMiddleware)
    app.include_router(student_course_subscriptions_routes.router)
    app.include_router(subscribe_student_routes.router)
    app.include_router(unsubscribe_student_routes.router)
    app.include_router(change_course_capacity_routes.router)
    app.include_router(register_course_routes.router)
    app.include_router(external_register_student_routes.router)
    app.include_router(course_catalogue_routes.router)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        """Report whether every supervised projection is still running."""
        failures = request.state.projection_supervisor.failures()
        if failures:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={name: str(error) for name, error in failures.items()},
            )
        return {"status": "ok"}

    return app
