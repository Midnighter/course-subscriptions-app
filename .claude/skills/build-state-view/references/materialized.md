# Materialized State View

Steps 3–7 for the **materialized** approach: a background subscription
consumes events as they are recorded and updates a standalone read model. The
FastAPI route only ever reads that model — it never touches the event store.
Read this only after Step 1 of `SKILL.md` selected this approach.

> **The central-application pattern applies here too.** Command slices, on-demand
> views, materialized views and automations all share the one process-wide
> `{ProjectName}App` (see `.build-kit/CLAUDE.md` → *Application wiring*). The stock
> `ProjectionRunner` cannot be used, because it takes an application **class** and
> constructs its own instance (`application_class(env=env)`) — under POPO that
> private store is invisible to the routes doing the writing, so the view would
> never update. Use `SharedAppProjectionRunner` from
> `src/snake_case({ProjectName})/projection.py` instead, as Step 3 does.

The projection is expressed as an `eventsourcing.projection.Projection`
subclass whose `process_event(envelope, tracking)` mutates a
`TrackingRecorder` (the materialized view). A runner wires the **shared**
`{ProjectName}App`, the `Projection`, and the view together, subscribes to the
application in a background thread, and keeps the view's tracked position in
sync. The FastAPI lifespan builds the view, registers a runner factory with the
process-wide `ProjectionSupervisor`, and yields the **view** to the route.

The view is a `TrackingRecorder`, and **which concrete recorder backs it is a
deployment choice, not a design choice** — see *Choosing the view's backend*
below. Write the view as an abstract interface plus one implementation per
backend you actually deploy; everything downstream (projection, route, tests)
depends only on the interface.

```text
{ProjectName}App.do(slice)                (write side, unchanged — SHARED instance)
    │  records TaggedEvent
    ▼
{SliceName}Runner                         (background thread)
    │  subscribes via app.application_subscription(topics=...)
    │  supervised: rebuilt over the same view if its thread dies
    ▼
{SliceName}Projection.process_event(envelope, tracking)
    │  mutates the view and calls view.insert_tracking(tracking) exactly once
    ▼
{SliceName}View (TrackingRecorder)        (the materialized state)
    │                                     backed by POPO or Postgres
    │                                     outlives every runner
    ▼
GET route reads {SliceName}View directly  (no event-store access)
```

---

## Choosing the view's backend

`{SliceName}View` extends `eventsourcing.persistence.TrackingRecorder`, which
is abstract. The library ships two concrete tracking recorders usable with a
DCB application:

| Recorder | Module | Use for |
|----------|--------|---------|
| `POPOTrackingRecorder` | `eventsourcing.popo` | In-memory. The default, and what every test in Steps 5–6 uses. Lost on process exit. |
| `PostgresTrackingRecorder` | `eventsourcing.postgres` | Durable, shared across processes. Production. |

> The library also ships a `SQLiteTrackingRecorder`, but **do not use it here.**
> The DCB write side provides factories only for `eventsourcing.dcb.popo` and
> `eventsourcing.dcb.postgres_tt`, so a `DcbApplication` cannot run on SQLite —
> and a durable view paired with a non-durable event store buys nothing. POPO
> and Postgres are the two supported options.

You do not choose the backend by importing a different class at the call site.
`create_view()` (Step 3) builds the view through
`InfrastructureFactory.construct(...).tracking_recorder(view_class)`, and the
factory is resolved from the **`PERSISTENCE_MODULE` environment variable**. So:

- The `view_class` you pass to `create_view()` must subclass the concrete
  recorder matching the configured factory — a `POPOFactory` asserts
  `issubclass(view_class, POPOTrackingRecorder)`, `PostgresFactory` asserts
  `PostgresTrackingRecorder`. Passing the wrong pair fails at startup.
- The environment is **name-scoped to `Projection.name`**. `Environment.get`
  tries `{NAME.upper()}_{KEY}` before the bare `{KEY}`, so
  `upper_snake_case({SliceName})_PERSISTENCE_MODULE` configures *this view* without
  touching the application's own persistence. The view and the event store
  can sit on different backends.
- **`env` here scopes the view only.** The runner no longer constructs an
  application, so the write side's `PERSISTENCE_MODULE` is configured once where
  `{ProjectName}App` is created in `main.py` — not through this `env`.

Default to **POPO only** unless the slice definition asks for durability. Write
the Postgres implementation only when it is actually deployed — an unused
second implementation is dead code that still has to be maintained. The
abstract `{SliceName}View` interface is what makes adding it later cheap.

### Backend-specific mechanics

The two implementations do not share a locking or persistence idiom — do not
copy one into the other:

| | POPO | Postgres |
|---|---|---|
| Mutual exclusion | `with self._database_lock:` (an `RLock`, POPO-only — it does not exist on the SQL recorders) | a database transaction: `with self.datastore.transaction(commit=True) as curs:` |
| Recording tracking | `self._insert_tracking(tracking)` | `self._insert_tracking(curs, tracking)` — takes the cursor, so the entry and its tracking row commit atomically |
| Duplicate-tracking check | call `self._assert_tracking_uniqueness(tracking)` yourself | enforced by the tracking table's primary key; `_insert_tracking` raises `IntegrityError` |
| Extra tables | none — plain Python containers on `self` | append `CREATE TABLE IF NOT EXISTS ...` to `self.sql_create_statements` in `__init__`; the factory calls `create_table()` when `CREATE_TABLE` is not disabled |

In both cases the point is the same: **the entry and its `Tracking` must be
persisted in one atomic step**, so a crash can't leave the view holding data
whose position was never recorded (or vice versa). That is what `add_entry`
takes a `tracking` argument for.

---

## Step 3 — Create `projection.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/projection.py`

Five pieces live here: the view's abstract interface, its concrete
implementation(s), the `Projection` that feeds it, a `{SliceName}Runner`, and the
module-level `create_view()` / `create_runner()` factories used by both the app
lifespan (Step 7) and the tests (Steps 5–6).

**The two factories are separate on purpose.** The view must be built exactly
once — the route holds a reference to it through lifespan state, and the
supervisor rebuilds the *runner* over that same view when a projection thread
dies. If `create_runner()` also built the view, every restart would strand the
route on a stale object. So `create_view()` is called once at startup;
`create_runner(app, view)` may be called many times.

