"""Everything the tool prints goes through one redacting console."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any, TextIO, cast

REDACTED = "[redacted]"
MIN_STATE_VALUE = 6
MIN_SECRET = 4


class Redactor:
    """Masks registered secrets and every string value found in a state document."""

    def __init__(self) -> None:
        """Start with nothing to mask."""
        self._values: set[str] = set()

    def add(self, value: str) -> None:
        """Mask `value` wherever it appears."""
        if len(value) >= MIN_SECRET:
            self._values.add(value)

    def add_state(self, document: Any) -> None:
        """Mask every string value in a state's outputs and resource instances, where secrets live."""
        for text in _state_values(document):
            if len(text) >= MIN_STATE_VALUE:
                self._values.add(text)

    def __call__(self, text: str) -> str:
        """`text` with every registered value replaced, longest first."""
        for value in sorted(self._values, key=len, reverse=True):
            if value in text:
                text = text.replace(value, REDACTED)
        return text


def _state_values(document: Any) -> Iterable[str]:
    """The string values of a state's outputs and of every resource instance's attributes."""
    if not isinstance(document, dict):
        return
    state = cast(dict[str, Any], document)
    outputs = state.get("outputs")
    if isinstance(outputs, dict):
        for output in cast(dict[str, Any], outputs).values():
            if isinstance(output, dict):
                yield from _strings(cast(dict[str, Any], output).get("value"))
    resources = state.get("resources")
    if isinstance(resources, list):
        for resource in cast(list[Any], resources):
            if not isinstance(resource, dict):
                continue
            instances = cast(dict[str, Any], resource).get("instances")
            if not isinstance(instances, list):
                continue
            for instance in cast(list[Any], instances):
                if isinstance(instance, dict):
                    fields = cast(dict[str, Any], instance)
                    for name in ("attributes", "attributes_flat", "private", "index_key"):
                        yield from _strings(fields.get(name))


def _strings(node: Any) -> Iterable[str]:
    """Every string leaf under `node`."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in cast(dict[Any, Any], node).values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in cast(list[Any], node):
            yield from _strings(item)


class Console:
    """Line output to stdout and stderr, always redacted."""

    def __init__(self, redactor: Redactor, out: TextIO | None = None, err: TextIO | None = None) -> None:
        """Bind the redactor and the streams, the process ones by default."""
        self.redactor = redactor
        self._out = out
        self._err = err

    def info(self, message: str) -> None:
        """Print one progress or result line."""
        print(self.redactor(message), file=self._out or sys.stdout, flush=True)

    def error(self, message: str) -> None:
        """Print one error line."""
        print(self.redactor(message), file=self._err or sys.stderr, flush=True)
