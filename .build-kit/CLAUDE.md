# Project notes for Claude

Conventions and gotchas that apply to every file in this repo. Skills should describe **what** to build; this file records **how** to keep it committable.

Placeholders below follow the build skills: `{ProjectName}` is `[project] name` in `pyproject.toml`, and `snake_case({ProjectName})` is the single top-level package under `src/` — confirm it on disk rather than deriving it.

## Tooling

- **hatch** manages every Python environment. Never call `python`/`pip`/`pytest` directly — use `hatch -e dev run <cmd>` or `hatch -e dev shell`. Each test suite has its own env: `unit-tests`, `acceptance-tests`, `integration-tests`, `documentation-tests`.
- **Run a suite as `hatch -e <env> run run`.** Each of those four envs defines its own `run` script that already carries the coverage flags *and the path to that suite's directory*. Running the whole set is four invocations, one per env; there is no single command that runs everything.
- **Never `hatch -e <env> run pytest` with no path.** `[tool.pytest.ini_options] testpaths = ["tests"]` is global, so a bare `pytest` collects **every** suite from inside a single suite's env — where the other suites' dependencies are absent, and where the duplicate basenames below collide. Naming a path explicitly (`hatch -e unit-tests run pytest tests/unit/test_projection.py -q --no-cov`) is fine and is the right way to iterate on one file.
- **pre-commit** runs on every commit. All hooks must pass; the list is authoritative in `.pre-commit-config.yaml`.
- **Commit each unit of work.** When a coherent chunk is done (a slice, a bug fix, a refactor), commit it. `git commit` triggers the pre-commit hooks, which is the intended way to catch header/docstring/style violations — don't rely on eyeballing the compliance rules below. If a hook rewrites files, stage the result and re-run until the commit succeeds.

## Pre-commit compliance rules

Every new Python module must satisfy these before it can be committed:

- **Copyright header** on line 1: `# Copyright {YYYY} {ProjectAuthor}` (ruff `CPY001`), where `{ProjectAuthor}` is the `name` from `[project] authors` in `pyproject.toml` — copy the exact spelling already used by existing modules.
- **Module docstring** immediately after the header (`D100`).
- **Class docstring** on every public class (`D101`).
- **Method/function docstring** on every public callable (`D102`, `D103`, `D104` for packages).
- **`raise ... from exc`** when re-raising inside `except` (`B904`).
- **String exception messages** go into a variable first, then `raise` (`EM101`).
- **Trailing commas** in multi-line calls / literals (`COM812`).
- **Imports sorted** — first-party is `src` / the project package name (`I001`).

## FastAPI / Pydantic gotchas

- **Depend on `Annotated[T, Depends(...)]`, not `T = Depends(...)`.** The old form trips `FAST002` and `B008`.
- **Pydantic model fields need runtime type imports.** `from __future__ import annotations` + PEP 563 is fine for everything *except* types that appear as `BaseModel` fields — Pydantic can't rebuild the schema when the class is under `TYPE_CHECKING`. Import such types at runtime and silence `TC003` with a `# noqa: TC003` comment on that specific line.
- **Dependency factories read from `request.state`, never `lru_cache`.** `get_application` (state-change / on-demand views) and per-slice view getters such as `get_snake_case({SliceName})_view` (materialized views) read the object the lifespan already yielded into `request.state` — they don't construct or cache anything themselves. `lru_cache` has no teardown hook and would pin the first test's instance across every later test; since nothing here uses it, integration tests need no `dependency_overrides` at all.
- **A dependency's parameter types must be importable at runtime.** `def get_application(request: Request)` with `Request` under `TYPE_CHECKING` makes FastAPI fail to resolve the annotation and silently treat `request` as a *query parameter* — every call then 422s with `{'loc': ['query', 'request'], 'msg': 'Field required'}`. Import such types at runtime and silence `TC002` with a `# noqa` on that line, same as for Pydantic field types.

## Observability

OpenTelemetry covers three seams the library gives no help with: the command path, the event store, and the projection threads — where a failure is silent by construction (a dead processing thread leaves a view quietly stale). All four hooks below are plain public methods or attributes; **nothing here monkeypatches the library.**

- **`src/snake_case({ProjectName})/telemetry.py` is shared runtime, written once at *First-time project setup*.** The module and its tests are in **`.build-kit/references/telemetry.md`** — copy them from there rather than deriving them from the bullets below, which are the *why*, not the source. If you are building a slice in an existing project and this file is missing, that project was set up incompletely: create it and its wiring first, commit as a `chore:`, then build the slice. Never write it per-slice, and never skip a slice's instrumentation because it is absent. Its contract:
  - `configure_telemetry()` — idempotent; called once from `create_app()`. Installs an SDK **only** when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and `OTEL_TRACES_EXPORTER` is not `none`. Honour the standard `OTEL_*` variables; do not invent project-specific ones.
  - `command_span(slice_)` — used by `{ProjectName}App.do`.
  - `consumer_span(envelope, name)` — used by `Projection.process_event`.
  - `instrument_recorder(app)` — wraps `app.recorder.append`/`.read` at the instance level. Call it **after** `{ProjectName}App()` is constructed; `recorder` is set in `__init__`.
- **Off by default, and that is a no-op tracer, not a cheat.** With no endpoint configured no `TracerProvider` is ever installed and `get_tracer()` returns a proxy to `NoOpTracer` — no exporter threads, no network, no I/O. It is *not* literally free: every no-op span still does one `contextvars` attach/detach. Cheap enough to leave in hot paths, not cheap enough to claim "zero overhead".
- **`application.py` gains exactly one override: `do()`.** This is the single documented exception to *"`application.py` is never edited"* — and it is written **once**, not per slice. Adding a slice still touches `main.py` only. That override already exists to capture command outcomes (see *Command outcomes*); the span wraps its whole body, including `save()`, because `trigger_event` fires inside `execute()`.
- **`do()` serves commands and on-demand view replays both, so dispatch the span name.** `isinstance(s, CommandSlice)` gets a command span; anything else is a replay. A blanket `command_span` around `do()` mislabels every on-demand view query.
- **Trace context travels in `TaggedEvent.metadata`, via `put_metadata_in_context`** — never by mutating events one at a time. `metadata` defaults from a contextvar, so every event constructed inside that block inherits the `traceparent` automatically, and it round-trips through the store (a `jsonb` column under Postgres) to reach the consumer.
- **Only write `traceparent` when the carrier is non-empty after `inject()`.** Under a no-op tracer the span context is invalid and `inject()` writes nothing; don't turn that into a `{"traceparent": None}` entry.
- **On extract, guard `span_ctx.is_valid` before building a `Link`.** Events written while telemetry was off carry no traceparent, and events replayed by `drain()` may link to traces already outside the retention window — neither is a bug.
- **`process_event` spans are links, never children.** Two independent reasons: the producing span ended long before (often minutes), and `BaseProjectionRunner`'s processing thread is a bare `threading.Thread`, which does **not** inherit contextvars — so ambient propagation is not merely wrong here, it is impossible. Use `SpanKind.CONSUMER` with a `Link`, per the OTel messaging conventions for temporally decoupled producers and consumers.
- **Instrumentation must never swallow an exception.** A span context manager that suppresses is the blanket `try/except` around `process_event` wearing a different hat: it advances past a poison event and diverges the view from the log permanently while `/livez` and `/readyz` both still report 200. Record the exception on the span and **re-raise**, so the supervisor still sees the thread die.
- **Guard `None` in metrics.** `recorder.head()` and `max_tracking_id()` both return `int | None`. Before a projection has processed anything its lag is *undefined*, not zero — skip the observation rather than reporting a fake backlog.
- **Both spans set a `correlation_id` attribute** — `command_span` off the context, `consumer_span` off the envelope, since the projection thread inherits no contextvars. That makes traces and the event log **joinable in both directions** without either becoming the other's source of truth. See *Event metadata*.
- **`opentelemetry-api` is a required dependency; only the *SDK* belongs to the `telemetry` extra, and the `dev` env alone.** Split them deliberately: `telemetry.py` and the `do()` override import from the API at module scope, so an API that is merely optional breaks every suite at import time — the no-op path is an API-level proxy to `NoOpTracer` and still needs the API installed. Put `opentelemetry-api` in `[project] dependencies` and **not** in the extra as well; listing it in both lets the extra pin or reinstall it independently of the base requirement. The extra holds sdk/exporter/instrumentation only. The test suites deliberately do not install *that*, so they exercise the no-op path. Don't add the SDK to a test env to assert on spans; construct an in-memory provider inside the test instead.

