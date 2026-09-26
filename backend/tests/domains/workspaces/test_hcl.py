"""The write time HCL check: what it accepts and what it refuses.

It is a structural check rather than a parse, so the bar is that everything a
person would reasonably write is accepted and the shapes that cannot parse at all
are refused. Accepting something the engine later rejects is the tolerated
direction; refusing something valid is not.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.domains.workspaces import hcl

VALID: list[str] = [
    "1",
    "-2.5e3",
    "true",
    "false",
    "null",
    "[]",
    "{}",
    '["a", "b"]',
    '{ env = "staging", size = 2 }',
    '{\n  env  = "staging"\n  size = 2\n}',
    '{"quoted key" = 1, colon: 2}',
    '["a", ["b", "c"], { d = "e" }]',
    "[\n  1,\n  2,\n]",
    '"a plain quoted string"',
    '"a brace } inside a string"',
    '"a bracket ] inside a string"',
    '"an escaped quote \\" inside"',
    '"escapes \\\\ \\n \\t \\u00e9 \\U0001F600"',
    '"${var.prefix}-suffix"',
    '"${ "}" }"',
    '"${ {a = 1}["a"] }"',
    '"${true /* } */}"',
    '"${true # }\n}"',
    '"${<<EOT\nin\nEOT\n}"',
    '"%{ if true }yes%{ endif }"',
    '"$${unfinished"',
    '"%%{unfinished"',
    '"$$${literal}"',
    "<<EOT\nline one }\nline two ]\nEOT",
    "<<EOT\nhello\nEOT\n",
    "<<-EOT\n  indented }\n  EOT",
    "<<END-TEXT\ntext\nEND-TEXT",
    "<<EOT\r\nwindows\r\nEOT\r\n",
    '<<EOT\n"unbalanced quote\nEOT',
    "<<EOT\na backslash \\\nEOT",
    '<<EOT\n${"a"}\nEOT',
    '<<EOT\n${""}EOT\nEOT',
    '<<EOT\n%{ for s in ["a", "b"] }${s}\n%{ endfor }\nEOT',
    "<<EOT\n$${x}\nEOT",
    "<<E\u0301T\nunicode marker\nE\u0301T",
    '# a leading comment\n["a"]',
    '["a"] // a trailing comment',
    '["a"] # a trailing comment',
    '/* a block comment */ ["a"]',
    "[1, # ]\n2]",
    "[1, /* ] */ 2]",
    "merge({ a = 1 }, { b = 2 })",
    'jsonencode({ a = "b" })',
    '[for s in ["a"] : upper(s)]',
    '{for k in ["a"] : k => 1}',
    "1 + 2 * 3",
    'true ? ["a"] : ["b"]',
    "true\n? 1\n: 2",
    "true == false",
    "1 != 2",
    "2 >= 1",
    "1 <= 2",
    "true && !false",
]
"""Every shape a tfvars value can take, including ones only a run can judge.

Function calls and references are here too: the engine refuses them in a tfvars
file, and accepting what the engine later rejects is the tolerated direction.
"""

INVALID: list[str] = [
    "",
    "   ",
    "# only a comment",
    "/* only a comment */",
    '["a", "b"',
    '{ env = "staging"',
    '["a"]]',
    "}",
    '["a", "b"}',
    '"unterminated',
    '["unterminated string"',
    '"a line\nbreak"',
    '"a backslash at the end\\',
    '"${unterminated"',
    "<<EOT\nno terminator here\n",
    "<<EOT\n${\nEOT\n",
    "/* unterminated comment",
]
"""Shapes that cannot parse, whatever they would mean."""

INJECTIONS: list[str] = [
    "true\nother = false",
    '1\nx = "pwned"',
    "1 = 2",
    "{a = 1}\nx = 2",
    '1)\nx = "pwned"\ny = (1',
    '<<EOT\nEOT\x1c\n"\nEOT\n)\nx = 55\ny = (1 #"',
    '{a = [<<EOT\n${[for\nEOT\nin ["z"] : EOT][0]}"\nEOT\n]}\n)\nx = 55\ny = (1 #"',
    '[1 < <<<EOT\n<EOT\n"\nEOT\n]\n)\nx = 55\ny = (1 #"',
]
"""Values that would end their own assignment in the runner's tfvars file.

