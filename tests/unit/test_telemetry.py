# Copyright 2026 Moritz E. Beber
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
from eventsourcing.domain import TaggedEvent, get_metadata_from_context
from eventsourcing.pydantic import Selector

from course_subscriptions import telemetry
from course_subscriptions.application import CourseSubscriptionsApp
from course_subscriptions.command import CommandSlice
from course_subscriptions.course_subscriptions.events import CourseCapacityChanged
from course_subscriptions.telemetry import (
    command_span,
    configure_telemetry,
    consumer_span,
    instrument_recorder,
)


class _SilentSlice(CommandSlice):
    """A command slice that runs successfully without recording anything."""

    def consistency_boundary(self) -> Selector:
        """Return a selector scoped to an entity that never existed."""
        return Selector(types=[CourseCapacityChanged], tags=["course:never-created"])

    def execute(self) -> None:
        """Record no event at all."""


class _RecordingSlice(CommandSlice):
    """A command slice that records one event, to drive the append wrapper."""

    def consistency_boundary(self) -> Selector:
        """Return a selector scoped to this slice's own entity."""
        return Selector(types=[CourseCapacityChanged], tags=["course:traced"])

    def execute(self) -> None:
        """Emit a single event."""
        self.trigger_event(
            CourseCapacityChanged,
            ["course:traced"],
            course_id="traced",
            capacity=10,
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
    with CourseSubscriptionsApp() as app:
        instrument_recorder(app)
        once = app.recorder.append

        instrument_recorder(app)

        assert app.recorder.append is once


def test_instrumented_recorder_still_appends_and_reads() -> None:
    """Keep append and read working through the wrappers, no-op spans and all."""
    with CourseSubscriptionsApp() as app:
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
    with CourseSubscriptionsApp() as app:
        instrument_recorder(app)
        app.do(_RecordingSlice())

        with pytest.raises(Exception, match=r".*"):
            app.do(_RecordingSlice())


def test_consumer_span_accepts_an_envelope_with_no_traceparent() -> None:
    """An envelope with no traceparent yields an unlinked span, not an error."""
    envelope = TaggedEvent(
        decision=CourseCapacityChanged(course_id="traced", capacity=10),
        tags=["course:traced"],
        metadata={},
    )

    with consumer_span(envelope, "register_student"):
        pass


def test_consumer_span_does_not_swallow_exceptions() -> None:
    """
    Let a failing projection branch propagate rather than ending quietly.

    A span that suppressed here would let the projection advance past a
    poison event while a health check kept reporting success.
    """
    envelope = TaggedEvent(
        decision=CourseCapacityChanged(course_id="traced", capacity=10),
        tags=["course:traced"],
        metadata={},
    )
    message = "poison event"

    with (
        pytest.raises(RuntimeError, match=message),
        consumer_span(envelope, "register_student"),
    ):
        raise RuntimeError(message)
