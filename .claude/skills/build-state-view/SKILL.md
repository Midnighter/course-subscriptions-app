---
name: build-state-view
description: Implements a pyeventsourcing state-view slice (read model / projection — on-demand replay or materialized view, FastAPI GET route, pytest tests) from a slice.json definition
---

# Build State View Slice

> Before doing anything else, read the slice definition from `.build-kit/.slices/<contextSlug>/<sliceFolder>/slice.json`. This file is the **source of truth** for all fields, events, and read-model shape. Never invent fields not defined there.
>
> **These two path segments are lowercase slugs, not snake_case**, and they slug differently from each other — they are written by the `load-slice` skill, which owns the rules:
>
> - `<contextSlug>` — the context lowercased, spaces to hyphens, non-alphanumeric removed (`"My Ctx"` → `my-ctx`). Falls back to `default` when the slice has no context.
> - `<sliceFolder>` — the slice title lowercased with **all spaces removed** and any `slice:` prefix stripped (`"View Dog Profile"` → `viewdogprofile`).
>
> So `ViewDogProfile` in the `Kennel` context reads from `.build-kit/.slices/kennel/viewdogprofile/slice.json`. Do **not** use the `snake_case(...)` form here — that convention applies to the generated Python paths below, not to this directory. If the path you derive is missing, list the context directory rather than guessing.

Project-wide conventions (tooling, pre-commit, test layout) live in `CLAUDE.md`. Consult it for anything not specific to building a slice.

---

## What a State View Slice is

A state-view slice is a **read model / projection**. It follows events emitted
by state-change slices in the same context and evolves them into queryable
state.

This can be done in one of two broad ways:

1. **On-demand**: The projection is rebuilt from the event store on every query. This is the simplest approach. It has the benefit that the view is always up-to-date with the event store, but it also means that every query hits your event store and prevents optimizations like caching or indexing.
2. **Materialized**: The projection is built in the background and is stored in a standalone `TrackingRecorder` — backed either by `POPOTrackingRecorder` (in-memory, the default) or `PostgresTrackingRecorder` (durable). This allows for faster queries, avoids hitting the event store on every query, but it also means that the view may be stale if the background process hasn't caught up with the event store and requires additional infrastructure.

Check any comments in the slice definition for guidance on which approach to use. If none is given, default to **on-demand**.

---

## Step 1 — Read the slice.json

From the slice definition, extract:
- **sliceName** — the projection name (becomes the `Slice` subclass name and the query method name)
- **context** — the bounded context (used to find `src/snake_case({ProjectName})/snake_case({Context})/events.py`)
- **events[]** — events this projection consumes
- **readModel / fields** — the shape of the entries this projection exposes
- **specifications[]** — test scenarios. If empty, still write at least one happy-path test (entity exists, projection returns expected fields) and one negative test (entity does not exist → 404).
- **approach** — on-demand or materialized, per the rule above.

### Placeholder grammar

Every placeholder in the templates below is **PascalCase**. There is only one form; derive it once from `sliceName` and reuse it verbatim.

| Placeholder | Derived from | Example |
|-------------|--------------|---------|
| `{SliceName}` | `sliceName` in PascalCase | `ViewDogProfile` |
| `{EventName}` | event type in PascalCase | `DogRegistered` |
| `{Context}` | bounded context in PascalCase | `Kennel` |
| `{ProjectName}` | `project.name` in `pyproject.toml`, PascalCase | `MyProject` |

`{ProjectName}` is the one placeholder that does **not** come from the slice definition — read it once from `[project] name` in `pyproject.toml`. It is already fixed for the whole repository, so `snake_case({ProjectName})` is simply the existing top-level package under `src/`; confirm it there rather than deriving a name that does not exist on disk.

Filesystem paths, Python module names, and method names are **derived from the PascalCase placeholder at code-generation time**, not carried as separate placeholders:

- Python module / package path → lowercase PascalCase split on word boundaries, joined with `_` (e.g. `ViewDogProfile` → `view_dog_profile`).
- Query method name → same as the Python module (snake_case).
- Environment-variable prefix → the snake_case form upper-cased, written `upper_snake_case({SliceName})` (e.g. `ViewDogProfile` → `VIEW_DOG_PROFILE`). Only the materialized approach uses this.

Apply these transforms mechanically; do not introduce new placeholder tokens.

**The URL is not on this list** — see *Addressing the view* below.

---

## Addressing the view

This applies to **both** approaches. Work the URL out before writing the route; the full rule and its table live in `CLAUDE.md` → *API addressing*.

