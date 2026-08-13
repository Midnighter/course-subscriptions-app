# Copyright 2026 Moritz E. Beber
"""Provide the Register Student automation."""

from __future__ import annotations

import contextlib
import logging
import os
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from eventsourcing.domain import TaggedEvent, put_metadata_in_context
from eventsourcing.errors import InfrastructureFactoryError
from eventsourcing.persistence import InfrastructureFactory, Tracking, TrackingRecorder
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.postgres import PostgresDatastore, PostgresTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import Environment, get_topic
from psycopg.sql import SQL, Identifier

from course_subscriptions.course_subscriptions.events import (
    ExternalStudentRegistered,
    StudentRegistered,
)
from course_subscriptions.course_subscriptions.register_student.slice import (
    RegisterStudentSlice,
)
from course_subscriptions.projection import SharedAppProjectionRunner
from course_subscriptions.telemetry import consumer_span

if TYPE_CHECKING:
    from eventsourcing.utils import EnvType

    from course_subscriptions.application import CourseSubscriptionsApp

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class RegisterStudentEntry:
    """One outstanding unit of work: a registration not yet confirmed."""

    student_id: str
    name: str
    course_limit: int
    attempts: int = 0


class RegisterStudentView(TrackingRecorder):
    """Abstract ledger of outstanding Register Student work items."""

    @abstractmethod
    def add_entry(self, entry: RegisterStudentEntry, tracking: Tracking) -> None:
        """Record an outstanding entry, atomically with the tracking position."""

    @abstractmethod
    def remove_entry(self, student_id: str, tracking: Tracking) -> None:
        """
        Drop the entry for `student_id`, atomically with the tracking position.

        Must be a no-op when no entry exists — the emitted event may arrive
        for work this process never recorded.
        """

    @abstractmethod
    def get_entries(self) -> list[RegisterStudentEntry]:
        """Return copies of every outstanding entry."""

    @abstractmethod
    def count_attempt(self, student_id: str) -> None:
        """Increment the attempt counter for `student_id`."""

    def close(self) -> None:
        """
        Release whatever the backend holds open. Idempotent.

        Backends without resources to release inherit this no-op, which is what
        lets the lifespan call `close()` without knowing which one it built.
        """


class POPORegisterStudentView(POPOTrackingRecorder, RegisterStudentView):
    """In-memory Register Student ledger, backed by the POPO tracking recorder."""

    def __init__(self) -> None:
        super().__init__()
        self._entries: dict[str, RegisterStudentEntry] = {}

    def add_entry(self, entry: RegisterStudentEntry, tracking: Tracking) -> None:
        """Record an outstanding entry, atomically with the tracking position."""
        with self._database_lock:
            self._assert_tracking_uniqueness(tracking)
            self._entries[entry.student_id] = entry
            self._insert_tracking(tracking)

    def remove_entry(self, student_id: str, tracking: Tracking) -> None:
        """Drop the entry for `student_id`, atomically with the tracking position."""
        with self._database_lock:
            self._assert_tracking_uniqueness(tracking)
            self._entries.pop(student_id, None)
            self._insert_tracking(tracking)

    def get_entries(self) -> list[RegisterStudentEntry]:
        """Return copies of every outstanding entry."""
        with self._database_lock:
            return [replace(entry) for entry in self._entries.values()]

    def count_attempt(self, student_id: str) -> None:
        """Increment the attempt counter for `student_id`."""
        with self._database_lock:
            if (entry := self._entries.get(student_id)) is not None:
                entry.attempts += 1