## Event metadata

Every recorded event carries three general-purpose keys in `TaggedEvent.metadata`: `correlation_id` (the flow it belongs to), `causation_id` (the event that caused it), and `created_at` (when the unit of work that wrote it ran). They ride on the **envelope, not the `Decision`**, so adding them changes no event schema and needs no migration.

- **`src/snake_case({ProjectName})/metadata.py` is shared runtime, written once at *First-time project setup*.** The module and its tests are in **`.build-kit/references/metadata.md`** — copy them from there rather than deriving them from the bullets below, which are the *why*, not the source. Same repair rule as `telemetry.py`: a project missing it was set up incompletely, so create it and its wiring first and commit as a `chore:`.
- **Metadata is seeded centrally, at exactly two places.** `MetadataMiddleware` puts a `correlation_id` in context per HTTP request; `{ProjectName}App.do` seeds `created_at` always, and `correlation_id` only when absent. **Slices and routes never touch metadata** — a slice that reaches for it is a slice that will disagree with the next one.
- **`MetadataMiddleware` must be pure ASGI, not `BaseHTTPMiddleware`.** The latter runs the endpoint in a separate anyio task, so a contextvar set in its `dispatch` never reaches the route and the metadata silently arrives empty. This is the single most likely way to ship a `correlation_id` that is always freshly minted and never the client's.
- **A client-supplied `correlation_id` is sanitised, never trusted.** It lands in a `jsonb` column, the logs, and a response header, so bound it and reject control characters. Replace an unusable one outright rather than truncating: a stored id is then either exactly what the client sent or one we minted, never a mangled prefix of the two.
- **`causation_id` is derived locally, always, and is never accepted from a client.** The invariant it buys is that every `causation_id` resolves to an event uuid in our own log. That is also why a **root command carries no `causation_id` at all**: an HTTP command has no causing *event*, and minting an id that resolves to nothing would break exactly the invariant that justified rejecting the client's. A root is still unambiguous — `correlation_id` present, `causation_id` absent.
- **`created_at` uses explicit UTC, not the library's `datetime_now_with_tzinfo()`.** That honours `TZINFO_TOPIC`, and the timestamp on a permanent log record should not be reconfigurable by an environment variable set for unrelated reasons. It is stamped per command, not per flow: an automation's command is a later unit of work than the trigger that caused it.
- **This is not a substitute for `traceparent`, and does not replace it.** They differ in lifetime (permanent versus the collector's retention window), in availability (always versus off unless an exporter is configured — every test env runs a no-op tracer), and in granularity (`causation_id` is a stored event's uuid, so it cannot be derived from a span id). Keep both, and make them **joinable** via the span attribute described under *Observability*.
- **Metadata is not queryable and is not part of the append condition.** `DcbQueryItem` is `types + tags` only. Any dedup or idempotency keyed on metadata can only ever be read-then-write, with a race window. The DCB-native way to make a command idempotent is a `command:<key>` tag inside `consistency_boundary()`, not a metadata lookup.
- **Adding a key later — `source`, `actor`, a tenant id — is a one-line change in `metadata.py` and nowhere else.** If a proposed key needs edits in a slice, it is in the wrong place.

## Test layout

- **No `__init__.py` under `tests/`.** Pytest doesn't need it; adding them is unwanted clutter. The consequence is that test modules are imported by basename, and a slice contributes `test_snake_case({SliceName}).py` to *both* `tests/acceptance/` and `tests/integration/` — so any single collection spanning both suites aborts with `import file mismatch`. That is the second reason a suite is run by its own `run` script and never by a bare `pytest` (see *Tooling*), and it is not a defect to "fix" by adding package markers.
- **`@pytest.fixture`** — no parentheses (`PT001`).
- **`tests/acceptance/`** — for `Slice`-based slices (state-change, on-demand view), given/when/then tests using `eventsourcing.dcb.gwt`. For `Projection`-based slices (automation, materialized view), GWT cannot drive a `Projection`: construct the view and projection directly and call `process_event` yourself — no `DcbApplication`, no runner, no background thread.
- **`tests/integration/`** — API-level tests using `fastapi.testclient.TestClient` against the real `create_app()`, via the shared `client` fixture in `tests/integration/conftest.py`. Never build a local `FastAPI()`: testing the real app is what catches route-prefix collisions between slices.
- **`tests/unit/`** and **`tests/documentation/`** — reserved for their respective purposes. Add real tests as soon as there's non-trivial logic to cover (a `Slice`'s `_tags()`, a validation branch, a runnable doctest); a placeholder test is only a stopgap for a suite that's genuinely empty so far, never a step to perform on principle.
- **`with TestClient(app) as client:`, not a bare `TestClient(app)`,** whenever the app defines a `lifespan`. The `with` block is what drives the lifespan context manager; without it the state the lifespan yields never exists and every route raises `AttributeError` on `request.state.dcb_app`.
- **Never `time.sleep` to wait for a background thread.** `TrackingRecorder.wait(context_name, notification_id, timeout)` exists for this — call it on the **view**, never on a runner (the supervisor may already have replaced it). It blocks the calling thread, not the event loop — safe to call from a sync test against an async app.
- **Integration tests need `httpx2`** in the `integration` dependency group and a matching `integration-tests` hatch env.

## First-time project setup

Before the first build skill runs in a new project, check whether the files below exist. If not, create them in this order before proceeding with the skill — the build skills themselves assume all of this is already in place. Every build skill's **Step 0** re-checks this list, so a project that was set up incompletely is repaired at the next slice rather than carried forward.

The shared runtime is these eight modules under `src/snake_case({ProjectName})/`:

| Module | Created |
|---|---|
| `__init__.py` | setup |
| `command.py` | setup |
| `metadata.py` | setup |
| `telemetry.py` | setup |
| `application.py` | setup |
| `view.py` | setup |
| `main.py` | setup |
| `projection.py` | the first time a projection slice is built (see *Projection runners*) |

Each is written **once** per project and is never a per-slice artefact. `projection.py` is the one deferred to first use, because it is dead code in a project with no projection — but Step 0 still checks for it, and a projection slice that finds it missing creates it before the slice, not alongside it.

The `/livez` and `/readyz` routes are deferred on the same terms: the slice that registers the **first** supervisor adds them, whatever that slice's type. They have nothing to report until a supervisor exists, and a supervisor without them is a projection that can die unobserved. See *Supervising projections* → *The `/livez` and `/readyz` routes*.

The order below matters: `telemetry.py` imports `CommandSlice` from `command.py` and `CORRELATION_ID_KEY` from `metadata.py`, `application.py` imports `command_metadata` from `metadata.py` and `command_span` from `telemetry.py`, `main.py` imports `MetadataMiddleware` from `metadata.py`, and `view.py` imports `{ProjectName}App` from `application.py`. `metadata.py` imports nothing of the project's own, which is why it can come this early.