Pick the consistency boundary as described under *Consistency boundary tags*
in `SKILL.md`, but note that here it constrains the `topics` the subscription
cares about, not a `Selector`. A materialized view has no per-entity replay
boundary at query time: `process_event` fires for *every* recorded event whose
type is in `topics`, and the view itself decides how to key its state
(typically by an id carried on the event body, not by DCB tags, since a
background subscription cannot scope itself to "one entity").

```python
import os
from abc import abstractmethod
from collections import defaultdict
from dataclasses import dataclass

from eventsourcing.domain import TaggedEvent
from eventsourcing.persistence import (
    InfrastructureFactory,
    Tracking,
    TrackingRecorder,
)
from eventsourcing.popo import POPOTrackingRecorder
from eventsourcing.projection import Projection
from eventsourcing.pydantic import Decision
from eventsourcing.utils import Environment, EnvType, get_topic

from snake_case({ProjectName}).application import {ProjectName}App
from snake_case({ProjectName}).projection import SharedAppProjectionRunner
from snake_case({ProjectName}).snake_case({Context}).events import {EventName}


@dataclass
class {EntryName}:
    """A single entry projected into the {SliceName} view."""

    field1: str
    field2: int


class {SliceName}View(TrackingRecorder):
    """Abstract materialized view for {SliceName}."""

    @abstractmethod
    def get_entries(self, entity_id: str) -> list[{EntryName}] | None:
        """Return the entries for `entity_id`, or None if none were ever recorded."""

    @abstractmethod
    def add_entry(
        self,
        entity_id: str,
        entry: {EntryName},
        tracking: Tracking,
    ) -> None:
        """Append an entry for `entity_id`, atomically with the tracking position."""

    def close(self) -> None:
        """Release whatever the backend holds open. Idempotent.

        Deliberately concrete, not abstract: a backend with nothing to release
        inherits this no-op, which is what lets the lifespan call `close()`
        without knowing which implementation it built.
        """


class POPO{SliceName}View(POPOTrackingRecorder, {SliceName}View):
    """In-memory {SliceName} view, backed by the POPO tracking recorder."""

    def __init__(self) -> None:
        super().__init__()
        self._entries: dict[str, list[{EntryName}]] = defaultdict(list)

    def get_entries(self, entity_id: str) -> list[{EntryName}] | None:
        """Return the entries for `entity_id`, or None if none were ever recorded."""
        with self._database_lock:
            if entity_id not in self._entries:
                return None
            return list(self._entries[entity_id])

    def add_entry(
        self,
        entity_id: str,
        entry: {EntryName},
        tracking: Tracking,
    ) -> None:
        """Append an entry for `entity_id`, atomically with the tracking position."""
        with self._database_lock:
            self._assert_tracking_uniqueness(tracking)
            self._entries[entity_id].append(entry)
            self._insert_tracking(tracking)


class {SliceName}Projection(Projection[{SliceName}View, TaggedEvent[Decision]]):
    """Projection that maintains the {SliceName} materialized view."""

    name = "snake_case({SliceName})"
    topics = (get_topic({EventName}),)

    def process_event(
        self,
        envelope: TaggedEvent[Decision],
        tracking: Tracking,
    ) -> None:
        """Dispatch on decision type and update the view, or just track the position."""
        match envelope.decision:
            case {EventName}(field1=field1, field2=field2, entity_id=entity_id):
                self.view.add_entry(
                    entity_id,
                    {EntryName}(field1=field1, field2=field2),
                    tracking,
                )
            case _:
                self.view.insert_tracking(tracking)


class {SliceName}Runner(SharedAppProjectionRunner):
    """Runs the {SliceName} projection over the shared application."""

    def __init__(
        self,
        view: {SliceName}View,
        app: {ProjectName}App,
        projection: {SliceName}Projection,
    ) -> None:
        """Subscribe the projection to the shared application."""
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
    """Build the {SliceName} materialized view.

    Call this exactly once per process: the route depends on the object it
    returns, and the supervisor rebuilds runners over it. Defaults to the
    in-memory view; pass `view_class` plus a matching `env` (see *Choosing the
    view's backend*) to materialize into Postgres instead.
    """
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
    """Construct a runner feeding `view` from the shared application.

    Safe to call repeatedly with the same `view`: a fresh runner resumes at
    the view's `max_tracking_id`, which is how the supervisor restarts a dead
    projection without replaying from zero.
    """
    return {SliceName}Runner(
        view=view,
        app=app,
        projection={SliceName}Projection(view=view),
    )
```

`EnvType` comes from `eventsourcing.utils` (it is just `Mapping[str, str]`).

`{SliceName}Runner` sets `self.view` itself — `BaseProjectionRunner` only stores
`self._tracking_recorder`; `self.view` was a `ProjectionRunner`-only attribute.

The `Environment` + `InfrastructureFactory` lines are the one thing
`ProjectionRunner` used to do for you. They are not incidental plumbing: naming
the `Environment` after `{SliceName}Projection.name` is what activates the
`upper_snake_case({SliceName})_PERSISTENCE_MODULE` scoping described above.

### Optional — a Postgres-backed implementation

Add this **only if the slice is deployed with a durable view**; otherwise the
POPO implementation above is the whole story. Note how it differs from the POPO
version on every point in the *Backend-specific mechanics* table:

