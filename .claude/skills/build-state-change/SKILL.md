---
name: build-state-change
description: Implements a pyeventsourcing state-change slice (Slice, FastAPI route, pytest tests) from a slice.json definition
---

# Build State Change Slice

> Before doing anything else, read the slice definition from `.build-kit/.slices/<contextSlug>/<sliceFolder>/slice.json`. This file is the **source of truth** for all fields, events, and metadata. Never invent fields not defined there.
>
> **These two path segments are lowercase slugs, not snake_case**, and they slug differently from each other — they are written by the `load-slice` skill, which owns the rules:
>
> - `<contextSlug>` — the context lowercased, spaces to hyphens, non-alphanumeric removed (`"My Ctx"` → `my-ctx`). Falls back to `default` when the slice has no context.
> - `<sliceFolder>` — the slice title lowercased with **all spaces removed** and any `slice:` prefix stripped (`"Admin Cancel License"` → `admincancellicense`).
>
> So `AdminCancelLicense` in the `Backoffice` context reads from `.build-kit/.slices/backoffice/admincancellicense/slice.json`. Do **not** use the `snake_case(...)` form here — that convention applies to the generated Python paths below, not to this directory. If the path you derive is missing, list the context directory rather than guessing.

Project-wide conventions (tooling, pre-commit, test layout) live in `.build-kit/CLAUDE.md`. Consult it for anything not specific to building a slice.

---

## What a State Change Slice is

A state-change slice processes a command using event sourcing. It:
1. Loads the current DCB projection state by replaying the events selected by the slice's consistency boundary
2. Validates the command against that state
3. Emits new events if valid, raises if not

The slice is expressed as an `eventsourcing.pydantic.Slice` subclass and invoked via **the project's one central application**, `{ProjectName}App` in `src/snake_case({ProjectName})/application.py`. A slice never defines an application of its own — see **Key patterns**.

---

## Step 0 — Verify the shared runtime

A slice is built *on* the shared runtime; it never carries a copy of it. Before reading the slice definition, confirm each of these exists under `src/snake_case({ProjectName})/`:

| Module | If missing |
|--------|------------|
| `__init__.py` | `.build-kit/CLAUDE.md` → *First-time project setup* |
| `command.py` | ditto — this slice type subclasses its `CommandSlice` |
| `metadata.py` | ditto — the module and its tests are in `.build-kit/references/metadata.md` |
| `telemetry.py` | ditto — the module and its tests are in `.build-kit/references/telemetry.md` |
| `application.py` | ditto — including the `do()` override and the `command_metadata()` / `command_span` inside it |
| `main.py` | ditto — including `configure_telemetry()`, `instrument_app()`, `instrument_recorder()` and `add_middleware(MetadataMiddleware)` |
| `projection.py` | not used by this slice type; leave it alone if absent |

A missing module is an incomplete setup, not an opt-out — in particular, **do not skip a slice's instrumentation because `telemetry.py` is absent.** Create what is missing, run the test suites, and commit it on its own as a `chore:` **before** starting the slice: a shared-runtime module folded into a `feat:` commit is unreviewable and reads as slice-specific when it is not.

If everything is present, change nothing. Import from these modules and move on.

---

## Step 1 — Read the slice.json

From the slice definition, extract:
- **sliceName** — the slice title (becomes the `Slice` subclass name and the route handler name)
- **context** — the bounded context (used to find `src/snake_case({ProjectName})/snake_case({Context})/events.py`)
- **commands[]** — list of commands with their data fields
- **events[]** — list of events emitted by each command
- **specifications[]** — test scenarios. If empty, still write at least one happy-path test and one invariant-violation test (e.g. "already processed").

### Placeholder grammar

Every placeholder in the templates below is **PascalCase**. There is only one form; derive it once from `sliceName` and reuse it verbatim.

| Placeholder | Derived from | Example |
|-------------|--------------|---------|
| `{SliceName}` | `sliceName` in PascalCase | `AdminCancelLicense` |
| `{EventName}` | event type in PascalCase | `LicenceCancelled` |
| `{Context}` | bounded context in PascalCase | `Backoffice` |
| `{ProjectName}` | `project.name` in `pyproject.toml`, PascalCase | `MyProject` |