1. **Resolve every `TODO` placeholder in `pyproject.toml`.** `grep -n TODO pyproject.toml` to find them all: `[project] name`, `description`, `authors`; `packages = ["src/TODO"]` and `version-file = "src/TODO/_version.py"`; `[tool.coverage.paths] source`/`omit`; `[tool.ruff] exclude`; `[tool.ruff.lint.isort] known-first-party`; `pyrefly check src/TODO`; and the three `--cov=TODO` occurrences in the `unit-tests`/`acceptance-tests`/`integration-tests` scripts. Never create `src/snake_case({ProjectName})/_version.py` by hand — `hatch-vcs` generates it at build time and it's gitignored.
2. **Create `src/snake_case({ProjectName})/__init__.py`** — copyright header plus a one-line module docstring naming the package.
3. **Create `src/snake_case({ProjectName})/command.py`** — the base class every state-change slice inherits and the outcome it carries. See *Command outcomes* below for why each field exists.
   ```python
   from typing import ClassVar, NamedTuple
   from uuid import UUID  # noqa: TC003

   from eventsourcing.pydantic import Slice
   from pydantic import BaseModel


   class CommandOutcome(NamedTuple):
       """The ids and append position of the events a command recorded."""

       event_ids: tuple[UUID, ...]
       position: int | None


   class CommandSlice(Slice):
       """Base class for state-change slices, carrying their command outcome."""

       outcome: ClassVar[CommandOutcome] = CommandOutcome(event_ids=(), position=None)


   class CommandResponse(BaseModel):
       """Response body reporting the outcome of a successful command."""

       event_ids: list[UUID]
       position: int | None
   ```
4. **Create `src/snake_case({ProjectName})/metadata.py`** — copy it from **`.build-kit/references/metadata.md`**, which holds the whole module verbatim. Do not derive it from the *Event metadata* section below; that section is the rationale, the reference file is the source. Create it now, unconditionally, for the same reason as `telemetry.py`: it costs nothing, and a project that ships without a `correlation_id` cannot retrofit one onto events already written.
5. **Create `src/snake_case({ProjectName})/telemetry.py`** — copy it from **`.build-kit/references/telemetry.md`**, which holds the whole module verbatim. Do not derive it from the *Observability* section above; that section is the rationale, the reference file is the source. Create it now, unconditionally: the dependencies are already declared, it costs nothing at runtime with no exporter configured, and a project that ships without it never acquires it later.
6. **Create `src/snake_case({ProjectName})/application.py`** — the one process-wide application. Import `DcbApplication` from `eventsourcing.pydantic`, **not** the generic `eventsourcing.dcb.application` — the Pydantic module wires the `Transcoder` this project needs. The `do()` override is written **here, once**, and is not a per-slice edit; see *Command outcomes* and *Observability*.
   ```python
   from eventsourcing.domain import TSlice
   from eventsourcing.pydantic import DcbApplication
   from fastapi import Request

   from snake_case({ProjectName}).command import CommandOutcome, CommandSlice
   from snake_case({ProjectName}).metadata import command_metadata
   from snake_case({ProjectName}).telemetry import command_span


   class {ProjectName}App(DcbApplication):
       """The single, process-wide DCB application."""

       def do(self, s: TSlice) -> TSlice:
           """Advance, execute and save a slice, capturing a command's outcome."""
           with command_metadata(), command_span(s):
               if type(s).do_projection:
                   s = self.repository.advance(s)
               s.execute()
               if isinstance(s, CommandSlice):
                   event_ids = tuple(envelope.uuid for envelope in s.new_decisions)
                   position = self.repository.save(s) if s.new_decisions else None
                   s.outcome = CommandOutcome(event_ids=event_ids, position=position)
               elif s.new_decisions:
                   self.repository.save(s)
               return s


   def get_application(request: Request) -> {ProjectName}App:
       """Return the process-wide application from FastAPI request state."""
       return request.state.dcb_app
   ```
   The span wraps the **whole** body, `save()` included, because `trigger_event` fires inside `execute()` and the trace context must still be in scope when the events are constructed *and* when they are appended. `command_metadata()` is **outermost**, so `command_span` can read the `correlation_id` off the context for its span attribute.
7. **Create `src/snake_case({ProjectName})/view.py`** — the read side's counterpart to `command.py`: everything a view route needs to report the position it reflects. See *View positions* below for why each piece exists.
   ```python
   from typing import Annotated, Any

   from eventsourcing.persistence import TrackingRecorder  # noqa: TC002
   from fastapi import Header, Response, status

   from snake_case({ProjectName}).application import {ProjectName}App

   CURRENT_POSITION_HEADER = "X-Current-Position"
   POSITION_AT_LEAST_HEADER = "X-Position-AtLeast"

   PositionAtLeast = Annotated[
       int | None,
       Header(
           alias=POSITION_AT_LEAST_HEADER,
           ge=0,
           description="Answer only once the view has reached at least this position.",
       ),
   ]

   _POSITION_HEADER_SPEC: dict[str, Any] = {
       CURRENT_POSITION_HEADER: {
           "description": (
               "Position of the last event this view reflects. Absent "
               "when the view has processed nothing."
           ),
           "schema": {"type": "integer"},
       },
   }

   VIEW_RESPONSES: dict[int | str, dict[str, Any]] = {
       status.HTTP_200_OK: {"headers": _POSITION_HEADER_SPEC},
       status.HTTP_425_TOO_EARLY: {
           "description": f"The view has not reached {POSITION_AT_LEAST_HEADER}.",
           "headers": _POSITION_HEADER_SPEC,
       },
   }

   NOT_FOUND_RESPONSE: dict[str, Any] = {
       "description": "This view holds no such entity.",
       "headers": _POSITION_HEADER_SPEC,
   }


   def materialized_position(view: TrackingRecorder) -> int | None:
       """Return the position of the last event a materialized view processed."""
       return view.max_tracking_id({ProjectName}App.context_name)


   def is_behind(current: int | None, at_least: int | None) -> bool:
       """Return whether a caller's required position has not been reached."""
       if at_least is None:
           return False
       return current is None or current < at_least


   def view_headers(current: int | None) -> dict[str, str]:
       """Return the headers every view response carries, position included."""
       headers = {"Cache-Control": "no-store"}
       if current is not None:
           headers[CURRENT_POSITION_HEADER] = str(current)
       return headers


   def too_early(current: int | None) -> Response:
       """Return an empty 425 telling the caller the view has not caught up."""
       return Response(
           status_code=status.HTTP_425_TOO_EARLY,
           headers=view_headers(current),
       )
   ```
   `{ProjectName}App.context_name` is a **class** attribute, so `materialized_position` reads it without an application instance — which is what lets a materialized view's route keep depending on its view alone, with no `get_application`. Never re-declare that string in a route.
8. **Create `src/snake_case({ProjectName})/main.py`** — a minimal bootstrap lifespan. Do **not** reach for `AsyncExitStack`/`ProjectionSupervisor` yet; that upgrade happens later, the first time a projection is added (see *Lifespan ownership* and *Supervising projections* below).
   ```python
   from collections.abc import AsyncIterator
   from contextlib import asynccontextmanager

   from fastapi import FastAPI

   from snake_case({ProjectName}).application import {ProjectName}App
   from snake_case({ProjectName}).metadata import MetadataMiddleware
   from snake_case({ProjectName}).telemetry import (
       configure_telemetry,
       instrument_app,
       instrument_recorder,
   )


   @asynccontextmanager
   async def lifespan(app: FastAPI) -> AsyncIterator[dict]:
       """Construct the process-wide application for the lifetime of the app."""
       with {ProjectName}App() as dcb_app:
           instrument_recorder(dcb_app)
           yield {"dcb_app": dcb_app}


   def create_app() -> FastAPI:
       """Build the FastAPI application, wiring in every slice's router."""
       configure_telemetry()
       app = FastAPI(lifespan=lifespan)
       instrument_app(app)
       app.add_middleware(MetadataMiddleware)
       return app
   ```
   Two orderings are load-bearing: `configure_telemetry()` comes **first** in `create_app()`, because `instrument_app` does nothing unless it finds the state that call sets; and `instrument_recorder(dcb_app)` comes **after** the application is constructed, because `recorder` is set in `__init__`. When the lifespan is later upgraded to an `AsyncExitStack`, `instrument_recorder(dcb_app)` moves to immediately after `stack.enter_context({ProjectName}App())`, on the same principle.

   Each slice's own build step adds its `include_router` line inside `create_app()` — that's the only per-slice edit to this file.
