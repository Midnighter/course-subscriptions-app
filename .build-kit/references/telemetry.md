# Telemetry reference implementation

The working source for `src/snake_case({ProjectName})/telemetry.py`, its wiring, and its
unit tests. Copy from here rather than deriving it from prose — the *why* lives in
`.build-kit/CLAUDE.md` → *Observability* and is deliberately not repeated below.

Read this when:

- **First-time project setup** creates the shared runtime (`.build-kit/CLAUDE.md` →
  *First-time project setup*), or
- a build skill's **Step 0** finds `telemetry.py` missing in an existing project.

`telemetry.py` is shared runtime, written **once** per project. If it already exists,
import from it and change nothing.

### Placeholders

Every placeholder used below, so you do not have to open a build skill to resolve one.
All are **PascalCase**; the `snake_case(...)` and `upper_snake_case(...)` forms are derived
from them at code-generation time, never carried separately.

| Placeholder | Derived from | Example |
|---|---|---|
| `{ProjectName}` | `[project] name` in `pyproject.toml`, PascalCase | `MyProject` |
| `snake_case({ProjectName})` | the single top-level package under `src/` — **confirm it on disk** rather than deriving a name that is not there | `my_project` |
| `{ProjectAuthor}` | `name` from `[project] authors` — copy the exact spelling already used by existing modules | `A. N. Author` |
| `{YYYY}` | the current year, matching the headers on existing modules | `2026` |
| `{Context}` | the bounded context in PascalCase, from the slice definition's `context` field. `snake_case({Context})` is the sub-package under the project package | `Kennel` → `src/my_project/kennel/` |
| `{SliceName}` | the slice title in PascalCase | `RegisterDog` |
| `{EventName}` | any event `Decision` in `snake_case({Context})/events.py` — the tests only need one that exists | `DogRegistered` |

The Example column is a worked illustration only — every value comes from the project you
are in, never from this table.

**When a context shares the project's name, the package path doubles up** —
`src/my_project/my_project/` for a `MyProject` context inside `MyProject`. That is
expected, not a mistake to correct: `{Context}` is resolved from the slice definition, and
the two segments mean different things even when they spell the same. Do not collapse them
to one.

Every file below still needs the copyright header and docstrings the pre-commit hooks
enforce — they are present in these templates, unlike the abbreviated ones in the build
skills.

---

## 1. `src/snake_case({ProjectName})/telemetry.py`

