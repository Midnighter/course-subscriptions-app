---
name: build-automation
description: Implements a pyeventsourcing automation slice (tracking view + Projection that fires a command, pytest tests) from a slice.json definition
---

# Build Automation Slice

> Before doing anything else, read the slice definition from `.build-kit/.slices/<contextSlug>/<sliceFolder>/slice.json`. This file is the **source of truth** for which trigger event drives the automation and what command it fires. Never invent fields not defined there.
>
> **These two path segments are lowercase slugs, not snake_case**, and they slug differently from each other — they are written by the `load-slice` skill, which owns the rules:
>
> - `<contextSlug>` — the context lowercased, spaces to hyphens, non-alphanumeric removed (`"My Ctx"` → `my-ctx`). Falls back to `default` when the slice has no context.
> - `<sliceFolder>` — the slice title lowercased with **all spaces removed** and any `slice:` prefix stripped (`"Cancel Expired License"` → `cancelexpiredlicense`).
>
> So `CancelExpiredLicense` in the `Backoffice` context reads from `.build-kit/.slices/backoffice/cancelexpiredlicense/slice.json`. Do **not** use the `snake_case(...)` form here — that convention applies to the generated Python paths below, not to this directory. If the path you derive is missing, list the context directory rather than guessing.

Project-wide conventions (tooling, pre-commit, test layout) live in `.build-kit/CLAUDE.md`. Consult it for anything not specific to building a slice.

---

## What an Automation Slice is

An automation slice reacts to events and fires a command in response — a
**state-change slice driven by a policy** instead of by an HTTP route.

In this codebase that is a `Projection` running under a runner. The runner
subscribes to the **shared, process-wide `{ProjectName}App`** via
`application_subscription`; for each event it calls the projection's
`process_event`, which maintains a `TrackingRecorder` view and issues the
command. The view holds both the outstanding work and the position of the last
processed event.

```
{ProjectName}App  ──subscription──▶  Projection.process_event
   (SHARED)  ▲                              │
      │                                     ├──▶ view.add_entry(entry, tracking)
      └────────── command ◀─────────────────┘
                     │
                     ▼
             emits new event ──▶ back through the same subscription
                                 └──▶ view.remove_entry(...)
```

The last arrow is the point. Events the automation emits **come back through the
same subscription**, and that is what drains the entry. The view is simultaneously
the automation's input state and its ledger of unfinished work — but only if
something actually reads that ledger back. See Step 4, which also covers the entries
that arrow will never reach: a command answering "already done" emits nothing, so
nothing comes back around for it.

That loop only closes when the automation subscribes to the *same* application
the command writes through. A runner that constructs its own `DcbApplication`
gets a private in-memory store: it never sees its trigger, and the event its own
command emits never comes back to drain the ledger. That is why Step 5 uses
`SharedAppProjectionRunner` — see `.build-kit/CLAUDE.md` → *Projection runners*.

---

## Step 0 — Verify the shared runtime

A slice is built *on* the shared runtime; it never carries a copy of it. Before reading the slice definition, confirm each of these exists — under `src/snake_case({ProjectName})/` unless another path is given:

| Module | If missing |
|--------|------------|
| `__init__.py` | `.build-kit/CLAUDE.md` → *First-time project setup* |
| `command.py` | ditto — the command this automation fires subclasses its `CommandSlice` |
| `metadata.py` | ditto — the module and its tests are in `.build-kit/references/metadata.md`; this slice type imports its `CORRELATION_ID_KEY` and `CAUSATION_ID_KEY` |
| `telemetry.py` | ditto — the module and its tests are in `.build-kit/references/telemetry.md` |
| `application.py` | ditto — including the `do()` override and the `command_metadata()` / `command_span` inside it |
| `main.py` | ditto — including `configure_telemetry()`, `instrument_app()`, `instrument_recorder()` and `add_middleware(MetadataMiddleware)` |
| `projection.py` | `.build-kit/CLAUDE.md` → *Projection runners*; **required** by this slice type — see Step 5 |
| `tests/unit/test_projection.py` | ditto — the upgrade tripwire that pairs with `projection.py`; **its absence is invisible**, since a suite passes just as green without it |
| `health.py` | `.build-kit/CLAUDE.md` → *Supervising projections*; holds the `/livez` and `/readyz` routes, required once a supervisor exists, and this slice type registers one |

A missing module is an incomplete setup, not an opt-out — in particular, **do not skip a slice's instrumentation because `telemetry.py` is absent.** Create what is missing, run the test suites, and commit it on its own as a `chore:` **before** starting the slice: a shared-runtime module folded into a `feat:` commit is unreviewable and reads as slice-specific when it is not.

If everything is present, change nothing. Import from these modules and move on.

---

## Step 1 — Read the slice.json

Extract:
- **sliceName** — the automation's name
- **context** — bounded context
- **processors[]** — the automation element. Its `dependencies` name the inbound read models and the **OUTBOUND `COMMAND`** it fires.
- **commands[]** — the command's data fields
- **events[]** — the events that command emits (these close the loop; see Step 3). Check for a **failure event** among them — a "… failed" outbound event enables the retry loop in Step 4b. Its absence is not a blocker; `drain()` covers recovery either way.
- **specifications[]** — test scenarios. Specs expecting the failure event are describing the automation's retry behaviour, not just the command's validation. If empty, still write at least the trigger-fires-the-command path, the emitted-event-drains-the-entry path, and the `drain()` recovery case.

### Placeholder grammar

Every placeholder in the templates below is **PascalCase**. There is only one form;
derive it once and reuse it verbatim.

| Placeholder | Derived from | Example |
|-------------|--------------|---------|
| `{SliceName}` | `sliceName` in PascalCase | `CancelExpiredLicenses` |
| `{TriggerEventName}` | the inbound event that starts the automation, PascalCase | `LicenseExpired` |
| `{EmittedEventName}` | the event the fired command emits, PascalCase | `LicenseCancelled` |
| `{FailureEventName}` | **optional** — the "… failed" outbound event, PascalCase. Only when the model defines one | `LicenseCancellationFailed` |
| `{EntryName}` | the work-item dataclass, `{SliceName}Entry` unless the model names it | `CancelExpiredLicensesEntry` |
| `{CommandSliceName}` | the `Slice` the automation fires, PascalCase | `CancelLicense` |
| `{Context}` | bounded context in PascalCase | `Backoffice` |
| `{ProjectName}` | `project.name` in `pyproject.toml`, PascalCase | `MyProject` |

`{ProjectName}` is the one placeholder that does **not** come from the slice definition —
read it once from `[project] name` in `pyproject.toml`. It is already fixed for the whole
repository, so `snake_case({ProjectName})` is simply the existing top-level package under
`src/`; confirm it there rather than deriving a name that does not exist on disk.

Filesystem paths, Python module names, and environment-variable prefixes are **derived
from the PascalCase placeholder at code-generation time**, not carried as separate
placeholders:

- Python module / package path → lowercase PascalCase split on word boundaries, joined
  with `_` (e.g. `CancelExpiredLicenses` → `cancel_expired_licenses`).
- Environment-variable prefix → the snake_case form upper-cased, written
  `upper_snake_case({SliceName})` (e.g. `CancelExpiredLicenses` →
  `CANCEL_EXPIRED_LICENSES`). This is what scopes the view's `PERSISTENCE_MODULE` — see
  *Choosing the view's backend* in Step 3.

There is no route-prefix transform: an automation has no `routes.py` (Step 2).

Apply these transforms mechanically; do not introduce new placeholder tokens.

---

## Step 2 — Build the command handler