`{ProjectName}` is the one placeholder that does **not** come from the slice definition — read it once from `[project] name` in `pyproject.toml`. It is already fixed for the whole repository, so `snake_case({ProjectName})` is simply the existing top-level package under `src/`; confirm it there rather than deriving a name that does not exist on disk.

Filesystem paths, Python module names, and method names are **derived from the PascalCase placeholder at code-generation time**, not carried as separate placeholders:

- Python module / package path → lowercase PascalCase split on word boundaries, joined with `_` (e.g. `AdminCancelLicense` → `admin_cancel_license`).
- Route handler name → same as the Python module (snake_case).

Apply these transforms mechanically; do not introduce new placeholder tokens.

**The URL is not on this list.** It is not derived from `{SliceName}` at all — a slice name is a build artefact, and publishing it as the address frames the command as data editing. The path comes from the slice's consistency boundary and its business intention; Step 4 below works it out, and the rule lives in `.build-kit/CLAUDE.md` → *API addressing*.

---

## Step 2 — Ensure the shared events module exists

Each context has one `src/snake_case({ProjectName})/snake_case({Context})/events.py` module holding every domain event for that context — events are shared across slices in the same context.

File location: `src/snake_case({ProjectName})/snake_case({Context})/events.py`.

### Event pattern

```python
from eventsourcing.pydantic import Decision


class {EventName}(Decision):
    """{one-line description of what this event means}."""

    field1: str
    field2: int
    # data fields from slice.json — use snake_case even if slice.json uses camelCase
```

> The template omits the copyright header and module docstring for brevity. Every real file needs them — commit the result and let pre-commit surface anything you missed (see `.build-kit/CLAUDE.md`).

Add each new event type to this module. Do NOT remove existing ones.

---

## Step 3 — Create `slice.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/slice.py`

### Choose the consistency boundary tags first

Before you write the slice, decide which **entity** its invariant guards. In DCB, `Selector.tags` scopes the replay: the app only rebuilds decision state from events whose tags **intersect** the selector's tags. Empty `tags=[]` means "every event of this type, everywhere" — a global boundary. That is almost never what you want.

Pick the tag values from the command arguments — never invent them, never derive them from wall-clock or generated data:

| Invariant scope | `tags=` should be |
|-----------------|-------------------|
| "This user can't do X twice" | `[f"user:{user_id}"]` |
| "This licence can't be cancelled twice" | `[f"licence:{licence_id}"]` |
| "At most N members per organisation" | `[f"orga:{orga_id}"]` |
| Two entities together (rare) | `[f"user:{user_id}", f"orga:{orga_id}"]` |
| **Truly global** (a system-wide invariant, e.g. singleton config) | `[]` — and justify it in the docstring |

The same tags must be attached at **emission time** via `trigger_event(..., tags=...)` — see the **Selector tags ⊆ trigger tags** rule in `.build-kit/CLAUDE.md`. Get this wrong and the invariant silently breaks.

### Full structure

```python
from eventsourcing.domain import event
from eventsourcing.pydantic import Selector

from snake_case({ProjectName}).command import CommandSlice
from snake_case({ProjectName}).snake_case({Context}).events import {EventName}


class {SliceName}Slice(CommandSlice):
    """DCB slice that processes the {SliceName} command."""

    def __init__(self, field1: str, field2: int) -> None:
        # Command arguments live on self — `execute()` takes no args because
        # `DcbApplication.do(slice_instance)` calls `.execute()` with no arguments.
        self.processed = False
        self._field1 = field1
        self._field2 = field2

    def _tags(self) -> list[str]:
        # Consistency boundary keyed by the entity this command mutates.
        # Replace `field1` with whatever id from slice.json identifies the entity.
        return [f"{{entity_kind}}:{self._field1}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector that defines this slice's consistency boundary."""
        # Selector.types is a **Sequence**, not a set. Use a list literal.
        # tags MUST match the tags used at `trigger_event` — see `_tags()`.
        return Selector(types=[{EventName}], tags=self._tags())

    @event({EventName})
    def _(self) -> None:
        # Project the event to attributes used during validation.
        self.processed = True

    def execute(self) -> None:
        """Validate and emit a {EventName} event."""
        if self.processed:
            msg = "already_processed"
            raise ValueError(msg)

        self.trigger_event(
            {EventName},
            self._tags(),
            field1=self._field1,
            field2=self._field2,
        )
```

