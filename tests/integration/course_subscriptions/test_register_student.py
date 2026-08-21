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
from course_subscriptions.course_subscriptions.slice.register_student.projection import (  # noqa: E501
    RegisterStudentEntry,
    create_runner,
    create_view,
)

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

    from course_subscriptions.course_subscriptions.slice.register_student.projection import (  # noqa: E501
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
    assert emitted.metadata["created_at"]
    assert view.get_entries() == []


def test_a_duplicate_registration_leaves_no_outstanding_entry(
    dcb_app: CourseSubscriptionsApp,
    view: RegisterStudentView,
) -> None:
    """
    A resubmission for a registered student clears its own ledger entry.

    `ExternalStudentRegistered` is recorded unconditionally, so this arrives
    as a fresh trigger and takes an entry — but the command refuses, and the
    `StudentRegistered` that would drain it was tracked when the first
    registration landed and is never redelivered. Without the already-applied
    discard the entry sits outstanding forever, and every genuinely stuck
    entry becomes indistinguishable from it.
    """
    _, first_position = _seed_external_registration(dcb_app, _STUDENT_ID)
    view.wait(
        context_name=dcb_app.context_name,
        notification_id=first_position + 1,
        timeout=5,
    )

    _seed_external_registration(dcb_app, _STUDENT_ID, correlation_id="corr-dup")
    # A barrier, not a fixture of the scenario: the duplicate advances
    # tracking from `add_entry`, which runs *before* the command and so before
    # the discard. Events are processed in order, so tracking reaching a later
    # position is what proves the duplicate was handled to completion.
    barrier_position = dcb_app.events.append(
        events=[
            TaggedEvent(
                decision=StudentRegistered(
                    student_id=_OTHER_STUDENT_ID,
                    name="Bo Nielsen",
                    course_limit=2,
                ),
                tags=[f"student:{_OTHER_STUDENT_ID}"],
            ),
        ],
    )
    view.wait(
        context_name=dcb_app.context_name,
        notification_id=barrier_position,
        timeout=5,
    )

    registered = [
        env
        for env in dcb_app.events.read()
        if isinstance(env.decision, StudentRegistered)
        and env.decision.student_id == _STUDENT_ID
    ]
    assert len(registered) == 1
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
    """
    An entry left outstanding by a crash is re-fired on restart, in its flow.

    The trigger sits at `position`, which the crashed run already tracked, so
    it is never redelivered — `drain()` is the only path back. The recovered
    registration must still land in the flow that asked for it, which is what
    the entry's stored causal ids are for.
    """
    with CourseSubscriptionsApp() as dcb_app:
        view = create_view()
        seed = TaggedEvent(
            decision=ExternalStudentRegistered(
                student_id=_STUDENT_ID,
                name="Anna Müller",
                course_limit=2,
            ),
            tags=[f"student:{_STUDENT_ID}"],
            metadata={"correlation_id": "corr-drain"},
        )
        position = dcb_app.events.append(events=[seed])
        # A crashed run: entry and tracking committed, command never landed.
        view.add_entry(
            RegisterStudentEntry(
                student_id=_STUDENT_ID,
                name="Anna Müller",
                course_limit=2,
                correlation_id="corr-drain",
                causation_id=str(seed.uuid),
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

        emitted = next(
            env
            for env in dcb_app.events.read()
            if isinstance(env.decision, StudentRegistered)
            and env.decision.student_id == _STUDENT_ID
        )
        assert emitted.metadata["correlation_id"] == "corr-drain"
        assert emitted.metadata["causation_id"] == str(seed.uuid)
