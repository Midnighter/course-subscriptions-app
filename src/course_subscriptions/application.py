# Copyright 2026 Moritz E. Beber
"""Provide the process-wide DCB application."""

from eventsourcing.pydantic import DcbApplication
from fastapi import Request  # noqa: TC002


class CourseSubscriptionsApp(DcbApplication):
    """The single, process-wide DCB application."""


def get_application(request: Request) -> CourseSubscriptionsApp:
    """Return the process-wide application from FastAPI request state."""
    return request.state.dcb_app