The command the automation fires is an ordinary state-change slice, and it is a
**separate artifact** — not part of this slice's files-to-create tree. Build it with
**build-state-change**, which creates
`src/snake_case({ProjectName})/snake_case({Context})/events.py` and
`src/snake_case({ProjectName})/snake_case({Context})/snake_case({CommandSliceName})/slice.py`,
then come back here. The automation reuses that `Slice` unchanged; the only difference
is what invokes it.

Two automation-specific constraints that build-state-change will not tell you:

- **Do NOT create a `routes.py`** for the automation itself — it is not driven over HTTP.
  (The command slice may still have one if the model also exposes it to users.)
- **The slice that emits the *trigger* event is a webhook ingress, and is addressed as
  one.** When the trigger comes from outside, its route records an external event rather
  than running a command — the domain's own command is what *this* automation fires,
  downstream. build-state-change builds that route; its step 3 covers the shape
  (`POST /webhooks/{external-event}`, tagged `webhooks`, answering 202). Do not let the
  slice's name decide: an `ExternalRegisterX` slice reads like a command, while the
  element actually crossing the wire is the event `X Registered`.
- **The command slice's boundary must be satisfiable from the trigger event alone.** The
  automation has no request body to fall back on: whatever `consistency_boundary()`
  selects on has to be reconstructible from the fields the trigger carries.

---

## Step 3 — Create `projection.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/projection.py`

Four pieces live here: the view (its abstract interface plus a concrete
implementation), the `Projection` that maintains it, and the two module-level factories
`create_view()` and `create_runner()` used by both the app lifespan and the tests
(Step 5).

**The two factories are separate on purpose.** The view is the automation's ledger and
must outlive any single runner: the supervisor rebuilds a dead runner over the *same*
view, so it resumes at `max_tracking_id` with its outstanding entries intact. If
`create_runner()` also built the view, every restart would strand the pending work and
replay from zero.

The templates below are shown one piece at a time. Together they need these imports and
module-level constants:

```python
import logging
import os
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, replace

from eventsourcing.domain import (
    TaggedEvent,
    get_metadata_from_context,
    put_metadata_in_context,
)
from eventsourcing.persistence import InfrastructureFactory, Tracking, TrackingRecorder
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import Environment, EnvType, get_topic

from snake_case({ProjectName}).application import {ProjectName}App
from snake_case({ProjectName}).metadata import CAUSATION_ID_KEY, CORRELATION_ID_KEY
from snake_case({ProjectName}).projection import SharedAppProjectionRunner
from snake_case({ProjectName}).snake_case({Context}).events import (
    {EmittedEventName},
    {TriggerEventName},
)
from snake_case({ProjectName}).snake_case({Context}).snake_case({CommandSliceName}).slice import (
    {CommandSliceName}Slice,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3   # tune per model; see Step 4c
```

Add `{FailureEventName}` to the events import, and `Record{FailureEventName}Slice` to the
slice imports, only when the model defines one (Step 4b). `get_metadata_from_context` is
used by the acceptance tests (Step 6) rather than by `projection.py` itself.

### Choosing the view's backend

`{SliceName}View` extends `eventsourcing.persistence.TrackingRecorder`, which is
abstract. The library ships two concrete tracking recorders usable with a DCB
application:

| Recorder | Module | Use for |
|----------|--------|---------|
| `POPOTrackingRecorder` | `eventsourcing.popo` | In-memory. The default, and what the tests in Steps 6–7 use. Lost on process exit. |
| `PostgresTrackingRecorder` | `eventsourcing.postgres` | Durable, shared across processes. Production. |

> The library also ships a `SQLiteTrackingRecorder`, but **do not use it here.** The DCB
> write side provides factories only for `eventsourcing.dcb.popo` and
> `eventsourcing.dcb.postgres_tt`, so a `DcbApplication` cannot run on SQLite — and a
> durable view paired with a non-durable event store buys nothing.

You do not choose the backend by importing a different class at the call site. The view
is built through `InfrastructureFactory.construct(...).tracking_recorder(view_class)`,
and the factory is resolved from the **`PERSISTENCE_MODULE` environment variable**. So:

- The `view_class` must subclass the concrete recorder matching the configured factory —
  a `POPOFactory` asserts `issubclass(view_class, POPOTrackingRecorder)`,
  `PostgresFactory` asserts `PostgresTrackingRecorder`. The wrong pair fails at startup.
- The environment is **name-scoped to `Projection.name`**. `Environment.get` tries
  `{NAME.upper()}_{KEY}` before the bare `{KEY}`, so
  `upper_snake_case({SliceName})_PERSISTENCE_MODULE` configures *this view* without
  touching the `DcbApplication`'s own persistence. The view and the event store can sit
  on different backends. This is why Step 5 constructs an `Environment` named after the
  projection.

Default to **POPO only** unless the slice definition asks for durability — an unused
second implementation is dead code that still has to be maintained. The abstract
`{SliceName}View` interface is what makes adding it later cheap.

The two implementations do not share a locking or persistence idiom — do not copy one
into the other:

| | POPO | Postgres |
|---|---|---|
| Mutual exclusion | `with self._database_lock:` (an `RLock`, POPO-only — it does not exist on the SQL recorders) | a transaction: `with self.datastore.transaction(commit=True) as curs:` |
| Recording tracking | `self._insert_tracking(tracking)` | `self._insert_tracking(curs, tracking)` — takes the cursor, so the entry and its tracking row commit atomically |
| Duplicate-tracking check | call `self._assert_tracking_uniqueness(tracking)` yourself | enforced by the tracking table's primary key; `_insert_tracking` raises `IntegrityError` — so put it **first** in a mutating method, and a redelivered notification does no domain work before the rollback |
| Extra tables | none — plain Python containers on `self` | append `CREATE TABLE IF NOT EXISTS ...` to `self.sql_create_statements` in `__init__`, **after** `super().__init__()` — `PostgresRecorder.__init__` assigns that attribute a fresh list, so an earlier append is silently discarded. The factory calls `create_table()` when `CREATE_TABLE` is not disabled |
| Releasing resources | nothing to release | `close()` closes the datastore's connection pool — see *Wiring it into the app lifespan* |

Give `{SliceName}View` a concrete, do-nothing `close()` alongside its abstract
methods, and override it only on the Postgres implementation. It must not be
abstract: the lifespan calls it without knowing which backend it built, and a
POPO-only project would otherwise have to implement a method that does nothing.

A Postgres `__init__` takes `(self, datastore: PostgresDatastore, **kwargs)` —
the factory supplies `datastore` and a `tracking_table_name` derived from
`Projection.name`, so the signature must forward `**kwargs` rather than fix its
own. **Leave `**kwargs` unannotated** (`.build-kit/CLAUDE.md` → *Pre-commit
compliance rules*).

In both cases the point is the same: **the entry and its `Tracking` must be persisted in
one atomic step**, so a crash cannot leave the view holding a work item whose position
was never recorded — or, worse, a recorded position with no work item, which is an
orphaned command that `drain()` can never recover. That is what every mutating method
below takes a `tracking` argument for.

### The view

The automation's view is a **ledger of unfinished work**, not accumulated read-model
state. That shapes the interface: entries are added when the trigger arrives and
**removed** when the emitted event confirms the command landed, so alongside `add_entry`
there is a `remove_entry` — and `get_entries()` takes **no argument**, because `drain()`
(Step 4) needs every outstanding item, not one entity's.

