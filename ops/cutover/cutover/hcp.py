"""The HCP Terraform API calls the cutover needs, with the token read locally and never logged."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urljoin, urlsplit

import httpx

from cutover.config import HCP_API, HCP_HOST

CREDENTIALS_FILE = Path.home() / ".terraform.d" / "credentials.tfrc.json"
FINAL_RUN_STATUSES = frozenset(
    {"applied", "planned_and_finished", "planned_and_saved", "discarded", "errored", "canceled", "force_canceled"}
)
MAX_REDIRECTS = 3
PAGE_SIZE = 100


class HcpError(Exception):
    """An HCP API call that failed; the message never carries a body that could hold state."""


@dataclass(frozen=True, order=True)
class VariableShape:
    """A variable without its value: the only facts compare-vars ever prints."""

    category: str
    key: str
    sensitive: bool
    hcl: bool
    source: str


@dataclass(frozen=True)
class HcpWorkspace:
    """The workspace fields the cutover reads."""

    id: str
    name: str
    organization: str
    locked: bool
    locked_by: str | None
    terraform_version: str


@dataclass(frozen=True)
class HcpStateVersion:
    """The current state version's metadata; lineage is only in the body."""

    id: str
    serial: int
    terraform_version: str
    download_url: str


class HcpApi(Protocol):
    """What the commands call on HCP; a fake implements it in tests."""

    def workspace(self, workspace_id: str) -> HcpWorkspace:
        """One workspace."""
        ...

    def account_id(self) -> str:
        """The user id the token belongs to, to tell our lock from someone else's."""
        ...

    def active_runs(self, workspace_id: str) -> list[str]:
        """Ids of runs not yet in a final status."""
        ...

    def current_state_version(self, workspace_id: str) -> HcpStateVersion | None:
        """The current state version, or `None` for a workspace with no state."""
        ...

    def download_state(self, version: HcpStateVersion) -> bytes:
        """The state body of one version."""
        ...

    def lock(self, workspace_id: str, reason: str) -> None:
        """Lock the workspace with a reason."""
        ...

    def unlock(self, workspace_id: str) -> None:
        """Unlock a workspace this token locked."""
        ...

    def variables(self, workspace_id: str) -> list[VariableShape]:
        """Workspace and variable set variables, values dropped."""
        ...


def load_hcp_token(path: Path = CREDENTIALS_FILE) -> str:
    """The app.terraform.io token from the Terraform CLI credentials file."""
    try:
        document = json.loads(path.read_text())
        token = document["credentials"][HCP_HOST]["token"]
    except (OSError, ValueError, KeyError, TypeError):
        raise HcpError(f"no {HCP_HOST} token in {path}; run terraform login") from None
    if not isinstance(token, str) or not token:
        raise HcpError(f"no {HCP_HOST} token in {path}; run terraform login")
    return token


def _attributes(resource: Any) -> dict[str, Any]:
    """A JSON:API resource's attributes."""
    return cast(dict[str, Any], cast(dict[str, Any], resource).get("attributes") or {})


def _related_id(resource: Any, name: str) -> str | None:
    """The id of a to-one relationship, if set."""
    relationships = cast(dict[str, Any], cast(dict[str, Any], resource).get("relationships") or {})
    data = cast(dict[str, Any], relationships.get(name) or {}).get("data")
    if isinstance(data, dict):
        related = cast(dict[str, Any], data).get("id")
        return str(related) if related else None
    return None


