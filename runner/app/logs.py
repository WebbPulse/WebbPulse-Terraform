"""Redaction and the CloudWatch Logs sink the engine's output is streamed to."""

from __future__ import annotations

import sys
import time
from types import TracebackType
from typing import TYPE_CHECKING, Iterable, Protocol, Sequence

if TYPE_CHECKING:
    from mypy_boto3_logs.client import CloudWatchLogsClient
else:
    CloudWatchLogsClient = object

REDACTED = "[redacted]"
MIN_REDACTABLE_LENGTH = 4
MAX_BATCH_EVENTS = 500


class Redactor:
    """Replaces known sensitive values with a placeholder before anything is emitted."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets: list[str] = []
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str | None) -> None:
        """Register one more value to mask, ignoring values too short to be meaningful."""
        if not secret or len(secret) < MIN_REDACTABLE_LENGTH:
            return
        if secret not in self._secrets:
            self._secrets.append(secret)
            self._secrets.sort(key=len, reverse=True)

    def extend(self, secrets: Iterable[str]) -> None:
        """Register several values to mask."""
        for secret in secrets:
            self.add(secret)

    def scrub(self, text: str) -> str:
        """Mask every registered value found in the text."""
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text


class LogSink(Protocol):
    """Anything the engine's output can be written to line by line."""

    def write(self, line: str) -> None:
        """Emit one already redacted line."""

    def flush(self) -> None:
        """Push anything buffered."""


class CloudWatchLogSink:
    """Writes redacted lines to a `<run_id>/<phase>` stream and mirrors them to stdout."""

    def __init__(
        self,
        client: CloudWatchLogsClient,
        log_group: str,
        stream_name: str,
        redactor: Redactor,
        *,
        mirror_to_stdout: bool = True,
    ) -> None:
        self._client = client
        self._log_group = log_group
        self._stream_name = stream_name
        self._redactor = redactor
        self._mirror = mirror_to_stdout
        self._pending: list[dict[str, object]] = []
        self._lines: list[str] = []
        self._stream_ready = False

    @property
    def lines(self) -> Sequence[str]:
        """Every redacted line written so far, for the uploaded log artifact."""
        return self._lines

    def text(self) -> str:
        """The captured log as a single document."""
        return "\n".join(self._lines) + ("\n" if self._lines else "")

    def _ensure_stream(self) -> None:
        if self._stream_ready:
            return
        try:
            self._client.create_log_stream(logGroupName=self._log_group, logStreamName=self._stream_name)
        except self._client.exceptions.ResourceAlreadyExistsException:
            pass
        self._stream_ready = True

    def write(self, line: str) -> None:
        """Redact, keep, mirror and buffer one line."""
        scrubbed = self._redactor.scrub(line.rstrip("\n"))
        self._lines.append(scrubbed)
        if self._mirror:
            print(scrubbed, flush=True)
        self._pending.append({"timestamp": int(time.time() * 1000), "message": scrubbed or " "})
        if len(self._pending) >= MAX_BATCH_EVENTS:
            self.flush()

    def flush(self) -> None:
        """Send the buffered events, dropping the batch rather than failing the phase."""
        if not self._pending:
            return
        batch = self._pending
        self._pending = []
        try:
            self._ensure_stream()
            self._client.put_log_events(
                logGroupName=self._log_group,
                logStreamName=self._stream_name,
                logEvents=batch,  # type: ignore[arg-type]
            )
        except Exception as error:
            print(f"log delivery failed: {type(error).__name__}", file=sys.stderr, flush=True)

    def __enter__(self) -> CloudWatchLogSink:
        """Enter the sink so the buffer is flushed on the way out."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Flush whatever is still buffered."""
        self.flush()