```python
from eventsourcing.postgres import PostgresDatastore, PostgresTrackingRecorder
from psycopg.sql import SQL, Identifier


class Postgres{SliceName}View(PostgresTrackingRecorder, {SliceName}View):
    """Durable {SliceName} view, backed by the Postgres tracking recorder."""

    def __init__(self, datastore: PostgresDatastore, **kwargs) -> None:
        # Append the DDL *after* super().__init__(): PostgresRecorder.__init__
        # assigns `sql_create_statements` a fresh list, so an earlier append is
        # silently discarded and the entries table is never created.
        super().__init__(datastore, **kwargs)
        self.entries_table_name = "snake_case({SliceName})_entries"
        self.check_identifier_length(self.entries_table_name)
        self.sql_create_statements.append(
            SQL(
                "CREATE TABLE IF NOT EXISTS {0}.{1} ("
                "entity_id text, field1 text, field2 bigint)",
            ).format(
                Identifier(self.datastore.schema),
                Identifier(self.entries_table_name),
            ),
        )

    def get_entries(self, entity_id: str) -> list[{EntryName}] | None:
        """Return the entries for `entity_id`, or None if none were ever recorded."""
        with self.datastore.transaction(commit=False) as curs:
            curs.execute(
                SQL("SELECT field1, field2 FROM {0}.{1} WHERE entity_id=%s").format(
                    Identifier(self.datastore.schema),
                    Identifier(self.entries_table_name),
                ),
                (entity_id,),
            )
            rows = curs.fetchall()
        if not rows:
            return None
        return [
            {EntryName}(field1=row["field1"], field2=row["field2"]) for row in rows
        ]

    def add_entry(
        self,
        entity_id: str,
        entry: {EntryName},
        tracking: Tracking,
    ) -> None:
        """Append an entry for `entity_id`, atomically with the tracking position."""
        with self.datastore.transaction(commit=True) as curs:
            self._insert_tracking(curs, tracking)
            curs.execute(
                SQL("INSERT INTO {0}.{1} VALUES (%s, %s, %s)").format(
                    Identifier(self.datastore.schema),
                    Identifier(self.entries_table_name),
                ),
                (entity_id, entry.field1, entry.field2),
            )

    def close(self) -> None:
        """Close the connection pool. Idempotent."""
        self.datastore.close()
```

The `datastore` argument is supplied by `PostgresFactory.tracking_recorder`,
which also passes a `tracking_table_name` derived from `Projection.name` and
calls `create_table()` — so `__init__` must accept and forward `**kwargs`
rather than fixing its own signature. **Leave `**kwargs` unannotated**
(`.build-kit/CLAUDE.md` → *Pre-commit compliance rules*).

**`_insert_tracking` comes first in a mutating method**, before any domain
statement. It is the Postgres counterpart to POPO's explicit
`_assert_tracking_uniqueness` guard: a redelivered or stale notification raises
`IntegrityError` there and the transaction rolls back either way, so ordering it
first simply means the redelivery does no domain work at all.

**Add `close()` to the abstract interface too, not just here.** The lifespan
calls it without knowing which implementation it holds.

To run against it, pass the class and a matching environment to `create_view()`.
This `env` reaches **only the view's** factory — the runner no longer builds an
application, so the write side is configured where `{ProjectName}App` is
constructed in `main.py`:

```python
create_view(
    view_class=Postgres{SliceName}View,
    env={
        # Read side — this view only, prefixed with the projection name.
        "upper_snake_case({SliceName})_PERSISTENCE_MODULE": "eventsourcing.postgres",
        # Connection settings for the view's own recorder.
        "POSTGRES_DBNAME": "...",
        "POSTGRES_HOST": "...",
        "POSTGRES_USER": "...",
        "POSTGRES_PASSWORD": "...",
    },
)
```

Read these from the process environment (`os.environ`) in the app's lifespan —
never hard-code credentials in the module.

**Move the write side with it.** A durable view over an in-memory event store is
not a useful configuration: on restart the store is empty and the view is stale
forever. The write side's `PERSISTENCE_MODULE` (`eventsourcing.dcb.postgres_tt`)
now belongs to `{ProjectName}App`'s own construction, so switching this view to
Postgres means changing *both* places — they are no longer coupled through one
`env` mapping the way `ProjectionRunner` coupled them.

The repo's `compose.yaml` starts a PostgreSQL matching these settings
(`docker compose up -d`), for manually exercising a durable view. It is
deliberately **not** wired into any test env — the suites stay on POPO.

Both tables are created automatically on first use: the factory derives the
tracking table's name from `Projection.name`
(`snake_case({SliceName})_tracking`) and calls `create_table()`, which also
executes the entries-table statement registered in `__init__`.

### Notes on the template

`.build-kit/CLAUDE.md` covers the `Projection` API rules this template relies on —
`name`, `topics`, matching on `envelope.decision`, the mandatory `case _:`
wildcard, and persisting `tracking` on every branch. Slice-specific points:

- **`Projection.name` is the snake_case slice name**, so it lines up with the
  route and module naming used everywhere else in this skill.
- **`create_view()` defaults to the POPO view and `env=None`**, which needs
  no configuration at all. Both parameters exist so a deployment can swap in
  `Postgres{SliceName}View` from Step 7's lifespan without touching this
  module; tests keep calling `create_view()` bare.
- **`create_runner()` takes the shared app and never constructs one.** That is
  the whole point: a privately-constructed application gets its own POPO store,
  so the view would never see the writes this slice exists to project.
- **Keep `{SliceName}View` abstract and depend on it everywhere.** The
  projection is typed `Projection[{SliceName}View, ...]` and the route depends
  on `{SliceName}View`, never on a concrete recorder — that is what lets the
  backend change without touching either.
- **Entity keying is application-level, not DCB-level.** There is no
  per-request consistency boundary here — `add_entry`/`get_entries` key by
  whatever id field the event carries (`entity_id` in the template). Take that
  id straight from the event's `case` pattern; do not invent a tag-derived key.

### Instrumenting `process_event`

`src/snake_case({ProjectName})/telemetry.py` exists — Step 0 established that.
Wrap the whole `match` in one consumer span, which is the only place a
projection needs telemetry:

```python
    def process_event(
        self,
        envelope: TaggedEvent[Decision],
        tracking: Tracking,
    ) -> None:
        """Dispatch on decision type and update the view, or just track the position."""
        with consumer_span(envelope, "snake_case({SliceName})"):
            match envelope.decision:
                ...
```

`consumer_span` extracts the `traceparent` the writing command left in
`envelope.metadata`, and — when that context is valid — opens a
`SpanKind.CONSUMER` span carrying a `Link` back to it.

- **A link, not a child span.** The producing request finished long ago, and
  this runs on a bare `threading.Thread` that inherits no contextvars, so there
  is no ambient context to be a child *of*. `.build-kit/CLAUDE.md` → *Observability*.
- **The span must not swallow the exception.** Record it and re-raise. A
  projection that logs an error and advances past a poison event diverges from
  the log permanently while `/livez` and `/readyz` both still report 200 — the same failure the
  blanket-`try/except` rule already forbids.
