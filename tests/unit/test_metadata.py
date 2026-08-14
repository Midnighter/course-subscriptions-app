# Copyright 2026 Moritz E. Beber
"""
Unit tests for the event metadata module.

`MetadataMiddleware` is exercised in the integration suite instead: it needs a
live ASGI stack to prove the thing worth proving, which is that the contextvar
it sets actually reaches the route handler.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from eventsourcing.domain import get_metadata_from_context, put_metadata_in_context

from course_subscriptions.metadata import (
    CORRELATION_ID_KEY,
    CREATED_AT_KEY,
    command_metadata,
    created_at,
    new_correlation_id,
    sanitise_correlation_id,
)


def test_new_correlation_id_is_a_uuid():
    """A minted id parses as a UUID, so it is opaque and collision-free."""
    assert UUID(new_correlation_id())


def test_new_correlation_ids_differ():
    """Two flows never share a minted id."""
    assert new_correlation_id() != new_correlation_id()


@pytest.mark.parametrize(
    "raw",
    [
        "corr-1",
        "5c6d1e6e-6f6a-4b0e-9b1a-0f0f0f0f0f0f",
        "order:42",
        "a" * 128,
    ],
)
def test_sanitise_correlation_id_accepts_a_usable_id(raw: str):
    """A bounded, printable id is passed through untouched."""
    assert sanitise_correlation_id(raw) == raw


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "a" * 129,
        "has space",
        "line\nbreak",
        "corr-1\r\nX-Injected: yes",
        "drop/slash",
    ],
)
def test_sanitise_correlation_id_replaces_an_unusable_id(raw: str | None):
    """
    An absent, oversized, or unprintable id is replaced rather than repaired.

    The value reaches a `jsonb` column, the logs, and a response header, so a
    client must not be able to smuggle a newline into any of them. Replacing
    keeps the invariant crisp: a stored id is either exactly what the client
    sent or one we minted, never a mangled prefix of the two.
    """
    sanitised = sanitise_correlation_id(raw)

    assert sanitised != raw
    assert UUID(sanitised)


def test_created_at_is_utc_and_parses():
    """The timestamp round-trips through `fromisoformat` as an aware UTC time."""
    parsed = datetime.fromisoformat(created_at())

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_command_metadata_seeds_a_correlation_id_when_absent():
    """A command reaching `do()` with no ambient flow starts one."""
    with command_metadata():
        metadata = get_metadata_from_context()

    assert UUID(metadata[CORRELATION_ID_KEY])
    assert metadata[CREATED_AT_KEY]


def test_command_metadata_preserves_an_existing_correlation_id():
    """
    An inherited flow survives, which is the whole point of seeding here.

    Both the HTTP middleware and an automation put a `correlation_id` in
    context before `do()` runs. Overwriting it would re-root every command
    into its own flow and silently undo the propagation.
    """
    with put_metadata_in_context({CORRELATION_ID_KEY: "corr-1"}), command_metadata():
        metadata = get_metadata_from_context()

    assert metadata[CORRELATION_ID_KEY] == "corr-1"


def test_command_metadata_refreshes_created_at():
    """
    `created_at` is stamped per command, not per flow.

    An automation's command is a later unit of work than the trigger that
    caused it, so it must carry its own time even though it inherits the flow.
    """
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    with (
        put_metadata_in_context({CREATED_AT_KEY: epoch.isoformat()}),
        command_metadata(),
    ):
        stamped = get_metadata_from_context()[CREATED_AT_KEY]

    assert datetime.fromisoformat(stamped) > epoch


def test_command_metadata_restores_the_context_on_exit():
    """The seeded metadata does not leak past the block."""
    with command_metadata():
        pass

    assert get_metadata_from_context() == {}