```python
from abc import abstractmethod
from dataclasses import dataclass, replace

from eventsourcing.persistence import Tracking, TrackingRecorder
from eventsourcing.popo import POPOTrackingRecorder


@dataclass
class {EntryName}:
    """
    One outstanding unit of work: a command that has not yet been confirmed.

    The causal ids are stored alongside the work, not derived at command time,
    because `drain()` (Step 4a) runs with no triggering envelope in hand.
    Without them a retried command would be re-rooted into a fresh flow — the
    same causal step, recorded as if it were a new cause. They are nullable so
    that rows written before this ledger carried them still load.
    """

    entity_id: str
    field: str
    attempts: int = 0          # see Step 4c
    correlation_id: str | None = None
    causation_id: str | None = None


class {SliceName}View(TrackingRecorder):
    """Abstract ledger of {SliceName} work items."""

    @abstractmethod
    def add_entry(self, entry: {EntryName}, tracking: Tracking) -> None:
        """Record an outstanding entry, atomically with the tracking position."""

    @abstractmethod
    def remove_entry(self, entity_id: str, tracking: Tracking) -> None:
        """Drop the entry for `entity_id`, atomically with the tracking position.

        Must be a no-op when no entry exists — the emitted event may arrive for
        work this process never recorded.
        """

    @abstractmethod
    def discard_entry(self, entity_id: str) -> None:
        """Drop the entry for `entity_id` without recording a position.

        The same deletion as `remove_entry`, minus the tracking, because
        neither caller has a position to record: on the trigger path
        `add_entry` already wrote this event's, and re-inserting it would
        raise; `drain()` is not processing an event at all.

        Must be a no-op when no entry exists.
        """

    @abstractmethod
    def get_entries(self) -> list[{EntryName}]:
        """Return copies of every outstanding entry."""

    @abstractmethod
    def count_attempt(self, entity_id: str) -> None:
        """Increment the attempt counter for `entity_id`."""


class POPO{SliceName}View(POPOTrackingRecorder, {SliceName}View):
    """In-memory {SliceName} ledger, backed by the POPO tracking recorder."""

    def __init__(self) -> None:
        super().__init__()
        self._entries: dict[str, {EntryName}] = {}

    def add_entry(self, entry: {EntryName}, tracking: Tracking) -> None:
        """Record an outstanding entry, atomically with the tracking position."""
        with self._database_lock:
            self._assert_tracking_uniqueness(tracking)
            self._entries[entry.entity_id] = entry
            self._insert_tracking(tracking)

    def remove_entry(self, entity_id: str, tracking: Tracking) -> None:
        """Drop the entry for `entity_id`, atomically with the tracking position."""
        with self._database_lock:
            self._assert_tracking_uniqueness(tracking)
            self._entries.pop(entity_id, None)
            self._insert_tracking(tracking)

    def discard_entry(self, entity_id: str) -> None:
        """Drop the entry for `entity_id` without recording a position."""
        with self._database_lock:
            self._entries.pop(entity_id, None)

    def get_entries(self) -> list[{EntryName}]:
        """Return copies of every outstanding entry."""
        with self._database_lock:
            return [replace(entry) for entry in self._entries.values()]

    def count_attempt(self, entity_id: str) -> None:
        """Increment the attempt counter for `entity_id`."""
        with self._database_lock:
            if (entry := self._entries.get(entity_id)) is not None:
                entry.attempts += 1
```

Notes on the template:

- **Keyed by `entity_id`, one entry per entity.** A second trigger for the same entity
  replaces the outstanding entry rather than queueing a duplicate command. If the model
  genuinely allows concurrent work items per entity, key on whatever the model makes
  unique instead — but then `remove_entry` needs that key too, not `entity_id`.
- **`count_attempt` and `discard_entry` do not take a `tracking`.** `count_attempt` is
  called from `drain()` and the retry path, which are not processing a new event, so
  there is no position to record. `discard_entry` is called from `_fire` (Step 3), which
  reaches it on *both* paths — and on the trigger path `add_entry` has already written
  that event's position, so passing it again would raise `IntegrityError`. Give the two
  deleting methods one shared private helper on the Postgres implementation, or
  `remove_entry` and `discard_entry` will drift apart.
- **`get_entries()` returns copies** (`replace(entry)`), or callers mutate view state
  through the returned dataclasses.

### topics — list the emitted event too

```python
topics = (get_topic({TriggerEventName}), get_topic({EmittedEventName}))
```

Two topics is the baseline, and both are mandatory. The **emitted** event is what drains
the entry once the command succeeds; omit it and the view grows forever, holding every
licence it ever cancelled.

Add a third topic, `get_topic({FailureEventName})`, **only if** the model defines a
failure event (Step 1). Models without one still work — `drain()` covers recovery either
way (Step 4a).

### The command port

```python
CommandPort = Callable[[{EntryName}], None]


def _no_command(entry: {EntryName}) -> None:
    """Discard the command — the default when no port is injected."""


AlreadyAppliedPort = Callable[[BaseException], bool]


def _no_already_applied(error: BaseException) -> bool:  # noqa: ARG001
    """Treat nothing as already-applied — the default when no port is injected."""
    return False


class {SliceName}Projection(
    Projection[{SliceName}View, TaggedEvent[Decision]],
):
    """..."""

    name = "snake_case({SliceName})"
    topics = (get_topic({TriggerEventName}), get_topic({EmittedEventName}))

    def __init__(
        self,
        view: {SliceName}View,
        command: CommandPort = _no_command,
        already_applied: AlreadyAppliedPort = _no_already_applied,
    ) -> None:
        super().__init__(view=view)
        self._command = command
        self._already_applied = already_applied
```

The command is **injected, not reached for**. A `Projection` receives only its view,
never the application, so it has no path to the write side on its own — and it should
not: injection is what makes the projection testable with no app, no runner, and no
background thread. `create_runner()` (Step 5) closes the port over the shared
application.

**`already_applied` is a second, narrower port**, for a domain that raises its own
idempotency guard when a command is re-issued against work it has already done (Step 4a
describes exactly when that happens). It answers one question — "does this exception mean
the command already succeeded?" — and its default answers "no" for everything, so a model
with no such guard is unaffected. Inject it the same way as `command`: `create_runner()`
closes it over the concrete exception the domain's command slice raises.

**A model with a failure event adds a third port, `failure`, to this same `__init__`** —
Step 4b has the signature. Two ports is the complete list only for a model without one.

### process_event

```python
    def process_event(
        self,
        envelope: TaggedEvent[Decision],
        tracking: Tracking,
    ) -> None:
        """..."""
        match envelope.decision:
            case {TriggerEventName}(field=field, entity_id=entity_id):
                entry = {EntryName}(
                    entity_id=entity_id,
                    field=field,
                    correlation_id=envelope.metadata.get(CORRELATION_ID_KEY),
                    causation_id=str(envelope.uuid),
                )
                # Record before commanding: a lingering entry is the observable
                # signal that the command did not land. It also puts the causal
                # ids somewhere durable before they are needed, which is what
                # lets `drain()` retry in the same flow rather than a new one.
                self.view.add_entry(entry, tracking)
                self._fire(entry)
            case {EmittedEventName}(entity_id=entity_id):
                self.view.remove_entry(entity_id, tracking)
            case _:
                self.view.insert_tracking(tracking)
```

**Order matters.** `add_entry(..., tracking)` first, then the command. This gives
at-most-once command delivery and leaves the todo view doubling as the retry
ledger — an entry that never drained is a command that never landed.

Keep the `case _:` wildcard and its `insert_tracking`. `match` is
exhaustive-by-branch, not exhaustive-by-topics, and a branch that skips the
tracking write makes every later `wait()` hang until timeout.

### Firing the command

At module level, alongside the ports:

```python
def _causation_metadata(entry: {EntryName}) -> dict[str, str]:
    """Derive the metadata naming this entry's trigger as the direct cause."""
    metadata: dict[str, str] = {}
    if entry.correlation_id is not None:
        metadata[CORRELATION_ID_KEY] = entry.correlation_id
    if entry.causation_id is not None:
        metadata[CAUSATION_ID_KEY] = entry.causation_id
    return metadata
```