Notes on the template:

- **`slice.py` contains the `Slice` subclass and nothing else.** Do not add a `DcbApplication` subclass here or anywhere in the slice package — the project has exactly one application (Step 4).
- `trigger_event`'s second positional argument is the tag sequence — it is positional-only, so pass `self._tags()` before the keyword event fields.
- If the invariant genuinely is global, drop `_tags()` and return `Selector(types=[{EventName}], tags=[])`. Add a one-line docstring on `consistency_boundary` explaining why.

### State complexity guide

| Scenario | Decision attributes |
|----------|---------------------|
| Simple create-once (per entity) | `self.created = False` |
| Idempotency across all entities (rare) | `self.processed_user_ids: set[str] = set()` |
| Count validation (per entity) | `self.count = 0`, `self.limit = N` |
| No validation needed | (only the command args on `self`) |

For the common per-entity case, tag scoping does the heavy lifting — you only need a single `bool` on `self` because the replayed events are already filtered to that entity.

---

## Step 4 — Create `routes.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/routes.py`

### Choose the address first

The URL names the **business intention**, not the slice. Work it out before writing the module; the full rule and its table live in `.build-kit/CLAUDE.md` → *API addressing*. In short:

1. **Take the entity from `_tags()`** — the decision you already made in Step 3. `tags=[f"licence:{licence_id}"]` gives `/licences/{licence_id}`. Pluralise the tag kind and kebab-case it. A path whose entity disagrees with the boundary tag is a bug.
2. **Nominalise the command's verb** into a plural noun of intent: `AdminCancelLicense` → `cancellation-requests`. Where that reads badly, use `{verb}-requests`.
3. **Commands that create their own entity** have no id to nest under — they go at the root: `POST /user-registrations`. Same for a genuinely global boundary (`tags=[]`).

```
AdminCancelLicense + tags=[f"licence:{licence_id}"]
    -> POST /licences/{licence_id}/cancellation-requests

RegisterUser (id generated by the command)
    -> POST /user-registrations
```

4. **Check the path is free.** `grep` it in `docs/openapi.json` — the committed spec is the source of truth for what already exists (`.build-kit/CLAUDE.md` → *The OpenAPI spec is the source of truth*). A collision means a different intention noun, or a mis-identified entity. On the very first slice the file does not exist yet; that is expected.

### Full structure

```python
from datetime import date  # runtime import — Pydantic model field type
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from snake_case({ProjectName}).application import {ProjectName}App, get_application
from snake_case({ProjectName}).command import CommandResponse
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).slice import {SliceName}Slice

router = APIRouter(tags=["snake_case({SliceName})"])


class {SliceName}Request(BaseModel):
    """Request body for the {SliceName} command."""

    # The entity id is NOT a field here — it is the path parameter below.
    field2: int


# The path, the id parameter and the id kwarg below are the *worked example*
# (`AdminCancelLicense` over `tags=[f"licence:{licence_id}"]`), not placeholder
# tokens — substitute the entity and intention you settled on above. Everything
# else in this template is verbatim.
@router.post(
    "/licences/{licence_id}/cancellation-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=CommandResponse,
    operation_id="snake_case({SliceName})",
    responses={
        status.HTTP_204_NO_CONTENT: {"description": "The command recorded nothing."},
    },
)
async def snake_case({SliceName})(
    licence_id: str,
    body: {SliceName}Request,
    app: Annotated[{ProjectName}App, Depends(get_application)],
) -> CommandResponse | Response:
    """{One-line description of the endpoint}."""
    try:
        slice_ = app.do({SliceName}Slice(licence_id=licence_id, **body.model_dump()))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if slice_.outcome.position is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return CommandResponse(
        event_ids=list(slice_.outcome.event_ids),
        position=slice_.outcome.position,
    )
```

Notes on the template:

