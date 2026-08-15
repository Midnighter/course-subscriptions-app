# Copyright 2026 Moritz E. Beber
"""Test the External Register Student route."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

_STUDENT_ID = "STU-2026-0042"


def test_external_register_student_route_returns_201(client: TestClient) -> None:
    """Recording a registration via the route returns HTTP 201."""
    response = client.post(
        "/students/register",
        json={
            "student_id": _STUDENT_ID,
            "name": "Anna Müller",
            "course_limit": 2,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["position"] is not None
    assert len(body["event_ids"]) == 1


def test_external_register_student_route_is_unconditional(
    client: TestClient,
) -> None:
    """A repeat submission for the same student still returns HTTP 201."""
    body = {
        "student_id": _STUDENT_ID,
        "name": "Anna Müller",
        "course_limit": 2,
    }

    first = client.post("/students/register", json=body)
    second = client.post("/students/register", json=body)

    assert first.status_code == 201
    assert second.status_code == 201


def test_external_register_student_route_missing_field_returns_422(
    client: TestClient,
) -> None:
    """A request missing a required field returns HTTP 422."""
    response = client.post(
        "/students/register",
        json={"student_id": _STUDENT_ID},
    )
    assert response.status_code == 422