- **Wrapping does not change the tracking rules.** Every branch inside the span
  still persists `tracking`, wildcard included, or `wait()` hangs until timeout.
- **Events written before instrumentation carry no `traceparent`.** The span is
  then unlinked rather than absent; that is expected, not a bug to code around.

### Read-model complexity guide

Every collection is keyed by entity id inside the view rather than scoped by a
replay boundary. Shapes below are the POPO implementation; the Postgres
equivalent is a table keyed on `entity_id`, and the abstract interface is
identical either way:

| Scenario | POPO view shape | Postgres equivalent |
|----------|-----------------|---------------------|
| Single-entity presence check | `dict[str, bool]` or `dict[str, {EntryName}]`, `get_entries` returns `None` for "never seen" | one row per entity, `entity_id` primary key |
| Append-only list per entity | `dict[str, list[{EntryName}]]`, as above | many rows per entity, index on `entity_id` |
| Count / aggregate per entity | `dict[str, int]`, increment in `process_event` | `INSERT ... ON CONFLICT (entity_id) DO UPDATE SET n = n + 1` |
| System-wide singleton (rare) | a single mutable attribute guarded by `self._database_lock` | a one-row table; justify it in the docstring, same as an empty-tags selector |

---

## Step 4 — Create `routes.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/routes.py`

The route depends on the **view**, not the application — the whole point of
materializing is that a query never touches the event store. The view is created
once by `create_view()` in the app's lifespan (Step 7) and reaches the route
through `request.state`, exactly like `get_application` reads
`request.state.dcb_app`.

It depends on the view rather than the runner for a second reason: the
supervisor replaces runners on restart, so a route holding a runner reference
could be left pointing at a dead one. The view is the stable object.

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from snake_case({ProjectName}).auth import require_dog_owner_self  # the slice's actor rule
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).projection import (
    {SliceName}View,
)
from snake_case({ProjectName}).view import (
    NOT_FOUND_RESPONSE,
    VIEW_RESPONSES,
    PositionAtLeast,
    is_behind,
    materialized_position,
    too_early,
    view_headers,
)

router = APIRouter(tags=["dogs"])  # the entity, pluralised — see SKILL.md step 5


def get_snake_case({SliceName})_view(request: Request) -> {SliceName}View:
    """Return the {SliceName} view populated by the projection lifespan."""
    return request.state.snake_case({SliceName})_view


class {EntryName}Response(BaseModel):
    """A single entry in the {SliceName} response."""

    field1: str
    field2: int


# The router tag above, and the path and the id parameter below, are the *worked
# example* (`ViewDogProfile` over `tags=[f"dog:{dog_id}"]`), not placeholder
# tokens — substitute the entity and situation you settled on in `SKILL.md` →
# *Addressing the view*.
@router.get(
    "/dogs/{dog_id}/profile",
    # Omit this line only if the board draws no actor for this slice —
    # see `SKILL.md` → *The actor rule*.
    dependencies=[Depends(require_dog_owner_self)],
    response_model=list[{EntryName}Response],
    operation_id="snake_case({SliceName})",
    responses={**VIEW_RESPONSES, status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE},
)
async def snake_case({SliceName})(
    dog_id: str,
    response: Response,
    view: Annotated[{SliceName}View, Depends(get_snake_case({SliceName})_view)],
    position_at_least: PositionAtLeast = None,
) -> list[{EntryName}Response] | Response:
    """{One-line description of the endpoint}."""
    position = materialized_position(view)
    if is_behind(position, position_at_least):
        return too_early(position)
    entries = view.get_entries(dog_id)
    if entries is None:
        msg = f"{dog_id} not found"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=msg,
            headers=view_headers(position),
        )
    response.headers.update(view_headers(position))
    return [
        {EntryName}Response(field1=entry.field1, field2=entry.field2)
        for entry in entries
    ]
```

Notes on the template:

- **No `@lru_cache`'d app factory here.** There is nothing to cache — the view is
  a long-lived object built once by the lifespan, not something a dependency
  function constructs per call.
- **The router carries no `prefix`; the full path goes on the decorator.** One
  greppable path string per slice and no path parameter hidden in a prefix. The
  slice name lives on in `operation_id=`, which is what links the endpoint back
  to the slice in the generated spec; `tags=` groups the endpoint by entity
  instead, and is deliberately shared with other slices. Regenerate it with
  regenerate the spec once the router is wired in (Step 7; `.build-kit/CLAUDE.md`
  → *The OpenAPI spec is the source of truth*).
- **The path parameter takes the entity's own name** (`dog_id`), not a generic
  `entity_id`. The view's internal `get_entries(...)` interface is unaffected —
  that is the projection's own API, not the public one.
- **`request.state`, never `request.app.state`.** The latter is a `State()` built
  in `Starlette.__init__` that never receives lifespan state, so reading from it
  raises `AttributeError` at request time (see `.build-kit/CLAUDE.md` → *Application
  wiring*). Step 6 needs no `dependency_overrides` for the view: it runs against
  the real app, whose lifespan populates this key.
- **Read the position *before* `get_entries`, not after.** The projection thread
  runs concurrently with this handler, so reading it afterwards could report a
  position newer than the entries just returned — the client would then believe
  a write it cannot see has been reflected. Reading first can only understate,
  which is the safe direction.
- **`materialized_position` is where `context_name` lives.** It reads it off the
  `{ProjectName}App` **class**, which is why this route still depends on the view
  alone and needs no `get_application` (`.build-kit/CLAUDE.md` → *View positions*).
  Never re-declare that string here.
- **The precondition check comes before `get_entries`, so 425 outranks 404.**
  This is the one place staleness is genuinely distinguishable from absence — a
  materialized view really can lag, unlike an on-demand one. A caller that sends
  `X-Position-AtLeast` gets 425 while the runner catches up and 404 only when the
  entity truly has no events. A caller that omits the header still cannot tell
  the two apart; that is the documented cost of omitting it, not a defect to work
  around. Either way, **do not add retry or backoff logic in the route** — the
  client polls, the server answers immediately (`SKILL.md` → *The position contract*).
- **All three responses carry the position.** The injected `response: Response`
  covers the 200 only — its headers are discarded when the handler raises or
  returns a `Response` of its own, hence `headers=` on the `HTTPException` and
  `too_early()` building its own. `view_headers()` also emits
  `Cache-Control: no-store`, which is required rather than tidy.
- **`response_model=` must stay explicit.** The return annotation is a union with
  `Response`, which FastAPI cannot derive a schema from. The bare-array response
  model is otherwise unchanged — the position rides in a header precisely so no
  existing body shape had to be wrapped in an envelope.
- **This route returns a list and still 404s, which is not a contradiction of the
  collection rule.** It is scoped to *one* entity, and `get_entries` distinguishes
  "never recorded" (`None`) from "recorded, currently empty" (`[]`) — so absence
  is real here. A view with no parent entity to be absent (a catalogue, a search)
  has nothing to 404 on: it answers 200 with an empty list and drops both the
  `HTTPException` and the `NOT_FOUND_RESPONSE` entry. Preserve that `None`/`[]`
  distinction in the view interface, or the 404 silently becomes unreachable.
- Status codes follow the *Error mapping* table in `SKILL.md`.

### Collection endpoints

If the view exposes a list (e.g. "all licences for an organisation"), the
template above already does this — `get_entries` returns `list[...] | None`.
For a single-entity scalar view, change `get_entries`/`add_entry` to
`get_entry`/`set_entry` and return one `{EntryName}Response`, mirroring the
"Single-entity presence check" row in the complexity guide.

---

## Step 5 — Acceptance tests (projection-level)

File: `tests/acceptance/snake_case({Context})/snake_case({SliceName})/test_snake_case({SliceName}).py`

GWT cannot drive a `Projection` (see `.build-kit/CLAUDE.md`), so acceptance tests here
construct the view and projection directly and call `process_event`
themselves — no runner, no background thread, no `ProjectionRunner` involved:

```python
from eventsourcing.domain import TaggedEvent
from eventsourcing.persistence import Tracking