And as a method on `{SliceName}Projection`, beside `process_event`:

```python
    def _fire(self, entry: {EntryName}) -> None:
        """Issue the command under the entry's own metadata, swallowing failures."""
        try:
            with put_metadata_in_context(_causation_metadata(entry)):
                self._command(entry)
        except Exception as error:
            if self._already_applied(error):
                # Not a failure: the command's own idempotency guard answered,
                # and it only answers this way after replaying the entity's
                # history, so it is proof the work landed — the same proof the
                # completion event would have carried. Take it as the
                # completion signal and drop the entry here (Step 4).
                self.view.discard_entry(entry.entity_id)
                logger.info("... already applied for %s", entry.entity_id)
            else:
                # An escaping exception permanently kills the runner's
                # processing thread, stalling every later event. Log and
                # leave the entry behind.
                logger.exception("failed to ... for %s", entry.entity_id)
```

`_causation_metadata` is a module-level function, not a method — it needs nothing from
the projection but the entry it is handed. `CORRELATION_ID_KEY` and `CAUSATION_ID_KEY`
come from the shared `metadata.py` (Step 0); do not spell the strings out again here.

**The guard is mandatory, not defensive style.** An exception escaping
`process_event` kills the runner's processing thread for good: the subscription
stops, `wait()` re-raises the original error, and no subsequent event is ever
processed. A routine domain `ValueError` from the command would take down the
whole automation.

**Metadata propagation** keeps the causal chain intact exactly where no human is
involved. `EventEnvelope.metadata` defaults to `get_metadata_from_context()` and
`TaggedEventMapper` round-trips `metadata`/`uuid` through the store, so wrapping
the call in `put_metadata_in_context` is sufficient — this mirrors what
`EventSourcedProjection.process_event` does natively. `correlation_id` is carried
forward when present (omitted when absent, so an untagged trigger is not a
crash); `causation_id` is the **trigger event's own uuid**, naming the direct cause.
Never take either id from a client — `causation_id` must always resolve to a real event
in this log, and `metadata.py` sanitises the one id a client may influence.

**Carry the two ids and nothing else — in particular, never a principal.** In a project
with `auth.py` the trigger event names the caller who authenticated it, and copying that
onto the automation's own events would assert an authentication that never took place:
this command runs on a projection thread, with no request and no caller. The absence is
recoverable, not lossy — `causation_id` leads a reader straight to the event that *does*
name a principal. Assert the absence in a test, so a later well-meant copy is caught.

**`_fire` takes the entry and nothing else**, and that is deliberate: recovery (Step 4a)
fires commands with no triggering envelope in hand, so the ids have to come off the
ledger row rather than a live event. Both call sites then pass the same thing, there is
one source of truth, and a retry is recorded as the same causal step rather than a new
cause. An entry whose ids are `None` yields `{}`, and `command_metadata()` in `do()`
seeds it a fresh flow — so rows written before the ledger carried these columns degrade
safely rather than raising.

### Telemetry — wrap `process_event` in a consumer span

Wrap the `match` in `process_event` in a single `consumer_span(envelope, ...)` — the
shape is in `.build-kit/references/telemetry.md` → *Inside a `Projection`*. That one span buys the whole
causal chain here, because `_fire` runs **inside** it: the command it issues opens its
own span under the consumer span, and the events that command emits inherit the
`traceparent` from the contextvar — so a trace runs trigger event → automation →
command → emitted event, across threads and across the log. This is the payoff of the
design; nothing further is needed to get it.

Two points specific to automations:

- **`_fire`'s `except` gains one branch, not a new guard.** The no-swallow rule for
  spans governs the span around `process_event`, which must re-raise so the supervisor
  sees the thread die. `_fire`'s broad catch is the deliberate exception to that, and it
  stays exactly as broad: it is narrowly scoped to the command port, and the lingering
  ledger entry *is* the signal — except when `already_applied(error)` says the command
  already succeeded. Then there is nothing to signal: log at `info`, not `exception`,
  skip the traceback, and **discard the entry** — that guard is the completion signal,
  so leaving the entry behind would report outstanding work that is already done
  (Step 4). Record a genuine failure on the current span before logging it — do not
  widen the guard, and do not let the span's own error handling suppress what `_fire`
  already handled.
- **`drain()` fires with no envelope**, so those commands start a fresh *trace* — the
  entry stores the causal ids, not a `traceparent`, and a trace orphaned by a crash may
  already be outside the retention window anyway. Expected, not a bug to code around.
  The `correlation_id`/`causation_id` chain survives regardless, which is the whole
  reason the permanent ids and the sampled trace are kept separate.

---

## Step 4 — Recovery: the orphaned-entry problem

**Guarding the command is not enough.** Read this before assembling the runner —
it is the failure mode most easily missed.

`add_entry` commits the entry *and its tracking position* atomically. A runner
resumes at `gt=tracking_recorder.max_tracking_id(...)`, strictly **after** the last
recorded position. So once `add_entry` succeeds, that trigger event is permanently
consumed. If the command then fails, the guard swallows it, the loop moves on — and
nothing will ever revisit that entry. It sits outstanding forever. Call this the
**never-landed** case.

A second case leaves an entry outstanding too, and looks identical from the view alone,
but is not stuck: the command actually **succeeded** — its emitted event is already in
the store — and the crash landed in the gap between that success and `process_event`
running the branch that calls `remove_entry` for it. Call this the **already-applied,
pending** case. That emitted event is still ahead of `max_tracking_id`, so once the
subscription opens it *will* be redelivered and drain the entry exactly as it would after
any other success.

A third case is the one most easily missed, because the two above are both about
crashes and this one is about ordinary traffic. If the trigger event is recorded
**unconditionally** — the external route writes down every submission and lets the
automation decide — then a resubmission for an entity whose work completed long ago
arrives as a perfectly ordinary trigger. `process_event` takes an entry for it (there is
no way to know it is a duplicate without asking the command), the command refuses, and
**no redelivery is coming**: that entity's completion event was tracked when the first
submission landed, so it sits *behind* `max_tracking_id` forever. Call this the
**already-applied, never-arriving** case.

The two already-applied cases are indistinguishable from inside `_fire`, and only one of
them self-heals — so `_fire` must not wait for the completion event in either. Step 3's
`already_applied` port is what makes that safe: a command's idempotency guard only
answers after replaying the entity's own history, so a `True` answer carries the same
proof the completion event would have. `_fire` therefore treats it as the completion
signal and calls `discard_entry`. In the pending case that merely beats the redelivery
to it (`remove_entry` is then a documented no-op); in the never-arriving case it is the
only thing that ever clears the row. Leaving the entry instead does not just leak a row:
it destroys the ledger's contract that **a lingering entry means the command did not
land**, and every genuinely stuck entry becomes indistinguishable from it.

Reversing the order does not help the never-landed case; it just trades a stuck entry for
a lost command. Two mechanisms are needed, and they cover different windows:

### 4a. `drain()` — always implement this

```python
    def drain(self) -> None:
        """
        Re-issue commands for entries left outstanding by an earlier crash.

        The retry is indistinguishable from the first attempt: the entry
        carries the original `correlation_id` and `causation_id`, so a
        recovered command lands in the flow that asked for it rather than in
        one invented at restart. A retry is the same causal step, not a new
        cause.
        """
        for entry in self.view.get_entries():
            if entry.attempts > MAX_ATTEMPTS:
                continue
            self.view.count_attempt(entry.entity_id)
            self._fire(entry)
```

Call it in `create_runner()` **before** constructing the runner, so it completes
before the subscription starts. This is the baseline recovery path and the **only**
one available when the model has no failure event.

Because the supervisor restarts a dead runner by calling `create_runner()` again,
`drain()` runs on every restart too — which is exactly right: a runner that died
mid-`process_event` may have left an entry recorded but never commanded.