class PostgresRegisterStudentView(PostgresTrackingRecorder, RegisterStudentView):
    """
    Durable Register Student ledger, backed by the Postgres tracking recorder.

    Where `POPORegisterStudentView` reaches for `self._database_lock` and
    `_assert_tracking_uniqueness` — both POPO-only — this holds a database
    transaction instead, and lets `_insert_tracking` be the uniqueness guard:
    it raises `IntegrityError` for a notification at or behind the recorded
    position, rolling the whole statement back with it. That call therefore
    comes *first* in the mutating methods, so a redelivered event does no
    domain work at all.

    Args:
        datastore: The connection pool, supplied by `PostgresFactory`.
        **kwargs: Forwarded to the tracking recorder — notably
            `tracking_table_name`, which the factory derives from the
            projection name.

    """

    def __init__(self, datastore: PostgresDatastore, **kwargs) -> None:
        # AFTER `super().__init__()`: it assigns `sql_create_statements` a
        # fresh list, so an earlier append would be discarded. The factory
        # calls `create_table()`, which runs every statement in that list.
        super().__init__(datastore, **kwargs)
        self.entries_table_name = "register_student_entries"
        self.check_identifier_length(self.entries_table_name)
        self._table = (
            Identifier(self.datastore.schema),
            Identifier(self.entries_table_name),
        )
        self.sql_create_statements.append(
            SQL(
                "CREATE TABLE IF NOT EXISTS {0}.{1} ("
                "student_id text PRIMARY KEY, "
                "name text NOT NULL, "
                "course_limit bigint NOT NULL, "
                "attempts bigint NOT NULL DEFAULT 0)",
            ).format(*self._table),
        )

    def add_entry(self, entry: RegisterStudentEntry, tracking: Tracking) -> None:
        """Record an outstanding entry, atomically with the tracking position."""
        with self.datastore.transaction(commit=True) as curs:
            self._insert_tracking(curs, tracking)
            # Upsert, to match the POPO ledger's dict assignment: a re-emitted
            # trigger overwrites rather than raising.
            curs.execute(
                SQL(
                    "INSERT INTO {0}.{1} (student_id, name, course_limit, attempts) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (student_id) DO UPDATE SET "
                    "name = EXCLUDED.name, "
                    "course_limit = EXCLUDED.course_limit, "
                    "attempts = EXCLUDED.attempts",
                ).format(*self._table),
                (entry.student_id, entry.name, entry.course_limit, entry.attempts),
            )

    def remove_entry(self, student_id: str, tracking: Tracking) -> None:
        """Drop the entry for `student_id`, atomically with the tracking position."""
        with self.datastore.transaction(commit=True) as curs:
            self._insert_tracking(curs, tracking)
            # A DELETE matching no row is already the required no-op.
            curs.execute(
                SQL("DELETE FROM {0}.{1} WHERE student_id = %s").format(*self._table),
                (student_id,),
            )

    def get_entries(self) -> list[RegisterStudentEntry]:
        """Return copies of every outstanding entry."""
        with self.datastore.transaction(commit=False) as curs:
            curs.execute(
                SQL(
                    "SELECT student_id, name, course_limit, attempts FROM {0}.{1}",
                ).format(*self._table),
            )
            rows = curs.fetchall()
        return [
            RegisterStudentEntry(
                student_id=row["student_id"],
                name=row["name"],
                course_limit=row["course_limit"],
                attempts=row["attempts"],
            )
            for row in rows
        ]

    def count_attempt(self, student_id: str) -> None:
        """Increment the attempt counter for `student_id`."""
        # No tracking to record: `drain()` runs outside event processing, so
        # there is no notification position to advance.
        with self.datastore.transaction(commit=True) as curs:
            curs.execute(
                SQL(
                    "UPDATE {0}.{1} SET attempts = attempts + 1 WHERE student_id = %s",
                ).format(*self._table),
                (student_id,),
            )

    def close(self) -> None:
        """Close the connection pool. Idempotent."""
        self.datastore.close()


CommandPort = Callable[[RegisterStudentEntry], None]


def _no_command(entry: RegisterStudentEntry) -> None:
    """Discard the command — the default when no port is injected."""


def _causation_metadata(envelope: TaggedEvent[Decision]) -> dict[str, str]:
    """Derive the metadata naming this envelope as the direct cause."""
    metadata = {}
    with contextlib.suppress(KeyError):
        metadata["correlation_id"] = envelope.metadata["correlation_id"]
    metadata["causation_id"] = str(envelope.uuid)
    return metadata


class RegisterStudentProjection(Projection[RegisterStudentView, TaggedEvent[Decision]]):
    """React to an external student registration by firing Register Student."""

    name = "register_student"
    topics = (get_topic(ExternalStudentRegistered), get_topic(StudentRegistered))

    def __init__(
        self,
        view: RegisterStudentView,
        command: CommandPort = _no_command,
    ) -> None:
        super().__init__(view=view)
        self._command = command

    def process_event(
        self,
        envelope: TaggedEvent[Decision],
        tracking: Tracking,
    ) -> None:
        """Fire Register Student on the trigger; drain the entry on its event."""
        with consumer_span(envelope, "register_student"):
            match envelope.decision:
                case ExternalStudentRegistered(
                    student_id=student_id,
                    name=name,
                    course_limit=course_limit,
                ):
                    entry = RegisterStudentEntry(
                        student_id=student_id,
                        name=name,
                        course_limit=course_limit,
                    )
                    # Record before commanding: a lingering entry is the
                    # observable signal that the command did not land.
                    self.view.add_entry(entry, tracking)
                    self._fire(entry, _causation_metadata(envelope))
                case StudentRegistered(student_id=student_id):
                    self.view.remove_entry(student_id, tracking)
                case _:
                    self.view.insert_tracking(tracking)

    def _fire(self, entry: RegisterStudentEntry, metadata: dict[str, str]) -> None:
        """Issue the command under the given metadata, swallowing failures."""
        try:
            with put_metadata_in_context(metadata):
                self._command(entry)
        except Exception:
            # An escaping exception permanently kills the runner's
            # processing thread, stalling every later event. Log and leave
            # the entry behind for drain() to retry.
            logger.exception("failed to register student %s", entry.student_id)

    def drain(self) -> None:
        """Re-issue commands for entries left outstanding by an earlier crash."""
        for entry in self.view.get_entries():
            if entry.attempts > MAX_ATTEMPTS:
                continue
            self.view.count_attempt(entry.student_id)
            # No triggering envelope exists here, so there is no causation
            # to carry.
            self._fire(entry, {})