- **A slice must never define its own application, nor its own dependency factory.** `get_application` is the single dependency; it reads the process-wide `{ProjectName}App` off `request.state` (Step 7 and `.build-kit/CLAUDE.md`).
- **The router carries no `prefix`; the full path goes on the decorator.** One greppable path string per slice, no path parameter hidden in a prefix, and no trailing-slash wart. The slice name lives on in `tags=` and `operation_id=`, which is what links the endpoint back to the slice in the generated spec.
- **The entity id is a path parameter and must not also be a body field.** It reaches the slice as an explicit keyword argument next to `**body.model_dump()`. A command with no entity to nest under (`POST /user-registrations`) keeps every field in the body and drops the path parameter.
- **Regenerate the spec once the route exists**: `hatch run docs:openapi`, then stage `docs/openapi.json` with the rest of the slice. The pre-commit hook will do it for you if you forget, but it will fail the commit doing so.
- `do()` takes an **instance** of the slice, not the class, and internally calls `slice.execute()` — do NOT call `.execute()` yourself.
- **A command slice subclasses `CommandSlice`, never `Slice` directly.** That base is what carries `outcome` — the ids of the events the command recorded and the position they were appended at. The library's `do()` discards both (`save()`'s return value is dropped, and `collect_events()` drains `new_decisions`), so `{ProjectName}App.do()` overrides it to capture them. A slice left on bare `Slice` still works but reports an empty outcome, and its route then answers 204 for every successful command.
- **Return the position; clients need it for read-your-writes.** It is the same value `TrackingRecorder.wait(context_name, notification_id, timeout)` polls, so a caller can tell whether a projection has caught up with its own write. It is also what a caller sends straight back to a view as `X-Position-AtLeast` to poll for that write becoming visible — the two halves of the contract in `build-state-view/SKILL.md` → *The position contract*. **Never drop it from the response** on the grounds that nothing in this slice reads it; the reader on the other side of the board does.
- **`status_code` is always `HTTP_201_CREATED`, never `HTTP_200_OK`.** A command that succeeds appends events to the log, which is a creation whatever the slice's verb suggests — `AdminCancelLicense` creates a `LicenceCancelled` event. Every command route in the project therefore answers 201 on success and 204 on a no-op; those are the only two success codes.
- **`response_model=` must be explicit on the decorator.** The return annotation is a union with `Response`, which FastAPI cannot derive a schema from.
- Use `status.HTTP_422_UNPROCESSABLE_CONTENT`; the older `..._ENTITY` alias raises a `StarletteDeprecationWarning`.
- **Route handlers add NO telemetry code.** Do not import OpenTelemetry here, do not open a span, do not touch `metadata`. The HTTP span comes from `FastAPIInstrumentor` in `create_app()` and the command span from `{ProjectName}App.do()` — both already exist, one level above every slice. A span opened in a route handler duplicates one of those two.

### Error mapping

| Exception | HTTP status |
|-----------|-------------|
| `ValueError` (validation failure inside `execute`) | `422 Unprocessable Entity` |
| Domain conflict class (e.g. duplicate) | `409 Conflict` |
| Anything else | let FastAPI return `500` |

Raise domain errors from `execute` (`raise ValueError("already_processed")`) and translate them in the route handler.

---

## Step 5 — Acceptance tests (slice-level, GWT)

File: `tests/acceptance/snake_case({Context})/snake_case({SliceName})/test_snake_case({SliceName}).py`

```python
import pytest
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).slice import {SliceName}Slice
from snake_case({ProjectName}).snake_case({Context}).events import {EventName}

# Reuse the same values for the entity id: the tag on `given()` events must
# fall inside the slice's consistency boundary or `when()` will not see them.
_FIELD1 = "value"
_TAGS = [f"{{entity_kind}}:{_FIELD1}"]


def _slice() -> {SliceName}Slice:
    return {SliceName}Slice(field1=_FIELD1, field2=1)


def test_snake_case({SliceName})_emits_snake_case({EventName})() -> None:
    """Happy path: the command emits {EventName} tagged for its entity."""
    given().when(_slice()).then(
        TaggedEvent(
            decision={EventName}(field1=_FIELD1, field2=1),
            tags=_TAGS,
        ),
    )


def test_snake_case({SliceName})_raises_when_already_processed() -> None:
    """Invariant: cannot process the same entity twice."""
    prior = TaggedEvent(
        decision={EventName}(field1=_FIELD1, field2=1),
        tags=_TAGS,
    )
    with pytest.raises(ValueError, match="already_processed"):
        given(prior).when(_slice())
```

