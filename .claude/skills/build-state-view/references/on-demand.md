# On-Demand State View

Steps 3–7 for the **on-demand** approach: the projection is rebuilt from the
event store on every query. Read this only after Step 1 of `SKILL.md` selected
this approach.

The projection is expressed as an `eventsourcing.pydantic.Slice` subclass whose
`execute()` is a no-op — the slice exists purely so its `consistency_boundary()`
selector drives the replay. The route constructs the view and hands it to **the
project's one central application**, `{ProjectName}App` in `src/snake_case({ProjectName})/application.py`,
which replays it and returns it. A slice never defines an application of its own —
see **Key patterns**.

```text
Request
    │
    ▼
{ProjectName}App.do({SliceName}View(...))  # replays matching events and evolves them as view slice attributes
    │
    ▼
Response model
```

---

## Step 3 — Create `projection.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/projection.py`

Pick the consistency boundary tags first — see *Consistency boundary tags* in
`SKILL.md`. For a single-entity read model, `tags=[]` is almost never right.

```python
from eventsourcing.domain import event
from eventsourcing.pydantic import Selector, Slice

from snake_case({ProjectName}).snake_case({Context}).events import {EventName}


class {SliceName}View(Slice):
    """DCB read-model slice that projects {SliceName} state for a single entity."""

    def __init__(self, entity_id: str) -> None:
        # Query arguments live on self — `execute()` takes no args because
        # `{ProjectName}App.do(slice_instance)` calls `.execute()` with no arguments.
        self._entity_id = entity_id
        self.found = False
        self.field1 = ""
        self.field2 = 0

    def _tags(self) -> list[str]:
        # Consistency boundary keyed by the entity this view reads.
        return [f"{{entity_kind}}:{self._entity_id}"]

    def consistency_boundary(self) -> Selector:
        """Return the selector that defines this view's consistency boundary."""
        # Selector.types is a **Sequence**, not a set. Use a list literal.
        # tags MUST be a subset of the tags used at `trigger_event` in the
        # emitting slice — see `_tags()`.
        return Selector(types=[{EventName}], tags=self._tags())

    @event({EventName})
    def _(self, field1: str, field2: int) -> None:
        # Project the event onto attributes read by the route.
        self.found = True
        self.field1 = field1
        self.field2 = field2

    def execute(self) -> None:
        """Read-only view: no command to run, no event to emit."""
        # Intentionally empty. The replay driven by `consistency_boundary()`
        # populates the attributes above before `do()` returns.
```

Notes on the template:

- **`projection.py` contains the view `Slice` and nothing else.** Do not add a `DcbApplication` subclass here or anywhere in the slice package — the project has exactly one application (Step 4).

### Read-model complexity guide

| Scenario | Projection attributes |
|----------|-----------------------|
| Single-entity presence check | `self.found = False` plus the fields to expose |
| Append-only list per entity (e.g. tricks per dog) | `self.items: list[str] = []` |
| Count / aggregate per entity | `self.count = 0` |
| Multi-entity collection view (rare) | `self.entries: dict[str, EntryState] = {}` — and re-scope tags |

For the common per-entity case, tag scoping does the heavy lifting: the replayed
events are already filtered to that entity, so a handful of plain attributes on
`self` are enough.

---

## Step 4 — Create `routes.py`

File: `src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/routes.py`

```python
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from snake_case({ProjectName}).application import {ProjectName}App, get_application
from snake_case({ProjectName}).auth import require_dog_owner_self  # the slice's actor rule
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).projection import {SliceName}View
from snake_case({ProjectName}).view import (
    NOT_FOUND_RESPONSE,
    VIEW_RESPONSES,
    PositionAtLeast,
    is_behind,
    too_early,
    view_headers,
)

router = APIRouter(tags=["dogs"])  # the entity, pluralised — see SKILL.md step 5


class {SliceName}Response(BaseModel):
    """Response body for the {SliceName} query."""

    dog_id: str
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
    response_model={SliceName}Response,
    operation_id="snake_case({SliceName})",
    responses={**VIEW_RESPONSES, status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE},
)
async def snake_case({SliceName})(
    dog_id: str,
    response: Response,
    app: Annotated[{ProjectName}App, Depends(get_application)],
    position_at_least: PositionAtLeast = None,
) -> {SliceName}Response | Response:
    """{One-line description of the endpoint}."""
    view = app.do({SliceName}View(entity_id=dog_id))
    position = view.last_known_position
    if is_behind(position, position_at_least):
        return too_early(position)
    if not view.found:
        msg = f"{dog_id} not found"
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=msg,
            headers=view_headers(position),
        )
    response.headers.update(view_headers(position))
    return {SliceName}Response(
        dog_id=dog_id,
        field1=view.field1,
        field2=view.field2,
    )
```