```python
# Copyright {YYYY} {ProjectAuthor}
"""OpenTelemetry tracing for the command path, the event store, and projections."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

from eventsourcing.domain import get_metadata_from_context, put_metadata_in_context
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Link, Span, SpanKind

from snake_case({ProjectName}).command import CommandSlice
from snake_case({ProjectName}).metadata import CORRELATION_ID_KEY

if TYPE_CHECKING:
    from collections.abc import Iterator

    from eventsourcing.dcb.api import DcbReadResponse
    from eventsourcing.dcb.application import BasicDcbApplication
    from eventsourcing.domain import TaggedEvent, TSlice
    from eventsourcing.pydantic import Decision
    from fastapi import FastAPI

TRACER_NAME = "snake_case({ProjectName})"

# State for `configure_telemetry`, held in a dict so the function needs no
# `global` statement. `enabled` records whether an SDK was actually installed.
_STATE = {"configured": False, "enabled": False}


def configure_telemetry() -> None:
    """
    Install an SDK tracer provider, but only when an exporter is configured.

    Idempotent, and called once from `create_app`. With no
    `OTEL_EXPORTER_OTLP_ENDPOINT` set — or with `OTEL_TRACES_EXPORTER=none` —
    no provider is installed at all, so `get_tracer` keeps returning a proxy to
    `NoOpTracer`: no exporter threads, no network, no I/O. Only the standard
    `OTEL_*` variables are honoured; the SDK reads the rest of its own
    configuration (service name, resource attributes, endpoint) from them
    directly.

    The SDK imports are deliberately function-local. `opentelemetry-sdk` lives
    in the `telemetry` extra and is absent from every test environment, so
    importing it at module scope would break those suites on import.
    """
    if _STATE["configured"]:
        return
    _STATE["configured"] = True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    exporter = os.environ.get("OTEL_TRACES_EXPORTER", "").casefold()
    if not endpoint or exporter == "none":
        return

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
    from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
    from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415

    provider = TracerProvider(resource=Resource.create())
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _STATE["enabled"] = True


def instrument_app(app: FastAPI) -> None:
    """
    Add ASGI request spans to the FastAPI application.

    A no-op unless `configure_telemetry` actually installed an SDK, which is
    what keeps the import safe: `opentelemetry-instrumentation-fastapi` ships
    in the `telemetry` extra alongside the SDK, so the only environments that
    reach the import are the ones that have it. Call this after
    `configure_telemetry`.

    Args:
        app: The FastAPI application to instrument.

    """
    if not _STATE["enabled"]:
        return

    from opentelemetry.instrumentation.fastapi import (  # noqa: PLC0415
        FastAPIInstrumentor,
    )

    FastAPIInstrumentor.instrument_app(app)


def _set_correlation_id(span: Span, metadata: dict[str, str]) -> None:
    """
    Copy the correlation id, if there is one, from metadata onto a span.

    Args:
        span: The span to annotate.
        metadata: The event metadata to read the correlation id from.

    """
    correlation_id = metadata.get(CORRELATION_ID_KEY)
    if correlation_id is not None:
        span.set_attribute(CORRELATION_ID_KEY, correlation_id)


@contextmanager
def command_span(slice_: TSlice) -> Iterator[None]:
    """
    Trace one `do()` call and propagate its trace context into the events.

    `do()` serves both commands and on-demand view replays, so the span name is
    dispatched on the slice type: a blanket "command" span would mislabel every
    catalogue query.

    Trace context travels in `TaggedEvent.metadata` via
    `put_metadata_in_context`, never by mutating events one at a time — the
    metadata defaults from a contextvar, so every event constructed inside the
    block inherits the `traceparent` automatically. Under a no-op tracer the
    span context is invalid and `inject` writes nothing, so the carrier is
    checked before it is put into context rather than recording an empty entry.

    The `correlation_id` already in context is copied onto the span as an
    attribute. Traces and the event log are then joinable in both directions
    without either becoming the other's source of truth — which matters,
    because a trace expires with the collector's retention window while the
    event log does not.

    Args:
        slice_: The slice being advanced, executed, and saved.

    Yields:
        None, for the duration of the span.

    """
    kind = "command" if isinstance(slice_, CommandSlice) else "replay"
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(f"{kind} {type(slice_).__name__}") as span:
        _set_correlation_id(span, get_metadata_from_context())
        carrier: dict[str, str] = {}
        inject(carrier)
        if not carrier:
            yield
        else:
            with put_metadata_in_context(carrier):
                yield


@contextmanager
def consumer_span(envelope: TaggedEvent[Decision], name: str) -> Iterator[None]:
    """
    Trace one `process_event` call, linked to the command that wrote the event.

    The span is a **link**, never a child, for two independent reasons: the
    producing span ended long before this runs — often minutes — and
    `BaseProjectionRunner` processes events on a bare `threading.Thread`, which
    inherits no contextvars, so there is no ambient context to be a child *of*.
    `SpanKind.CONSUMER` plus a `Link` is what the OTel messaging conventions
    prescribe for temporally decoupled producers and consumers.

    `context=Context()` makes that explicit by starting the span at the root,
    rather than relying on the thread happening to carry no ambient span.

    Events written while telemetry was off — and events replayed by `drain()`
    whose trace has already aged out of the retention window — carry no usable
    `traceparent`. The span is then unlinked rather than absent; that is
    expected, not a bug to code around.

    Nothing is suppressed: `start_as_current_span` records an exception on the
    span and lets it propagate, so the supervisor still sees the thread die and
    the view stops rather than silently advancing past a poison event.

    Args:
        envelope: The event being processed; its metadata carries the traceparent.
        name: The projection this span is for, used as the span name.

    Yields:
        None, for the duration of the span.

    """
    producer = trace.get_current_span(extract(envelope.metadata)).get_span_context()
    links = [Link(producer)] if producer.is_valid else []
    tracer = trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(
        f"process {name}",
        context=Context(),
        kind=SpanKind.CONSUMER,
        links=links,
    ) as span:
        # Read off the envelope, not the context: this thread inherits no
        # contextvars, so the event itself is the only source here.
        _set_correlation_id(span, envelope.metadata)
        yield


def instrument_recorder(app: BasicDcbApplication) -> None:
    """
    Wrap the application's recorder `append` and `read` in client spans.

    Patched on the instance, not the class, so a test building several
    applications instruments each one independently. Call this only after the
    application is constructed, since `recorder` is set in its `__init__`.

    Neither wrapper suppresses an exception: `start_as_current_span` records it
    on the span and lets it propagate, so a failing append still fails.

    Args:
        app: The constructed application whose recorder to instrument.

    """
    recorder = app.recorder
    if getattr(recorder.append, "telemetry_wrapped", False):
        return

    tracer = trace.get_tracer(TRACER_NAME)
    wrapped_append = recorder.append
    wrapped_read = recorder.read

    def append(*args, **kwargs) -> int:
        events = kwargs.get("events", args[0] if args else ())
        with tracer.start_as_current_span(
            "DcbRecorder.append",
            kind=SpanKind.CLIENT,
        ) as span:
            span.set_attribute("eventsourcing.event_count", len(events))
            return wrapped_append(*args, **kwargs)

    def read(*args, **kwargs) -> DcbReadResponse:
        with tracer.start_as_current_span("DcbRecorder.read", kind=SpanKind.CLIENT):
            return wrapped_read(*args, **kwargs)

    append.telemetry_wrapped = True  # type: ignore[attr-defined]
    read.telemetry_wrapped = True  # type: ignore[attr-defined]
    recorder.append = append  # type: ignore[method-assign]
    recorder.read = read  # type: ignore[method-assign]
```