### GWT API notes

- `then()` compares `TaggedEvent` **instances**, not classes. Construct the expected event with the same fields **and the same tags** the slice would emit.
- Cross-entity isolation can't be tested here (`.build-kit/CLAUDE.md` explains why GWT rejects out-of-boundary histories). Prove it in the **integration suite**: post two commands with different entity ids and assert both succeed.

---

## Step 6 — Integration tests (API-level, TestClient)

File: `tests/integration/snake_case({Context})/test_snake_case({SliceName}).py`

These prove the FastAPI route wires the slice correctly and returns the right status codes and bodies. They belong in `tests/integration/` — a separate hatch env from acceptance tests.

**Use the shared `client` fixture from `tests/integration/conftest.py`** — do not build a local `FastAPI()` and do not register any `dependency_overrides`. That fixture wraps the real `create_app()` in a `with TestClient(...)` block, so each test runs the lifespan and gets its own freshly-constructed `{ProjectName}App` over its own in-memory store. Testing the real app is also the second line of defence against two slices claiming the same path — the first being the spec you grepped in Step 4.

```python
from fastapi.testclient import TestClient


def test_snake_case({SliceName})_returns_201(client: TestClient) -> None:
    """A valid request returns HTTP 201."""
    response = client.post(
        "/licences/lic_123/cancellation-requests",
        json={"field2": 1},
    )
    assert response.status_code == 201


def test_snake_case({SliceName})_twice_returns_422(client: TestClient) -> None:
    """Repeating the command returns HTTP 422 with the domain error message."""
    path = "/licences/lic_123/cancellation-requests"
    client.post(path, json={"field2": 1})
    response = client.post(path, json={"field2": 1})
    assert response.status_code == 422
    assert response.json()["detail"] == "already_processed"


def test_snake_case({SliceName})_missing_field_returns_422(client: TestClient) -> None:
    """A request missing a required field returns HTTP 422 (Pydantic validation)."""
    response = client.post("/licences/lic_123/cancellation-requests", json={})
    assert response.status_code == 422
```

The URL is the worked example again — use the path you chose in Step 4, and note that the entity id now travels in it rather than in `json=`. Where a test needs the id in two places (arranged history and request), take it from the shared id fixture rather than repeating the literal.

### Arranging prerequisite history

If the route under test needs events that another slice would normally have produced, **seed them as raw `TaggedEvent`s** — never by driving the other slice's route or `Slice`:

```python
@pytest.fixture
def prior_thing(dcb_app: {ProjectName}App, entity_id: UUID, tags: list[str]) -> {EventName}:
    """Seed the fact this command depends on."""
    decision = {EventName}(field1=..., field2=...)
    dcb_app.events.append(events=[TaggedEvent(decision=decision, tags=tags)])
    return decision


def test_snake_case({SliceName})_after_prior(
    client: TestClient, prior_thing: {EventName}, entity_id: UUID,
) -> None:
    ...
```

The rules:

- **Each seeded fact is its own `@pytest.fixture`, declared in the test's signature** — never a helper called from the test body. Fixtures compose (a richer history depends on a simpler one), so a test names only the deepest fact it needs. This also makes ordering structural: pytest resolves the graph before the body runs, so seeding cannot accidentally happen after the request under test.
- **Ids get fixtures too**, so the arrangement, the request URL and the boundary tags all share one value. Put the shared ones (`dcb_app`, ids, `tags`) in `tests/integration/conftest.py` and keep slice-specific histories in the slice's own test module.
- **Return the seeded `Decision`** so the test can assert against exactly what it arranged.
- **Raw events, not another slice.** A test for this route should not have to satisfy some other slice's validation rules, nor break when they change — and raw events let a test construct histories a slice would legitimately refuse to produce.
- **Pass `events=` only** — supplying `cb`/`after` re-introduces an append condition. Omitting both is the unconditional write a seed wants.
- **Tags must satisfy Selector tags ⊆ trigger tags.** Seeding under the wrong tag is silently invisible rather than an error.

`dcb_app` is the same application the routes use, reached via `client.app_state["dcb_app"]` — **not** `client.app.state`, which is a different object that never receives lifespan state.

---

