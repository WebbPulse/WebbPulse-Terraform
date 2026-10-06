"""Read only calls to the owned control plane, behind a token provider so no key is ever minted here."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from cutover.hcp import VariableShape

TokenProvider = Callable[[], str]
TERMINAL_RUN_STATUSES = frozenset({"applied", "planned_and_finished", "errored", "cancelled", "discarded"})
GATE_HEADER = "x-origin-verify"
CREDENTIALS_FILE = Path.home() / ".terraform.d" / "credentials.tfrc.json"


class PlaneError(Exception):
    """A plane call that failed or a token that could not be found."""


@dataclass(frozen=True)
class PlaneWorkspace:
    """The plane workspace fields preflight checks."""

    id: str
    name: str
    engine: str
    engine_version: str
    vcs_repo: str | None


class PlaneApi(Protocol):
    """What the commands call on the plane; a fake implements it in tests."""

    def workspace(self, workspace_id: str) -> PlaneWorkspace | None:
        """One workspace, or `None` if it does not exist."""
        ...

    def active_runs(self, workspace_id: str) -> list[str]:
        """Ids of the workspace's runs not yet in a terminal status."""
        ...

    def variables(self, workspace_id: str) -> list[VariableShape]:
        """The workspace's variables, values dropped."""
        ...


def _env_name(host: str) -> str:
    """The `TF_TOKEN_` suffix Terraform uses for a host."""
    return "TF_TOKEN_" + re.sub(r"[^A-Za-z0-9]", "_", host.replace("-", "__"))


def default_token_provider(host: str, credentials: Path = CREDENTIALS_FILE) -> TokenProvider:
    """Read an existing token the way wp-tf does: WP_TF_TOKEN, TF_TOKEN_<host>, then `terraform login`'s file."""

    def provide() -> str:
        """Look the token up at call time, so a dry run never reads it."""
        for name in ("WP_TF_TOKEN", _env_name(host)):
            value = os.environ.get(name)
            if value:
                return value
        try:
            token = json.loads(credentials.read_text())["credentials"][host]["token"]
        except (OSError, ValueError, KeyError, TypeError):
            token = None
        if not isinstance(token, str) or not token:
            raise PlaneError(f"no token for {host}: set WP_TF_TOKEN or run terraform login {host}")
        return token

    return provide


class HttpPlane:
    """`PlaneApi` over httpx, asking the provider for a token on every request."""

    def __init__(
        self,
        api_url: str,
        token_provider: TokenProvider,
        gate: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Bind the API root, token provider and optional access gate value."""
        self._base = api_url.rstrip("/") + "/api/v1"
        self._token = token_provider
        self._gate = gate if gate is not None else os.environ.get("WP_TF_GATE")
        self._http = httpx.Client(transport=transport, timeout=60, follow_redirects=False)

    def _get(self, path: str, params: dict[str, str] | None = None) -> httpx.Response:
        """One authenticated GET."""
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        if self._gate:
            headers[GATE_HEADER] = self._gate
        return self._http.get(f"{self._base}{path}", headers=headers, params=params)

    def _json(self, response: httpx.Response, what: str) -> dict[str, Any]:
        """The decoded body, or an error naming only the status."""
        if response.status_code >= 400:
            raise PlaneError(f"plane {what} answered {response.status_code}")
        return cast(dict[str, Any], response.json())

    def workspace(self, workspace_id: str) -> PlaneWorkspace | None:
        """GET /workspaces/{id}."""
        response = self._get(f"/workspaces/{workspace_id}")
        if response.status_code == 404:
            return None
        body = self._json(response, "workspace")
        return PlaneWorkspace(
            id=str(body.get("workspace_id") or workspace_id),
            name=str(body.get("name") or ""),
            engine=str(body.get("engine") or "terraform"),
            engine_version=str(body.get("engine_version") or ""),
            vcs_repo=cast(str | None, body.get("vcs_repo")),
        )

    def active_runs(self, workspace_id: str) -> list[str]:
        """GET /runs?workspace_id=, keeping non terminal runs."""
        body = self._json(self._get("/runs", {"workspace_id": workspace_id}), "runs")
        return [
            str(cast(dict[str, Any], run)["run_id"])
            for run in cast(list[Any], body.get("items") or [])
            if str(cast(dict[str, Any], run).get("status")) not in TERMINAL_RUN_STATUSES
        ]

    def variables(self, workspace_id: str) -> list[VariableShape]:
        """GET /workspaces/{id}/variables, discarding any value field."""
        body = self._json(self._get(f"/workspaces/{workspace_id}/variables"), "variables")
        shapes: list[VariableShape] = []
        for item in cast(list[Any], body.get("items") or []):
            variable = cast(dict[str, Any], item)
            shapes.append(
                VariableShape(
                    category=str(variable.get("category")),
                    key=str(variable.get("key")),
                    sensitive=bool(variable.get("sensitive")),
                    hcl=bool(variable.get("hcl")),
                    source="plane",
                )
            )
        return sorted(shapes)
