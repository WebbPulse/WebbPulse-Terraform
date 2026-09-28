"""The runner's liveness beat, which the control plane turns into a Step Functions task heartbeat."""

from __future__ import annotations

import sys
import threading
from types import TracebackType
from typing import Callable

from app.api import ApiError, HeartbeatRefused


class Heartbeat:
    """Calls `beat` every `interval` seconds on a daemon thread until stopped.

    A passing failure is printed and retried at the next beat, since the state's
    heartbeat timeout allows many missed beats. A refusal means the run ended or
    its phase moved on, so the beat stops and `on_refused` is told why.
    """

    def __init__(
        self,
        beat: Callable[[], None],
        interval: float,
        on_refused: Callable[[str], None],
    ) -> None:
        self._beat = beat
        self._interval = interval
        self._on_refused = on_refused
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="heartbeat", daemon=True)
        self.beats = 0

    def _loop(self) -> None:
        while not self._stopped.wait(self._interval):
            try:
                self._beat()
            except HeartbeatRefused as refusal:
                print(f"heartbeat refused, stopping the phase: {refusal}", file=sys.stderr, flush=True)
                self._on_refused(str(refusal))
                return
            except ApiError as error:
                print(f"heartbeat not delivered: {error}", file=sys.stderr, flush=True)
                continue
            self.beats += 1

    def start(self) -> None:
        """Begin beating."""
        self._thread.start()

    def stop(self) -> None:
        """Stop beating and wait for an in flight beat to finish."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval + 5)

    def __enter__(self) -> Heartbeat:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
