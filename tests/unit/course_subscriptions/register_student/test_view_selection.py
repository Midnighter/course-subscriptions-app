# Copyright 2026 Moritz E. Beber
"""Test that the Register Student ledger picks its backend from the environment."""

from __future__ import annotations

import pytest
from eventsourcing.errors import InfrastructureFactoryError
from eventsourcing.utils import Environment

from course_subscriptions.course_subscriptions.slice.register_student.projection import (
    POPORegisterStudentView,
    PostgresRegisterStudentView,
    RegisterStudentProjection,
    _select_view_class,
    create_view,
)


def _environment(**overrides: str) -> Environment:
    """
    Build the view's environment without consulting `os.environ`.

    `create_view` layers overrides over the process environment, which would
    make these assertions depend on the machine running them. Selection is
    tested against the `Environment` directly instead, so a developer with
    `PERSISTENCE_MODULE` exported cannot flip the result.
    """
    return Environment(RegisterStudentProjection.name, overrides)


def test_unconfigured_environment_selects_the_in_memory_ledger():
    """With nothing configured, the ledger matches the factory's own default."""
    assert _select_view_class(_environment()) is POPORegisterStudentView


def test_persistence_module_selects_the_durable_ledger():
    """The bare variable reaches this view through the fallback lookup."""
    view_class = _select_view_class(
        _environment(PERSISTENCE_MODULE="eventsourcing.postgres"),
    )
    assert view_class is PostgresRegisterStudentView


def test_prefixed_variable_moves_this_view_alone():
    """
    The projection-scoped variable wins over the bare one.

    This is what lets the ledger go durable while the event store stays on
    another backend entirely.
    """
    view_class = _select_view_class(
        _environment(
            REGISTER_STUDENT_PERSISTENCE_MODULE="eventsourcing.postgres",
            PERSISTENCE_MODULE="eventsourcing.popo",
        ),
    )
    assert view_class is PostgresRegisterStudentView


def test_unsupported_module_names_the_ones_that_exist():
    """
    An unmapped backend fails loudly at startup.

    The factory would otherwise build a bare tracking recorder with none of
    the ledger's methods, and the first event to arrive would die on an
    `AttributeError` inside the projection thread.
    """
    with pytest.raises(InfrastructureFactoryError, match=r"eventsourcing\.postgres"):
        _select_view_class(_environment(PERSISTENCE_MODULE="eventsourcing.sqlite"))


def test_explicit_view_class_overrides_the_environment():
    """Passing a class bypasses selection — how the other suites stay in memory."""
    view = create_view(
        view_class=POPORegisterStudentView,
        env={"REGISTER_STUDENT_PERSISTENCE_MODULE": "eventsourcing.popo"},
    )
    assert isinstance(view, POPORegisterStudentView)