class RegisterStudentRunner(SharedAppProjectionRunner):
    """Run the Register Student automation over the shared application."""

    def __init__(
        self,
        view: RegisterStudentView,
        app: CourseSubscriptionsApp,
        projection: RegisterStudentProjection,
    ) -> None:
        self.view = view
        self.projection = projection
        super().__init__(
            projection=projection,
            app=app,
            tracking_recorder=view,
            topics=projection.topics,
        )


_VIEW_CLASSES: dict[str, type[RegisterStudentView]] = {
    "eventsourcing.popo": POPORegisterStudentView,
    "eventsourcing.postgres": PostgresRegisterStudentView,
}

# What `InfrastructureFactory.construct` falls back to when the variable is
# unset. The mapping must agree with it, or the factory and the view class
# would be chosen for different backends — and the factory asserts the pair.
DEFAULT_PERSISTENCE_MODULE = "eventsourcing.popo"


def _select_view_class(env: Environment) -> type[RegisterStudentView]:
    """
    Resolve the view implementation matching the configured backend.

    `env.get` tries `REGISTER_STUDENT_PERSISTENCE_MODULE` before the bare
    `PERSISTENCE_MODULE`, so this view can move to Postgres on its own,
    without dragging the event store along.

    Args:
        env: The view's environment, already scoped to the projection name.

    Returns:
        The view class implementing this slice's ledger on that backend.

    Raises:
        InfrastructureFactoryError: If no implementation exists for the
            configured persistence module.

    """
    module = (
        env.get(InfrastructureFactory.PERSISTENCE_MODULE, "")
        or DEFAULT_PERSISTENCE_MODULE
    )
    try:
        return _VIEW_CLASSES[module]
    except KeyError:
        msg = (
            f"No Register Student view for persistence module {module!r}; "
            f"expected one of {sorted(_VIEW_CLASSES)}."
        )
        raise InfrastructureFactoryError(msg) from None


def create_view(
    view_class: type[RegisterStudentView] | None = None,
    env: EnvType | None = None,
) -> RegisterStudentView:
    """
    Build the Register Student ledger view. Called ONCE per process.

    With no `view_class`, the implementation is selected from the configured
    persistence module, so a deployment picks its backend by environment
    alone. An explicit `view_class` still wins, which is what the tests use.

    Args:
        view_class: The ledger implementation to build, or None to select it
            from the environment.
        env: Overrides layered over `os.environ`, scoping this view only.

    Returns:
        The constructed ledger view, with its tables created.

    """
    environment = Environment(
        RegisterStudentProjection.name,
        {**os.environ, **(env or {})},
    )
    if view_class is None:
        view_class = _select_view_class(environment)
    factory: InfrastructureFactory[RegisterStudentView] = (
        InfrastructureFactory.construct(env=environment)
    )
    return factory.tracking_recorder(view_class)


def create_runner(
    app: CourseSubscriptionsApp,
    view: RegisterStudentView,
) -> RegisterStudentRunner:
    """
    Construct a runner driving `view` from the shared application.

    Safe to call repeatedly against the same view: the supervisor calls
    this again on every restart.
    """

    def command(entry: RegisterStudentEntry) -> None:
        app.do(
            RegisterStudentSlice(
                student_id=entry.student_id,
                name=entry.name,
                course_limit=entry.course_limit,
            ),
        )

    projection = RegisterStudentProjection(view=view, command=command)
    # Recover work orphaned by an earlier crash before the subscription
    # resumes; those events are past max_tracking_id and will never be
    # redelivered.
    projection.drain()
    return RegisterStudentRunner(view=view, app=app, projection=projection)
