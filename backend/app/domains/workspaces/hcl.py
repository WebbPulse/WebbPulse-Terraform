"""Write time checking of an HCL variable value, and how it reaches the engine.

No HCL parser is installed in this environment and none is added for this: the
backend ships as a Lambda image and a parser would be a dependency carried by
every request path to serve one write route. What is checked here instead is the
structure a broken expression breaks first, which is what a person actually
typos: an unterminated string or heredoc, and unbalanced brackets or braces. An
expression that passes still has to mean something to the engine, so the engine
remains the authority and a run is where a semantically wrong expression fails.

The point of checking at all is that a variable is written once and read by every
subsequent run. A value that cannot possibly parse is worth refusing at the write
rather than turning every later plan on that workspace into a syntax error.
"""

from __future__ import annotations

import re
from typing import Final

MAX_HCL_LENGTH: Final = 32_768
"""The same ceiling the value field carries, restated so a direct caller of the
validator cannot hand it something unbounded to scan."""

_OPENERS: Final = {"(": ")", "[": "]", "{": "}"}
_CLOSERS: Final = {")": "(", "]": "[", "}": "{"}

_HEREDOC_START: Final = re.compile(r"<<-?([^\W\d][\w-]*)\r?\n")
"""An HCL heredoc opener with its optional indentation marker."""

_VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
MAX_TEMPLATE_DEPTH: Final = 64


class InvalidHcl(ValueError):
    """The value cannot be an HCL expression, whatever it would mean.

    The message is written for the person who typed the value and names nothing
    about the value itself, so it is safe to return from the API for a sensitive
    variable as well as a plain one.
    """


def validate_name(key: str) -> None:
    """Reject names that cannot be emitted as native HCL assignments."""
    if not _VARIABLE_NAME.fullmatch(key):
        raise InvalidHcl("An HCL variable name must contain only letters, digits, underscores or hyphens.")


def validate(value: str) -> None:
    """Refuse a value that cannot parse as an HCL expression.

    Scans once, tracking whether the cursor is inside a quoted string, a heredoc
    or a comment, and matching brackets outside them. Terraform's own parse is
    still the authority on meaning; this only rules out the structurally
    impossible.

    Raises:
        InvalidHcl: The expression is empty, overlong, has an unterminated string
            or heredoc, or has unbalanced brackets.
    """
    if len(value) > MAX_HCL_LENGTH:
        raise InvalidHcl("The HCL expression is too long.")
    if not value.strip():
        raise InvalidHcl("An HCL expression cannot be empty.")

    stack: list[str] = []
    index = 0
    length = len(value)
    has_expression = False
    while index < length:
        character = value[index]

        if character == "#" or value.startswith("//", index):
            newline = value.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            if end == -1:
                raise InvalidHcl("The HCL expression has an unterminated comment.")
            index = end + 2
            continue

        if not character.isspace():
            has_expression = True
        if (
            character == "="
            and not stack
            and (index == 0 or value[index - 1] not in "=!<>")
            and not value.startswith("==", index)
        ):
            raise InvalidHcl("An HCL value must be one expression, not an assignment.")

        heredoc = _HEREDOC_START.match(value, index)
        if heredoc is not None:
            index = _skip_heredoc(value, heredoc.end(), heredoc.group(1))
            continue

        if character == '"':
            index = _skip_quoted(value, index + 1)
            continue

        if character in _OPENERS:
            stack.append(character)
        elif character in _CLOSERS:
            if not stack or stack[-1] != _CLOSERS[character]:
                raise InvalidHcl("The HCL expression has unbalanced brackets.")
            stack.pop()
        index += 1

    if stack:
        raise InvalidHcl("The HCL expression has unbalanced brackets.")
    if not has_expression:
        raise InvalidHcl("An HCL expression cannot contain only comments.")


def _skip_quoted(value: str, index: int, depth: int = 0) -> int:
    """The index just past a quoted string that began before `index`.

    A `${` interpolation inside the string is stepped over as a nested scan, so a
    quote inside it cannot be read as the string's own closing quote.
    """
    if depth >= MAX_TEMPLATE_DEPTH:
        raise InvalidHcl("The HCL expression has too many nested templates.")
    length = len(value)
    while index < length:
        character = value[index]
        if character == "\\":
            index += 2
            continue
        if value.startswith("$${", index) or value.startswith("%%{", index):
            index += 3
            continue
        if value.startswith("${", index) or value.startswith("%{", index):
            index = _skip_interpolation(value, index + 2, depth + 1)
            continue
        if character == '"':
            return index + 1
        index += 1
    raise InvalidHcl("The HCL expression has an unterminated string.")


def _skip_interpolation(value: str, index: int, template_depth: int) -> int:
    """The index just past a `${...}` or `%{...}` block that began before `index`."""
    length = len(value)
    depth = 1
    while index < length:
        character = value[index]
        if character == "#" or value.startswith("//", index):
            newline = value.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue
        if value.startswith("/*", index):
            end = value.find("*/", index + 2)
            if end == -1:
                raise InvalidHcl("The HCL expression has an unterminated comment.")
            index = end + 2
            continue
        heredoc = _HEREDOC_START.match(value, index)
        if heredoc is not None:
            index = _skip_heredoc(value, heredoc.end(), heredoc.group(1))
            continue
        if character == '"':
            index = _skip_quoted(value, index + 1, template_depth)
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise InvalidHcl("The HCL expression has an unterminated interpolation.")


def _skip_heredoc(value: str, index: int, marker: str) -> int:
    """The index just past a heredoc body closed by `marker` on its own line."""
    cursor = index
    length = len(value)
    while cursor <= length:
        end = value.find("\n", cursor)
        line = value[cursor:] if end == -1 else value[cursor:end]
        if line.strip() == marker:
            return length if end == -1 else end + 1
        if end == -1:
            break
        cursor = end + 1
    raise InvalidHcl("The HCL expression has an unterminated heredoc.")


__all__ = ["MAX_HCL_LENGTH", "InvalidHcl", "validate", "validate_name"]