Notes on the template:

- **A slice must never define its own application, nor its own dependency factory.** `get_application` is the single dependency; it reads the process-wide `{ProjectName}App` off `request.state` (Step 7 and `.build-kit/CLAUDE.md`).
- **`do()` takes an instance and returns it**, having replayed the events matching `consistency_boundary()` through the `@event` handlers and then called `execute()` (a no-op here). It mutates the perspective in place *and* returns it, so `view = app.do(...)` is the documented contract.
- **`last_known_position` needs no code in `projection.py`.** It is inherited machinery: `Perspective.__new__` initialises it to `None` and `repository.advance()` — called by `do()` for any slice with `@event` handlers — assigns it the replay's head position. `projection.py` stays "the view `Slice` and nothing else"; the route just reads the attribute.
- **The `is_behind` check here always passes, and it stays anyway.** `advance()` sets `last_known_position` to the *store head* rather than the boundary's last event, so an on-demand view is caught up by construction. Keeping the check means a client can poll any view the same way without knowing which kind it is — see `SKILL.md` → *The position contract*.
- **The 425 comes before the 404.** A caller polling for the entity it just created would otherwise be told the entity does not exist and stop.
- **All three responses carry the position.** The injected `response: Response` covers the 200 only — its headers are discarded when the handler raises or returns a `Response` of its own, which is why the 404 passes `headers=` to `HTTPException` and `too_early()` builds its own. `view_headers()` also emits `Cache-Control: no-store`; that is required, not tidiness (`.build-kit/CLAUDE.md` → *View positions*).
- **`response_model=` must stay explicit on the decorator.** The return annotation is a union with `Response`, which FastAPI cannot derive a schema from, so the success schema has to be declared — the same reason command routes declare it. The view class is a `Slice`, not a `BaseModel`: map its attributes onto a Pydantic response model explicitly.
- **The router carries no `prefix`; the full path goes on the decorator.** One greppable path string per slice and no path parameter hidden in a prefix. The slice name lives on in `operation_id=`, which is what links the endpoint back to the slice in the generated spec; `tags=` groups the endpoint by entity instead, and is deliberately shared with other slices.
- **The path parameter takes the entity's own name** (`dog_id`), not a generic `entity_id`. The internal `{SliceName}View(entity_id=…)` keyword is unaffected — that is the projection's own interface, not the public one.
- **Regenerate the spec once the route is wired in** (Step 7; `.build-kit/CLAUDE.md` → *The OpenAPI spec is the source of truth*), then stage `docs/openapi.json` with the rest of the slice.

The FastAPI/Pydantic house rules this template follows (`Annotated[…, Depends(…)]`, `EM101` message variables, runtime imports for Pydantic field types) are in `.build-kit/CLAUDE.md`. Status codes follow the *Error mapping* table in `SKILL.md`.

### Collection endpoints

If the view exposes a list (e.g. "all licences for an organisation"), keep the
same pattern but change the slice's `_tags()` to the parent entity, project into
a `list[…]` on `self`, and return `list[{SliceName}Response]`.

**Drop the 404 entirely when you do.** A collection view has no absence case: an
empty collection at 200 is the complete, correct answer to "what is in here?",
and 404 would tell the caller its address was wrong when it was not. So the
`found` check and `HTTPException` come out, `responses=VIEW_RESPONSES` loses its
`NOT_FOUND_RESPONSE` entry, and the return annotation narrows to
`list[{SliceName}Response] | Response`. The precondition check stays exactly where
it is — it guards the whole response, not the lookup, so it has nothing to do
with whether a 404 exists.

The address follows the tags, as always: a list scoped to a parent entity nests
under it (`GET /organisations/{organisation_id}/licences`), while a list with no
single parent goes to the root and takes query parameters
(`GET /available-stays?from=…&to=…`).

---

## Step 5 — Acceptance tests (slice-level, GWT)

File: `tests/acceptance/snake_case({Context})/snake_case({SliceName})/test_snake_case({SliceName}).py`

Slice-level tests drive the view directly through
`eventsourcing.dcb.gwt.given(...).when(...)` and assert on the view's projected
attributes.

