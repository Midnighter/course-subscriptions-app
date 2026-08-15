# Event metadata reference implementation

The working source for `src/snake_case({ProjectName})/metadata.py`, its wiring, and its
tests. Copy from here rather than deriving it from prose — the *why* lives in
`.build-kit/CLAUDE.md` → *Event metadata* and is deliberately not repeated below.

Read this when:

- **First-time project setup** creates the shared runtime (`.build-kit/CLAUDE.md` →
  *First-time project setup*), or
- a build skill's **Step 0** finds `metadata.py` missing in an existing project.

`metadata.py` is shared runtime, written **once** per project. If it already exists,
import from it and change nothing. It imports nothing of the project's own, so it can be
created before any slice exists — but it must exist before `application.py` (which
imports `command_metadata`) and `main.py` (which imports `MetadataMiddleware`).

### Placeholders

Every placeholder used below, so you do not have to open a build skill to resolve one.
All are **PascalCase**; the `snake_case(...)` form is derived from them at
code-generation time, never carried separately.

| Placeholder | Derived from | Example |
|---|---|---|
| `{ProjectName}` | `[project] name` in `pyproject.toml`, PascalCase | `CourseSubscriptions` |
| `snake_case({ProjectName})` | the single top-level package under `src/` — **confirm it on disk** rather than deriving a name that is not there | `course_subscriptions` |
| `{ProjectAuthor}` | `name` from `[project] authors` — copy the exact spelling already used by existing modules | `Moritz E. Beber` |
| `{YYYY}` | the current year, matching the headers on existing modules | `2026` |
| `{SliceName}` | the slice title in PascalCase — only used by the automation snippet in §2 | `RegisterStudent` |
| `{GetPath}` | any GET route the app already serves; `/healthz` once a supervisor exists | `/healthz` |
| `{CommandPath}` | any POST route that runs a command through `do()` | `/students/register` |
| `{CommandBody}` | a valid request body for that route | `{"student_id": "STU-2026-0042", "name": "Anna Müller", "course_limit": 2}` |

`{GetPath}`, `{CommandPath}`, and `{CommandBody}` only appear in the integration tests in
§4. They need a route that already exists — so in a project whose first slice is not yet
built, write §1–§3 now and add §4 with the first route.

Every file below still needs the copyright header and docstrings the pre-commit hooks
enforce — they are present in these templates, unlike the abbreviated ones in the build
skills.

---

## 1. `src/snake_case({ProjectName})/metadata.py`

```python
# Copyright {YYYY} {ProjectAuthor}
"""Provide the general-purpose metadata that every recorded event carries."""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from eventsourcing.domain import get_metadata_from_context, put_metadata_in_context
from starlette.datastructures import Headers, MutableHeaders

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_ID_KEY = "correlation_id"
CAUSATION_ID_KEY = "causation_id"
CREATED_AT_KEY = "created_at"

CORRELATION_ID_HEADER = "X-Correlation-ID"

# A correlation id is stored in a `jsonb` column and echoed into logs and a
# response header, so a client-supplied one has to be bounded and free of
# control characters before it is trusted with any of that.
_CORRELATION_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")


def new_correlation_id() -> str:
    """Mint an identifier for a flow that has none yet."""
    return str(uuid4())


def sanitise_correlation_id(raw: str | None) -> str:
    """
    Return the caller's correlation id when it is usable, else a fresh one.

    Rejecting rather than truncating keeps the invariant simple: every stored
    `correlation_id` is either exactly what the client sent or one we minted,
    never a mangled prefix of the two.

    Args:
        raw: The inbound header value, or None when the client sent none.

    Returns:
        A correlation id safe to store, log, and echo.

    """
    if raw is not None and _CORRELATION_ID_PATTERN.fullmatch(raw):
        return raw
    return new_correlation_id()


def created_at() -> str:
    """
    Return the current UTC time, ISO 8601 formatted.

    Deliberately not the library's `datetime_now_with_tzinfo()`: that honours
    `TZINFO_TOPIC`, and the timestamp on a permanent log record should not be
    reconfigurable by an environment variable set for unrelated reasons.

    Returns:
        The current time in UTC, as an ISO 8601 string.

    """
    return datetime.now(tz=UTC).isoformat()


@contextmanager
def command_metadata() -> Iterator[None]:
    """
    Seed the metadata every event recorded inside the block inherits.

    `created_at` is stamped on every call, since it describes *this* unit of
    work. `correlation_id` is seeded only when absent, so the id put in context
    by `MetadataMiddleware` — or inherited by an automation from its triggering
    event — survives untouched. `causation_id` is never seeded here: a command
    reaching this point directly has no causing event, and an automation has
    already supplied one.

    Yields:
        None, for the duration of the seeded metadata.

    """
    metadata = {CREATED_AT_KEY: created_at()}
    if CORRELATION_ID_KEY not in get_metadata_from_context():
        metadata[CORRELATION_ID_KEY] = new_correlation_id()
    with put_metadata_in_context(metadata):
        yield


class MetadataMiddleware:
    """
    Seed one correlation id per HTTP request, and echo it back to the caller.

    Pure ASGI rather than `BaseHTTPMiddleware`, and that is not a style
    preference: `BaseHTTPMiddleware` runs the endpoint in a separate anyio
    task, so a contextvar set in its `dispatch` never reaches the route. The
    metadata would silently arrive empty.

    One request yields one correlation id, however many commands the route
    issues, which is what makes the id name the *flow* rather than the write.

    Args:
        app: The ASGI application this middleware wraps.

    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """
        Put a correlation id in context for the duration of one request.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.

        """
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        correlation_id = sanitise_correlation_id(
            Headers(scope=scope).get(CORRELATION_ID_HEADER),
        )

        async def send_with_correlation_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_ID_HEADER] = correlation_id
            await send(message)

        with put_metadata_in_context({CORRELATION_ID_KEY: correlation_id}):
            await self.app(scope, receive, send_with_correlation_id)
```

