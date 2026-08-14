# Copyright 2026 Moritz E. Beber
"""Provide the process-wide DCB application."""

from __future__ import annotations

from typing import TYPE_CHECKING

from eventsourcing.pydantic import DcbApplication
from fastapi import Request  # noqa: TC002

from course_subscriptions.command import CommandOutcome, CommandSlice
from course_subscriptions.metadata import command_metadata
from course_subscriptions.telemetry import command_span

if TYPE_CHECKING:
    from eventsourcing.domain import TSlice


class CourseSubscriptionsApp(DcbApplication):
    """The single, process-wide DCB application."""

    context_name = "subscriptions"

    def do(self, s: TSlice) -> TSlice:
        """
        Advance, execute and save a slice, capturing a command's outcome.

        ``DcbRepository.save`` returns the append position and internally
        drains ``new_decisions`` via ``collect_events`` — both are lost by the
        base implementation, so a ``CommandSlice`` needs its event ids read
        off ``new_decisions`` before ``save`` runs. The whole body runs under
        one span, ``save`` included, because ``trigger_event`` fires inside
        ``execute`` and the trace context must still be in scope when the
        events are constructed *and* when they are appended.

        ``command_metadata`` is outermost so ``command_span`` can read the
        ``correlation_id`` off the context for its span attribute. It is also
        the fallback seed for every non-HTTP entry point — ``drain()``, the
        manual scenario script, and the test suites — which reach this method
        without passing through ``MetadataMiddleware``.
        """
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


def get_application(request: Request) -> CourseSubscriptionsApp:
    """Return the process-wide application from FastAPI request state."""
    return request.state.dcb_app
