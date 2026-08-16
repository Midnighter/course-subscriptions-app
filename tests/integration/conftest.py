# Copyright 2026 Moritz E. Beber
"""Provide shared fixtures for the integration test suite."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from course_subscriptions.auth import (
    COURSES_MANAGE,
    REGISTRATIONS_WRITE,
    SUBSCRIPTIONS_WRITE,
    PrincipalType,
)
from course_subscriptions.main import create_app

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from course_subscriptions.application import CourseSubscriptionsApp

COURSE_MANAGER_ID = "MGR-2026-0001"
REGISTRAR_ID = "registrar"


def _auth(principal_id: str, principal_type: PrincipalType, *scopes: str) -> dict:
    """
    Build the `Authorization` header a caller of that shape would send.

    Kept here rather than spelled out per test so the fake token's format lives
    in one place beside `parse_token`, which is the only production code that
    knows it.

    Args:
        principal_id: The subject the token names.
        principal_type: Whether that subject is a person or a machine.
        *scopes: The permissions the token grants.

    Returns:
        Request headers carrying the bearer token.

    """
    claims = (principal_id, principal_type.value, " ".join(scopes))
    return {"Authorization": f"Bearer {'|'.join(claims)}"}


@pytest.fixture
def client() -> Iterator[TestClient]:
    """
    Run the app's lifespan and expose a TestClient bound to it.

    Deliberately anonymous. Every authenticated request passes its own headers,
    so a reader of any one test can see which actor it speaks for.
    """
    with TestClient(create_app()) as client:
        yield client


@pytest.fixture
def manager_auth() -> dict:
    """Return the headers of a course manager."""
    return _auth(COURSE_MANAGER_ID, PrincipalType.USER, COURSES_MANAGE)


@pytest.fixture
def student_auth() -> Callable[[str], dict]:
    """
    Return a factory for a student's headers, given the student's id.

    A factory rather than a value because a student's `principal_id` *is* their
    `student_id`, and each test names its own.
    """

    def _student_auth(student_id: str) -> dict:
        return _auth(student_id, PrincipalType.USER, SUBSCRIPTIONS_WRITE)

    return _student_auth


@pytest.fixture
def registrar_auth() -> dict:
    """Return the headers of the registrar's machine credential."""
    return _auth(REGISTRAR_ID, PrincipalType.SERVICE, REGISTRATIONS_WRITE)


@pytest.fixture
def registrar_id() -> str:
    """Return the id the registrar's token names, for asserting on metadata."""
    return REGISTRAR_ID


@pytest.fixture
def dcb_app(client: TestClient) -> CourseSubscriptionsApp:
    """Return the process-wide application backing the test client."""
    return client.app_state["dcb_app"]
