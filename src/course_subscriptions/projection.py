# Copyright 2026 Moritz E. Beber
"""Provide shared infrastructure for running and supervising projections."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Self

from eventsourcing.projection import BaseProjectionRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from eventsourcing.persistence import TrackingRecorder

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESTARTS = 3


class SharedAppProjectionRunner(BaseProjectionRunner):
    """
    A runner over the shared, process-wide application.

    ``BaseProjectionRunner.__exit__`` unconditionally closes ``self.app``,
    which is fatal when that application is shared with the routes writing
    the runner's trigger events. This override reproduces the rest of
    ``__exit__`` and drops only that one call.
    """

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the runner's threads without closing the shared application."""
        self.stop()
        self._stop_thread.join()
        self._subscription.__exit__(exc_type, exc_val, exc_tb)
        self._processing_thread.join()
        if self._thread_error:
            error = self._thread_error
            self._thread_error = None
            raise error


class _Registration:
    """Bookkeeping for one projection registered with the supervisor."""

    def __init__(
        self,
        name: str,
        tracking_recorder: TrackingRecorder,
        factory: Callable[[], SharedAppProjectionRunner],
        max_restarts: int,
    ) -> None:
        self.name = name
        self.tracking_recorder = tracking_recorder
        self.factory = factory
        self.max_restarts = max_restarts
        self.runner: SharedAppProjectionRunner | None = None
        self.restarts = 0
        self.last_position: int | None = None
        self.failure: BaseException | None = None


class ProjectionSupervisor:
    """
    Watch every registered projection runner and restart it on failure.

    One watchdog thread probes every registered runner with a non-blocking
    ``run_forever(timeout=0)``; a dead runner is rebuilt from its factory over
    the *same* view, so it resumes at ``max_tracking_id`` rather than
    replaying from zero. Restarts are counted by tracking position, not by
    call count, so unrelated transient faults reset the counter while a
    stuck poison event keeps incrementing it. Past ``max_restarts`` the
    runner is left dead and reported via ``failures()``.
    """

    def __init__(self, context_name: str, poll_interval: float = 1.0) -> None:
        self._context_name = context_name
        self._poll_interval = poll_interval
        self._registrations: dict[str, _Registration] = {}
        self._stop_event = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watch, daemon=True)

    def register(
        self,
        name: str,
        tracking_recorder: TrackingRecorder,
        factory: Callable[[], SharedAppProjectionRunner],
        max_restarts: int = DEFAULT_MAX_RESTARTS,
    ) -> None:
        """Register a projection. Call before entering the supervisor."""
        self._registrations[name] = _Registration(
            name=name,
            tracking_recorder=tracking_recorder,
            factory=factory,
            max_restarts=max_restarts,
        )

    def failures(self) -> dict[str, BaseException]:
        """Return the exception of every projection that has stopped for good."""
        return {
            name: reg.failure
            for name, reg in self._registrations.items()
            if reg.failure is not None
        }

    def _watch(self) -> None:
        while not self._stop_event.wait(timeout=self._poll_interval):
            for reg in self._registrations.values():
                if reg.failure is not None or reg.runner is None:
                    continue
                try:
                    reg.runner.run_forever(timeout=0)
                except Exception as exc:  # noqa: BLE001
                    self._handle_failure(reg, exc)

    def _handle_failure(self, reg: _Registration, exc: BaseException) -> None:
        logger.error("projection %s died", reg.name, exc_info=exc)
        position = reg.tracking_recorder.max_tracking_id(self._context_name)
        if position != reg.last_position:
            reg.restarts = 0
        reg.last_position = position
        reg.restarts += 1
        if reg.restarts > reg.max_restarts:
            reg.failure = exc
            logger.error(
                "projection %s exceeded %d restarts, giving up",
                reg.name,
                reg.max_restarts,
            )
            return
        reg.runner = reg.factory()
        reg.runner.__enter__()

    def __enter__(self) -> Self:
        """Construct and enter every registered runner, then start watching."""
        for reg in self._registrations.values():
            reg.runner = reg.factory()
            reg.runner.__enter__()
        self._watchdog_thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop watching and exit every runner still alive."""
        self._stop_event.set()
        self._watchdog_thread.join()
        for reg in self._registrations.values():
            if reg.runner is not None:
                reg.runner.__exit__(exc_type, exc_val, exc_tb)