### 4b. Failure events — skip this section unless the model defines one

**This whole section is conditional.** If Step 1 found no failure event among the
command's outbound dependencies, the automation is complete with `drain()` alone — go to
Step 4c, then Step 5, and omit the failure port, the `{FailureEventName}` topic, the
`_retry` branch, and the failure-recording slice entirely.

If `slice.json` *does* list one (e.g. `"License cancellation failed"`), record it and add
it to `topics`. The failure then returns through the same subscription and the retry
becomes ordinary event processing — no timer, no polling, no second thread:

```python
            case {FailureEventName}(entity_id=entity_id):
                self._retry(entity_id, tracking, envelope)
```

`_retry` looks the entry up, replaces its causal ids with the failure event's
(`correlation_id` carried forward from `envelope.metadata`, `causation_id` set to
`str(envelope.uuid)`), re-records it through `add_entry`, and then calls `_fire(entry)`.
The retried command therefore names the failure event as its cause — and so does a later
`drain()` of the same entry, since the ids now live on the row.

Inject the failure recorder as a **separate optional port** with a no-op default, so the
projection class works unchanged for models that have no such event:

```python
FailurePort = Callable[[{EntryName}, str], None]


def _no_failure(entry: {EntryName}, reason: str) -> None:
    """Discard the failure — the default for models with no failure event."""
```

**This adds a third parameter to the `__init__` shown in Step 3** — the same shape as the
other two, defaulted so a model without a failure event never has to pass it:

```python
    def __init__(
        self,
        view: {SliceName}View,
        command: CommandPort = _no_command,
        already_applied: AlreadyAppliedPort = _no_already_applied,
        failure: FailurePort = _no_failure,
    ) -> None:
        super().__init__(view=view)
        self._command = command
        self._already_applied = already_applied
        self._failure = failure
```

`create_runner()` in Step 5 passes `failure=failure` to match; omit both the parameter and
that argument when the model defines no failure event.

Appending the failure event can itself fail, so wrap that call in its own
`try/except` too — otherwise the recovery path becomes a new way to kill the runner.

**The slice that records a failure needs a boundary that never matches.** Recording
a failure is an observation, not a decision: the same entity may fail repeatedly. A
DCB boundary is an append *condition*, so a selector matching prior failures makes
the second recording raise `IntegrityError`.

Give `consistency_boundary()` a selector that cannot match anything in the store: name an
event type this slice never emits, paired with a tag value nothing is ever emitted under.
Both halves matter — a real type with an unused tag still risks matching if another slice
later adopts that tag, and an unused type with a real tag is the collision below.

Note `Selector(types=[], tags=[...])` is **not** "no boundary" — it means "fail if
any event with these tags exists", which is exactly the collision to avoid.

### 4c. Attempt counting

Carry `attempts: int = 0` on the entry and increment it on every fire. Past
`MAX_ATTEMPTS`, stop firing and leave the entry **parked** — still visible in the
view for inspection, rather than retried forever.

The counter earns its keep on the failure-event path (4b), where a permanently failing
command would otherwise loop indefinitely between the failure event and the retry. But
**implement it even without a failure event**: `drain()` checks `attempts` too, so a
crash-orphaned entry whose command fails on every restart stays parked instead of
re-firing on each boot.

---

## Step 5 — Assemble the runner

The stock `ProjectionRunner` is unusable here for **two** independent reasons. It takes
*classes* — `application_class`, `projection_class`, `view_class` — and constructs
everything itself: it calls `projection_class(view=view)` with no opportunity to pass the
command port (Step 3), and it calls `application_class(env=env)`, giving the projection a
**private** event store. Under POPO that store is invisible to the routes that emit the
trigger, so the automation would never fire, and the event its own command emits would
never come back to drain the ledger.

`SharedAppProjectionRunner` (in `src/snake_case({ProjectName})/projection.py`) is the
answer to both. It subclasses `BaseProjectionRunner`, which takes an
**already-constructed** `projection`, `app`, and `tracking_recorder` — exactly the seam an
injected port needs — and it overrides `__exit__` to not close the application it was
handed. Create that module if it does not exist yet; it is shared runtime, written once
per project, never generated per slice. See `.build-kit/CLAUDE.md` → *Projection runners* for what
it must guarantee.

Building the view by hand is the one thing `ProjectionRunner` was legitimately doing for
you. That is what the `Environment` + `InfrastructureFactory.construct` lines below are —
not incidental plumbing, but the backend selection described in Step 3, written out
explicitly. Naming the `Environment` after `{SliceName}Projection.name` is what activates
the `upper_snake_case({SliceName})_PERSISTENCE_MODULE` scoping.

```python
class {SliceName}Runner(SharedAppProjectionRunner):
    """..."""

    def __init__(
        self,
        view: {SliceName}View,
        app: {ProjectName}App,
        projection: {SliceName}Projection,
    ) -> None:
        self.view = view
        self.projection = projection
        super().__init__(
            projection=projection,
            app=app,
            tracking_recorder=view,
            topics=projection.topics,
        )


def create_view(
    view_class: type[{SliceName}View] = POPO{SliceName}View,
    env: EnvType | None = None,
) -> {SliceName}View:
    """Build the {SliceName} ledger view. Called ONCE per process."""
    environment = Environment(
        {SliceName}Projection.name,
        {**os.environ, **(env or {})},
    )
    factory: InfrastructureFactory[{SliceName}View] = (
        InfrastructureFactory.construct(env=environment)
    )
    return factory.tracking_recorder(view_class)


def create_runner(
    app: {ProjectName}App,
    view: {SliceName}View,
) -> {SliceName}Runner:
    """Construct a runner driving `view` from the shared application.

    Safe to call repeatedly against the same view: the supervisor calls this
    again on every restart.
    """

    def command(entry: {EntryName}) -> None:
        app.do({CommandSliceName}Slice(...))

    # Recognizes the command's own idempotency guard — match whatever
    # exception and message {CommandSliceName}Slice actually raises for work
    # it has already done. Omit this closure and the `already_applied=`
    # argument below when the command has no such guard; `_no_already_applied`
    # is the default (Step 3), and every command exception is then logged as
    # a failure exactly as before the already-applied case (Step 4) existed —
    # and no entry is ever discarded on the strength of a guess.
    def already_applied(error: BaseException) -> bool:
        return isinstance(error, ValueError) and str(error) == "..."

    # `failure` and the `failure=` argument below are BOTH omitted when the
    # model defines no failure event; `_no_failure` is the default (Step 4b).
    def failure(entry: {EntryName}, reason: str) -> None:
        app.do(Record{FailureEventName}Slice(..., reason=reason))

    projection = {SliceName}Projection(
        view=view,
        command=command,
        already_applied=already_applied,
        failure=failure,
    )
    # Recover work left outstanding before the subscription resumes. The
    # never-landed case (Step 4) needs this: that trigger event is past
    # max_tracking_id and will never be redelivered. The already-applied,
    # pending case does not — its emitted event is still ahead of
    # max_tracking_id and would drain the entry on its own — but drain()
    # retries it anyway, and `already_applied` above turns that into the
    # discard rather than a logged failure. For an already-applied entry whose
    # completion event is *behind* max_tracking_id, that same discard is the
    # only thing that ever clears it.
    projection.drain()
    return {SliceName}Runner(view=view, app=app, projection=projection)
```

Points that matter:

- **The closure over `app` is the whole trick** — the projection reaches the write side
  through a callable it neither owns nor can introspect. And because `app` is now the
  *shared* application, the emitted event travels back through the same subscription,
  which is what closes the loop drawn at the top of this file.
