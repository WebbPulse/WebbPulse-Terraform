"""Keeping the engine's vended AWS sessions and identity tokens fresh for as long as the phase runs."""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from types import TracebackType
from typing import Callable

from app.api import ApiError, CredentialsRefused
from app.credential_files import CredentialFiles
from app.logs import Redactor
from app.models import RefreshedCredentials
from app.workload_identity import WorkloadIdentityFiles

REFRESH_LEAD_SECONDS = 600
"""How long before the earlier session expires the runner asks for new ones."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CredentialRefresher:
    """Checks every `interval` seconds and rewrites the credential files before they expire.

    The new secrets are registered with the redactor before they reach a file the
    engine reads, and only the new expiry is reported. A failed attempt is
    reported and retried at the next check, while the current keys still have at
    least the lead left. A refusal means the run ended or its phase moved on,
    which the heartbeat acts on, so refreshing simply stops.
    """

    def __init__(
        self,
        refresh: Callable[[], RefreshedCredentials],
        files: CredentialFiles,
        redactor: Redactor,
        report: Callable[[str], None],
        interval: float,
        *,
        lead_seconds: float = REFRESH_LEAD_SECONDS,
        clock: Callable[[], datetime] = _now,
        identity: WorkloadIdentityFiles | None = None,
    ) -> None:
        self._refresh = refresh
        self._files = files
        self._identity = identity
        self._redactor = redactor
        self._report = report
        self._interval = interval
        self._lead = timedelta(seconds=lead_seconds)
        self._clock = clock
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="credential-refresher", daemon=True)
        self.refreshes = 0

    def _expires_at(self) -> datetime | None:
        """The earliest expiry among the sessions and identity tokens."""
        identity = self._identity.expires_at if self._identity else None
        known = [expiry for expiry in (self._files.expires_at, identity) if expiry is not None]
        return min(known) if known else None

    def due(self) -> bool:
        """Whether the earliest session or token is within the lead of its expiry."""
        expires_at = self._expires_at()
        return expires_at is not None and expires_at - self._clock() <= self._lead

    def refresh_once(self) -> bool:
        """Refresh now, returning False once the control plane has refused."""
        try:
            refreshed = self._refresh()
        except CredentialsRefused as refusal:
            self._report(f"credential refresh refused, keeping the current keys: {refusal}")
            return False
        except ApiError as error:
            self._report(f"credential refresh not completed, retrying: {error}")
            return True
        self._redactor.extend(refreshed.secrets())
        self._files.write(refreshed.aws_credentials, refreshed.backend_credentials)
        if self._identity is not None:
            self._identity.write(refreshed.workload_identity)
        self.refreshes += 1
        expires_at = self._expires_at()
        until = expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if expires_at else "an unknown time"
        self._report(f"credentials refreshed until {until}")
        return True

    def _loop(self) -> None:
        while not self._stopped.wait(self._interval):
            if not self.due():
                continue
            try:
                if not self.refresh_once():
                    return
            except Exception as error:
                print(f"credential refresh failed: {type(error).__name__}", file=sys.stderr, flush=True)

    def start(self) -> None:
        """Begin checking."""
        self._thread.start()

    def stop(self) -> None:
        """Stop checking and wait for an in flight refresh to finish."""
        self._stopped.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self._interval + 30)

    def __enter__(self) -> CredentialRefresher:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