class HcpClient:
    """`HcpApi` over httpx; the token is only ever sent to app.terraform.io."""

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, base: str = HCP_API) -> None:
        """Bind the token; nothing is called until a method is."""
        self._token = token
        self._base = base.rstrip("/")
        self._http = httpx.Client(transport=transport, timeout=60, follow_redirects=False)

    def _headers(self, url: str) -> dict[str, str]:
        """Auth for the HCP host only, so a redirect to blob storage never receives the token."""
        headers = {"Content-Type": "application/vnd.api+json"}
        if urlsplit(url).hostname == HCP_HOST:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _request(self, method: str, path: str, body: Any = None, params: dict[str, str] | None = None) -> Any:
        """One API call returning the decoded document, or `None` for an empty answer."""
        url = path if path.startswith("https://") else f"{self._base}{path}"
        response = self._http.request(
            method, url, headers=self._headers(url), params=params, content=None if body is None else json.dumps(body)
        )
        if response.status_code >= 400:
            raise HcpError(f"HCP {method} {urlsplit(url).path} answered {response.status_code}")
        if not response.content:
            return None
        return response.json()

    def _pages(self, path: str, params: dict[str, str] | None = None) -> Iterator[Any]:
        """Every resource across a paginated listing."""
        page = 1
        while True:
            query = {"page[size]": str(PAGE_SIZE), "page[number]": str(page), **(params or {})}
            document = cast(dict[str, Any], self._request("GET", path, params=query) or {})
            yield from cast(list[Any], document.get("data") or [])
            pagination = cast(dict[str, Any], cast(dict[str, Any], document.get("meta") or {}).get("pagination") or {})
            next_page = pagination.get("next-page")
            if not next_page:
                return
            page = int(next_page)

    def workspace(self, workspace_id: str) -> HcpWorkspace:
        """GET /workspaces/:id."""
        data = cast(dict[str, Any], self._request("GET", f"/workspaces/{workspace_id}"))["data"]
        attributes = _attributes(data)
        return HcpWorkspace(
            id=str(data["id"]),
            name=str(attributes.get("name")),
            organization=_related_id(data, "organization") or "",
            locked=bool(attributes.get("locked")),
            locked_by=_related_id(data, "locked-by"),
            terraform_version=str(attributes.get("terraform-version") or ""),
        )

    def account_id(self) -> str:
        """GET /account/details."""
        return str(cast(dict[str, Any], self._request("GET", "/account/details"))["data"]["id"])

    def active_runs(self, workspace_id: str) -> list[str]:
        """Runs, newest first, keeping those not in a final status."""
        document = cast(
            dict[str, Any],
            self._request("GET", f"/workspaces/{workspace_id}/runs", params={"page[size]": str(PAGE_SIZE)}),
        )
        active: list[str] = []
        for run in cast(list[Any], document.get("data") or []):
            if str(_attributes(run).get("status")) not in FINAL_RUN_STATUSES:
                active.append(str(cast(dict[str, Any], run)["id"]))
        return active

    def current_state_version(self, workspace_id: str) -> HcpStateVersion | None:
        """GET /workspaces/:id/current-state-version, `None` on a 404."""
        url = f"{self._base}/workspaces/{workspace_id}/current-state-version"
        response = self._http.get(url, headers=self._headers(url))
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise HcpError(f"HCP current state version answered {response.status_code}")
        data = cast(dict[str, Any], response.json())["data"]
        attributes = _attributes(data)
        url = str(attributes.get("hosted-state-download-url") or "")
        if not url:
            raise HcpError("current state version has no download url")
        return HcpStateVersion(
            id=str(data["id"]),
            serial=int(attributes["serial"]),
            terraform_version=str(attributes.get("terraform-version") or ""),
            download_url=url,
        )

    def download_state(self, version: HcpStateVersion) -> bytes:
        """Follow at most a few redirects by hand, re-deciding auth for each host."""
        url = version.download_url
        for _ in range(MAX_REDIRECTS + 1):
            response = self._http.get(url, headers=self._headers(url))
            if response.is_redirect:
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code >= 400:
                raise HcpError(f"state download answered {response.status_code}")
            return response.content
        raise HcpError("state download redirected too many times")

    def lock(self, workspace_id: str, reason: str) -> None:
        """POST /workspaces/:id/actions/lock."""
        self._request("POST", f"/workspaces/{workspace_id}/actions/lock", body={"reason": reason})

    def unlock(self, workspace_id: str) -> None:
        """POST /workspaces/:id/actions/unlock."""
        self._request("POST", f"/workspaces/{workspace_id}/actions/unlock")

    def variables(self, workspace_id: str) -> list[VariableShape]:
        """Workspace variables over variable set ones, keyed by category and key, values discarded on read."""
        shapes: dict[tuple[str, str], VariableShape] = {}
        for varset in self._pages(f"/workspaces/{workspace_id}/varsets"):
            varset_id = str(cast(dict[str, Any], varset)["id"])
            name = str(_attributes(varset).get("name") or varset_id)
            for variable in self._pages(f"/varsets/{varset_id}/relationships/vars"):
                shape = _shape(variable, f"varset:{name}")
                if shape is not None:
                    shapes[(shape.category, shape.key)] = shape
        document = cast(dict[str, Any], self._request("GET", f"/workspaces/{workspace_id}/vars") or {})
        for variable in cast(list[Any], document.get("data") or []):
            shape = _shape(variable, "workspace")
            if shape is not None:
                shapes[(shape.category, shape.key)] = shape
        return sorted(shapes.values())


def _shape(variable: Any, source: str) -> VariableShape | None:
    """A variable's shape, or `None` for a category the plane has no equivalent of."""
    attributes = _attributes(variable)
    category = str(attributes.get("category"))
    if category not in ("terraform", "env"):
        return None
    return VariableShape(
        category=category,
        key=str(attributes.get("key")),
        sensitive=bool(attributes.get("sensitive")),
        hcl=bool(attributes.get("hcl")),
        source=source,
    )