from snake_case({ProjectName}).snake_case({Context}).events import {EventName}
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).projection import (
    {EntryName},
    {SliceName}Projection,
    POPO{SliceName}View,
)

_ENTITY_ID = "value"


def _projection() -> tuple[{SliceName}Projection, POPO{SliceName}View]:
    view = POPO{SliceName}View()
    return {SliceName}Projection(view=view), view


def test_snake_case({SliceName})_projects_snake_case({EventName})() -> None:
    """Happy path: processing {EventName} adds an entry to the view."""
    projection, view = _projection()
    envelope = TaggedEvent(
        decision={EventName}(entity_id=_ENTITY_ID, field1="a", field2=1),
        tags=[f"{{entity_kind}}:{_ENTITY_ID}"],
    )
    projection.process_event(envelope, Tracking("upstream", 1))
    assert view.get_entries(_ENTITY_ID) == [{EntryName}(field1="a", field2=1)]


def test_snake_case({SliceName})_reports_absent_when_no_events() -> None:
    """An entity with no processed events is absent, not an empty list."""
    _projection_unused, view = _projection()
    assert view.get_entries(_ENTITY_ID) is None
```

### Acceptance-test notes

- **Construct `Tracking(context_name, notification_id)` yourself** — pick any
  `context_name` string (consistent within a test) and strictly increasing
  `notification_id` values, mirroring what `DcbApplicationSubscription` would
  hand the projection in production. Reusing an id raises `IntegrityError`
  (see `.build-kit/CLAUDE.md`).
- **Always test against `POPO{SliceName}View`,** even when the slice also ships
  a Postgres implementation. It needs no configuration, no server, and no
  cleanup. The behaviour under test is the projection's dispatch logic, which
  is backend-independent by construction. If a Postgres implementation exists,
  cover *it* with a separate opt-in test that skips when no database is
  reachable — do not make the whole suite require one.
- **This bypasses `topics` filtering entirely.** Calling `process_event`
  directly means the test is responsible for only sending events the
  projection is meant to handle. A test asserting the projection *ignores* an
  unrelated event type is still valid and useful — send a `TaggedEvent` for a
  type not in the `match`, and assert `view.max_tracking_id(...)` still
  advanced (proving the `_` branch tracked it) while `get_entries` for any
  entity stayed unaffected.

---

## Step 6 — Integration tests (API-level, against the real app)

File: `tests/integration/snake_case({Context})/test_snake_case({SliceName}).py`

These prove the FastAPI route, the lifespan-started runner, and the write path
work together end-to-end, including waiting for the background thread to catch
up.

**Use the real `create_app()` via the shared `client` fixture** in
`tests/integration/conftest.py` — never a local `FastAPI()` (see `.build-kit/CLAUDE.md` →
*Test layout*). Once Step 7 wires this slice into the app's lifespan, that is
also the only way to exercise the wiring you actually ship.

Two things follow from the shared-application design:

- **Seed through the app the runner subscribes to** — reach it with
  `client.app_state["dcb_app"]`. There is no `runner.app` to seed through any
  more, and appending anywhere else is invisible to the subscription.
- **Wait on the view, not a runner.** The supervisor keeps runners private, and
  `TrackingRecorder.wait`'s `interrupt` parameter is optional, so the view
  synchronizes on its own. Never `time.sleep`.

```python
import pytest
from eventsourcing.domain import TaggedEvent
from fastapi.testclient import TestClient

from snake_case({ProjectName}).application import {ProjectName}App
from snake_case({ProjectName}).snake_case({Context}).events import {EventName}
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).projection import (
    {SliceName}View,
)


@pytest.fixture
def dcb_app(client: TestClient) -> {ProjectName}App:
    """Return the application opened by the real app's lifespan."""
    return client.app_state["dcb_app"]


@pytest.fixture
def view(client: TestClient) -> {SliceName}View:
    """Return the materialized view built by the projection lifespan."""
    return client.app_state["snake_case({SliceName})_view"]


@pytest.fixture
def entity_id() -> str:
    """Return the id shared by the arrangement and the request."""
    return "entity-1"


@pytest.fixture
def prior_thing(
    dcb_app: {ProjectName}App, view: {SliceName}View, entity_id: str,
) -> {EventName}:
    """Seed the fact this view projects, and wait for it to be projected."""
    decision = {EventName}(entity_id=entity_id, field1="a", field2=1)
    position = dcb_app.events.append(
        events=[
            TaggedEvent(decision=decision, tags=[f"{{entity_kind}}:{entity_id}"]),
        ],
    )
    view.wait(
        context_name=dcb_app.context_name,
        notification_id=position,
        timeout=5,
    )
    return decision


