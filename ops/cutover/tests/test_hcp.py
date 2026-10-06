"""The HCP client over a mock transport: auth scope, paging and value dropping."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from cutover.hcp import HcpClient, HcpError, HcpStateVersion, VariableShape, load_hcp_token

from .fakes import HCP_TOKEN, HCP_WS, OUTPUT_SECRET, make_state

Handler = Callable[[httpx.Request], httpx.Response]


def client(handler: Handler) -> HcpClient:
    """A client whose every request goes to `handler`."""
    return HcpClient(HCP_TOKEN, transport=httpx.MockTransport(handler))


def test_token_loading(tmp_path: Path) -> None:
    """The app.terraform.io token is read; a missing one names the file, not a value."""
    path = tmp_path / "credentials.tfrc.json"
    path.write_text(json.dumps({"credentials": {"app.terraform.io": {"token": HCP_TOKEN}}}))
    assert load_hcp_token(path) == HCP_TOKEN
    path.write_text(json.dumps({"credentials": {"other.host": {"token": HCP_TOKEN}}}))
    with pytest.raises(HcpError) as raised:
        load_hcp_token(path)
    assert HCP_TOKEN not in str(raised.value)
    with pytest.raises(HcpError):
        load_hcp_token(tmp_path / "absent")


def test_workspace_fields() -> None:
    """Organization and lock holder come from relationships."""

    def handler(request: httpx.Request) -> httpx.Response:
        """One workspace document."""
        assert request.headers["authorization"] == f"Bearer {HCP_TOKEN}"
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": HCP_WS,
                    "attributes": {
                        "name": "WebbPulse-Terraform-staging",
                        "locked": True,
                        "terraform-version": "~> 1.10",
                    },
                    "relationships": {
                        "organization": {"data": {"id": "WebbPulse", "type": "organizations"}},
                        "locked-by": {"data": {"id": "user-me", "type": "users"}},
                    },
                }
            },
        )

    workspace = client(handler).workspace(HCP_WS)
    assert (workspace.organization, workspace.locked, workspace.locked_by) == ("WebbPulse", True, "user-me")


def test_download_never_sends_the_token_off_host() -> None:
    """A redirect to blob storage gets no Authorization header."""
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Redirect from HCP to a storage host."""
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "app.terraform.io":
            return httpx.Response(307, headers={"location": "https://archivist.terraform.io/v1/object/abc"})
        return httpx.Response(200, content=make_state())

    version = HcpStateVersion("sv-1", 70, "1.16.4", "https://app.terraform.io/api/v2/state-versions/sv-1/download")
    assert client(handler).download_state(version) == make_state()
    assert seen == [("app.terraform.io", f"Bearer {HCP_TOKEN}"), ("archivist.terraform.io", None)]


def test_download_redirect_loop_is_bounded() -> None:
    """Endless redirects fail."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Always redirect."""
        return httpx.Response(302, headers={"location": "https://archivist.terraform.io/again"})

    version = HcpStateVersion("sv-1", 70, "1.16.4", "https://archivist.terraform.io/start")
    with pytest.raises(HcpError, match="too many"):
        client(handler).download_state(version)


def test_errors_never_carry_the_body() -> None:
    """A failed call names the status only."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Fail with a body holding a secret."""
        return httpx.Response(500, text=OUTPUT_SECRET)

    with pytest.raises(HcpError) as raised:
        client(handler).workspace(HCP_WS)
    assert "500" in str(raised.value)
    assert OUTPUT_SECRET not in str(raised.value)
    version = HcpStateVersion("sv-1", 70, "1.16.4", "https://archivist.terraform.io/x")
    with pytest.raises(HcpError) as raised:
        client(handler).download_state(version)
    assert OUTPUT_SECRET not in str(raised.value)


def test_current_state_version_and_absence() -> None:
    """The state version's serial and engine are read; a 404 is no state."""
    present = {
        "data": {
            "id": "sv-9",
            "attributes": {"serial": 70, "terraform-version": "1.16.4", "hosted-state-download-url": "https://x/y"},
        }
    }
    found = client(lambda request: httpx.Response(200, json=present)).current_state_version(HCP_WS)
    assert found == HcpStateVersion("sv-9", 70, "1.16.4", "https://x/y")
    assert client(lambda request: httpx.Response(404)).current_state_version(HCP_WS) is None


def test_active_runs_keep_only_unfinished() -> None:
    """Final statuses are filtered out."""
    runs = {
        "data": [
            {"id": "run-1", "attributes": {"status": "applied"}},
            {"id": "run-2", "attributes": {"status": "planning"}},
            {"id": "run-3", "attributes": {"status": "force_canceled"}},
            {"id": "run-4", "attributes": {"status": "policy_checked"}},
        ]
    }
    assert client(lambda request: httpx.Response(200, json=runs)).active_runs(HCP_WS) == ["run-2", "run-4"]


def test_lock_sends_the_reason() -> None:
    """The lock body carries the reason."""
    bodies: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the body."""
        bodies.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": {}})

    client(handler).lock(HCP_WS, "TF-26: cutover to the owned control plane")
    assert bodies == [
        (f"/api/v2/workspaces/{HCP_WS}/actions/lock", {"reason": "TF-26: cutover to the owned control plane"})
    ]


def test_variables_drop_values_and_prefer_workspace() -> None:
    """Values are discarded, workspace variables beat varset ones and other categories are skipped."""

    def var(key: str, category: str, value: str, sensitive: bool = False) -> dict[str, Any]:
        """One variable resource."""
        return {
            "id": f"var-{key}",
            "attributes": {"key": key, "value": value, "category": category, "sensitive": sensitive, "hcl": False},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        """Two varset pages, one varset's vars and the workspace's vars."""
        path = request.url.path
        if path.endswith("/varsets"):
            page = request.url.params["page[number]"]
            if page == "1":
                data = [{"id": "varset-1", "attributes": {"name": "aws"}}]
                return httpx.Response(200, json={"data": data, "meta": {"pagination": {"next-page": 2}}})
            return httpx.Response(200, json={"data": [], "meta": {"pagination": {"next-page": None}}})
        if path.endswith("/relationships/vars"):
            data = [var("region", "env", "us-west-2"), var("shared", "terraform", OUTPUT_SECRET, True)]
            return httpx.Response(200, json={"data": data})
        data = [var("shared", "terraform", OUTPUT_SECRET), var("policy", "policy-set", "x")]
        return httpx.Response(200, json={"data": data})

    shapes = client(handler).variables(HCP_WS)
    assert shapes == [
        VariableShape("env", "region", False, False, "varset:aws"),
        VariableShape("terraform", "shared", False, False, "workspace"),
    ]
    assert OUTPUT_SECRET not in repr(shapes)