```python
from eventsourcing.dcb.gwt import given
from eventsourcing.domain import TaggedEvent

from snake_case({ProjectName}).snake_case({Context}).events import {EventName}
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).projection import {SliceName}View

# Reuse the same values for the entity id: the tag on `given()` events must
# fall inside the view's consistency boundary or `when()` will not see them.
_ENTITY_ID = "value"
_TAGS = [f"{{entity_kind}}:{_ENTITY_ID}"]


def _view() -> {SliceName}View:
    return {SliceName}View(entity_id=_ENTITY_ID)


def test_snake_case({SliceName})_projects_snake_case({EventName})() -> None:
    """Happy path: replaying {EventName} populates the view's fields."""
    prior = TaggedEvent(
        decision={EventName}(field1="a", field2=1),
        tags=_TAGS,
    )
    view = _view()
    given(prior).when(view)
    assert view.found is True
    assert view.field1 == "a"
    assert view.field2 == 1


def test_snake_case({SliceName})_reports_not_found_when_no_events() -> None:
    """When no events have been emitted, the view reports the entity as absent."""
    view = _view()
    given().when(view)
    assert view.found is False
```

### GWT API notes

- **Stop at `when()`.** `.then(*TaggedEvent)` compares *emitted* events, which a read-only view has none of — assert on the view's projected attributes instead.
- Cross-entity isolation can't be tested here (`.build-kit/CLAUDE.md` explains why GWT rejects out-of-boundary histories). Prove it in the **integration suite**: query two entities and assert each returns its own state.

---

## Step 6 — Integration tests (API-level, TestClient)

File: `tests/integration/snake_case({Context})/test_snake_case({SliceName}).py`

These prove the FastAPI route wires the projection correctly and returns the
right status codes and bodies. They belong in `tests/integration/` — a separate
test env from acceptance tests.

**Use the shared `client` fixture from `tests/integration/conftest.py`** — do not
build a local `FastAPI()` and do not register any `dependency_overrides`. That
fixture wraps the real `create_app()` in a `with TestClient(...)` block, so each
test runs the lifespan and gets its own freshly-constructed `{ProjectName}App` over its
own in-memory store. Testing the real app is also the second line of defence
against two slices claiming the same path — the first being the spec you grepped
in *Addressing the view*.

```python
from fastapi.testclient import TestClient


def test_snake_case({SliceName})_missing_entity_returns_404(client: TestClient) -> None:
    """Querying an entity with no events returns HTTP 404."""
    response = client.get("/dogs/does-not-exist/profile")
    assert response.status_code == 404
```

The URL is the worked example again — use the path you chose in *Addressing the view*.

### Arranging the events the view reads

To exercise the happy path you need events in the store. **Seed them as raw
`TaggedEvent`s** — never by driving the emitting state-change slice:

```python
@pytest.fixture
def prior_thing(dcb_app: {ProjectName}App, entity_id: str, tags: list[str]) -> {EventName}:
    """Seed the fact this view projects."""
    decision = {EventName}(field1="a", field2=1)
    dcb_app.events.append(events=[TaggedEvent(decision=decision, tags=tags)])
    return decision


def test_snake_case({SliceName})_returns_projected_state(
    client: TestClient, prior_thing: {EventName}, entity_id: str,
) -> None:
    ...
```

The rules:

- **Each seeded fact is its own `@pytest.fixture`, declared in the test's signature** — never a helper called from the test body. Fixtures compose (a richer history depends on a simpler one), so a test names only the deepest fact it needs. This also makes ordering structural: pytest resolves the graph before the body runs, so seeding cannot accidentally happen after the request under test.
- **Ids get fixtures too**, so the arrangement and the URL share one value. Put the shared ones (`dcb_app`, ids, `tags`) in `tests/integration/conftest.py` and keep slice-specific histories in the slice's own test module.
- **Return the seeded `Decision`** so the test can assert the response against exactly what it arranged.
- **Raw events, not another slice.** A test for this view should not have to satisfy the emitting slice's validation rules, nor break when they change — and raw events let a test construct histories a slice would legitimately refuse to produce.
- **Pass `events=` only** — supplying `cb`/`after` re-introduces an append condition. Omitting both is the unconditional write a seed wants.
- **Tags must satisfy Selector tags ⊆ trigger tags.** Seeding under the wrong tag is silently invisible rather than an error.

`dcb_app` is the same application the routes use, reached via
`client.app_state["dcb_app"]` — **not** `client.app.state`, which is a different
object that never receives lifespan state.

Cross-entity isolation ("another entity's events do not leak into this one's
view") belongs here too, since acceptance-level GWT refuses histories outside the
boundary.

### The position contract needs two tests of its own

The route reports where it is and honours a caller's precondition (`SKILL.md` → *The
position contract*). Both halves are invisible when broken — a route that forgets the
header still answers a perfectly good 200 — so pin them, using the same seeding fixtures:

```python
def test_snake_case({SliceName})_reports_its_position(
    client: TestClient, prior_thing: {EventName}, entity_id: str,
) -> None:
    """A successful query reports the position the view reflects."""
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

- **The 425 test is deterministic because the position is unreachable**, not because it
  raced the store into a lag window. An on-demand view is caught up by construction, so
  there is no real lag to catch — any position the store could actually reach would pass
  the check and return 200.
- **Seed at least one event first.** Against an empty store `last_known_position` is
  `None`, the header is absent, and the first test would fail for the wrong reason.
- **Assert `>= 1`, never a literal.** The position is the store head, so it counts every
  event the fixtures seeded, not only the ones this view projects.

### A guarded view needs two more tests

A **401** with no `Authorization` header, and a **403** as the wrong actor — with the data
seeded, so the test proves the rule refused the read rather than that there was nothing to
return. Assert `response.json()["detail"]` as well as the status. For an owner-scoped view,
add a third: the *other* subject reading their **own** account gets 200 and sees none of
this one's data. That is what distinguishes "the view is scoped by id" from "the rule
happens to refuse cross-account reads" — two different things, and only the first survives
a change to the rule.

Take headers from the auth fixtures in `tests/integration/conftest.py`; the `client`
fixture stays anonymous so each test names the actor it speaks for.

---

## Step 7 — Wire the router into the central FastAPI app

**Mandatory for every slice.** In `src/snake_case({ProjectName})/main.py`, add the import and the
`include_router` line inside `create_app()`:

```python
from snake_case({ProjectName}).snake_case({Context}).snake_case({SliceName}).routes import (
    router as snake_case({SliceName})_router,
)

def create_app() -> FastAPI:
    ...
    app.include_router(snake_case({SliceName})_router)
```

Those two lines are the only per-slice change to `main.py` — the `lifespan` is
**not** touched, and `src/snake_case({ProjectName})/application.py` is never edited at all.

**Append the line to the end of the block; do not reorder the existing ones.**
Starlette serves the first route whose pattern fully matches, so a path parameter
registered ahead of a literal it can match silently swallows that literal's requests
and answers them from the wrong handler — no startup error, no log line. Appending
keeps every already-working route ahead of yours. If this view's path *could* be
matched by an existing parameterised path, redesign the address rather than relying
on the order (`.build-kit/CLAUDE.md` → *Never let a literal segment sit where a path
parameter could match it*).

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

---

## Key patterns

- **Projection state lives on `self`.** `__init__` sets defaults; `@event` handlers mutate them; `execute()` is a no-op; the route reads them and maps to a response model.
- **Tags scope the boundary; attributes answer the query.** `Selector.tags` narrows the replay to the affected entity; the fields on `self` then reflect that entity's projected state. Never rely on state alone with `tags=[]` unless the read model is genuinely system-wide.
- **One application for the whole process.** Slices never subclass `DcbApplication`. Each `DcbApplication()` instance gets its *own* in-memory store, so a per-slice application would replay an event store no writer ever writes to — the view would always report the entity absent.
- **The application's lifetime belongs to the FastAPI lifespan.** `DcbApplication` is a context manager; hold it with `with`, never `lru_cache`, which has no teardown hook and so would skip `close()` (and, under Postgres, the connection-pool teardown).
- **This slice type adds no telemetry code — it is already covered.** The project is instrumented (Step 0), and an on-demand view has no background thread and no consumer side: the request's HTTP span from `instrument_app` covers it end to end, `do()`'s own span labels the replay, and the reads it performs show up as event-store spans. So do not import OpenTelemetry into `projection.py` or `routes.py`, and do not open a span around the replay — a `consumer_span` is the materialized approach's concern, not this one. Nothing missing here; nothing to add.

---

## Files to create

```
docs/
    openapi.json                                          # REGENERATED, not hand-written
src/snake_case({ProjectName})/
    main.py                                               # EDITED, not created — one import + one include_router line
    metadata.py                                           # SHARED RUNTIME — verified in Step 0; this slice type adds nothing to it
    telemetry.py                                          # SHARED RUNTIME — verified in Step 0; this slice type adds nothing to it
    application.py                                        # SHARED RUNTIME — verified in Step 0; NOT edited by a slice
    view.py                                               # SHARED RUNTIME — verified in Step 0; the position helpers routes.py imports
src/snake_case({ProjectName})/snake_case({Context})/
    events.py                                             # shared event Decisions (add new types here; do not remove existing ones)
src/snake_case({ProjectName})/snake_case({Context})/snake_case({SliceName})/
    __init__.py                                           # package marker
    projection.py                                         # View Slice ONLY — no application class
    routes.py                                             # FastAPI router with GET endpoint
tests/acceptance/snake_case({Context})/snake_case({SliceName})/
    test_snake_case({SliceName}).py                       # slice-level GWT tests (eventsourcing.dcb.gwt)
tests/integration/snake_case({Context})/
    test_snake_case({SliceName}).py                       # API-level tests (fastapi.testclient.TestClient)
```