---

## 2. Wiring — five lines, written once

`telemetry.py` does nothing until it is called. The whole footprint is one import and one
`with` in `application.py`, plus one import and three calls in `main.py`. Neither file is
ever edited again for telemetry, and neither is a per-slice edit.

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

The span wraps the **whole** body, `save()` included, because `trigger_event` fires inside
`execute()` and the trace context has to still be in scope when the events are constructed
*and* when they are appended.

`command_metadata()` sits **outside** `command_span(s)`, so `_set_correlation_id` finds a
`correlation_id` in context to annotate the span with. See
`.build-kit/references/metadata.md` for that module.

### `src/snake_case({ProjectName})/main.py`

```python
from snake_case({ProjectName}).telemetry import (
    configure_telemetry,
    instrument_app,
    instrument_recorder,
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[dict]:
    with {ProjectName}App() as dcb_app:
        instrument_recorder(dcb_app)          # AFTER construction — `recorder` is set in `__init__`
        yield {"dcb_app": dcb_app}


def create_app() -> FastAPI:
    configure_telemetry()                     # FIRST — `instrument_app` reads the state it sets
    app = FastAPI(lifespan=lifespan)
    instrument_app(app)
    ...                                       # the existing `include_router` lines
    return app
```

Under an `AsyncExitStack` lifespan (once a projection exists), `instrument_recorder(dcb_app)`
goes immediately after `stack.enter_context({ProjectName}App())`, on the same principle.

### Inside a `Projection`

```python
from snake_case({ProjectName}).telemetry import consumer_span


    def process_event(self, envelope: TaggedEvent[Decision], tracking: Tracking) -> None:
        with consumer_span(envelope, "snake_case({SliceName})"):
            match envelope.decision:
                ...
```

One span around the **whole** `match`. For an automation that is all that is needed to get
the full causal chain — `_fire` runs inside it, the command it issues opens its own span
beneath, and the events that command emits inherit the `traceparent` from the contextvar.

---

## 3. `tests/unit/test_telemetry.py`

These run **without** `opentelemetry-sdk`, which is exactly the point: the SDK belongs to
the `telemetry` extra and is absent from every test environment, so the suite exercises the
no-op path an unconfigured deployment actually takes. Do not add the SDK to a test env in
order to assert on spans — construct an in-memory provider inside the test instead.

Substitute any one of the project's own event types for `{EventName}`; the two fixture slices
only need a real `Decision` to emit and a boundary to violate, so the first slice built is
always a fine source for it.