---

## 2. Wiring — two lines, written once

`metadata.py` does nothing until it is called. The whole footprint is one import and one
context manager in `application.py`, plus one import and one `add_middleware` in
`main.py`. Neither file is ever edited again for metadata, and neither is a per-slice
edit. **No slice, route, or view ever touches metadata directly** — that is what keeps
adding a future key (`source`, `actor`, tenant) a one-line change in `metadata.py`.

### `src/snake_case({ProjectName})/application.py`

```python
from snake_case({ProjectName}).metadata import command_metadata
from snake_case({ProjectName}).telemetry import command_span


class {ProjectName}App(DcbApplication):
    def do(self, s: TSlice) -> TSlice:
        with command_metadata(), command_span(s):
            ...            # the existing body, unchanged
            return s
```

`command_metadata()` goes **outermost**, so `command_span` can read the `correlation_id`
off the context and set it as a span attribute. `do()` is the single choke point every
command and replay passes through, which makes it the fallback seed for every non-HTTP
entry point: `drain()`, scripts, and the test suites.

### `src/snake_case({ProjectName})/main.py`

```python
from snake_case({ProjectName}).metadata import MetadataMiddleware


def create_app() -> FastAPI:
    configure_telemetry()
    app = FastAPI(lifespan=lifespan)
    instrument_app(app)
    app.add_middleware(MetadataMiddleware)
    ...                                       # the existing `include_router` lines
    return app
```

### Inside an automation's `Projection`

An automation is the one place that seeds `causation_id`, and it does so from the
triggering envelope's uuid — never from anything a client sent. The ids are stored on the
ledger entry rather than derived at command time, because `drain()` runs with no envelope
in hand:

```python
from snake_case({ProjectName}).metadata import CAUSATION_ID_KEY, CORRELATION_ID_KEY


def _causation_metadata(entry: {SliceName}Entry) -> dict[str, str]:
    metadata = {}
    if entry.correlation_id is not None:
        metadata[CORRELATION_ID_KEY] = entry.correlation_id
    if entry.causation_id is not None:
        metadata[CAUSATION_ID_KEY] = entry.causation_id
    return metadata


    def _fire(self, entry: {SliceName}Entry) -> None:
        try:
            with put_metadata_in_context(_causation_metadata(entry)):
                self._command(entry)
        except Exception as error:
            ...
```

Both call sites — `process_event` and `drain()` — then pass an entry and nothing else, so
a retry is recorded as the same causal step rather than a new cause. See
`.claude/skills/build-automation/SKILL.md` for the full projection.

---

## 3. `tests/unit/test_metadata.py`