A view is addressed by **the situation the reader is looking at**, never by the projection that produces it. `/view-dog-profile/{entity_id}` names a build artefact and forces the client to know how the read model was implemented; `/dogs/{dog_id}/profile` names what they asked for.

1. **Take the entity from the boundary tags** — the decision you make in *Consistency boundary tags* below. `tags=[f"dog:{dog_id}"]` gives `/dogs/{dog_id}`. Pluralise the tag kind and kebab-case it.
2. **Name the situation**, not the slice: `profile`, `itinerary`, `upcoming-arrivals`, `cancellation-context`. Strip the `View`/`Get`/`List` verb — it is carried by the HTTP method.
3. **Collection and search views** have no single entity to nest under. They live at the root and take query parameters: `GET /available-stays?from=…&to=…&guests=2`. A globally-scoped view (`tags=[]`) is addressed the same way.

```
ViewDogProfile     + tags=[f"dog:{dog_id}"]    ->  GET /dogs/{dog_id}/profile
ViewHostArrivals   + tags=[f"host:{host_id}"]  ->  GET /hosts/{host_id}/upcoming-arrivals
SearchAvailableStays (no single entity)        ->  GET /available-stays?from=…&to=…
```

4. **Check the path is free.** `grep` it in `docs/openapi.json` — the committed spec is the source of truth for what already exists (`CLAUDE.md` → *The OpenAPI spec is the source of truth*). Two views over the same entity are fine and expected (`/dogs/{dog_id}/profile` and `/dogs/{dog_id}/history`); two views on the *same path* are a bug.

The slice name still has to be traceable, so it moves into the OpenAPI metadata: `tags=["snake_case({SliceName})"]` on the router and `operation_id="snake_case({SliceName})"` on the route.

---

## Step 2 — Ensure the shared events module exists

Each context has one `src/snake_case({ProjectName})/snake_case({Context})/events.py` module holding every domain event for that context — events are shared across slices in the same context. A view **consumes** events emitted by other (state-change) slices; do not redefine them here.

File location: `src/snake_case({ProjectName})/snake_case({Context})/events.py`.

### Event pattern (for reference — the state-change slice is what creates these)

```python
from eventsourcing.pydantic import Decision


class {EventName}(Decision):
    """{one-line description of what this event means}."""

    field1: str
    field2: int
    # data fields from slice.json — use snake_case even if slice.json uses camelCase
```

> The template omits the copyright header and module docstring for brevity. Every real file needs them — commit the result and let pre-commit surface anything you missed (see `CLAUDE.md`).

If the view depends on an event type that isn't in `events.py` yet, add it. Never remove existing event types.

---

## Consistency boundary tags

This applies to **both** approaches — decide it before writing the projection.

In DCB, `Selector.tags` scopes the replay: the app only rebuilds view state from events whose tags **intersect** the selector's tags. Empty `tags=[]` means "every event of this type, everywhere" — for a single-entity read model that is almost never what you want.

Pick the tag values from the query arguments — never invent them, never derive them from wall-clock or generated data:

| View scope | `tags=` should be |
|------------|-------------------|
| "Show one user's profile" | `[f"user:{user_id}"]` |
| "Show one licence's history" | `[f"licence:{licence_id}"]` |
| "Show one organisation's roster" | `[f"orga:{orga_id}"]` |
| Two entities together (rare) | `[f"user:{user_id}", f"orga:{orga_id}"]` |
| **Truly global** (system-wide dashboard, singleton config) | `[]` — and justify it in the docstring |

Whatever you pick must satisfy the **Selector tags ⊆ trigger tags** rule in `CLAUDE.md` — check the emitting slice's `trigger_event(..., tags=...)` call, not just the slice.json.

---

## Error mapping

Applies to the GET route in both approaches:

| Exception | HTTP status |
|-----------|-------------|
| View finished with the entity absent (`view.found is False`) | `404 Not Found` |
| Query-parameter validation failure (Pydantic) | `422 Unprocessable Entity` — FastAPI does this automatically |
| Anything else | let FastAPI return `500` |

**Views read, they don't decide.** If the requested entity has no events, return 404; otherwise return the projection. Do not raise domain errors from a projection.

---

## Steps 3–7 — follow the reference for your chosen approach

Now read **one** of the following and follow its Steps 3–7. Do not read both.

| Approach | Reference |
|----------|-----------|
| On-demand (default) | `references/on-demand.md` |
| Materialized | `references/materialized.md` |

Each reference covers the projection module, the FastAPI route, acceptance tests, integration tests, app wiring, and its own files-to-create tree.