9. **Create `tests/integration/conftest.py`** with the shared `client` fixture only — slice-specific fixtures (ids, seeded histories) belong in each slice's own test module, not here.
   ```python
   from collections.abc import Iterator

   import pytest
   from fastapi.testclient import TestClient

   from snake_case({ProjectName}).main import create_app


   @pytest.fixture
   def client() -> Iterator[TestClient]:
       """Run the app's lifespan and expose a TestClient bound to it."""
       with TestClient(create_app()) as client:
           yield client
   ```
10. **Create `tests/unit/test_metadata.py`** and **`tests/integration/test_metadata.py`** — both in `.build-kit/references/metadata.md`. The unit tests pin the pure functions and the seeding rules; the middleware needs the integration suite, because `httpx` lives in the `integration` dependency group and because the claim worth testing — that the contextvar reaches the *route handler* — needs a live ASGI stack to be worth anything.
11. **Create `tests/unit/test_telemetry.py`** — also in `.build-kit/references/telemetry.md`. These are real tests of real logic, not placeholders: they pin the no-op path, the idempotence of `instrument_recorder`, and that no wrapper swallows an exception. They must pass **without** `opentelemetry-sdk` installed, which is what proves the no-op path.
12. **Create `tests/unit/test_view.py`** — real tests too, and the cheapest place to pin the position contract, since `is_behind` and `view_headers` are pure functions. Cover every branch: no `at_least` given, `current is None`, `current < at_least`, `current == at_least`, `current > at_least`, and that `view_headers(None)` omits `X-Current-Position` while still carrying `Cache-Control`. The two `None` cases mean opposite things — "no precondition" versus "nothing processed yet" — and a test that only exercises one of them will not catch them being collapsed.

    Also assert that **every documented response declares `X-Current-Position`** — the 200 and 425 in `VIEW_RESPONSES`, plus `NOT_FOUND_RESPONSE` — and that `VIEW_RESPONSES` holds *only* those two status codes, so 404 stays opt-in for the views that genuinely have an absence case. This is not busywork over a constant: a header the routes send but the spec omits is invisible to every generated client, and **no request-level test can catch it**, because the response the route actually returns is correct. The omission is only visible in `docs/openapi.json`, which nothing else asserts on.
13. **Fill in the remaining `TODO` titles** in `README.md`, `mkdocs.yml`, and `docs/index.md` with the project's display name. These don't block any tooling, but resolve them as part of setup rather than leaving them for later.

Do not create placeholder tests as part of this setup — see *Test layout* below. None of `tests/unit/test_metadata.py`, `tests/integration/test_metadata.py`, `tests/unit/test_telemetry.py`, or `tests/unit/test_view.py` is one.

## Building a Slice

Every slice is built by a skill, never by hand. Read `sliceType` from
`.build-kit/.slices/<contextName>/<folder>/slice.json` and invoke the matching skill:

| `sliceType` | Skill | Builds |
|---|---|---|
| `STATE_CHANGE` | `build-state-change` | A `CommandSlice` plus its `POST` route |
| `STATE_VIEW` | `build-state-view` | A read model plus its `GET` route |
| `AUTOMATION` | `build-automation` | A `Projection` that reacts to events by issuing a command |

`build-state-view` then splits again, inside the skill: on-demand (the default — replay a
`Slice` per request, no background thread) or materialized (a `Projection` kept up to date
by a runner). The skill's own Steps 3–7 dispatch to `references/on-demand.md` or
`references/materialized.md`; read exactly one.

Each skill begins with **Step 0 — Verify the shared runtime**, which re-checks the module
list in *First-time project setup* above. Do not skip it, even on a project that has
already shipped slices: the shared runtime is what a slice is built *on*, and a project
missing a piece of it will otherwise keep producing slices that quietly lack it.

## pyeventsourcing DCB API quick reference

- The class is `DcbApplication` (lowercase `cb`), not `DCBApplication`.
- `Selector(types=[E], tags=[])` — `types` is a `Sequence`, not a set.
- `app.do(slice_instance)` — pass an **instance**, not the class; `do()` internally calls `slice.execute()` with no arguments, so command arguments must live on `self`.
- **`@event` handlers receive only the fields they declare.** The library inspects the handler's signature and passes only the matching kwargs — so `def _(self): ...` and `def _(self, order_id: str): ...` are both valid on the same event type. Declare only the fields the handler actually needs.
- **Selector tags ⊆ trigger tags.** Every tag a `consistency_boundary()` selector asks for must be present on the tags the emitting slice passes to `trigger_event(..., tags=...)`. If the selector's tags aren't a subset, the replay misses the event and the next command (or view) sees a history in which it never happened.
- **`Selector(types=[], tags=[])` is not "no boundary" — it is "fail if *any* event exists anywhere."** An empty selector is a DCB append condition over the whole store, so two writes for unrelated entities in the same test collide with `IntegrityError`. Always scope `types`/`tags` to the entity, including in test-only emitting slices.
- **`repository.save(slice_)` returns the `int` append position** — the same value `TrackingRecorder.wait` polls via `max_tracking_id`. Don't try to read it off `slice_.new_decisions`; `collect_events()` drains that list during `save()`.

### Application wiring

- **One `{ProjectName}App` per process**, created by the FastAPI lifespan in `src/snake_case({ProjectName})/main.py`; slices never subclass `DcbApplication`. Separate `DcbApplication()` instances each get their own in-memory store (`PERSISTENCE_MODULE` defaults to `eventsourcing.dcb.popo`), so per-slice applications silently cannot see each other's events — an event written through one endpoint would be invisible to the next.
- **The lifespan *yields* `{"dcb_app": app}`; `get_application` reads `request.state.dcb_app`.** Starlette merges the yielded mapping into the lifespan scope state and shallow-copies it into every request scope, so handlers can't clobber it. Do not assign to `app.state` — it is a different object that never receives lifespan state.
- **`DcbApplication` is a context manager.** Hold it with `with`, never `lru_cache` — the latter has no teardown hook, so `close()` (and, under Postgres, the connection-pool teardown) never runs.
- **Adding a slice touches `main.py` only** — one router import plus one `include_router` line, appended to the block. Registration order is load-bearing when a literal segment could be matched by another route's path parameter; see *API addressing* → *Never let a literal segment sit where a path parameter could match it*. `application.py` is never edited *by a slice*, because `do()` is generic over any `Slice`. (The one process-wide `do()` override is written once and is not a per-slice edit — see *Command outcomes* and *Observability*.)
- **Materialized views and automations use the shared application too.** They are *not* exempt. `ProjectionRunner` takes an application *class* and constructs its own instance, so it is unusable here — see *Projection runners* below for what to use instead.

### API addressing

**The URL names the business intention, never the slice.** A slice name is a build artefact; putting it in the path (`POST /admin-cancel-license/`, `GET /view-dog-profile/{id}`) publishes the internal structure as the public contract and frames every command as data editing. The address should point at what the caller wants to achieve, and a query's address at the situation it describes.

**The entity segment comes from the consistency boundary.** The slice already names the entity and its id when it picks `_tags()` — reuse that decision rather than making a second one:

```
tags = [f"licence:{licence_id}"]   ->   /licences/{licence_id}/...
```

That is what makes an address checkable: a route whose entity segment disagrees with the slice's boundary tag is a bug, not a style preference.

**A command's URL carries the domain's own verb; a view's carries a noun.** That asymmetry is deliberate. `POST` says only "something happened here"; the action has to be named somewhere, and the domain already named it on the board (`Subscribe Student`, `Change Course Capacity`). Nominalising that back into a noun-phrase resource (`/subscriptions`, `/capacity-changes`) invents a collection nobody models, and re-frames a decision the business makes as a row a client inserts. `GET`, by contrast, *is* the verb — a view path that repeats it (`/list-catalogue`) says the same thing twice.