```python
# Copyright {YYYY} {ProjectAuthor}
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

from snake_case({ProjectName}).metadata import (
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
```

---

## 4. `tests/integration/test_metadata.py`

These need the ASGI stack, not a unit fixture: the claim being tested is that a
contextvar set in middleware survives into the route handler, and only a real request
proves it. `client` and `dcb_app` are the shared fixtures from `tests/integration/conftest.py`.

```python
# Copyright {YYYY} {ProjectAuthor}
"""Test that HTTP ingress seeds the metadata every event inherits."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from eventsourcing.domain import get_metadata_from_context
from fastapi import FastAPI, status
from fastapi.testclient import TestClient

from snake_case({ProjectName}).metadata import (
    CORRELATION_ID_HEADER,
    CORRELATION_ID_KEY,
    CREATED_AT_KEY,
    MetadataMiddleware,
)

if TYPE_CHECKING:
    from snake_case({ProjectName}).application import {ProjectName}App


def test_response_echoes_a_minted_correlation_id(client: TestClient) -> None:
    """A request without the header still gets a flow, reported back."""
    response = client.get("{GetPath}")

    assert response.status_code == status.HTTP_200_OK
    assert UUID(response.headers[CORRELATION_ID_HEADER])


def test_response_echoes_a_supplied_correlation_id(client: TestClient) -> None:
    """A usable client id is adopted verbatim, so the caller can join on it."""
    response = client.get("{GetPath}", headers={CORRELATION_ID_HEADER: "corr-1"})

    assert response.headers[CORRELATION_ID_HEADER] == "corr-1"


def test_a_hostile_correlation_id_is_replaced(client: TestClient) -> None:
    """
    An unusable client id is replaced rather than echoed.

    `httpx` refuses to send a header containing a newline at all, so the case
    reachable over HTTP is the oversized one — still enough to prove the
    middleware sanitises rather than trusting what it is handed.
    """
    oversized = "x" * 200

    response = client.get("{GetPath}", headers={CORRELATION_ID_HEADER: oversized})

    assert response.headers[CORRELATION_ID_HEADER] != oversized
    assert UUID(response.headers[CORRELATION_ID_HEADER])


def test_the_correlation_id_reaches_the_route_handler() -> None:
    """
    The contextvar survives into the endpoint, not just the middleware.

    This is the assertion `BaseHTTPMiddleware` would fail: it runs the endpoint
    in a separate anyio task, so the metadata would arrive empty and every
    event would silently lose its flow. A standalone app keeps the claim about
    the middleware alone, with no application or projections in the way.
    """
    app = FastAPI()
    app.add_middleware(MetadataMiddleware)

    @app.get("/metadata")
    async def read_metadata() -> dict[str, str]:
        return get_metadata_from_context()

    with TestClient(app) as standalone:
        body = standalone.get(
            "/metadata",
            headers={CORRELATION_ID_HEADER: "corr-1"},
        ).json()

    assert body[CORRELATION_ID_KEY] == "corr-1"


def test_a_command_records_the_requests_correlation_id(
    client: TestClient,
    dcb_app: {ProjectName}App,
) -> None:
    """The id seeded at ingress lands on the events the route's command writes."""
    response = client.post(
        "{CommandPath}",
        json={CommandBody},
        headers={CORRELATION_ID_HEADER: "corr-1"},
    )

    assert response.status_code == status.HTTP_201_CREATED, response.text
    event_id = UUID(response.json()["event_ids"][0])
    recorded = next(env for env in dcb_app.events.read() if env.uuid == event_id)
    assert recorded.metadata[CORRELATION_ID_KEY] == "corr-1"
    assert recorded.metadata[CREATED_AT_KEY]
    # A root command has no causing *event*, and minting an id that resolves to
    # nothing would break the invariant that every causation_id names a real
    # event in our own log.
    assert "causation_id" not in recorded.metadata


def test_non_http_scopes_pass_straight_through() -> None:
    """A lifespan or websocket scope is forwarded untouched."""
    seen: list[str] = []

    async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:  # noqa: ANN401, ARG001
        seen.append(scope["type"])

    middleware = MetadataMiddleware(downstream)

    asyncio.run(middleware({"type": "lifespan"}, None, None))

    assert seen == ["lifespan"]
```