- **`{SliceName}Runner` must set `self.view` itself.** `BaseProjectionRunner` only stores
  `self._tracking_recorder`; `self.view` was a `ProjectionRunner`-only attribute.
- **`drain()` must run before the runner is constructed** — constructing it opens the
  subscription and starts the processing thread.
- **`env` now scopes the view only.** The event store's `PERSISTENCE_MODULE` is
  configured where `{ProjectName}App` is constructed, not here. Moving the view to
  Postgres therefore means moving the write side separately — they are no longer coupled
  through one `env` mapping.

### Wiring it into the app lifespan

The runner is started by the process-wide `ProjectionSupervisor` in
`src/snake_case({ProjectName})/main.py`, which restarts it if the processing thread dies.

The block below is the **body of the existing `lifespan`**, not a whole file — edit
`main.py` in place. It already holds `create_app()` with `configure_telemetry()` and
`instrument_app(app)` from *First-time project setup* (`.build-kit/CLAUDE.md` → step 6);
leave both alone. If this is the project's first projection, the lifespan is still the
`with {ProjectName}App() as dcb_app:` form — upgrading it to an `AsyncExitStack` must carry
`instrument_recorder(dcb_app)` across, or the event store silently stops being traced.

```python
async with AsyncExitStack() as stack:
    dcb_app = stack.enter_context({ProjectName}App())   # entered FIRST -> closed LAST
    instrument_recorder(dcb_app)                        # AFTER construction — `recorder` is set in `__init__`
    supervisor = ProjectionSupervisor(context_name=dcb_app.context_name)

    snake_case({SliceName})_view = create_view()
    stack.callback(snake_case({SliceName})_view.close)  # runs AFTER the supervisor stops
    supervisor.register(
        "snake_case({SliceName})",
        snake_case({SliceName})_view,
        partial(create_runner, dcb_app, snake_case({SliceName})_view),
    )

    stack.enter_context(supervisor)                     # exits BEFORE the app
    yield {
        "dcb_app": dcb_app,
        "snake_case({SliceName})_view": snake_case({SliceName})_view,
        "projection_supervisor": supervisor,
    }
```

- **`stack.enter_context`, not `enter_async_context`** — both are sync context managers.
- **Enter order is the teardown contract.** The application goes in first so it closes
  last; every runner stops before the store it reads from does.
- **`stack.callback(view.close)` goes *before* the supervisor is entered**, so it runs
  *after* the supervisor stops. The view owns a connection pool of its own —
  `create_view()` builds a `PostgresDatastore` through its own factory, separate from
  the one `{ProjectName}App` holds — and nothing else ever closes it. Closing it while a
  projection thread is still writing through it would fail. A no-op on POPO, which is
  why the leak stays invisible until the ledger is made durable.
- **`instrument_recorder(dcb_app)` stays immediately after the application is entered**,
  before the supervisor exists — the runners it starts read through that recorder, so
  instrumenting later leaves the first events they process untraced.
- **One supervisor per process**, shared with every other projection. Adding this
  automation to an app that already has one adds a `create_view()` + `register(...)` pair
  and one key in the yielded mapping — nothing else.
- **`register()` before entering the supervisor**; entering is what starts the runners.
- **The lifespan yields the view, never the runner.** The supervisor swaps runners on
  restart, so a reference held elsewhere could point at a dead one. Automations often
  need no route at all — yield the view anyway, because that is what the tests in Step 7
  synchronize on.
- **An automation with no HTTP surface still belongs in this lifespan.** Do not give it
  its own process or its own application; the loop only closes over a shared store.
- **If this is the project's first supervisor, add `health.py` in the same step** — the
  `/livez` and `/readyz` routes, the lag gauge and the 503 test are all in
  `.build-kit/CLAUDE.md` → *Supervising projections* → *The `/livez` and `/readyz`
  routes*. An automation needs them more than a
  view does, not less: a view at least has a route someone can notice going stale, whereas
  a dead automation has no symptom at all until someone asks why nothing happened. Leave
  the routes untouched if an earlier slice already added them.
- **Count restarts here.** The supervisor is the only place that knows a runner died, so
  increment a restart counter where it rebuilds one, and expose lag as the gauge described
  in that same section. An automation with no route needs this more than a view does, not less:
  without an HTTP surface, a silently dead automation has no other symptom.

---

## Step 6 — Acceptance tests

File: `tests/acceptance/snake_case({Context})/snake_case({SliceName})/test_snake_case({SliceName}).py`

GWT cannot drive a `Projection` (see `.build-kit/CLAUDE.md`), so construct the view and
projection directly and call `process_event` yourself. Pass a recording fake as
the command port — no app, no runner, no thread:

```python
class _Recorder:
    """Command port that records each call along with its ambient metadata."""

    def __init__(self) -> None:
        self.calls: list[tuple[{EntryName}, dict[str, str]]] = []

    def __call__(self, entry: {EntryName}) -> None:
        self.calls.append((entry, get_metadata_from_context()))
```

Capturing `get_metadata_from_context()` **at call time** is what lets you assert
on propagation; the context is unwound by the time the test body resumes.

Build the pair in a fixture and drive it directly:

```python
@pytest.fixture
def recorder() -> _Recorder:
    """Return a command port that records its calls."""
    return _Recorder()


@pytest.fixture
def projection(recorder: _Recorder) -> {SliceName}Projection:
    """Return a projection over a fresh in-memory view."""
    return {SliceName}Projection(view=POPO{SliceName}View(), command=recorder)


def test_trigger_fires_the_command(
    projection: {SliceName}Projection, recorder: _Recorder,
) -> None:
    """The trigger records an entry and fires the command."""
    envelope = TaggedEvent(
        decision={TriggerEventName}(entity_id="a", field="value"),
        tags=["{{entity_kind}}:a"],
        metadata={"correlation_id": "corr-1"},
    )
    projection.process_event(envelope, Tracking("upstream", 1))

    entry, metadata = recorder.calls[0]
    assert entry.entity_id == "a"
    assert metadata["causation_id"] == str(envelope.uuid)
    assert metadata["correlation_id"] == "corr-1"
    assert projection.view.get_entries() == [entry]


def test_emitted_event_drains_the_entry(projection: {SliceName}Projection) -> None:
    """The event the command emits removes the outstanding entry."""
    projection.process_event(
        TaggedEvent(
            decision={TriggerEventName}(entity_id="a", field="value"),
            tags=["{{entity_kind}}:a"],
        ),
        Tracking("upstream", 1),
    )
    projection.process_event(
        TaggedEvent(
            decision={EmittedEventName}(entity_id="a"),
            tags=["{{entity_kind}}:a"],
        ),
        Tracking("upstream", 2),
    )
    assert projection.view.get_entries() == []
```

Cover, in the same style:
- the **entry itself** stores `correlation_id` and `causation_id`, and the metadata `_fire` used equals exactly those two — that is what `drain()` later recovers from
- a trigger with no `correlation_id` still fires cleanly, carrying `causation_id` alone
- **a command that raises does not propagate**, and leaves the entry in place
- `drain()` re-fires an entry seeded straight into the view (the orphan case) **under that entry's own causal ids**, fires an entry with no stored ids under `{}` rather than raising, skips one already past `MAX_ATTEMPTS`, and is a no-op on a clean view
- when a failure event exists: the failure port is called on failure, the failure event drives another attempt, attempts stop at `MAX_ATTEMPTS`, and the exhausted entry stays parked

### Acceptance-test notes

- **Use strictly increasing `Tracking` notification ids** — pick any
  `context_name` string (consistent within a test) and never reuse an id; reuse
  raises `IntegrityError` from `_assert_tracking_uniqueness`.
- **Always test against `POPO{SliceName}View`,** even when the slice also ships a
  Postgres implementation. It needs no configuration, no server, and no cleanup.
  The behaviour under test is the projection's dispatch logic, which is
  backend-independent by construction.
