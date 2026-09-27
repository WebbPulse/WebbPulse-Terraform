"""No logging call's `extra` names a reserved `LogRecord` attribute.

`Logger.makeRecord` raises `KeyError` for such a key, but only when the level is
enabled, so a suite running at the default `WARNING` never sees an `info` call
fail while a Lambda at `INFO` does. Scanning the source catches it at every level.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"
LOG_METHODS = frozenset({"debug", "info", "warning", "error", "exception", "critical", "log"})
RESERVED = frozenset(logging.LogRecord("n", logging.INFO, "p", 0, "m", None, None).__dict__) | {"message", "asctime"}


def reserved_extra_keys(source: str) -> list[tuple[int, str]]:
    """The line and key of every literal `extra` key a log call would be refused."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            found.extend(
                (node.lineno, key.value)
                for key in keyword.value.keys
                if isinstance(key, ast.Constant) and key.value in RESERVED
            )
    return found


def test_the_scan_catches_a_reserved_key() -> None:
    """The scan flags the exact shape that broke check replacement."""
    source = '_log.info("x", extra={"event": "e", "name": "n"})'
    assert reserved_extra_keys(source) == [(1, "name")]
    with pytest.raises(KeyError):
        logging.getLogger("reserved").makeRecord("r", logging.INFO, "f", 1, "m", (), None, extra={"name": "n"})


def test_no_log_call_in_the_app_uses_a_reserved_extra_key() -> None:
    """Every `extra=` literal under `app/` avoids the attributes `LogRecord` owns."""
    offenders = [
        f"{path.relative_to(APP_DIR.parent)}:{line} {key}"
        for path in sorted(APP_DIR.rglob("*.py"))
        for line, key in reserved_extra_keys(path.read_text())
    ]
    assert not offenders, f"Reserved LogRecord keys in extra: {offenders}"
