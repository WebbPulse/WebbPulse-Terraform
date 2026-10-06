"""The identity rate limiter reaches a table Terraform actually creates.

`webbpulse.ratelimit.RateLimiter` names its table from the `DYNAMODB_TABLE_PREFIX`
environment variable, not from this project's settings. Left unset, it targets a bare
`rate-limits` table that does not exist, every count fails open, and prod identity
routes go unlimited while the suite stays green, so the wiring is asserted here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from webbpulse.dynamodb import TABLE_PREFIX_ENV
from webbpulse.ratelimit import RATE_LIMIT_TABLE, RateLimiter

TERRAFORM = Path(__file__).resolve().parents[3] / "terraform"

PREFIX = "webbpulse-terraform-prod"


def test_the_limiter_names_its_table_from_the_stack_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the variable Terraform sets, the limiter targets `<prefix>-rate-limits`."""
    monkeypatch.setenv(TABLE_PREFIX_ENV, PREFIX)
    assert RateLimiter(region_name="us-west-2").table_name == f"{PREFIX}-{RATE_LIMIT_TABLE}"


def test_terraform_sets_the_prefix_variable_from_the_stack_prefix() -> None:
    """Every domain function's environment carries the prefix under the package's name."""
    source = (TERRAFORM / "lambda_domains.tf").read_text()
    assert re.search(rf"^\s*{TABLE_PREFIX_ENV}\s*=\s*local\.prefix\s*$", source, re.MULTILINE)


def test_terraform_creates_the_rate_limit_table() -> None:
    """The table exists under the package's logical name, keyed on `pk` with a TTL."""
    source = (TERRAFORM / "dynamodb.tf").read_text()
    block = re.search(rf'"{RATE_LIMIT_TABLE}"\s*=\s*\{{(.*?)\n    \}}', source, re.DOTALL)
    assert block is not None
    assert 'hash_key = "pk"' in block.group(1)
    assert 'ttl_attribute          = "expires_at"' in block.group(1)


def test_the_identity_function_may_write_the_rate_limit_table() -> None:
    """The workspaces function serves identity, so its write tables include the limiter's."""
    source = (TERRAFORM / "lambda_domains.tf").read_text()
    workspaces = re.search(r"workspaces = \{.*?tables\s*=\s*\[(.*?)\]", source, re.DOTALL)
    assert workspaces is not None
    assert f'"{RATE_LIMIT_TABLE}"' in workspaces.group(1)