- **This bypasses `topics` filtering entirely.** Calling `process_event` directly
  means the test is responsible for only sending events the projection is meant
  to handle. A test asserting the projection *ignores* an unrelated event type is
  still valid and useful — send a `TaggedEvent` for a type not in the `match`,
  and assert `view.max_tracking_id(...)` still advanced (proving the `case _:`
  branch tracked it) while the entries stayed unaffected.

---

## Step 7 — Integration tests

File: `tests/integration/snake_case({Context})/test_snake_case({SliceName}).py`

Seed history as **raw `TaggedEvent`s** through the **shared application** — never by
driving another slice, and never through a privately-constructed one. The runner
subscribes to the application the lifespan opened, so that is the only store whose
appends it can see.

If the project has an HTTP surface, take both the app and the view from the real
`create_app()`'s lifespan state via the shared `client` fixture (see `.build-kit/CLAUDE.md` →
*Test layout*):

```python
@pytest.fixture
def dcb_app(client: TestClient) -> {ProjectName}App:
    """Return the application opened by the real app's lifespan."""
    return client.app_state["dcb_app"]


@pytest.fixture
def view(client: TestClient) -> {SliceName}View:
    """Return the ledger view built by the projection lifespan."""
    return client.app_state["snake_case({SliceName})_view"]
```

Otherwise build the same three objects the lifespan would, in the same order:

```python
@pytest.fixture
def dcb_app() -> Iterator[{ProjectName}App]:
    """Open one application, closed after every runner that reads from it."""
    with {ProjectName}App() as app:
        yield app


@pytest.fixture
def view() -> {SliceName}View:
    """Build the ledger view once; runners come and go over it."""
    return create_view()


@pytest.fixture
def runner(
    dcb_app: {ProjectName}App, view: {SliceName}View,
) -> Iterator[{SliceName}Runner]:
    """Run the automation for the duration of one test."""
    with create_runner(dcb_app, view) as started:
        yield started
```

Fixture teardown is LIFO by dependency, so `runner` exits before `dcb_app` — the same
ordering the lifespan's `AsyncExitStack` enforces in production.

The seeding fixture then appends through the app and waits on the **view**:

```python
@pytest.fixture
def trigger(
    dcb_app: {ProjectName}App,
    view: {SliceName}View,
    runner: {SliceName}Runner,
    entity_id: str,
) -> TaggedEvent[Decision]:
    """Seed the trigger event and wait for the automation to settle."""
    seed = TaggedEvent(
        decision={TriggerEventName}(entity_id=entity_id, field="value"),
        tags=[f"{{entity_kind}}:{entity_id}"],
        metadata={"correlation_id": "corr-1"},
    )
    position = dcb_app.events.append(events=[seed])
    view.wait(
        context_name=dcb_app.context_name,
        notification_id=position + 1,
        timeout=5,
    )
    return seed
```

**Wait on the view, not the runner.** `TrackingRecorder.wait`'s `interrupt` argument is
optional, so the view synchronizes on its own — which matches production, where the
supervisor keeps runners private. The `runner` fixture still appears in the signature:
it is what must be *running* for the wait to ever succeed, even though nothing calls it.

Three things reliably bite:

**Wait one position past the seed.** `append()` returns the position of the event
it wrote; the automation appends its own event immediately after, so
`position + 1` covers the full feedback loop and the assertions see a settled
view. Never `time.sleep`.

**Tag the seed for the command's boundary.** The seed must carry every tag the
command slice's `consistency_boundary()` selects on. If the selector's tags are
not a subset of the trigger's tags, the replay misses the event and the command
runs against a history in which it never happened.

**Set `metadata` on the seed to test propagation.** `metadata` and `uuid`
round-trip through the store, so a seeded `correlation_id` is what the
projection reads back — and `seed.uuid` is the value the emitted event's
`causation_id` must equal. Returning the whole `TaggedEvent` from the fixture is
what makes that assertion possible:

```python
def test_carries_causation(dcb_app, trigger) -> None:
    """The command names the trigger as its cause."""
    emitted = ...  # read the appended event back off dcb_app.events.read()
    assert emitted.metadata["causation_id"] == str(trigger.uuid)
    assert emitted.metadata["correlation_id"] == "corr-1"
```

Seeding is unconditional — `append()` builds a DCB append condition only when
`cb`/`after` is given — so repeated seeds into the same entity's tags all
succeed. That is what removes the need for a test-only seeding `Slice`, its
hand-scoped `consistency_boundary()`, and the `execute()`-before-`save()` step
that a `Slice`-based seed would require.

Also assert here that entities are handled independently — GWT cannot prove
cross-entity isolation.

**Prove the recovery paths against real infrastructure too.** This test applies to
every automation, and it is the one case that must **not** use the `runner`
fixture: it needs to consume the position *before* any runner exists, then start
one — so it depends on `dcb_app` and `view` only.

```python
def test_drain_recovers_an_orphaned_entry(
    dcb_app: {ProjectName}App, view: {SliceName}View, entity_id: str,
) -> None:
    """An entry left outstanding by a crash is re-fired on restart."""
    position = dcb_app.events.append(
        events=[
            TaggedEvent(
                decision={TriggerEventName}(entity_id=entity_id, field="value"),
                tags=[f"{{entity_kind}}:{entity_id}"],
            ),
        ],
    )
    # A crashed run: entry and tracking committed, command never landed.
    view.add_entry(
        {EntryName}(entity_id=entity_id, field="value"),
        Tracking(dcb_app.context_name, position),
    )

    # create_runner() drains before subscribing, exactly as on a supervisor restart.
    with create_runner(dcb_app, view):
        view.wait(
            context_name=dcb_app.context_name,
            notification_id=position + 1,
            timeout=5,
        )
        assert view.get_entries() == []
```

This is also the test that proves a **supervisor restart** recovers correctly: the
restart path is precisely "call `create_runner(app, view)` again over the same view",
and the fresh runner resumes at `max_tracking_id` rather than replaying from zero.

The test above is the never-landed case. Write a second one for the already-applied case
(Step 4a) — it needs `import logging` alongside `pytest` for `caplog`:

```python
def test_drain_retries_an_already_applied_entry_without_failing(
    dcb_app: {ProjectName}App,
    view: {SliceName}View,
    entity_id: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """drain()'s retry of an already-applied entry is not logged as a failure."""
    position = dcb_app.events.append(
        events=[
            TaggedEvent(
                decision={TriggerEventName}(entity_id=entity_id, field="value"),
                tags=[f"{{entity_kind}}:{entity_id}"],
            ),
        ],
    )
    # A crashed run: entry and tracking committed at the trigger's position —
    # but this time the command DID succeed. Its emitted event is already in
    # the store, just never tracked, because the crash landed before
    # process_event's {EmittedEventName} branch ran.
    view.add_entry(
        {EntryName}(entity_id=entity_id, field="value"),
        Tracking(dcb_app.context_name, position),
    )
    dcb_app.events.append(
        events=[
            TaggedEvent(
                decision={EmittedEventName}(entity_id=entity_id),
                tags=[f"{{entity_kind}}:{entity_id}"],
            ),
        ],
    )

    # drain() re-fires the command and hits its own idempotency guard; the
    # already_applied port (Step 3) is what keeps that from being logged as a
    # failure and discards the entry on the spot. The emitted event appended
    # above would remove it too, once redelivered — this is the one case where
    # both paths work, which is why the never-arriving case needs its own test.
    with caplog.at_level(logging.ERROR), create_runner(dcb_app, view):
        view.wait(
            context_name=dcb_app.context_name,
            notification_id=position + 1,
            timeout=5,
        )
        assert view.get_entries() == []
    assert caplog.records == []
```

