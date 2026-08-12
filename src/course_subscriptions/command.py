# Copyright 2026 Moritz E. Beber
"""Provide shared infrastructure for state-change (command) slices."""

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