def test_snake_case({SliceName})_missing_entity_returns_404(client: TestClient) -> None:
    """Querying an entity with no events returns HTTP 404."""
    response = client.get("/dogs/does-not-exist/profile")
    assert response.status_code == 404


def test_snake_case({SliceName})_returns_projected_entries(
    client: TestClient, prior_thing: {EventName}, entity_id: str,
) -> None:
    """After a write catches up, the route returns the projected entries."""
    response = client.get(f"/dogs/{entity_id}/profile")
    assert response.status_code == 200
    assert response.json() == [
        {"field1": prior_thing.field1, "field2": prior_thing.field2},
    ]


def test_snake_case({SliceName})_isolates_other_entities(
    client: TestClient, dcb_app: {ProjectName}App, view: {SliceName}View,
    prior_thing: {EventName}, entity_id: str,
) -> None:
    """Another entity's events do not leak into this entity's view."""
    position = dcb_app.events.append(
        events=[
            TaggedEvent(
                decision={EventName}(entity_id="entity-2", field1="b", field2=2),
                tags=["{{entity_kind}}:entity-2"],
            ),
        ],
    )
    view.wait(
        context_name=dcb_app.context_name,
        notification_id=position,
        timeout=5,
    )
    response = client.get(f"/dogs/{entity_id}/profile")
    assert response.json() == [
        {"field1": prior_thing.field1, "field2": prior_thing.field2},
    ]
```

**Add one test proving the shared store is really shared** — drive a command
route that emits `{EventName}`, then `GET` this view and assert the write shows
up. That is the property a runner-owned application silently broke, and no
seeded-event test can catch it.

### The position contract needs two tests of its own

The route reports where the projection has got to and honours a caller's
precondition (`SKILL.md` → *The position contract*). Both halves are invisible
when broken — a route that forgets the header still answers a perfectly good 200
— so pin them, reusing the same settled fixtures:

```python
def test_snake_case({SliceName})_reports_its_position(
    client: TestClient, prior_thing: {EventName}, entity_id: str,
) -> None:
    """A successful query reports the position the view has processed up to."""
    response = client.get(f"/dogs/{entity_id}/profile")
    assert response.status_code == 200
    assert int(response.headers["X-Current-Position"]) >= 1


def test_snake_case({SliceName})_reports_too_early_when_behind(
    client: TestClient, prior_thing: {EventName}, entity_id: str,
) -> None:
    """A precondition the view cannot meet is answered with 425, not with stale data."""
    response = client.get(
        f"/dogs/{entity_id}/profile",
        headers={"X-Position-AtLeast": "1000000"},
    )
    assert response.status_code == 425
    assert response.content == b""
```

- **The 425 test asks for a position that will never be reached, and that is the
  point.** Do not "improve" it by racing the runner into a real lag window —
  seeding an event and querying before the background thread catches up passes
  only when the thread happens to lose, which is a flaky test wearing a
  correctness test's clothes. An unreachable position exercises the same branch
  every time.
- **Depend on `prior_thing` in both.** It appends *and* waits, so the projection
  has processed something by the time the test body runs. Without it
  `max_tracking_id` is `None`, the header is absent, and the first test fails for
  the wrong reason.
- **Assert `>= 1`, never a literal.** The position is the upstream notification
  id, so it counts every event the fixtures seeded, not only the ones this
  projection keeps.

### Integration-test notes

`.build-kit/CLAUDE.md` covers the mechanics this fixture depends on: the mandatory
`with TestClient(app) as ...` for lifespan-bearing apps, seeding with
`app.events.append(events=[...])`, and `wait()` over `time.sleep`.
Slice-specific points:

- **Seed raw `TaggedEvent`s through the shared application.** The runner
  subscribes to the application the lifespan opened, so that is the only store
  whose appends it can see. Events appended to any other `DcbApplication`
  instance are silently invisible under POPO.
- **`append()` returns the position `wait()` needs.** That is the whole reason
  the seeding fixture can synchronize: append, then wait on what it returned.
  Never `time.sleep`.
- **Seeding is unconditional, so it repeats freely.** `append()` builds a DCB
  append condition only when `cb`/`after` is given; passing `events=` alone
  imposes none. Two seeds into the same entity's tags therefore both succeed —
  no `advance()`, no `IntegrityError`, and no test-only emitting `Slice` whose
  `consistency_boundary()` you would have to scope by hand.
- **Each seeded fact is its own fixture, declared in the test's signature** —
  never a helper called from the test body. The fixture both appends *and*
  waits, so a test that names it is guaranteed a settled view before its
  first line runs. Fixtures compose, ids get their own fixtures so arrangement
  and URL share one value, and each returns its `Decision` so the test asserts
  against exactly what it arranged.
- **Tags must satisfy Selector tags ⊆ trigger tags.** Seeding under the wrong
  tag is silently invisible rather than an error. The view keys its state by an
  id on the event body, but the tags still have to match what the real emitting
  slice would use, or the projection sees a history the rest of the system does
  not.
- **The lifespan yields the view, and the test reads it from
  `client.app_state`.** Never reach for the runner: the supervisor owns it, may
  replace it at any moment, and nothing a test needs lives on it. Both the
  seeding target (`dcb_app`) and the synchronization point (`view`) come from
  lifespan state.
- **The whole stack stays in memory here** — `create_view()` defaults to POPO
  and the app defaults to `eventsourcing.dcb.popo`, so there is no database, no
  env vars, and no teardown beyond the `TestClient` `with` block. The route code
  exercised is identical to the Postgres deployment's, because it depends on the
  abstract `{SliceName}View`.
- **A guarded view needs two more tests**: a **401** with no `Authorization`
  header, and a **403** as the wrong actor — with the data seeded and the view
  settled, so the test proves the rule refused the read rather than that there
  was nothing to return. Assert `response.json()["detail"]` as well as the
  status. For an owner-scoped view, add a third in which the *other* subject
  reads their **own** account, gets 200, and sees none of this one's data:
  that separates "the view is scoped by id" from "the rule refuses cross-account
  reads". Headers come from the auth fixtures in
  `tests/integration/conftest.py`; the `client` fixture stays anonymous.

---

## Step 7 — Wire the router, the view, and the supervisor into the FastAPI app

The top-level FastAPI application is `src/snake_case({ProjectName})/main.py`.
Unlike the other approaches, this one *does* touch its `lifespan`, because the
view has to be built and its runner started with the app.

**This is an edit to an existing file, never a replacement.** `main.py` was
written at *First-time project setup* (`.build-kit/CLAUDE.md` → step 6) and every
later slice has added a router line to it. Pasting the block below over that file
deletes work — most damagingly `create_app()` itself, which the integration
suite imports, and the telemetry wiring, which nothing would report as missing.
Lines this slice adds are marked `# ADD`; everything else is shown so you can see
where they go, and should already be there.

