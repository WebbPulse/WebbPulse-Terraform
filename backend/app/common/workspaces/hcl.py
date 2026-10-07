"""Write time checking of an HCL variable value, and how it reaches the engine.

No HCL parser is installed in this environment and none is added for this: the
backend ships as a Lambda image and a parser would be a dependency carried by
every request path to serve one write route. What is checked here instead is the
structure a broken expression breaks first: an unterminated string, heredoc,
comment or interpolation, and unbalanced brackets or braces. An expression that
passes still has to mean something to the engine, so the engine remains the
authority and a run is where a semantically wrong expression fails.

The runner writes each value as `key = (\\n<value>\\n)` into a native tfvars file,
so the check is also the boundary that keeps a value inside its own assignment.
That only holds if this scan finds the same string, heredoc, comment and
interpolation boundaries the engine's scanner does, so each rule below mirrors
`hclsyntax/scan_tokens.rl` in hashicorp/hcl v2. Where the two could still differ,
the scan errs towards seeing a heredoc the engine would not, which the engine
then rejects as two `<` operators rather than parsing differently. A value
whose brackets balance outside every string, heredoc and comment cannot close
the wrapping parenthesis, and a top level `=` is refused as well.
"""

from __future__ import annotations

import re
from typing import Final

MAX_HCL_LENGTH: Final = 32_768
"""The same ceiling the value field carries, restated so a direct caller of the
validator cannot hand it something unbounded to scan."""

MAX_TEMPLATE_DEPTH: Final = 64
"""How deeply strings, heredocs and interpolations may nest before the value is
refused, which keeps the recursive scan inside the interpreter's stack."""

_OPENERS: Final = {"(": ")", "[": "]", "{": "}"}
_CLOSERS: Final = {")": "(", "]": "[", "}": "{"}

_HEREDOC_START: Final = re.compile(r"<<-?([^\r\n<]+)\r?\n")
"""A heredoc opener. The engine's marker is an identifier; this accepts any run
up to the line end that holds no `<`, a superset that never starts a heredoc
later in the line than the engine would."""

_GO_SPACE: Final = "\t\n\v\f\r \x85\xa0                　"
"""Exactly the characters Go's `bytes.TrimSpace` removes, which is how the engine
compares a heredoc line to its marker. Python's `str.strip()` also removes
U+001C to U+001F, which would end a heredoc a line before the engine does."""

_VARIABLE_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")


class InvalidHcl(ValueError):
    """The value cannot be an HCL expression, whatever it would mean.

    The message is written for the person who typed the value and names nothing
    about the value itself, so it is safe to return from the API for a sensitive
    variable as well as a plain one.
    """


def validate_name(key: str) -> None:
    """Refuse a key that cannot be written unquoted as a native tfvars attribute name."""
    if not _VARIABLE_NAME.fullmatch(key):
        raise InvalidHcl("An HCL variable name must contain only letters, digits, underscores or hyphens.")


def validate(value: str) -> None:
    """Refuse a value that cannot parse as a single HCL expression.

    Scans once, stepping over strings, heredocs and comments the way the engine's
    scanner does and matching brackets outside them. Terraform's own parse is
    still the authority on meaning; this only rules out the structurally
    impossible and anything that could end its own assignment.

    Raises:
        InvalidHcl: The expression is empty, overlong, has an unterminated string,
            heredoc, comment or interpolation, has unbalanced brackets, nests too
            deeply, or holds a top level `=`.
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

        comment_end = _skip_comment(value, index)
        if comment_end is not None:
            index = comment_end
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
            index = _skip_heredoc(value, heredoc.end(), heredoc.group(1), 1)
            continue

        if character == '"':
            index = _skip_quoted(value, index + 1, 1)
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


def _check_depth(depth: int) -> None:
    """Refuse nesting deep enough to threaten the interpreter's stack."""
    if depth > MAX_TEMPLATE_DEPTH:
        raise InvalidHcl("The HCL expression has too many nested templates.")


def _skip_comment(value: str, index: int) -> int | None:
    """The index just past a comment starting at `index`, or `None` if none does.

    A `#` or `//` comment runs to the end of its line, newline included, and a
    `/*` comment to the first `*/`, which do not nest.
    """
    if value[index] == "#" or value.startswith("//", index):
        newline = value.find("\n", index)
        return len(value) if newline == -1 else newline + 1
    if value.startswith("/*", index):
        end = value.find("*/", index + 2)
        if end == -1:
            raise InvalidHcl("The HCL expression has an unterminated comment.")
        return end + 2
    return None


def _skip_quoted(value: str, index: int, depth: int) -> int:
    """The index just past a quoted string whose opening quote is before `index`.

    A backslash takes the next character with it, `$${` and `%%{` are literal,
    and a `${` or `%{` is stepped over as a nested scan so a quote inside it is
    not read as the string's own closing quote. A line break outside an
    interpolation ends the string with an error, as it does for the engine.
    """
    _check_depth(depth)
    length = len(value)
    while index < length:
        character = value[index]
        if character in "\r\n":
            raise InvalidHcl("A quoted HCL string cannot span lines. Use a heredoc instead.")
        if character == "\\":
            if index + 1 >= length or value[index + 1] in "\r\n":
                raise InvalidHcl("The HCL expression has an unterminated string.")
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


def _skip_interpolation(value: str, index: int, depth: int) -> int:
    """The index just past a `${...}` or `%{...}` whose opener is before `index`.

    The body is an expression, so it may hold comments, strings, heredocs and
    object braces of its own; only the brace that balances the opener ends it.
    """
    _check_depth(depth)
    length = len(value)
    braces = 1
    while index < length:
        character = value[index]
        comment_end = _skip_comment(value, index)
        if comment_end is not None:
            index = comment_end
            continue
        heredoc = _HEREDOC_START.match(value, index)
        if heredoc is not None:
            index = _skip_heredoc(value, heredoc.end(), heredoc.group(1), depth + 1)
            continue
        if character == '"':
            index = _skip_quoted(value, index + 1, depth + 1)
            continue
        if character == "{":
            braces += 1
        elif character == "}":
            braces -= 1
            if braces == 0:
                return index + 1
        index += 1
    raise InvalidHcl("The HCL expression has an unterminated interpolation.")


def _skip_heredoc(value: str, index: int, marker: str, depth: int) -> int:
    """The index just past a heredoc whose body starts at `index`.

    The body is a template: backslashes are literal, `$${` and `%%{` are
    literal, and a `${` or `%{` is a nested scan that may span lines. A line
    closes the heredoc only when no interpolation started on it and, trimmed of
    Go's whitespace, it is exactly the marker, which is the engine's rule.
    """
    _check_depth(depth)
    length = len(value)
    line_start = index
    at_line_start = True
    while index < length:
        if value.startswith("$${", index) or value.startswith("%%{", index):
            index += 3
            continue
        if value.startswith("${", index) or value.startswith("%{", index):
            at_line_start = False
            index = _skip_interpolation(value, index + 2, depth + 1)
            continue
        if value[index] == "\n":
            if at_line_start and value[line_start:index].strip(_GO_SPACE) == marker:
                return index + 1
            at_line_start = True
            line_start = index + 1
        index += 1
    if at_line_start and value[line_start:].strip(_GO_SPACE) == marker:
        return length
    raise InvalidHcl("The HCL expression has an unterminated heredoc.")


__all__ = ["MAX_HCL_LENGTH", "MAX_TEMPLATE_DEPTH", "InvalidHcl", "validate", "validate_name"]
