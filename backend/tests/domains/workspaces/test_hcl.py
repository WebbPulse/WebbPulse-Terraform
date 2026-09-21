"""The write time HCL check: what it accepts and what it refuses.

It is a structural check rather than a parse, so the bar is that everything a
person would reasonably write is accepted and the shapes that cannot parse at all
are refused. Accepting something the engine later rejects is the tolerated
direction; refusing something valid is not.
"""

import pytest

from app.domains.workspaces import hcl


@pytest.mark.parametrize(
    "expression",
    [
        '["a", "b"]',
        "[]",
        "{}",
        '{ env = "staging", size = 2 }',
        '{\n  env  = "staging"\n  size = 2\n}',
        "true",
        "42",
        '"a plain quoted string"',
        '["a", ["b", "c"], { d = "e" }]',
        'jsonencode({ a = "b" })',
        '"${var.prefix}-suffix"',
        '"a brace } inside a string"',
        '"a bracket ] inside a string"',
        '"an escaped quote \\" inside"',
        "<<EOT\nline one }\nline two ]\nEOT",
        "<<-EOT\n  indented }\n  EOT",
        '# a leading comment\n["a"]',
        '["a"] // a trailing comment',
        '/* a block comment */ ["a"]',
        '"%{ if true }yes%{ endif }"',
        "merge({ a = 1 }, { b = 2 })",
    ],
)
def test_valid_expressions_are_accepted(expression: str) -> None:
    """Every shape a person would write for a list, map or scalar passes."""
    hcl.validate(expression)


@pytest.mark.parametrize(
    "expression",
    [
        '["a", "b"',
        '{ env = "staging"',
        '["a"]]',
        "}",
        '"unterminated',
        '["unterminated string"',
        "<<EOT\nno terminator here\n",
        "/* unterminated comment",
        '["a", "b"}',
        "",
        "   ",
    ],
)
def test_impossible_expressions_are_refused(expression: str) -> None:
    """A shape that cannot parse whatever it means is refused."""
    with pytest.raises(hcl.InvalidHcl):
        hcl.validate(expression)


def test_an_overlong_expression_is_refused() -> None:
    """The scan is bounded, so nothing unbounded is scanned."""
    with pytest.raises(hcl.InvalidHcl):
        hcl.validate("[" + ("a," * hcl.MAX_HCL_LENGTH) + "]")


def test_the_message_never_carries_the_expression() -> None:
    """The refusal is safe to return for a sensitive value as well as a plain one."""
    with pytest.raises(hcl.InvalidHcl) as raised:
        hcl.validate('["super-secret-value"')
    assert "super-secret-value" not in str(raised.value)