```python
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager      # ADD AsyncExitStack
from functools import partial                                   # ADD

from fastapi import FastAPI

from snake_case({ProjectName}).application import {ProjectName}App
from snake_case({ProjectName}).projection import ProjectionSupervisor   # ADD
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).projection import (  # ADD
    create_runner,
    create_view,
)
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).routes import (      # ADD
    router as snake_case({SliceName})_router,
)
from snake_case({ProjectName}).telemetry import (
    configure_telemetry,
    instrument_app,
    instrument_recorder,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[dict[str, object]]:
    """Open the application and every supervised projection for the app's life."""
    async with AsyncExitStack() as stack:
        dcb_app = stack.enter_context({ProjectName}App())
        instrument_recorder(dcb_app)   # AFTER construction — `recorder` is set in `__init__`
        supervisor = ProjectionSupervisor(context_name=dcb_app.context_name)   # ADD

        snake_case({SliceName})_view = create_view()                           # ADD
        stack.callback(snake_case({SliceName})_view.close)                     # ADD
        supervisor.register(                                                   # ADD
            "snake_case({SliceName})",
            snake_case({SliceName})_view,
            partial(create_runner, dcb_app, snake_case({SliceName})_view),
        )
        # supervisor.register("other_slice", other_view, partial(...))

        stack.enter_context(supervisor)                                        # ADD
        yield {
            "dcb_app": dcb_app,
            "snake_case({SliceName})_view": snake_case({SliceName})_view,      # ADD
            "projection_supervisor": supervisor,                               # ADD
        }


def create_app() -> FastAPI:
    """Build the FastAPI application, wiring in every slice's router."""
    configure_telemetry()          # FIRST — `instrument_app` reads the state it sets
    app = FastAPI(lifespan=lifespan)
    instrument_app(app)
    app.add_middleware(MetadataMiddleware)
    # ... every earlier slice's existing `include_router` lines, untouched ...
    app.include_router(snake_case({SliceName})_router)                         # ADD
    return app
```

If this is the project's first projection, the lifespan being upgraded is the
`with {ProjectName}App() as dcb_app:` form from step 6. Move
`instrument_recorder(dcb_app)` along with it, to immediately after
`stack.enter_context(...)` — same principle, same reason. Dropping it is silent:
no test fails, no route breaks, the event store simply stops being traced.

Details that are load-bearing rather than stylistic:

- **Append the `include_router` line; do not reorder the existing ones.**
  Starlette serves the first route whose pattern fully matches, so a path
  parameter registered ahead of a literal it can match silently swallows that
  literal's requests and answers them from the wrong handler — no startup error,
  no log line. Appending keeps every already-working route ahead of yours. If
  this view's path *could* be matched by an existing parameterised path,
  redesign the address rather than relying on the order (`.build-kit/CLAUDE.md`
  → *Never let a literal segment sit where a path parameter could match it*).
- **`stack.enter_context`, not `enter_async_context`.** Both `{ProjectName}App`
  and `ProjectionSupervisor` are *sync* context managers. `AsyncExitStack`
  handles both kinds; picking the wrong method fails at startup.
- **Enter order is the teardown contract.** The application goes in **first** so
  it closes **last** — every runner must be stopped before the store it reads
  from is closed. The supervisor goes in after it and therefore exits before it.
- **`stack.callback(view.close)` goes *before* the supervisor is entered**, so
  it runs *after* the supervisor stops. The view owns a connection pool of its
  own — `create_view()` builds a `PostgresDatastore` through its own factory,
  entirely separate from the one `{ProjectName}App` holds — and nothing else
  ever closes it. Closing it while a projection thread is still writing through
  it would fail. A no-op on POPO, which is exactly why the leak is invisible
  until the view is made durable.
- **`instrument_recorder(dcb_app)` stays immediately after the application is
  entered**, before the supervisor exists. `recorder` is set in `__init__`, and
  the runners the supervisor starts read through it — instrument it after they
  are running and the first events they process go untraced.
- **One supervisor per process, shared by every slice.** Adding a second
  materialized slice adds a `create_view()` + `register(...)` pair and one more
  key in the yielded mapping — never a second supervisor, and never a second
  application.
- **`register()` before `enter_context(supervisor)`.** Entering is what
  constructs and starts the runners; registering afterwards would leave the
  projection unsupervised.
- **`partial(create_runner, dcb_app, view)` is the restart factory.** The
  supervisor calls it again for each restart, always against the same view, so
  the new runner resumes at `max_tracking_id` (see `.build-kit/CLAUDE.md` → *Supervising
  projections*).
- **The lifespan yields the view, never the runner** — the supervisor swaps
  runners, so a reference held elsewhere could point at a dead one.

If the project already has a supervisor (any earlier projection slice built one),
this slice adds only three lines to the existing lifespan — `create_view()`,
`supervisor.register(...)`, and one key in the yielded mapping — plus its
`include_router`. Create `supervisor` and the `AsyncExitStack` only if this is
the project's first projection — and if you did create them, add `health.py` with
its `/livez` and `/readyz` routes in the same step (see *Reporting projection
health* below). A supervisor without them can only report a dead projection to a
log nobody is reading.

Once the router is included the route exists on the real app, so **regenerate the
spec and stage it with the slice** — the project's regeneration command is in
`.build-kit/CLAUDE.md` → *The OpenAPI spec is the source of truth*, and it
rewrites `docs/openapi.json` in place.

Confirm the diff adds exactly the path you intended and changes nothing else — a
diff that *moves* an existing endpoint means this slice took a path another one
was already using.