The fifth to seventh were accepted by an earlier version of this scan and parsed
by terraform with `x = 55` as a second attribute: the first because Python's
`strip()` trims U+001C where Go's `TrimSpace` does not, the second because a
heredoc line inside an interpolation was taken for the closing marker.
"""


@pytest.mark.parametrize("expression", VALID)
def test_valid_expressions_are_accepted(expression: str) -> None:
    """Every shape a person would write for a list, map or scalar passes."""
    hcl.validate(expression)


@pytest.mark.parametrize("expression", INVALID)
def test_impossible_expressions_are_refused(expression: str) -> None:
    """A shape that cannot parse whatever it means is refused."""
    with pytest.raises(hcl.InvalidHcl):
        hcl.validate(expression)


@pytest.mark.parametrize("expression", INJECTIONS)
def test_a_value_cannot_start_another_assignment(expression: str) -> None:
    """Nothing that could close the wrapping parenthesis or add an attribute passes."""
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


def test_deep_templates_raise_a_validation_error() -> None:
    """Nested templates must not exhaust the API worker's call stack."""
    expression = '"${' * 600 + "true" + '}"' * 600
    with pytest.raises(hcl.InvalidHcl):
        hcl.validate(expression)


@pytest.mark.parametrize("name", ["subnets", "_private", "with-hyphen", "a1"])
def test_identifier_names_are_accepted(name: str) -> None:
    """A name the runner can write unquoted passes."""
    hcl.validate_name(name)


@pytest.mark.parametrize("name", ["dotted.name", "1digit", "has space", ""])
def test_names_that_cannot_be_written_unquoted_are_refused(name: str) -> None:
    """A name the runner would refuse to write is refused at the API instead."""
    with pytest.raises(hcl.InvalidHcl):
        hcl.validate_name(name)


@pytest.fixture(scope="module")
def terraform_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An initialised configuration with a target variable and a bystander `x`."""
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("terraform is not installed")
    directory = tmp_path_factory.mktemp("hcl")
    (directory / "main.tf").write_text(
        'variable "key" {\n  type = any\n}\n'
        'variable "x" {\n  type    = any\n  default = "safe"\n}\n'
        'variable "y" {\n  type    = any\n  default = "safe"\n}\n'
    )
    subprocess.run([terraform, "init", "-input=false"], cwd=directory, capture_output=True, check=True, timeout=120)
    return directory


def _terraform_reads(directory: Path, expression: str) -> object | None:
    """What terraform makes of `x` with the value written as the runner writes it."""
    (directory / "zz_webbpulse.auto.tfvars").write_text(f"key = (\n{expression}\n)\n")
    result = subprocess.run(
        ["terraform", "console"],
        input="jsonencode([var.x, var.key])\n",
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return None
    return json.loads(json.loads(result.stdout.strip()))[0]


@pytest.mark.parametrize("expression", INJECTIONS)
def test_every_refused_injection_is_one_terraform_would_have_honoured_or_refused(
    terraform_dir: Path, expression: str
) -> None:
    """The regression table is real: terraform either errors or sees `x` changed."""
    assert _terraform_reads(terraform_dir, expression) in (None, 55, "pwned", False, 2)


@pytest.mark.parametrize("expression", [value for value in VALID if "(" not in value and "var." not in value])
def test_terraform_never_sees_a_second_attribute_in_an_accepted_value(terraform_dir: Path, expression: str) -> None:
    """An accepted value leaves the bystander variable alone, or fails the run outright."""
    assert _terraform_reads(terraform_dir, expression) in (None, "safe")
