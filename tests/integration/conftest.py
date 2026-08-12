# Copyright 2026 Moritz E. Beber
"""Provide shared fixtures for the integration test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from course_subscriptions.main import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from course_subscriptions.application import CourseSubscriptionsApp


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Run the app's lifespan and expose a TestClient bound to it."""
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def dcb_app(client: TestClient) -> CourseSubscriptionsApp:
    """Return the process-wide application backing the test client."""
    return client.app_state["dcb_app"]