| Case | Path |
|------|------|
| Command on an existing entity | `POST /{entities}/{entity_id}/{action}` — `POST /licences/{licence_id}/cancel` |
| Command that creates the entity | `POST /{entities}/{action}` — `POST /users/register` |
| Global boundary (`tags=[]`) | `POST /{entities}/{action}` on the command's subject; if no subject can be named, `POST /{action}` at root — justify it in the docstring, same rule as the empty selector |
| Two-entity boundary (rare) | Nest under the entity the command *mutates*; the other stays in the body |
| Single-entity view | `GET /{entities}/{entity_id}/{situation}` — `GET /dogs/{dog_id}/profile` |
| Collection / search view | `GET /{situation-plural}?params` — `GET /available-stays?from=…&to=…` |
| Inbound webhook (external event) | `POST /webhooks/{external-event}` — `POST /webhooks/student-registered` |

- **`{entities}`** — the boundary tag's kind, pluralised, kebab-case.
- **`{action}`** — the command's own name with the noun the path already carries **dropped**, the verb kept, and the command's *object* appended when it has one. The entity is in the path; repeating it is noise.
  ```
  AdminCancelLicence  + /licences/{licence_id}/  ->  cancel
  ChangeCourseCapacity + /courses/{course_id}/   ->  change-capacity
  SubscribeStudent     + /students/{student_id}/ ->  subscribe-to-course   (object: course)
  RegisterCourse       + /courses/              ->  register
  ```
  Keep a qualifier when dropping it would collide: if both `AdminCancelLicence` and `CustomerCancelLicence` exist, they are `admin-cancel` and `customer-cancel`, not two routes called `cancel`. Imperative verb, never a gerund or a noun — `cancel`, not `cancelling` or `cancellation`.
- **A creating command keeps its id in the body.** `POST /courses/register` carries `course_id` as body data even when the client supplies it, because an id for a thing that does not exist yet is not an address. This is the one case where the id is *not* lifted to a path parameter.
- **`{situation}`** — what the reader is looking at, not what the projection is called: `profile`, `itinerary`, `upcoming-arrivals`, `cancellation-context`. `ViewDogProfile` is the slice; `profile` is the situation.
- **`{external-event}`** — the *external* event's own name, **past participle**, kebab-case: `student-registered`, `payment-settled`. Not an imperative. Read the board before choosing it: an automation slice fed from outside has an external event upstream of the automation and a command *downstream* of it, so the thing crossing the wire is the event, and the command is ours to issue afterwards. `POST /students/register` states the opposite — that the caller is instructing us and that we may decline — when in fact the registrar has already decided and our only honest answers are "recorded" and "retry me". The endpoint's behaviour already agrees: a redelivery is absorbed, because a sender's retry is not a second registration.
- **The OpenAPI tag names the entity, never the slice** — `tags=["{entities}"]` on the router, reusing the same pluralised, kebab-case entity kind as the path's `{entities}` segment. A tag is the only grouping a reader of `/docs` or a generated client gets, so one tag per slice groups nothing: it renders as a flat list of one-endpoint sections in build order. Tagging by entity puts every command and every view over a course under `courses`, whichever slice built it.
  - Every **command** path starts with `{entities}`, so for commands the tag is exactly the first path segment — including the creating ones: `POST /courses/register` is tagged `courses`.
  - A **collection view** sits at the root, so its tag is *not* its first segment. Take what the collection is **of**: `GET /course-catalogue` is tagged `courses`.
  - A genuinely global boundary (`tags=[]`) still has a subject; tag it with that. If no entity can be named, the slice is probably mis-scoped.
  - **An inbound webhook is tagged `webhooks`, not by its entity** — the one deliberate exception. `/webhooks/` is an operational boundary rather than a naming one: these are the paths an external retry loop hammers, that need signature verification and at-least-once tolerance, and that no first-party client should ever call. Filing `student-registered` under `students` puts it in the group a student-facing client browses and implies it is theirs to call.
- **The slice name still has to be traceable, so it lives in `operation_id`** — an explicit `operation_id="snake_case({SliceName})"` on the route. That, not the tag, is what links an endpoint back to the slice that built it in the generated spec.
- **The full path goes on the decorator; the router carries no `prefix`.** One greppable path string per slice, no path parameters hidden in a prefix, and no trailing-slash wart.
- **The entity id is a path parameter, not a body field**, whenever the command is nested under an *existing* entity. Drop it from the request model and pass it alongside: `{SliceName}Slice(licence_id=licence_id, **body.model_dump())`. A creating command is the exception noted above.
- **Operational routes are exempt from the entity scheme, but not from tagging.** `/livez`, `/readyz` and anything like them are infrastructure, not domain — leave the path flat and tag them `infrastructure`, so `/docs` and generated clients group them apart from the domain surface rather than scattering them untagged.

**Never let a literal segment sit where a path parameter could match it.** Starlette walks the route table in registration order and serves the **first full match**, so `GET /courses/{course_id}` registered before `GET /courses/catalogue` swallows every catalogue request and answers it from the *detail* handler with `course_id="catalogue"` — a 404 from the wrong route, with no warning at import time and no error in the log. Nothing in this kit enforces an ordering, and `include_router` lines are appended per slice, so the safe order today is an accident of build order rather than a property anyone maintains.

- **Prefer addresses that cannot collide over an ordering you have to remember.** This is why a collection view keeps a root-level address — `GET /course-catalogue`, never `GET /courses/catalogue`. It leaves `/courses/{course_id}` free forever, and costs nothing.
- The command scheme is already safe by construction: a command path always ends in `{action}`, a literal, and never bottoms out at a bare `POST /{entities}/{entity_id}`. `POST /courses/register` and `POST /courses/{course_id}/change-capacity` differ in segment count and cannot shadow each other.
- If a literal and a parameterised sibling ever genuinely must coexist, register the **literal first** and leave a comment at both `include_router` lines saying the order is load-bearing. Prefer redesigning the address instead.
- **`grep` the committed spec before choosing a path** — see *The OpenAPI spec is the source of truth*. A shadowed route is invisible there (both paths appear, correctly), so the spec catches duplicates but **not** shadowing. That check is yours to make.

### The OpenAPI spec is the source of truth

Addresses now take per-slice judgement, so nothing guarantees a rebuild lands on the same URL. `docs/openapi.json` is the record that closes that gap: it is generated from the real `create_app()`, committed, and regenerated by the `hatch-docs-openapi` pre-commit hook on every change. A renamed or colliding endpoint shows up as a diff in a file under review rather than as a silent break.

- **Read it before choosing a path.** `grep` the spec for the path you intend to use; if it is taken, the slice needs a different action verb (or you have mis-identified the entity). Check for *shadowing* too, not just exact duplicates: a new parameterised path that could match an existing literal one is a break the spec cannot show you.
- **Never hand-edit it.** Change the route, run `hatch run docs:openapi`, stage the result.
- It does not exist until the first slice with a route is built — that is expected, not a setup step you missed.

### Command outcomes

A command route answers with the ids of the events it recorded and the position they were appended at. Neither survives the library's own `do()`: `repository.save()` returns the `int` position and `do()` drops it, while `save()` internally calls `collect_events()`, which drains `new_decisions`. Both must be captured *between* `execute()` and `save()`.

