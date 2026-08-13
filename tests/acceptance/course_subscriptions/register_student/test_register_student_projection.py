# Copyright 2026 Moritz E. Beber
"""Test the Register Student automation's projection."""

import pytest
from eventsourcing.domain import TaggedEvent, get_metadata_from_context
from eventsourcing.persistence import Tracking

from course_subscriptions.course_subscriptions.events import (
    ExternalStudentRegistered,
    StudentRegistered,
)
from course_subscriptions.course_subscriptions.register_student.projection import (
    MAX_ATTEMPTS,
    POPORegisterStudentView,
    RegisterStudentEntry,
    RegisterStudentProjection,
)

_STUDENT_ID = "STU-2026-0042"
_TAGS = [f"student:{_STUDENT_ID}"]


class _Recorder:
    """Command port that records each call along with its ambient metadata."""

    def __init__(self) -> None:
        self.calls: list[tuple[RegisterStudentEntry, dict[str, str]]] = []

    def __call__(self, entry: RegisterStudentEntry) -> None:
        self.calls.append((entry, get_metadata_from_context()))


class _Failing:
    """Command port that always raises."""

    def __call__(self, _entry: RegisterStudentEntry) -> None:
        msg = "boom"
        raise ValueError(msg)


@pytest.fixture
def recorder() -> _Recorder:
    """Return a command port that records its calls."""
    return _Recorder()


@pytest.fixture
def projection(recorder: _Recorder) -> RegisterStudentProjection:
    """Return a projection over a fresh in-memory view."""
    return RegisterStudentProjection(view=POPORegisterStudentView(), command=recorder)


def _trigger(*, correlation_id: str | None = None) -> TaggedEvent:
    metadata = {"correlation_id": correlation_id} if correlation_id else {}
    return TaggedEvent(
        decision=ExternalStudentRegistered(
            student_id=_STUDENT_ID,
            name="Anna Müller",
            course_limit=2,
        ),
        tags=_TAGS,
        metadata=metadata,
    )


def test_trigger_fires_the_command(
    projection: RegisterStudentProjection,
    recorder: _Recorder,
) -> None:
    """The trigger records an entry and fires the command."""
    envelope = _trigger(correlation_id="corr-1")
    projection.process_event(envelope, Tracking("upstream", 1))

    entry, metadata = recorder.calls[0]
    assert entry.student_id == _STUDENT_ID
    assert entry.name == "Anna Müller"
    assert entry.course_limit == 2
    assert metadata["causation_id"] == str(envelope.uuid)
    assert metadata["correlation_id"] == "corr-1"
    assert projection.view.get_entries() == [entry]


def test_trigger_with_no_correlation_id_still_fires(
    projection: RegisterStudentProjection,
    recorder: _Recorder,
) -> None:
    """A trigger without a correlation id still fires cleanly."""
    envelope = _trigger()
    projection.process_event(envelope, Tracking("upstream", 1))

    _, metadata = recorder.calls[0]
    assert "correlation_id" not in metadata
    assert metadata["causation_id"] == str(envelope.uuid)


def test_emitted_event_drains_the_entry(projection: RegisterStudentProjection) -> None:
    """The event the command emits removes the outstanding entry."""
    projection.process_event(_trigger(), Tracking("upstream", 1))
    projection.process_event(
        TaggedEvent(
            decision=StudentRegistered(
                student_id=_STUDENT_ID,
                name="Anna Müller",
                course_limit=2,
            ),
            tags=_TAGS,
        ),
        Tracking("upstream", 2),
    )
    assert projection.view.get_entries() == []


def test_unrelated_event_still_advances_tracking() -> None:
    """An event outside `topics` is tracked but leaves entries untouched."""
    view = POPORegisterStudentView()
    projection = RegisterStudentProjection(view=view)
    projection.process_event(
        TaggedEvent(
            decision=StudentRegistered(
                student_id=_STUDENT_ID,
                name="Anna Müller",
                course_limit=2,
            ),
            tags=_TAGS,
        ),
        Tracking("upstream", 1),
    )
    assert view.max_tracking_id("upstream") == 1
    assert view.get_entries() == []


def test_failing_command_does_not_propagate_and_leaves_entry() -> None:
    """A command that raises is swallowed, leaving the entry in place."""
    view = POPORegisterStudentView()
    projection = RegisterStudentProjection(view=view, command=_Failing())
    projection.process_event(_trigger(), Tracking("upstream", 1))

    assert len(view.get_entries()) == 1


def test_drain_refires_an_orphaned_entry(recorder: _Recorder) -> None:
    """`drain()` re-fires an entry seeded straight into the view."""
    view = POPORegisterStudentView()
    view.add_entry(
        RegisterStudentEntry(
            student_id=_STUDENT_ID,
            name="Anna Müller",
            course_limit=2,
        ),
        Tracking("upstream", 1),
    )
    projection = RegisterStudentProjection(view=view, command=recorder)

    projection.drain()

    assert len(recorder.calls) == 1
    entry, metadata = recorder.calls[0]
    assert entry.student_id == _STUDENT_ID
    assert metadata == {}


def test_drain_skips_entries_past_max_attempts(recorder: _Recorder) -> None:
    """`drain()` does not re-fire an entry already past MAX_ATTEMPTS."""
    view = POPORegisterStudentView()
    view.add_entry(
        RegisterStudentEntry(
            student_id=_STUDENT_ID,
            name="Anna Müller",
            course_limit=2,
            attempts=MAX_ATTEMPTS + 1,
        ),
        Tracking("upstream", 1),
    )
    projection = RegisterStudentProjection(view=view, command=recorder)

    projection.drain()

    assert recorder.calls == []


def test_drain_is_a_no_op_on_a_clean_view(recorder: _Recorder) -> None:
    """`drain()` does nothing when there are no outstanding entries."""
    projection = RegisterStudentProjection(
        view=POPORegisterStudentView(),
        command=recorder,
    )

    projection.drain()

    assert recorder.calls == []
