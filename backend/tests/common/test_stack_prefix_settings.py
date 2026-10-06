"""Settings derived from the stack prefix, which keep the function environment under 4KB."""

from __future__ import annotations

import pytest

from app.common.composition.settings import STACK_TABLE_FIELDS, Settings

PREFIX = "webbpulse-terraform-staging"


@pytest.fixture
def prefix_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment that names the stack prefix and no table or run role prefix."""
    for field in (*STACK_TABLE_FIELDS, "RUN_ROLE_NAME_PREFIX"):
        monkeypatch.delenv(field, raising=False)
    monkeypatch.setenv("IDENTITY_TABLE_PREFIX", PREFIX)


def test_table_names_derive_from_the_stack_prefix(prefix_only: None) -> None:
    settings = Settings()
    assert settings.WORKSPACES_TABLE == f"{PREFIX}-workspaces"
    assert settings.CONFIG_VERSIONS_TABLE == f"{PREFIX}-config-versions"
    assert settings.VCS_UPLOADS_TABLE == f"{PREFIX}-vcs-uploads"
    assert {getattr(settings, field) for field in STACK_TABLE_FIELDS} == {
        f"{PREFIX}-{logical_name}" for logical_name in STACK_TABLE_FIELDS.values()
    }


def test_the_run_role_prefix_derives_from_the_stack_prefix(prefix_only: None) -> None:
    assert Settings().RUN_ROLE_NAME_PREFIX == f"{PREFIX}-workspace-"


def test_an_explicit_table_name_wins(prefix_only: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNS_TABLE", "custom-runs")
    assert Settings().RUNS_TABLE == "custom-runs"


def test_no_stack_prefix_derives_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("IDENTITY_TABLE_PREFIX", raising=False)
    monkeypatch.delenv("VCS_UPLOADS_TABLE", raising=False)
    assert Settings().VCS_UPLOADS_TABLE == ""
