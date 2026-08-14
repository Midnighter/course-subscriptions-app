# Copyright 2026 Moritz E. Beber
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

from course_subscriptions.command import CommandSlice
from course_subscriptions.metadata import CORRELATION_ID_KEY

if TYPE_CHECKING:
    from collections.abc import Iterator

    from eventsourcing.dcb.api import DcbReadResponse
    from eventsourcing.dcb.application import BasicDcbApplication
    from eventsourcing.domain import TSlice, TaggedEvent
    from eventsourcing.pydantic import Decision
    from fastapi import FastAPI

TRACER_NAME = "course_subscriptions"

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