```python
# Copyright {YYYY} {ProjectAuthor}
"""
Unit tests for the telemetry module.

These run without `opentelemetry-sdk`, which lives in the `telemetry` extra and
is deliberately absent from every test environment. That is the point: they
exercise the no-op path an unconfigured deployment actually takes, where
`get_tracer` returns a proxy to `NoOpTracer`.

What is therefore *not* covered here is the recording path — that a valid span
context really does reach event metadata as a `traceparent`. Asserting it needs
a real SDK provider installed globally, which would both defeat the no-op
coverage below and leak into every other test in the process. It is left to
manual verification against a collector.
"""

import pytest
from eventsourcing.domain import get_metadata_from_context
from eventsourcing.pydantic import Selector

from snake_case({ProjectName}) import telemetry
from snake_case({ProjectName}).application import {ProjectName}App
from snake_case({ProjectName}).command import CommandSlice
from snake_case({ProjectName}).snake_case({Context}).events import {EventName}
from snake_case({ProjectName}).telemetry import (
    command_span,
    configure_telemetry,
    instrument_recorder,
)


class _SilentSlice(CommandSlice):
    """A command slice that runs successfully without recording anything."""

    def consistency_boundary(self) -> Selector:
        """Return a selector scoped to an entity that never existed."""
        return Selector(types=[{EventName}], tags=["entity:never-created"])

    def execute(self) -> None:
        """Record no event at all."""


class _RecordingSlice(CommandSlice):
    """A command slice that records one event, to drive the append wrapper."""

    def consistency_boundary(self) -> Selector:
        """Return a selector scoped to this slice's own entity."""
        return Selector(types=[{EventName}], tags=["entity:traced"])

    def execute(self) -> None:
        """Emit a single event."""
        self.trigger_event(
            {EventName},
            ["entity:traced"],
            # ... the event's own fields
        )


def test_configure_telemetry_installs_nothing_without_an_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Leave the tracer a no-op proxy when no exporter endpoint is configured.

    Guards the claim that telemetry is off by default: were an SDK provider
    installed here, this would raise `ModuleNotFoundError` instead, since this
    environment has no SDK to install.
    """
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setitem(telemetry._STATE, "configured", value=False)  # noqa: SLF001

    configure_telemetry()

    assert telemetry._STATE["enabled"] is False  # noqa: SLF001


def test_configure_telemetry_skips_a_configured_endpoint_when_told_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Honour `OTEL_TRACES_EXPORTER=none` even with an endpoint set.

    Same guard as above: reaching the SDK import in this environment raises.
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4317")
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")
    monkeypatch.setitem(telemetry._STATE, "configured", value=False)  # noqa: SLF001

    configure_telemetry()

    assert telemetry._STATE["enabled"] is False  # noqa: SLF001


def test_command_span_writes_no_traceparent_under_a_noop_tracer() -> None:
    """
    Put no metadata in context when the span context is invalid.

    A no-op span injects nothing into the carrier, and an empty carrier must
    not become a `{"traceparent": None}` entry on every event.
    """
    with command_span(_SilentSlice()):
        assert get_metadata_from_context() == {}


def test_command_span_does_not_swallow_exceptions() -> None:
    """Let a failing command propagate rather than ending the span quietly."""
    message = "command failed"

    with pytest.raises(RuntimeError, match=message), command_span(_SilentSlice()):
        raise RuntimeError(message)


def test_command_span_traces_replays_as_well_as_commands() -> None:
    """
    Accept a perspective that is not a command.

    `do()` serves on-demand view replays too, so `command_span` has to take
    both without objecting to the slice type.
    """
    with command_span(_SilentSlice()):
        pass


def test_instrument_recorder_is_idempotent() -> None:
    """
    Wrap the recorder once, however often the lifespan runs.

    The wrappers are installed on the instance, so a second call would
    otherwise nest a second span around every append and read.
    """
    with {ProjectName}App() as app:
        instrument_recorder(app)
        once = app.recorder.append

        instrument_recorder(app)

        assert app.recorder.append is once


def test_instrumented_recorder_still_appends_and_reads() -> None:
    """Keep append and read working through the wrappers, no-op spans and all."""
    with {ProjectName}App() as app:
        instrument_recorder(app)

        slice_ = app.do(_RecordingSlice())

        assert slice_.outcome.position is not None
        assert app.recorder.head() == slice_.outcome.position


def test_instrumented_recorder_does_not_swallow_append_failures() -> None:
    """
    Let a rejected append surface rather than ending the span quietly.

    Recording the same entity twice violates the append condition, which the
    library raises through. The span must not turn that into a silent success.
    """
    with {ProjectName}App() as app:
        instrument_recorder(app)
        app.do(_RecordingSlice())

        with pytest.raises(Exception, match=r".*"):
            app.do(_RecordingSlice())
```

### Testing `consumer_span`

Add these to the same module once the project has a `Projection` to instrument. Both stay on
the no-op path, so they need no SDK:

- **An envelope with no `traceparent` yields an unlinked span, not an error** — build a
  `TaggedEvent` with `metadata={}` and assert the block runs.
- **`consumer_span` re-raises** — same shape as
  `test_command_span_does_not_swallow_exceptions`. This is the one that matters: a span
  that suppressed would let the projection advance past a poison event while `/livez`
  and `/readyz` both still reported 200.

---

## 4. Dependencies

Already declared by the project template, and **not** something to re-derive:
`opentelemetry-api` is a required dependency in `[project] dependencies`; the `telemetry`
extra holds `opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi` and
`opentelemetry-exporter-otlp-proto-grpc` only. `.build-kit/CLAUDE.md` → *Observability*
explains why the split is load-bearing and why no test env may install the extra.

If you do change any of it, regenerate **all** lock files together —
`.build-kit/CLAUDE.md` → *Regenerating lock files*. A partial regeneration leaves the other
environments pinned against a stale constraint hash.