This test only proves something if `{CommandSliceName}Slice` actually raises on a
repeat, which any command with a consistency boundary and a "this already happened"
check does. A command with no such guard has no already-applied case to distinguish —
omit the `already_applied` port (Step 3) and this test along with it.

**Add a second test for the never-arriving case** whenever the trigger event is recorded
unconditionally. Seed a trigger, let it complete, then seed a *second* trigger for the
same entity and assert the ledger ends up empty and the command emitted nothing the
second time. It is the discard in `_fire`, not a redelivery, that has to clear it — the
test above cannot tell the two apart, because there both paths lead to an empty view.
Synchronise on a **later** event's position, not the duplicate's own: `add_entry` writes
the duplicate's position *before* the command runs, so waiting on it can observe the
entry in the window before the discard. Appending one unrelated in-topic event as a
barrier is enough — events are processed in order.

**Seeding into a live runner's view raises `IntegrityError`.** The subscription
processes that same event and calls `add_entry` with the same `Tracking`, so
whichever write loses the race trips `_assert_tracking_uniqueness` — on the
background thread, surfacing later out of `wait()` rather than at the call. The
ordering above is also the more faithful simulation: a crash consumes the
position *before* the process restarts, which is exactly why `drain()` has to run
before the runner is constructed (Step 5).

For the retry loop — **only when the model has a failure event** — run a persistently
failing command under a real runner and wait for `position + MAX_ATTEMPTS`, since each
attempt appends one failure event.

**Prove the loop actually closes over a shared store.** Drive the HTTP route that emits
the trigger, then assert the automation's command landed — read the emitted event back
off `dcb_app.events.read()`, or check the ledger drained. A privately-constructed
application passes every seeded-event test above and fails this one, which is why it is
worth writing.

---

## Files to create

```
src/snake_case({ProjectName})/
    projection.py                     # SHARED RUNTIME — SharedAppProjectionRunner + ProjectionSupervisor (create ONLY if absent; never per-slice)
    metadata.py                       # SHARED RUNTIME — verified in Step 0; supplies the causal id keys, never written per-slice
    telemetry.py                      # SHARED RUNTIME — verified in Step 0; supplies `consumer_span`, never written per-slice
    main.py                           # EDITED, not created — view, supervisor registration, and health.py's router if this is the first supervisor
    health.py                         # SHARED RUNTIME — /livez + /readyz; create ONLY if this slice registers the project's first supervisor

src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/
    __init__.py
    projection.py                     # view, projection, runner, create_view, create_runner
    slice.py                          # only if the model defines a failure event

tests/unit/
    test_projection.py                # SHARED RUNTIME — the upgrade tripwire; create alongside projection.py, never per-slice

tests/acceptance/snake_case({Context})/snake_case({SliceName})/
    test_snake_case({SliceName}).py   # direct process_event calls, fake ports, drain()

tests/integration/snake_case({Context})/
    test_snake_case({SliceName}).py   # shared app, seeded events, wait() on the view, recovery
```

No `routes.py`. No `__init__.py` under `tests/`.

`src/snake_case({ProjectName})/projection.py` is **shared runtime, not generated per
slice** — it holds the one hand-written `__exit__` override that couples to private
`BaseProjectionRunner` attributes, plus the supervisor every projection registers with.
If it already exists, import from it and change nothing.

The command slice under
`src/snake_case({ProjectName})/snake_case({Context})/snake_case({CommandSliceName})/` is
**not** listed here — it is build-state-change's artifact (Step 2), with its own tests.

---

## Checklist

- [ ] `Projection.name` set explicitly (it scopes env vars and table names)
- [ ] `topics` includes **both** the trigger event and the event the command emits
- [ ] The command port is injected via `__init__`, with a no-op default
- [ ] `add_entry(..., tracking)` runs **before** the command is fired
- [ ] Every `process_event` branch persists tracking, including `case _:`
- [ ] The command call is wrapped in `try/except Exception` + `logger.exception(...)`
- [ ] `_fire` takes the entry alone and derives its metadata via `_causation_metadata(entry)`; no call site passes a `dict`
- [ ] The entry carries `correlation_id` and `causation_id` (nullable), populated on the trigger and persisted by every view implementation — including the Postgres `CREATE TABLE`, the `INSERT`'s `DO UPDATE SET`, and the `SELECT`
- [ ] `drain()` implemented, and called in `create_runner()` **before** the runner is constructed
- [ ] `{SliceName}Runner` subclasses `SharedAppProjectionRunner`, and sets `self.view` itself
- [ ] `src/snake_case({ProjectName})/projection.py` exists (created only if absent — never per-slice)
- [ ] `tests/unit/test_projection.py` asserts the runner's `__exit__` never calls `app.close()`, and that assertion was watched to fail against a deliberately reintroduced close
- [ ] `create_view()` and `create_runner(app, view)` are separate; the runner never constructs a `DcbApplication`
- [ ] `create_runner(app, view)` is safe to call repeatedly over the same view — that is the supervisor's restart path
- [ ] The lifespan enters the application **first** and the supervisor after it, both via `stack.enter_context`, and yields the view (never the runner)
- [ ] Entries carry `attempts`; firing stops past `MAX_ATTEMPTS` and the entry stays parked
- [ ] `get_entries()` takes no argument and returns copies, so callers cannot mutate view state
- [ ] `add_entry` / `remove_entry` write the entry and its `Tracking` in one atomic step
- [ ] `discard_entry` and `count_attempt` take **no** `Tracking`, and the two deleting methods share one private helper on the Postgres view
- [ ] `view_class` subclasses the recorder matching the configured `PERSISTENCE_MODULE` (POPO unless durability was asked for)
- [ ] If the model has a failure event: it is in `topics`, injected as a separate optional port, its recording is itself guarded, and its slice's boundary can never match
- [ ] If the model has **no** failure event: no failure port, no third topic, no `_retry` branch — `drain()` alone is the recovery path
- [ ] `correlation_id` carried forward (omitted if absent); `causation_id` set to `str(envelope.uuid)` — never taken from a client
- [ ] Acceptance tests call `process_event` directly with strictly increasing tracking ids
- [ ] Integration tests seed raw `TaggedEvent`s through the **shared** application, and `view.wait(context_name=..., notification_id=position + 1, ...)` rather than sleeping
- [ ] The seeded event carries every tag the command slice's boundary selects on
- [ ] The `drain()` recovery test builds its own view and app, consuming the position **before** any runner is constructed
- [ ] If the command has an idempotency guard, `already_applied` is injected in `create_runner()` and a test proves `drain()`'s retry of an already-applied entry logs no failure (`caplog` at `ERROR`) and leaves no entry behind — `_fire` discards it rather than waiting for the emitted event
- [ ] If the trigger event is recorded unconditionally, a test proves a duplicate trigger for completed work leaves the ledger empty, synchronised on a later event's position than the duplicate's own
- [ ] No `routes.py` created (automations are not exposed via HTTP) — `health.py` is the one exception, and it is operational, not this slice's surface
- [ ] `health.py` exists with `/livez` and `/readyz`, its router is `include_router`'d last in `create_app()`, and its 503 path has an integration test; added here if this slice registered the project's first supervisor, left untouched if an earlier one did
- [ ] Step 0 ran: `command.py`, `metadata.py`, `telemetry.py`, `application.py`, `main.py`, `projection.py` and `tests/unit/test_projection.py` all exist, and anything created there was committed as a `chore:` before this slice
- [ ] `process_event`'s `match` is wrapped in one `consumer_span(envelope, ...)` that re-raises, and `_fire`'s broad guard stays narrowly scoped to the command port — its already-applied branch logs at `info`, everything else still at `exception`