- **`src/snake_case({ProjectName})/command.py` is shared runtime, written once at *First-time project setup*** — never per-slice, and created before the slice if a project turns out to be missing it. Same rule as `telemetry.py` and `projection.py`. It holds `CommandOutcome` (a `NamedTuple` of `event_ids: tuple[UUID, ...]` and `position: int | None`), `CommandSlice`, and the `CommandResponse` body model.
- **`CommandSlice` is the base class for every state-change slice**; on-demand view slices stay on plain `Slice`. This is the only thing distinguishing the two kinds — the library gives them the same base — and it is what lets `do()` tell a command apart from a view replay.
- **`CommandSlice.outcome` defaults to an empty outcome, not `None`.** A route then reads `outcome.position` without narrowing an optional first. It is a class attribute shared by every subclass, which is why both fields are immutable and `event_ids` is a tuple.
- **The outcome rides on the slice, so `do()` keeps its `-> TSlice` signature.** Returning a union would break the base class contract and every on-demand view route, which depend on `do()` handing the perspective back.
- **`position` is the append position — the last event of the batch.** It is the value `TrackingRecorder.wait(context_name, notification_id, timeout)` polls, which is what makes read-your-writes possible for a caller.
- **A successful command answers 201, never 200.** Every command that succeeds appends events to the log, so the response *is* a creation — the verb in the slice name is beside the point (`UnsubscribeStudent` creates a `StudentUnsubscribed` event just as `RegisterCourse` creates a `CourseRegistered` one). Uniform status means a client never has to know which command it called to know what success looks like.
- **Nothing recorded means HTTP 204.** `do()` skips `save()` when `new_decisions` is empty, so there is no position to report. Unreachable through the API while every slice either emits or raises — cover it with a unit test that drives a silent `CommandSlice` directly.
- **An inbound webhook answers 202, not 201 — the one exception, and it is about who did the work.** A webhook route records the *external* event; the command the domain actually runs is issued later by the automation that follows it, so at the moment the response goes out the thing the sender named has not happened yet. 201 would claim it had. 202 says "recorded, and I will act on it", which is both true and what a webhook sender expects. The 204 branch is unchanged, and so is the body — the caller still gets a `position` and still polls the view with `X-Position-AtLeast`, which is the only way it can find out when the automation has caught up. Do not generalise this to ordinary command routes: their work *is* done when they answer.

### View positions

A command answers with the position its events landed at; a view answers with the position it *reflects*. Together those two numbers are what make read-your-writes checkable: a caller keeps the `position` from its 201 and polls the view until the view has reached it. Without this, a client that has just written cannot tell a stale read from a settled one, and — for a materialized view — cannot tell staleness from the entity genuinely not existing, because both read as 404.

The polling is the **client's** job, on whatever interval it likes. The server answers immediately, every time.

**Two position sources, both already provided by the library.** Neither needs new plumbing on the slice:

| View kind | Current position |
|---|---|
| On-demand (`Slice` replay) | `view.last_known_position`, set by `repository.advance()` inside `do()` |
| Materialized (`TrackingRecorder`) | `materialized_position(view)` — i.e. `view.max_tracking_id(context_name)` |

- **`last_known_position` is the store head, not the boundary's last event.** `advance()` assigns it `read_response.head`, and with no `limit` both backends compute that as the global maximum position (`InMemoryDcbRecorder` returns `self.events[-1].position`; `postgres_tt` runs `SELECT MAX(id)`). That is what puts it on the same scale as a command's `position` — and it means **an on-demand view is caught up by construction**, so its check always passes. Keep the check anyway: a client must not have to know which kind of view it is talking to in order to poll it.
- **Both are `int | None`, and `None` is not `0`.** For an on-demand view it means the store is empty; for a materialized one, that the projection has processed nothing yet. Undefined is not "at the beginning", so **omit `X-Current-Position` entirely** rather than sending a placeholder.
- **The precondition is checked *before* the 404.** Reversed, a caller polling for the entity it just created gets 404 on the first attempt and stops. Staleness outranks absence.
- **Not every view has a 404 to order against.** A *collection* view's absence is an empty collection at 200 — `/course-catalogue` over an empty store is a correct, complete answer, not a missing resource. Only a **single-entity** view has a genuine absence case, and only it raises the 404. Do not invent one to satisfy a template: an empty list that 404s tells a client the address is wrong when it is not. `VIEW_RESPONSES` therefore documents only 200 and 425; a single-entity route adds `responses={**VIEW_RESPONSES, status.HTTP_404_NOT_FOUND: NOT_FOUND_RESPONSE}`.
- **Every documented response declares the header, not just the 200.** `X-Current-Position` on the 425 is the whole reason a client can size its next poll, and on the 404 it is how a poller distinguishes "not there yet" from "not there" — but a header the route sends and the spec omits is invisible to every generated client. That is why `_POSITION_HEADER_SPEC` is declared once and shared by all three entries rather than inlined into the 200.
- **Never block in a route.** Do not reach for `view.wait()` here: it sleeps the *calling* thread, which in an `async def` route is the event loop, and even handed to a threadpool it pins one worker per waiting client. `wait()` is a test and lifespan tool. The route reports and returns.
- **Views send `Cache-Control: no-store`**, which `view_headers()` does for you. This is load-bearing rather than tidiness: the request header changes the *status* without changing the body, so it does not fragment a shared cache key the way a query parameter would. A cache that stored the 200 could otherwise replay it to a caller whose `X-Position-AtLeast` was never met.
- **The precondition travels in a header, not the address.** `X-Position-AtLeast` does not select a different resource — the representation is identical whatever the caller asks for; only the server's willingness to answer changes. That is a conditional request, in the same family as `If-None-Match`. The *API addressing* rules above are therefore untouched by this.
- **Route handlers still add no telemetry.** Unchanged: the HTTP span comes from `FastAPIInstrumentor` and the command span from `do()`.

### Projection runners

Runners are **threads in the API process**. This is not a design choice: the library ships no multiprocessing runner, and POPO subscriptions are strictly process-local (`POPOApplicationRecorder` keeps listeners as `threading.Event` objects in process memory). Cross-process subscription is possible only on Postgres via `LISTEN`/`NOTIFY`, and nothing implements it.

- **Never let a runner construct its own application.** `ProjectionRunner` calls `application_class(env=env)` internally, giving it a private store; under POPO that store is invisible to the routes writing through `{ProjectName}App`, so the view never updates and an automation never sees its trigger. Use `BaseProjectionRunner`, which takes an already-constructed `app`.
- **`BaseProjectionRunner.__exit__` unconditionally calls `self.app.close()`,** with no flag or hook to prevent it. `SharedAppProjectionRunner` in `src/snake_case({ProjectName})/projection.py` overrides `__exit__` wholesale to drop that one call. Under POPO `close()` is a no-op so the bug is invisible; on Postgres it closes a connection pool that **cannot be reopened** (`PoolClosed` on every later request).
- **That override reaches into four private attributes** (`_stop_thread`, `_subscription`, `_processing_thread`, `_thread_error`) of an alpha library. It lives in exactly one hand-written module so a version bump has one place to audit.
- **Write the upgrade tripwire in the same step as `projection.py`** — `tests/unit/test_projection.py`, asserting that exiting a runner does **not** close the shared application. Assert on the `close()` call itself (wrap `app.close` and check it was never invoked), not merely that the app still works: under POPO `close()` is a no-op, so a reintroduced close passes every behavioural check and only surfaces on Postgres, where the pool cannot reopen. A tripwire you have not seen fail is not a tripwire — add `self.app.close()` to the override, watch the test fail, then take it out again.
- **Constructing a runner starts it.** `__init__` subscribes at `gt=tracking_recorder.max_tracking_id(...)` and starts both threads; `__enter__` only enters the subscription. There is no deferred start.
- **A view owns a *second* connection pool, and nothing in the library closes it.** `create_view()` resolves its own `InfrastructureFactory` from the projection-scoped environment, so under Postgres it builds a `PostgresDatastore` entirely separate from the one `{ProjectName}App` holds. Give the abstract `{SliceName}View` a concrete no-op `close()` — not an abstract one, since the lifespan calls it without knowing which backend it built — override it on the Postgres implementation with `self.datastore.close()`, and register it in the lifespan as `stack.callback(view.close)` **before** `stack.enter_context(supervisor)`, so it runs after the supervisor has stopped every thread still writing through that pool. Under POPO the whole thing is a no-op, which is precisely why the leak survives every test suite.
- **The view is the stable identity, not the runner.** Restarts replace the runner but keep the view, so the view is what goes in lifespan state, what routes depend on, and what tests `wait()` on. Build it once with `create_view()`; `create_runner(app, view)` may be called repeatedly.
- **Lifespan ownership**: the application is entered **first** so it closes **last**; the supervisor is entered after it and torn down before it. Both are *sync* context managers — use `AsyncExitStack.enter_context`, not `enter_async_context`.

