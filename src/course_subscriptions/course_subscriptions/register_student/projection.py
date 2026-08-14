# Copyright 2026 Moritz E. Beber
"""Provide the Register Student automation."""

from __future__ import annotations

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
from course_subscriptions.metadata import CAUSATION_ID_KEY, CORRELATION_ID_KEY
from course_subscriptions.projection import SharedAppProjectionRunner
from course_subscriptions.telemetry import consumer_span

if TYPE_CHECKING:
    from eventsourcing.utils import EnvType
    from psycopg import Cursor
    from psycopg.rows import DictRow

    from course_subscriptions.application import CourseSubscriptionsApp

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


@dataclass
class RegisterStudentEntry:
    """
    One outstanding unit of work: a registration not yet confirmed.

    The causal ids are stored alongside the work, not derived at command time,
    because `drain()` runs with no triggering envelope in hand. Without them a
    retried registration would be re-rooted into a fresh flow — the same causal
    step, recorded as if it were a new cause. They are nullable so that rows
    written before this ledger carried them still load.
    """

    student_id: str
    name: str
    course_limit: int
    attempts: int = 0
    correlation_id: str | None = None
    causation_id: str | None = None


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
    def discard_entry(self, student_id: str) -> None:
        """
        Drop the entry for `student_id` without recording a position.

        The same deletion as `remove_entry`, minus the tracking, because
        neither caller has a position to record: on the trigger path
        `add_entry` already wrote this event's, and re-inserting it would
        raise; `drain()` is not processing an event at all.

        Must be a no-op when no entry exists.
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

    def discard_entry(self, student_id: str) -> None:
        """Drop the entry for `student_id` without recording a position."""
        with self._database_lock:
            self._entries.pop(student_id, None)

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
                "attempts bigint NOT NULL DEFAULT 0, "
                "correlation_id text, "
                "causation_id text)",
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
                    "INSERT INTO {0}.{1} "
                    "(student_id, name, course_limit, attempts, "
                    "correlation_id, causation_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (student_id) DO UPDATE SET "
                    "name = EXCLUDED.name, "
                    "course_limit = EXCLUDED.course_limit, "
                    "attempts = EXCLUDED.attempts, "
                    "correlation_id = EXCLUDED.correlation_id, "
                    "causation_id = EXCLUDED.causation_id",
                ).format(*self._table),
                (
                    entry.student_id,
                    entry.name,
                    entry.course_limit,
                    entry.attempts,
                    entry.correlation_id,
                    entry.causation_id,
                ),
            )

    def _delete_entry(self, curs: Cursor[DictRow], student_id: str) -> None:
        """Delete the entry for `student_id` on an already-open cursor."""
        # A DELETE matching no row is already the no-op both callers require.
        curs.execute(
            SQL("DELETE FROM {0}.{1} WHERE student_id = %s").format(*self._table),
            (student_id,),
        )

    def remove_entry(self, student_id: str, tracking: Tracking) -> None:
        """Drop the entry for `student_id`, atomically with the tracking position."""
        with self.datastore.transaction(commit=True) as curs:
            self._insert_tracking(curs, tracking)
            self._delete_entry(curs, student_id)

    def discard_entry(self, student_id: str) -> None:
        """Drop the entry for `student_id` without recording a position."""
        # No tracking to record, for the reasons given on the abstract method.
        with self.datastore.transaction(commit=True) as curs:
            self._delete_entry(curs, student_id)

    def get_entries(self) -> list[RegisterStudentEntry]:
        """Return copies of every outstanding entry."""
        with self.datastore.transaction(commit=False) as curs:
            curs.execute(
                SQL(
                    "SELECT student_id, name, course_limit, attempts, "
                    "correlation_id, causation_id FROM {0}.{1}",
                ).format(*self._table),
            )
            rows = curs.fetchall()
        return [
            RegisterStudentEntry(
                student_id=row["student_id"],
                name=row["name"],
                course_limit=row["course_limit"],
                attempts=row["attempts"],
                correlation_id=row["correlation_id"],
                causation_id=row["causation_id"],
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


AlreadyAppliedPort = Callable[[BaseException], bool]


def _no_already_applied(error: BaseException) -> bool:  # noqa: ARG001
    """Treat nothing as already-applied — the default when no port is injected."""
    return False


def _causation_metadata(entry: RegisterStudentEntry) -> dict[str, str]:
    """
    Derive the metadata naming this entry's trigger as the direct cause.

    Read off the entry rather than a live envelope, so that `drain()` — which
    has no envelope — issues its retry under exactly the metadata the first
    attempt used. Entries written before the ledger carried these ids yield an
    empty dict, and `command_metadata` then seeds a fresh flow for them.

    Args:
        entry: The outstanding work item to derive metadata from.

    Returns:
        The metadata to record the resulting events under.

    """
    metadata = {}
    if entry.correlation_id is not None:
        metadata[CORRELATION_ID_KEY] = entry.correlation_id
    if entry.causation_id is not None:
        metadata[CAUSATION_ID_KEY] = entry.causation_id
    return metadata


class RegisterStudentProjection(Projection[RegisterStudentView, TaggedEvent[Decision]]):
    """React to an external student registration by firing Register Student."""

    name = "register_student"
    topics = (get_topic(ExternalStudentRegistered), get_topic(StudentRegistered))

    def __init__(
        self,
        view: RegisterStudentView,
        command: CommandPort = _no_command,
        already_applied: AlreadyAppliedPort = _no_already_applied,
    ) -> None:
        super().__init__(view=view)
        self._command = command
        self._already_applied = already_applied

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
                        correlation_id=envelope.metadata.get(CORRELATION_ID_KEY),
                        causation_id=str(envelope.uuid),
                    )
                    # Record before commanding: a lingering entry is the
                    # observable signal that the command did not land. It also
                    # puts the causal ids somewhere durable before they are
                    # needed, which is what lets `drain()` retry in the same
                    # flow rather than starting a new one.
                    self.view.add_entry(entry, tracking)
                    self._fire(entry)
                case StudentRegistered(student_id=student_id):
                    self.view.remove_entry(student_id, tracking)
                case _:
                    self.view.insert_tracking(tracking)

    def _fire(self, entry: RegisterStudentEntry) -> None:
        """Issue the command under the entry's own metadata, swallowing failures."""
        try:
            with put_metadata_in_context(_causation_metadata(entry)):
                self._command(entry)
        except Exception as error:
            if self._already_applied(error):
                # Not a failure: the command's own idempotency guard answered,
                # and it only answers this way after replaying the student's
                # history, so it is proof the registration landed — the same
                # proof the completion event would have carried. Take it as the
                # completion signal and drop the entry here.
                #
                # Waiting for that event instead only works while it is still
                # ahead of max_tracking_id, which holds for a crash between
                # the command and its tracking, but not for a duplicate
                # ExternalStudentRegistered: the emitted event was tracked long
                # ago and is never redelivered, so the entry would linger
                # forever, falsely signalling outstanding work.
                self.view.discard_entry(entry.student_id)
                logger.info(
                    "student %s already registered; discarding the "
                    "outstanding entry",
                    entry.student_id,
                )
            else:
                # An escaping exception permanently kills the runner's
                # processing thread, stalling every later event. Log and
                # leave the entry behind for drain() to retry.
                logger.exception("failed to register student %s", entry.student_id)

    def drain(self) -> None:
        """
        Re-issue commands for entries left outstanding by an earlier crash.

        The retry is indistinguishable from the first attempt: the entry
        carries the original `correlation_id` and `causation_id`, so a
        recovered registration lands in the flow that asked for it rather than
        in one invented at restart. A retry is the same causal step, not a new
        cause.
        """
        for entry in self.view.get_entries():
            if entry.attempts > MAX_ATTEMPTS:
                continue
            self.view.count_attempt(entry.student_id)
            self._fire(entry)


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

    def already_applied(error: BaseException) -> bool:
        # `RegisterStudentSlice.execute()` raises this bare `ValueError` when
        # the student is already registered — the string is a public
        # contract, surfaced as `detail` by the HTTP routes.
        return (
            isinstance(error, ValueError) and str(error) == "student_already_registered"
        )

    projection = RegisterStudentProjection(
        view=view,
        command=command,
        already_applied=already_applied,
    )
    # Recover work orphaned by an earlier crash before the subscription
    # resumes. Three windows land here, and they look identical from the view
    # alone: (a) the command never landed — that trigger event is past
    # max_tracking_id and will never be redelivered, so this is the only
    # recovery; (b) the command DID land but its completion event was never
    # tracked — that event is still in the store and would drain the entry on
    # its own once the subscription opens; (c) the entry came from a duplicate
    # trigger for a registration completed long ago, whose completion event is
    # behind max_tracking_id and never comes back. In (b) and (c) the retry
    # reaches RegisterStudentSlice's idempotency guard, and `already_applied`
    # turns that into the discard — which is redundant in (b) but the only
    # thing that ever clears (c).
    projection.drain()
    return RegisterStudentRunner(view=view, app=app, projection=projection)
