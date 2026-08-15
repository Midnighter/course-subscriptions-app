# Copyright 2026 Moritz E. Beber
"""Provide the FastAPI application bootstrap."""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import TYPE_CHECKING

from fastapi import FastAPI

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
from course_subscriptions.health import router as health_router
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
        # Entered FIRST so it closes last: the projections below run over this
        # store, and tearing it down while a projection thread is still writing
        # would fail. No reachability probe here — a backend that cannot open
        # its store raises out of this line, which is the fail-fast we want, and
        # one that connects lazily is an outage for `/readyz` to report rather
        # than a reason to refuse to start.
        dcb_app = stack.enter_context(CourseSubscriptionsApp())
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
    # Registration order is load-bearing: Starlette serves the first route whose
    # pattern fully matches, so a path parameter registered ahead of a literal it
    # can match silently swallows that literal's requests and answers them from
    # the wrong handler — no startup error, no log line. Append new slices at the
    # end of this block; never reorder what is already here. The addresses below
    # are chosen so that no such collision exists (see `.build-kit/CLAUDE.md` →
    # "Never let a literal segment sit where a path parameter could match it"),
    # so keep it that way rather than making the order carry the correctness.
    app.include_router(student_course_subscriptions_routes.router)
    app.include_router(subscribe_student_routes.router)
    app.include_router(unsubscribe_student_routes.router)
    app.include_router(change_course_capacity_routes.router)
    app.include_router(register_course_routes.router)
    app.include_router(external_register_student_routes.router)
    app.include_router(course_catalogue_routes.router)
    # `/livez` and `/readyz` are root-level literals no slice path can shadow,
    # so this line is safe anywhere in the block; it sits last to keep the
    # append-only rule above unqualified.
    app.include_router(health_router)

    return app