### Supervising projections

One unhandled exception in `process_event` **permanently kills** the processing thread — no retry, no restart — and the error stays hidden in `_thread_error` until the next `wait()`/`run_forever()`/`__exit__`. Left alone, that is a silently stale view.

- **One process-wide `ProjectionSupervisor`** owns a single watchdog thread over every registered projection. It probes with `run_forever(timeout=0)` (non-blocking; re-raises the stored error) and rebuilds dead runners via the registered factory.
- **A restart resumes where the dead runner stopped.** Tracking is committed atomically with each view mutation, so a fresh runner over the same view subscribes at `max_tracking_id` — no loss, no replay from zero.
- **Count restarts by position, not by count.** If `max_tracking_id` advanced since the last death the projection made progress, so reset the counter; unchanged means the same poison event, so increment. Without this, unrelated transient faults accumulate and eventually stop a healthy projection for good.
- **Past `max_restarts`, stop and report.** The runner stays dead and surfaces in `supervisor.failures()`, which both `/livez` and `/readyz` turn into a 503. A view frozen at a known position is honest; one that skipped an event is silently wrong forever.
- **Do not wrap `process_event` in a blanket `try/except`.** Swallowing an event and advancing past it diverges the view from the log permanently while health checks still report 200. Automations keep their targeted guard around the *command port* (`_fire`), where the lingering ledger entry is itself the signal — which only holds while entries linger for one reason, so `_fire` discards the entry when the command's own idempotency guard says the work already landed.
- **The watchdog is a thread, not an asyncio task,** because tearing a runner down makes two unbounded `Thread.join()` calls and may block on a dead database socket. On the event loop that would stall every request; on a thread it degrades one projection.
- **Health routes report; they never restart.** Recovery is the supervisor's job, so health checks stay free of side effects. Expose the watchdog's own liveness as `is_watching()` alongside `failures()` — a supervisor that has stopped watching is invisible to `failures()` by construction.

#### The `/livez` and `/readyz` routes

**The slice that registers the first supervisor also adds these routes** — whichever slice type it happens to be. A supervisor with no health surface is the failure it exists to prevent: past `max_restarts` the runner stays dead, and without this the process answers every request happily while the view sits frozen. If an earlier slice already added them, leave them exactly as they are.

**They are two routes, not one, because an orchestrator asks two questions with two different remedies.** A single probe answers both and so answers neither actionably.

| | `/livez` | `/readyz` |
|---|---|---|
| Event store unreachable | 200 | 503 — `event_store` |
| A projection's own store unreachable | 200 | 503 — `view:<name>` |
| Terminal projection failure (`supervisor.failures()`) | 503 | 503 — `<name>` |
| Watchdog thread dead | 503 | 200 |

- **`/livez` — "restart me."** Only unrecoverable in-process state: a runner past `max_restarts` (a fresh process rebuilds it from `max_tracking_id`, so a restart *is* the recovery), or a watchdog thread that has died (nothing will restart a projection from here on, and `failures()` stays empty through the rot because nothing is left to observe a death). It never touches any store.
- **`/readyz` — "drain me."** Whether this instance can serve correct traffic right now: the same terminal failures, plus a live round trip to the event store *and to every registered projection's store*. Restarting a replica does not bring a store back, and a restart loop across every replica during an outage is strictly worse than every replica sitting unready.
- **What makes `/livez`'s store-blindness honest is the remedy, not a startup handshake.** Do **not** add a reachability probe to the lifespan: a backend that cannot open its store already raises out of `{ProjectName}App()`, so a misconfigured URI fails startup without one, and the only thing a retry loop there buys is a boot-ordering window a container restart already provides. Liveness covers in-process rot a restart fixes; a dependency outage is not that, whenever it starts.
- **Probe every registered projection, not a named backend.** Each projection resolves its own infrastructure from a name-scoped environment, so each may hold a connection of its own — and two projections against the same database still get separate pools. Iterate `supervisor.tracking_recorders()` and call `materialized_position(recorder)` on each, deduplicating by `id(getattr(recorder, "datastore", recorder))` so recorders sharing a datastore are one round trip. A new projection is then covered by registering it, with no edit here.
- **Collect every failure into one 503 body rather than short-circuiting.** An operator reading it during an outage wants the whole list; naming one dependency and stopping reads as "only this one broke". Namespace the projection keys (`view:<name>`) so they cannot collide with the bare names `failures()` uses.
- **`head()` and `max_tracking_id()` returning `None` is success.** It means the store is reachable and has nothing yet. Only an exception means unreachable — the same `None`-guarding trap as in *Instrumentation*.
- **Close the response model's `status` field** — `Literal["alive", "ready"]`, not `str`. An open string publishes no contract, so a typo in a return value breaks every generated client with nothing to catch it.
- **Projection lag is not a readiness condition.** Keep it an observable gauge (below); per-request staleness is already the read side's job via `X-Current-Position` / `X-Position-AtLeast` → 425. A lag threshold is a policy guess that flaps under write bursts and can pull every replica at once during a backlog.

Unlike a single `/healthz`, these no longer fit as closures in `create_app()`: give them their own `src/snake_case({ProjectName})/health.py` with a router, `include_router`'d last, exactly like a slice's.

```python
router = APIRouter(tags=["infrastructure"])


def get_supervisor(request: Request) -> ProjectionSupervisor:
    """Return the process-wide projection supervisor from FastAPI request state."""
    return request.state.projection_supervisor


@router.get("/livez", response_model=HealthResponse, operation_id="livez")
async def livez(
    supervisor: Annotated[ProjectionSupervisor, Depends(get_supervisor)],
) -> HealthResponse:
    """Report whether this process can still recover on its own."""
    # ... failures() -> 503, then is_watching() -> 503 ...


@router.get("/readyz", response_model=HealthResponse, operation_id="readyz")
def readyz(  # deliberately sync — see below
    app: Annotated[{ProjectName}App, Depends(get_application)],
    supervisor: Annotated[ProjectionSupervisor, Depends(get_supervisor)],
) -> HealthResponse:
    """Report whether this instance can serve correct traffic right now."""
    # ... failures() -> 503, then head() and every registered projection's
    # tracking recorder, each guarded, collected into one 503 ...
```

`request.state.projection_supervisor` is the key the lifespan yields — the route reads the supervisor, never a runner and never a view. `Request`, and any type used in a live `Annotated` parameter annotation, must be imported at runtime rather than under `TYPE_CHECKING`, or FastAPI mistakes `Request` for a query parameter and the route 422s; see *FastAPI / Pydantic gotchas*. Being operational routes they keep flat paths and are exempt from the addressing convention in *API addressing*.

**`readyz` is `def`, not `async def`, on purpose.** Every probe it makes is blocking socket I/O, and on the event loop a hung socket would stall every request in the process — the same reasoning that makes the watchdog a thread rather than a task. FastAPI runs a sync handler in the threadpool, so a hung probe degrades one worker instead. This matters more than it looks: a Postgres tracking read retries internally before giving up, so an unreachable projection store makes this route *slow*, not instantaneous — size the container/pod probe timeout above that, or a clean 503 naming the dependency is replaced by an opaque timeout naming nothing. `livez` stays `async def`; it only reads in-memory state.

Register each view's lag as an observable gauge alongside it. That is the metric distinguishing "healthy" from "running but hopelessly behind", which `failures()` alone cannot tell you:

```python
head = app.recorder.head()
tracked = view.max_tracking_id(app.context_name)
if head is not None and tracked is not None:
    yield Observation(head - tracked, {"projection": "snake_case({SliceName})"})
```

**Both calls return `int | None`.** Before the projection has processed anything its lag is *undefined*, not zero — skip the observation rather than reporting a fake backlog the moment the process starts.

**Cover the 503 in `tests/integration/`.** A health route that has only ever been observed returning 200 is not known to work; this is the one path that matters, and it is unreachable through any other endpoint.

