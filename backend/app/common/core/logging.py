"""The application logger, on the shared package's JSON formatter.

The one name the rest of the backend imports to reach it. Request and user ids
arrive through `webbpulse.log_context`, so a call site adds fields with `extra=`.
"""

from __future__ import annotations

import logging

from webbpulse.logging import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger", "logger"]

logger: logging.Logger = get_logger("app")
"""The shared application logger. A plain `logging.Logger`, so `extra={...}` is how a
call site adds fields."""
