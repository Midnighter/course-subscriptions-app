# Copyright 2026 Moritz E. Beber
"""Test the External Register Student route."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi.testclient import TestClient

_STUDENT_ID = "STU-2026-0042"
_URL = "/webhooks/student-registered"
_BODY = {
    "student_id": _STUDENT_ID,
    "name": "Anna Müller",
    "course_limit": 2,
}


def test_external_register_student_route_returns_202(
    client: TestClient,
    registrar_auth: dict,
) -> None:
    """Recording a registration via the route returns HTTP 202."""
    response = client.post(_URL, json=_BODY, headers=registrar_auth)
    assert response.status_code == 202
    body = response.json()
    assert body["position"] is not None
    assert len(body["event_ids"]) == 1


def test_external_register_student_route_is_unconditional(
    client: TestClient,
    registrar_auth: dict,
) -> None:
    """A repeat submission for the same student still returns HTTP 202."""
    first = client.post(_URL, json=_BODY, headers=registrar_auth)
    second = client.post(_URL, json=_BODY, headers=registrar_auth)

    assert first.status_code == 202
    assert second.status_code == 202


def test_external_register_student_route_missing_field_returns_422(
    client: TestClient,
    registrar_auth: dict,
) -> None:
    """A request missing a required field returns HTTP 422."""
    response = client.post(
        _URL,
        json={"student_id": _STUDENT_ID},
        headers=registrar_auth,
    )
    assert response.status_code == 422


def test_external_register_student_without_a_token_returns_401(
    client: TestClient,
) -> None:
    """
    An anonymous caller cannot report a registration.

    This is the one route that mints students out of nothing, so an open
    webhook would let anybody enrol anybody.
    """
    response = client.post(_URL, json=_BODY)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_external_register_student_as_a_student_returns_403(
    client: TestClient,
    student_auth: Callable[[str], dict],
) -> None:
    """
    A person cannot post to the webhook, however scoped.

    The route records an *external* fact the registrar has already decided.
    A human calling it would be asserting something no upstream system said,
    which is why the rule turns on the principal's type and not on a scope.
    """
    response = client.post(_URL, json=_BODY, headers=student_auth(_STUDENT_ID))
    assert response.status_code == 403
    assert response.json()["detail"] == "not_a_service"
