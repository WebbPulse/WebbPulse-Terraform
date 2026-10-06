"""The plane client over a mock transport and the lazy token provider."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cutover.hcp import VariableShape
from cutover.plane import HttpPlane, PlaneError, default_token_provider

from .fakes import ATTRIBUTE_SECRET, PLANE_WS

HOST = "staging.terraform.webbpulse.com"


def test_token_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """WP_TF_TOKEN wins, then TF_TOKEN_<host>, then the credentials file."""
    path = tmp_path / "credentials.tfrc.json"
    path.write_text(json.dumps({"credentials": {HOST: {"token": "from-file"}}}))
    monkeypatch.delenv("WP_TF_TOKEN", raising=False)
    monkeypatch.delenv("TF_TOKEN_staging_terraform_webbpulse_com", raising=False)
    provide = default_token_provider(HOST, path)
    assert provide() == "from-file"
    monkeypatch.setenv("TF_TOKEN_staging_terraform_webbpulse_com", "from-host-env")
    assert provide() == "from-host-env"
    monkeypatch.setenv("WP_TF_TOKEN", "from-wp")
    assert provide() == "from-wp"


def test_token_provider_is_lazy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Building a provider reads nothing; only calling it fails when no token exists."""
    monkeypatch.delenv("WP_TF_TOKEN", raising=False)
    monkeypatch.delenv("TF_TOKEN_staging_terraform_webbpulse_com", raising=False)
    provide = default_token_provider(HOST, tmp_path / "absent")
    with pytest.raises(PlaneError, match="no token"):
        provide()


def test_client_calls_provider_per_request_and_sends_gate() -> None:
    """The token is fetched for each call and the gate header is sent when set."""
    issued: list[int] = []
    seen: list[httpx.Headers] = []

    def provide() -> str:
        """Count calls."""
        issued.append(1)
        return "wpk_test"

    def handler(request: httpx.Request) -> httpx.Response:
        """A workspace with a VCS repo, or none."""
        seen.append(request.headers)
        if request.url.path.endswith("missing"):
            return httpx.Response(404)
        body = {
            "workspace_id": PLANE_WS,
            "name": "n",
            "engine": "terraform",
            "engine_version": "1.16.4",
            "vcs_repo": None,
        }
        return httpx.Response(200, json=body)

    plane = HttpPlane("https://api.example/", provide, gate="g", transport=httpx.MockTransport(handler))
    workspace = plane.workspace(PLANE_WS)
    assert workspace is not None and workspace.engine_version == "1.16.4"
    assert plane.workspace("missing") is None
    assert len(issued) == 2
    assert seen[0]["authorization"] == "Bearer wpk_test"
    assert seen[0]["x-origin-verify"] == "g"


def test_runs_and_variables() -> None:
    """Terminal runs are dropped and variable values are never kept."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Runs or variables."""
        if request.url.path.endswith("/runs"):
            assert request.url.params["workspace_id"] == PLANE_WS
            items = [{"run_id": "run-a", "status": "applied"}, {"run_id": "run-b", "status": "planning"}]
            return httpx.Response(200, json={"items": items})
        items = [{"key": "db", "value": ATTRIBUTE_SECRET, "category": "terraform", "sensitive": True, "hcl": False}]
        return httpx.Response(200, json={"items": items})

    plane = HttpPlane("https://api.example", lambda: "t", gate="", transport=httpx.MockTransport(handler))
    assert plane.active_runs(PLANE_WS) == ["run-b"]
    shapes = plane.variables(PLANE_WS)
    assert shapes == [VariableShape("terraform", "db", True, False, "plane")]
    assert ATTRIBUTE_SECRET not in repr(shapes)


def test_errors_name_the_status_only() -> None:
    """A failure never echoes the body."""
    plane = HttpPlane(
        "https://api.example",
        lambda: "t",
        gate="",
        transport=httpx.MockTransport(lambda request: httpx.Response(403, text=ATTRIBUTE_SECRET)),
    )
    with pytest.raises(PlaneError) as raised:
        plane.variables(PLANE_WS)
    assert "403" in str(raised.value) and ATTRIBUTE_SECRET not in str(raised.value)
