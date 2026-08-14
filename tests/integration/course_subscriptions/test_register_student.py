# Copyright 2026 Moritz E. Beber
"""Test the Register Student automation and route end to end."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from eventsourcing.domain import TaggedEvent
from eventsourcing.persistence import Tracking

from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.course_subscriptions.events import (
    ExternalStudentRegistered,
    StudentRegistered,
)
from course_subscriptions.course_subscriptions.register_student.projection import (
    RegisterStudentEntry,
    create_runner,
    create_view,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from course_subscriptions.course_subscriptions.register_student.projection import (
        RegisterStudentView,
    )

_STUDENT_ID = "STU-2026-0042"
_OTHER_STUDENT_ID = "STU-2026-0099"


@pytest.fixture
def view(client: TestClient) -> RegisterStudentView:
    """Return the ledger view built by the app's lifespan."""
    return client.app_state["register_student_view"]


def _seed_external_registration(
    dcb_app: CourseSubscriptionsApp,
    student_id: str,
    *,
    correlation_id: str | None = None,
) -> tuple[TaggedEvent, int]:
    seed = TaggedEvent(
        decision=ExternalStudentRegistered(
            student_id=student_id,
            name="Anna Müller",
            course_limit=2,
        ),
        tags=[f"student:{student_id}"],
        metadata={"correlation_id": correlation_id} if correlation_id else {},
    )
    position = dcb_app.events.append(events=[seed])
    return seed, position


def test_external_registration_triggers_register_student(
    dcb_app: CourseSubscriptionsApp,
    view: RegisterStudentView,
) -> None:
    """An external registration drives the command and drains the ledger."""
    seed, position = _seed_external_registration(
        dcb_app,
        _STUDENT_ID,
        correlation_id="corr-1",
    )

    view.wait(
        context_name=dcb_app.context_name,
        notification_id=position + 1,
        timeout=5,
    )

    emitted = next(
        env
        for env in dcb_app.events.read()
        if isinstance(env.decision, StudentRegistered)
        and env.decision.student_id == _STUDENT_ID
    )
    assert emitted.decision.name == "Anna Müller"
    assert emitted.decision.course_limit == 2
    assert emitted.metadata["causation_id"] == str(seed.uuid)
    assert emitted.metadata["correlation_id"] == "corr-1"
    assert view.get_entries() == []


def test_two_students_handled_independently(
    dcb_app: CourseSubscriptionsApp,
    view: RegisterStudentView,
) -> None:
    """Two external registrations for different students both land."""
    _, first_position = _seed_external_registration(dcb_app, _STUDENT_ID)
    _, second_position = _seed_external_registration(dcb_app, _OTHER_STUDENT_ID)

    view.wait(
        context_name=dcb_app.context_name,
        notification_id=max(first_position, second_position) + 1,
        timeout=5,
    )

    registered_ids = {
        env.decision.student_id
        for env in dcb_app.events.read()
        if isinstance(env.decision, StudentRegistered)
    }
    assert registered_ids == {_STUDENT_ID, _OTHER_STUDENT_ID}
    assert view.get_entries() == []


def test_drain_recovers_an_orphaned_entry() -> None:
    """An entry left outstanding by a crash is re-fired on restart."""
    with CourseSubscriptionsApp() as dcb_app:
        view = create_view()
        position = dcb_app.events.append(
            events=[
                TaggedEvent(
                    decision=ExternalStudentRegistered(
                        student_id=_STUDENT_ID,
                        name="Anna Müller",
                        course_limit=2,
                    ),
                    tags=[f"student:{_STUDENT_ID}"],
                ),
            ],
        )
        # A crashed run: entry and tracking committed, command never landed.
        view.add_entry(
            RegisterStudentEntry(
                student_id=_STUDENT_ID,
                name="Anna Müller",
                course_limit=2,
            ),
            Tracking(dcb_app.context_name, position),
        )

        # create_runner() drains before subscribing, exactly as on a
        # supervisor restart.
        with create_runner(dcb_app, view):
            view.wait(
                context_name=dcb_app.context_name,
                notification_id=position + 1,
                timeout=5,
            )
            assert view.get_entries() == []