One benign exception: adding the **first** parameter to a previously
parameter-less operation makes FastAPI emit a `422` response for it that was
not there before. A view route picks up its first parameter from
`X-Position-AtLeast` alone, so a collection view with no path or query
parameters gains a 422 the moment it adopts the position contract. That is
generated, correct, and not a collision — do not try to suppress it.

### Choosing a durable backend

**This lifespan is the one place the backend is chosen.** The bare
`create_view()` above materializes into memory, which is the right default and
correct for a single-process deployment that can afford to rebuild on restart.
For a durable view, pass the view class and its env:

```python
        snake_case({SliceName})_view = create_view(
            view_class=Postgres{SliceName}View,
            env={
                "upper_snake_case({SliceName})_PERSISTENCE_MODULE": "eventsourcing.postgres",
                # POSTGRES_DBNAME / HOST / USER / PASSWORD from os.environ
            },
        )
```

**The write side moves separately now.** `create_view()`'s `env` scopes the view
only; the event store's `PERSISTENCE_MODULE` is configured where
`{ProjectName}App` is constructed. Move both together — a durable view over an
in-memory event store goes permanently stale on the first restart, and the two
are no longer coupled through a single `env` the way they were when the runner
built its own application.

Nothing in `routes.py`, the projection, or either test suite is touched — they
all depend on the abstract `{SliceName}View`. Do not add this unless the slice
definition calls for durability.

### Reporting projection health

Registering a supervisor above created the obligation to report on it: past
`max_restarts` the runner stays dead, and without a health surface the process
answers every request happily while the view sits frozen.

**`health.py`, its lag gauge and its 503 test are in `.build-kit/CLAUDE.md` →
*Supervising projections* → *The `/livez` and `/readyz` routes*.** Add the module
there if this slice registered the project's **first** supervisor; leave it
exactly as it is if an earlier slice already did. Both routes report and never
restart — recovery is the supervisor's job, so health checks stay free of side
effects.

---

## Key patterns

- **The view is a `TrackingRecorder`, not a `Slice`.** State lives in a
  standalone recorder object (`POPO{SliceName}View` above); the projection
  mutates it via ordinary methods (`add_entry`, `get_entries`), not `@event`
  handlers.
- **`TrackingRecorder` is abstract; the backend is a deployment choice.**
  `POPOTrackingRecorder` (in-memory) and `PostgresTrackingRecorder` (durable)
  are the two options for a DCB application. Declare an abstract
  `{SliceName}View` and let the projection, route, and tests depend only on
  that, so swapping backends touches one line in the app lifespan.
- **`create_view()` resolves the recorder from the environment**, not from the
  import. `upper_snake_case({SliceName})_PERSISTENCE_MODULE` selects the factory
  for *this view*; the `view_class` passed in must subclass the matching
  concrete recorder or construction fails an assertion.
- **The runner subscribes to the shared application; it never builds one.**
  `ProjectionRunner` takes an application *class* and instantiates it, which
  would give the projection a private store the command routes cannot write
  to — so this skill uses `SharedAppProjectionRunner` instead (see `.build-kit/CLAUDE.md`
  → *Projection runners*).
- **Entry and tracking must be written atomically** — under
  `self._database_lock` for POPO, inside one `datastore.transaction(commit=True)`
  for Postgres. This is why `add_entry` takes the `Tracking`.
- **The view is the stable identity; the runner is disposable.** The supervisor
  rebuilds runners over the same view, so the view is what the lifespan yields,
  what the route depends on, and what tests `wait()` on. Nothing outside the
  supervisor holds a runner reference.
- **Entity keying is application-level, not DCB-level.** A background
  subscription cannot scope itself to one entity, so the view keys its own
  state by an id carried on the event body — there is no per-query replay
  boundary the way there is in the on-demand approach.
- **In-memory testing, whatever the deployment backend.**
  `POPOTrackingRecorder`/`POPOApplicationRecorder` need no environment
  configuration — the real app's lifespan per test (Step 6) or one fresh
  view+projection pair with no runner at all (Step 5), no cleanup needed
  beyond exiting the `with` block. Tests never require a database.

---

## Files to create

```
docs/
    openapi.json                                          # REGENERATED, not hand-written
src/snake_case({ProjectName})/
    main.py                                               # EDITED, not created — router, view, supervisor registration, health.py's router
    health.py                                             # SHARED RUNTIME — /livez + /readyz; create ONLY if this slice registers the project's first supervisor
    projection.py                                         # SHARED RUNTIME — SharedAppProjectionRunner + ProjectionSupervisor (create ONLY if absent; never per-slice)
    metadata.py                                           # SHARED RUNTIME — verified in Step 0; never written per-slice
    telemetry.py                                          # SHARED RUNTIME — verified in Step 0; supplies `consumer_span`, never written per-slice
    view.py                                               # SHARED RUNTIME — verified in Step 0; the position helpers routes.py imports
src/snake_case({ProjectName})/snake_case({Context})/
    events.py                                             # shared event Decisions (add new types here; do not remove existing ones)
src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/
    __init__.py                                           # package marker
    projection.py                                         # View interface + POPO impl (+ Postgres impl only if deployed) + Projection + Runner + create_view() + create_runner()
    routes.py                                             # FastAPI router reading the view from request.state
tests/unit/
    test_projection.py                                    # SHARED RUNTIME — the upgrade tripwire; create alongside projection.py, never per-slice
tests/acceptance/snake_case({Context})/snake_case({SliceName})/
    test_snake_case({SliceName}).py                       # projection-level tests (direct process_event calls, no runner)
tests/integration/snake_case({Context})/
    test_snake_case({SliceName}).py                       # API-level tests (real create_app(), seed through the app, wait on the view)
```

`src/snake_case({ProjectName})/projection.py` is **shared runtime, not generated
per slice** — it holds the one hand-written `__exit__` override that couples to
private `BaseProjectionRunner` attributes, plus the supervisor every projection
registers with. If it already exists, import from it and change nothing; if it
does not, create it once (see `.build-kit/CLAUDE.md` → *Projection runners* and
*Supervising projections* for exactly what it must guarantee) — together with
`tests/unit/test_projection.py`, the tripwire that catches a library upgrade
putting `app.close()` back into `__exit__`.