It is also unreachable through the app's *own* supervisor, and that is by design rather than an oversight: an automation guards its command port (`_fire`) precisely so a poison command never kills the processing thread, so no request sequence can drive the real wiring terminal. Don't try to defeat that guard. Instead:

1. Build a **second, entirely real** `ProjectionSupervisor` over its own application — real supervisor, real `SharedAppProjectionRunner`, real watchdog thread, real restart-counting. Nothing is mocked except the projection itself, whose `process_event` raises unconditionally.
2. Register it with `max_restarts=0` and `poll_interval=0.1`, so one genuine exception is enough to give up for good and the watchdog notices in a tenth of a second.
3. Append one triggering event, then wait for the watchdog. The poisoned view never inserts tracking, so it never advances — `pytest.raises(TimeoutError)` around `view.wait(..., notification_id=1, timeout=2)` is a blocking, non-busy way to hand it wall-clock time. Assert on `supervisor.failures()` before touching the route, so a failure here is distinguishable from a routing bug.
4. Substitute it for the app's own via `client.app_state["projection_supervisor"] = supervisor` — `app_state` is the same mutable dict the lifespan populates and the `client` fixture already reads, so the route under test is the genuine one from `main.py`, reading `request.state.projection_supervisor` exactly as in production.

Two things to be plain about in the test's own docstring: the 503 path never flows through the supervisor `main.py` constructs, and step 3 costs the full timeout (~2s) on every green run, because the timeout *is* the synchronisation.

### Projections (materialized views)

- **`Projection.topics` is a tuple of topic strings** (`get_topic(EventClass)`), not `Selector` instances. It filters what the subscription pulls before `process_event` is called.
- **`match` on `envelope.decision`, not `envelope`.** `envelope` is a `TaggedEvent[Decision]`; the payload is `envelope.decision`. Keep a `case _:` wildcard even though `topics` already filters — `match` is exhaustive-by-branch, not exhaustive-by-topics.
- **Every `process_event` branch must persist the tracking position** via `add_entry(..., tracking)` or `view.insert_tracking(tracking)`, whether or not the view changed. `wait()` polls `max_tracking_id`, which only advances when something records that `Tracking`; a branch that forgets it makes every later `wait()` hang until timeout.
- **Tracking uniqueness is enforced by the recorder**, not the projection — reusing a `Tracking` notification id raises `eventsourcing.persistence.IntegrityError` from `_assert_tracking_uniqueness`. Use strictly increasing ids across events in a test.
- **Set `Projection.name` explicitly.** It picks prefixed env vars and (for database-backed recorders) table names; the `__init_subclass__` default is the class's own `__name__`.

### Testing DCB code

- GWT test helpers: `given(*TaggedEvent).when(slice_instance).then(*TaggedEvent)`. `then` compares `TaggedEvent` instances, not classes.
- **`then(*expected)` only compares `.decision` and `.tags`** — it deliberately ignores the auto-generated `.uuid` field on each `TaggedEvent`. A raw `assert when.collected == [...]` does full dataclass equality *including* `.uuid` and spuriously fails even when decision/tags match. Always assert via `.then(...)`, not raw equality. If a test needs a field the slice generates itself (e.g. a timestamp), read it off `when.collected[0].decision.<field>` and feed it into the expected event.
- **`given`/`when` only drive `Slice` objects** — they dispatch through `@event`-decorated handlers on the object passed to `when()`. A `Projection` is driven by `process_event` instead, so test it by constructing the view and projection directly and calling `process_event(envelope, Tracking(context_name, notification_id))` yourself. No runner, no background thread.
- **Seed integration histories with `app.events.append(events=[...])`, not by driving other slices.** Passing raw `TaggedEvent`s keeps a slice's tests independent of other slices' validation rules, and permits histories a slice would legitimately refuse to emit. Omit `cb`/`after` to get an unconditional append (`DcbEventStore.append` builds a `DcbAppendCondition` only when one is given). Tags must still satisfy Selector tags ⊆ trigger tags, or the seeded events are silently invisible to the slice under test.
- **This applies to runner-driven suites too** (materialized views, automations), where it replaces the older test-only emitting `Slice`. Seed through the **shared `{ProjectName}App`** the runner subscribes to — not through `runner.app`, which only worked when runners owned a private store. Two further gains over a `Slice`-based seed: `append()` returns the `int` position to pass straight to `wait()`, and because the append is unconditional, repeated seeds into the same entity's tags all succeed — no `repository.advance()` replay, no `IntegrityError`, and no `execute()`-before-`save()` step.
- **Wait on the *view*, not the runner.** `TrackingRecorder.wait(context_name, notification_id, timeout)` takes an optional `interrupt`, so a test needs no runner reference: `view.wait(context_name=app.context_name, notification_id=position, timeout=5)`. This matches production, where the supervisor keeps runners private and only views reach routes. It raises `TimeoutError` on expiry. Automations still wait on `position + 1` to cover the command's own emitted event.
- **`TaggedEvent.metadata` and `.uuid` round-trip through the store**, so a seed can set `metadata={"correlation_id": ...}` and the projection reads it back off the envelope. Returning the seeded `TaggedEvent` (not just its `Decision`) is what lets a test assert an emitted event's `causation_id == str(seed.uuid)`.
- **Never write into a live runner's view from the test thread.** Calling `view.add_entry(entry, Tracking(app.context_name, position))` while a runner is subscribed races the subscription, which processes the same event and inserts the same `Tracking` — whichever write loses trips `IntegrityError` on the background thread, surfacing later out of `wait()` rather than at the call site. To simulate a crash, consume the position *before* any runner exists, then start one; that is also the faithful ordering, and why `drain()` runs before the runner is constructed.
- **`app.events.read()` returns an iterator of `TaggedEvent`s**, not an object with an `.envelopes` attribute. Iterate it directly to assert on recorded events.
- **Each seeded fact is a fixture, declared in the test signature** — not a helper called from the test body. Fixtures compose (a richer history depends on a simpler one), ids get their own fixtures so arrangement and request body share one value, and each returns its `Decision` so the test can assert against what it arranged. This also makes ordering structural: pytest resolves the graph before the body runs.
- **`repository.save()` is not a seeding API** — it takes a `Perspective` and derives an append condition from its `consistency_boundary()`/`last_known_position`. `app.events` is the `DcbEventStore`; that is the seeding primitive.
- **Reach the app under test via `client.app_state["dcb_app"]`**, not `client.app.state` — the latter is a `State()` built in `Starlette.__init__` that never receives lifespan state and raises `AttributeError`.
- **GWT refuses histories outside the consistency boundary.** Prior events on `given()` must carry tags overlapping the slice's `consistency_boundary()`, or `when()` raises `AssertionError("Consistency boundary wouldn't have selected: ...")`. This is deliberate — but it means cross-entity isolation ("another entity's events don't leak into this one") can't be proven at the acceptance level. That property belongs in the integration suite.

## Regenerating lock files

`requirements.txt` and `requirements/requirements-<env>.txt` are hatch-pip-compile
lock files, and they are committed. **Never** regenerate them by creating or syncing a
single environment (`hatch env create dev` and friends) — the envs share a
`pip-compile-constraint = "dev"`, so a partial regeneration leaves the rest of the lock
files pinned against a stale constraint hash. After **any** change to `dependencies`,
`[project.optional-dependencies]`, `[dependency-groups]`, or an env's `features` /
`extra-dependencies`, regenerate all of them together:

1. Remove the existing lock files:
   ```
   rm -rf requirements*
   ```
2. Remove all existing hatch environments:
   ```
   hatch env prune
   ```
3. Recreate every environment, which regenerates the lock files:
   ```
   hatch env show --json \
     | jq -r 'keys[] | select(startswith("hatch-") | not)' \
     | xargs -I{} sh -c 'hatch env create "{}"'
   ```

Commit the regenerated lock files alongside the `pyproject.toml` change that caused them.