## Step 7 — Wire the router into the central FastAPI app

**Mandatory for every slice.** In `src/snake_case({ProjectName})/main.py`, add the import and the `include_router` line inside `create_app()`:

```python
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).routes import (
    router as snake_case({SliceName})_router,
)

def create_app() -> FastAPI:
    ...
    app.include_router(snake_case({SliceName})_router)
```

Those two lines are the only per-slice change to `main.py` — the `lifespan` is **not** touched, and `src/snake_case({ProjectName})/application.py` is never edited at all.

Once the router is included the route exists on the real app, so **regenerate the spec and stage it with the slice**:

```
hatch run docs:openapi     # rewrites docs/openapi.json
```

`docs/openapi.json` is the project's record of what URLs exist (`.build-kit/CLAUDE.md` → *The OpenAPI spec is the source of truth*). Confirm the diff adds exactly the path you intended and changes nothing else — a diff that *moves* an existing endpoint means this slice took a path another one was already using.

`create_app()` already calls `configure_telemetry()`, `instrument_app(app)` and `add_middleware(MetadataMiddleware)`, and `application.py` already overrides `do()` with `command_metadata()` and `command_span` around it — Step 0 established that. Those are process-wide, written once, and **not** per-slice edits: leave them alone. Your slice inherits its command span, the trace context, and the `correlation_id` / `created_at` on every event it emits, for free. Route handlers add no telemetry or metadata code — in particular, **a route never reads or writes event metadata itself**; see `.build-kit/CLAUDE.md` → *Event metadata*.

---

## Key patterns

- **Decision state lives on `self`.** `__init__` sets defaults; `@event` handlers mutate them; `execute` reads them.
- **Tags scope the boundary; state answers the invariant.** `Selector.tags` narrows the replay to the affected entity; the `bool`/`set`/counter on `self` then answers "has this already happened *here*?". Never rely on state alone with `tags=[]` unless the invariant is genuinely system-wide.
- **Selector tags ⊆ trigger tags.** Every tag your selector asks for must be present on the event you emit; otherwise the next command replays a version of history that doesn't include it.
- **Raise, don't return errors.** Any invalid command must raise; the route translates the exception to an HTTP status.
- **One application for the whole process.** Slices never subclass `DcbApplication`. Each `DcbApplication()` instance gets its *own* in-memory store, so per-slice applications silently cannot see each other's events — a member invited through one endpoint would be invisible to the next.
- **The application's lifetime belongs to the FastAPI lifespan.** `DcbApplication` is a context manager; hold it with `with`, never `lru_cache`, which has no teardown hook and so would skip `close()` (and, under Postgres, the connection-pool teardown).
- **Emitted events carry trace context for free.** `{ProjectName}App.do()` opens the command span and puts the `traceparent` into the event-metadata contextvar, from which every `TaggedEvent` constructed inside `execute()` inherits it. Never set `metadata["traceparent"]` by hand in a slice, and never pass `metadata=` to `trigger_event` for this purpose — that bypasses the contextvar and produces events that link to nothing.

---

## Files to create

```
docs/
    openapi.json                                          # REGENERATED, not hand-written — `hatch run docs:openapi`
src/snake_case({ProjectName})/
    main.py                                               # EDITED, not created — one import + one include_router line
    command.py                                            # SHARED RUNTIME — verified in Step 0; never written per-slice
    metadata.py                                           # SHARED RUNTIME — verified in Step 0; never written per-slice
    telemetry.py                                          # SHARED RUNTIME — verified in Step 0; never written per-slice
    application.py                                        # SHARED RUNTIME — verified in Step 0; NOT edited by a slice
src/snake_case({ProjectName})/snake_case({Context})/
    events.py                                             # add new event `Decision` here (shared across slices)
src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/
    __init__.py                                           # package marker
    slice.py                                              # Slice subclass ONLY — no application class
    routes.py                                             # FastAPI router with POST endpoint
tests/acceptance/snake_case({Context})/snake_case({SliceName})/
    test_snake_case({SliceName}).py                       # slice-level GWT tests (eventsourcing.dcb.gwt)
tests/integration/snake_case({Context})/
    test_snake_case({SliceName}).py                       # API-level tests (fastapi.testclient.TestClient)
```
