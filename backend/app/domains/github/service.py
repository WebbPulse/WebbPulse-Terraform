"""The GitHub App's lifecycle: create it from a manifest, install it, list what it sees.

One App per environment. The manifest flow creates it: a one-time state goes to GitHub
with the manifest, GitHub redirects back with a code, and the conversion writes the
credentials into the `app` secret and the non-secret facts into this domain's table.
Installing works the same way, with a state that GitHub echoes on the setup redirect.

Three row kinds share the table. `app/app` holds the App's slug, id and links.
`state/<value>` is a one-time state, deleted as it is used. `installation/<id>` is one
installation, stored only once GitHub has confirmed it belongs to this App.
"""

from __future__ import annotations

import logging
import secrets
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import quote, urlencode

import boto3
from boto3.dynamodb.conditions import Key
from webbpulse.dynamodb import Repository
from webbpulse.integrations.github import (
    AppInstallation,
    GitHubAppClient,
    GitHubAppSettings,
    GitHubNotConfigured,
    GitHubNotFound,
    convert_manifest_code,
)
from webbpulse.ops.config import SecretStore

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.github.loader import github_app_settings, invalidate

if TYPE_CHECKING:  # pragma: no cover
    import httpx

logger = logging.getLogger(__name__)

MANIFEST: Final = "manifest"
INSTALL: Final = "install"
STATE_TTL_SECONDS: Final = {MANIFEST: 1800, INSTALL: 600}
"""How long each state lives: the manifest form can sit open while a name is chosen,
an install redirect is quicker."""

APP_KEY: Final = {"pk": "app", "sk": "app"}
STATE_PK: Final = "state"
INSTALLATION_PK: Final = "installation"

CREATED_PATH: Final = "/settings/github/created"
"""The SPA route GitHub redirects to after creating the App, the manifest `redirect_url`."""
SETUP_PATH: Final = "/settings/github/setup"
"""The SPA route GitHub redirects to after an install, the manifest `setup_url`."""

LOGO_PATH: Final = "/github-app-logo.png"
"""The App logo the SPA serves, for the manual upload GitHub has no API for."""
BADGE_BACKGROUND: Final = "#4d9fff"
"""The badge background: the dark theme's accent, which is the mark's own fill."""

PERMISSIONS: Final = {
    "metadata": "read",
    "contents": "read",
    "checks": "write",
    "statuses": "write",
    "pull_requests": "write",
}
"""What the App asks for: read a repository's code, report runs back on commits and PRs."""


class AppAlreadyConfigured(Exception):
    """The environment already has its one App."""


class InvalidState(Exception):
    """A state that is missing, used, expired or issued for another step."""


class FrontendUrlMissing(Exception):
    """No SPA origin is configured, so there is nowhere for GitHub to redirect back to."""


class SlugMissing(Exception):
    """The App is configured but its slug is unknown, so there is no install URL."""


class InstallationNotFound(Exception):
    """No stored installation carries that id."""


def http_client() -> httpx.Client | None:
    """The HTTP client GitHub calls go through; `None` lets the client build its own.

    The seam the tests replace with a mock transport.
    """
    return None


def _table(settings: Settings | None = None) -> Repository:
    """This domain's table."""
    return repositories.github(settings)


def _now() -> datetime:
    """The current time, as stored on rows."""
    return datetime.now(UTC)


def _frontend_base(settings: Settings) -> str:
    """The SPA origin, without a trailing slash."""
    base = settings.IDENTITY_FRONTEND_BASE_URL.strip().rstrip("/")
    if not base:
        raise FrontendUrlMissing
    return base


def _settings_or_none(settings: Settings) -> GitHubAppSettings | None:
    """The App credentials, or `None` while the App is not configured."""
    try:
        return github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)
    except GitHubNotConfigured:
        return None


def _app_settings(settings: Settings) -> GitHubAppSettings:
    """The App credentials, raising `GitHubNotConfigured` while there are none."""
    return github_app_settings(settings.app_secret_arn, region_name=settings.AWS_REGION_NAME)


def _app_client(settings: Settings) -> GitHubAppClient:
    """A client for this environment's App, built from the cached credentials."""
    return GitHubAppClient.from_settings(_app_settings(settings), client=http_client())


def app_status(settings: Settings | None = None) -> dict[str, Any]:
    """Whether the App exists, what it is called, and what the page may offer next."""
    resolved = settings or get_settings()
    configured = _settings_or_none(resolved)
    row = _table(resolved).get(APP_KEY) or {}
    slug = str(row.get("slug") or resolved.GITHUB_APP_SLUG or "") or None
    app_id = row.get("app_id")
    if app_id is None and configured is not None:
        app_id = configured.app_id
    html_url = row.get("html_url") or (f"https://github.com/apps/{slug}" if slug else None)
    return {
        "configured": configured is not None,
        "slug": slug,
        "app_id": str(app_id) if app_id is not None else None,
        "name": row.get("name"),
        "html_url": html_url,
        "owner_login": row.get("owner_login"),
        "settings_url": _settings_url(row, slug),
        "created_at": row.get("created_at"),
        "can_create": configured is None,
        "can_install": configured is not None and slug is not None,
        "logo_path": LOGO_PATH,
        "badge_background": BADGE_BACKGROUND,
    }


def _settings_url(row: dict[str, Any], slug: str | None) -> str | None:
    """The App's settings page on GitHub, under the organization or the user that owns it."""
    if not slug:
        return None
    owner = row.get("owner_login")
    if row.get("owner_type") == "Organization" and owner:
        return f"https://github.com/organizations/{quote(str(owner))}/settings/apps/{quote(slug)}"
    return f"https://github.com/settings/apps/{quote(slug)}"


def _issue_state(
    purpose: str,
    actor: str | None,
    settings: Settings,
    *,
    organization: str | None = None,
) -> str:
    """Store and return a fresh one-time state for `purpose`."""
    value = secrets.token_urlsafe(32)
    now = int(time.time())
    item: dict[str, Any] = {
        "pk": STATE_PK,
        "sk": value,
        "purpose": purpose,
        "expires_at": now + STATE_TTL_SECONDS[purpose],
        "created_at": now,
    }
    if actor:
        item["created_by"] = actor
    if organization:
        item["organization"] = organization
    _table(settings).put(item)
    return value


def consume_state(value: str | None, purpose: str, settings: Settings | None = None) -> dict[str, Any]:
    """Delete the state and return it, or raise `InvalidState`.

    The delete is the read, so a state works exactly once even when two callbacks race.
    Expiry is checked here rather than trusted to the table's TTL sweep, which can lag by
    days.
    """
    if not value:
        raise InvalidState
    resolved = settings or get_settings()
    response = _table(resolved).table.delete_item(
        Key={"pk": STATE_PK, "sk": value},
        ReturnValues="ALL_OLD",
    )
    old: dict[str, Any] = dict(response.get("Attributes") or {})
    if not old or old.get("purpose") != purpose:
        raise InvalidState
    if int(str(old.get("expires_at", 0))) <= int(time.time()):
        raise InvalidState
    return old


def manifest_for(name: str, settings: Settings) -> dict[str, Any]:
    """The App manifest GitHub creates the App from."""
    base = _frontend_base(settings)
    return {
        "name": name,
        "url": base,
        "redirect_url": f"{base}{CREATED_PATH}",
        "setup_url": f"{base}{SETUP_PATH}",
        "setup_on_update": True,
        "public": False,
        "default_permissions": dict(PERMISSIONS),
        "default_events": [],
    }


def default_app_name(settings: Settings) -> str:
    """The App's name: the product, suffixed with the environment outside production."""
    environment = settings.environment
    return "webbpulse-terraform" if environment == "production" else f"webbpulse-terraform-{environment}"


def start_manifest(
    *,
    organization: str | None,
    name: str | None,
    actor: str | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Issue a manifest state and return what the SPA posts to GitHub."""
    resolved = settings or get_settings()
    if _settings_or_none(resolved) is not None:
        raise AppAlreadyConfigured
    manifest = manifest_for(name or default_app_name(resolved), resolved)
    state = _issue_state(MANIFEST, actor, resolved, organization=organization)
    query = urlencode({"state": state})
    if organization:
        action_url = f"https://github.com/organizations/{quote(organization)}/settings/apps/new?{query}"
    else:
        action_url = f"https://github.com/settings/apps/new?{query}"
    return {"action_url": action_url, "manifest": manifest, "state": state}


def complete_manifest(
    *,
    code: str,
    state: str,
    actor: str | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Convert the manifest code, store the credentials and the App row, and report status.

    The state is consumed first, so a forged or replayed callback never reaches GitHub.
    The credentials go into the `app` secret with a read-merge-put, which keeps every
    other key, and are never returned or logged.
    """
    resolved = settings or get_settings()
    issued = consume_state(state, MANIFEST, resolved)
    if _settings_or_none(resolved) is not None:
        raise AppAlreadyConfigured
    conversion = convert_manifest_code(code, client=http_client())
    values = conversion.app_secret_values()
    store = SecretStore(
        boto3.client("secretsmanager", region_name=resolved.AWS_REGION_NAME),
        resolved.app_secret_arn,
    )
    store.set_many(values)
    invalidate()
    row: dict[str, Any] = {
        **APP_KEY,
        "app_id": str(conversion.id),
        "slug": conversion.slug,
        "name": conversion.name,
        "html_url": conversion.html_url,
        "owner_login": conversion.owner_login,
        "created_at": _now().isoformat(),
    }
    if issued.get("organization"):
        row["owner_type"] = "Organization"
    if actor:
        row["created_by"] = actor
    _table(resolved).put(row)
    logger.info(
        "GitHub App created from the manifest",
        extra={"event": "github.app.created", "app_id": conversion.id, "slug": conversion.slug},
    )
    return app_status(resolved)


def start_install(*, actor: str | None, settings: Settings | None = None) -> dict[str, Any]:
    """Issue an install state and return the GitHub URL that installs the App."""
    resolved = settings or get_settings()
    _app_settings(resolved)
    status = app_status(resolved)
    slug = status["slug"]
    if not slug:
        raise SlugMissing
    state = _issue_state(INSTALL, actor, resolved)
    install_url = f"https://github.com/apps/{quote(slug)}/installations/new?{urlencode({'state': state})}"
    return {"install_url": install_url, "state": state}


def _installation_row(installation: AppInstallation, existing: dict[str, Any] | None) -> dict[str, Any]:
    """The stored shape of an installation GitHub has just confirmed."""
    now = _now().isoformat()
    return {
        "pk": INSTALLATION_PK,
        "sk": str(installation.id),
        "installation_id": str(installation.id),
        "account_login": installation.account_login,
        "account_type": installation.account_type,
        "account_avatar_url": installation.account_avatar_url,
        "repository_selection": installation.repository_selection,
        "html_url": installation.html_url,
        "suspended": installation.suspended_at is not None,
        "suspended_at": installation.suspended_at.isoformat() if installation.suspended_at else None,
        "installed_at": (existing or {}).get("installed_at") or now,
        "updated_at": now,
    }


def _installation_key(installation_id: int | str) -> dict[str, str]:
    """The key of one stored installation."""
    return {"pk": INSTALLATION_PK, "sk": str(installation_id)}


def record_installation(
    *,
    installation_id: int,
    setup_action: str | None,
    state: str | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Store an installation from the setup redirect, once the state and GitHub agree.

    An install needs a state this API issued, which is the CSRF check: the redirect is
    unauthenticated and anyone can craft one. An `update` redirect, which GitHub sends when
    an account changes the App's repositories and carries no state of ours, only refreshes
    an installation already stored. Either way GitHub is asked whether the installation
    belongs to this App before anything is written.
    """
    resolved = settings or get_settings()
    table = _table(resolved)
    existing = table.get(_installation_key(installation_id))
    if state:
        consume_state(state, INSTALL, resolved)
    elif setup_action != "update" or existing is None:
        raise InvalidState
    with _app_client(resolved) as client:
        installation = client.get_app_installation(installation_id)
    row = _installation_row(installation, existing)
    table.put(row)
    return render_installation(row)


def render_installation(row: dict[str, Any]) -> dict[str, Any]:
    """An installation row as the API returns it."""
    return {
        key: row.get(key)
        for key in (
            "installation_id",
            "account_login",
            "account_type",
            "account_avatar_url",
            "repository_selection",
            "html_url",
            "suspended",
            "suspended_at",
            "installed_at",
            "updated_at",
        )
    }


def list_installations(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every stored installation, oldest id first."""
    resolved = settings or get_settings()
    rows = _table(resolved).iter_query(Key("pk").eq(INSTALLATION_PK))
    return [render_installation(row) for row in rows]


def refresh_installation(installation_id: int, settings: Settings | None = None) -> dict[str, Any]:
    """Re-read one installation from GitHub, dropping it when GitHub no longer has it."""
    resolved = settings or get_settings()
    table = _table(resolved)
    existing = table.get(_installation_key(installation_id))
    if existing is None:
        raise InstallationNotFound
    try:
        with _app_client(resolved) as client:
            installation = client.get_app_installation(installation_id)
    except GitHubNotFound:
        table.delete(_installation_key(installation_id))
        raise InstallationNotFound from None
    row = _installation_row(installation, existing)
    table.put(row)
    return render_installation(row)


def remove_installation(installation_id: int, settings: Settings | None = None) -> None:
    """Forget one installation here. Uninstalling the App is done on GitHub."""
    resolved = settings or get_settings()
    table = _table(resolved)
    if table.get(_installation_key(installation_id)) is None:
        raise InstallationNotFound
    table.delete(_installation_key(installation_id))


def list_repositories(installation_id: int, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every repository a stored installation covers, as GitHub lists them now."""
    resolved = settings or get_settings()
    if _table(resolved).get(_installation_key(installation_id)) is None:
        raise InstallationNotFound
    with _app_client(resolved) as client:
        repositories_found = client.list_installation_repositories(installation_id)
    return [
        {
            "id": repository.get("id"),
            "name": repository.get("name"),
            "full_name": repository.get("full_name"),
            "private": bool(repository.get("private")),
            "html_url": repository.get("html_url"),
            "default_branch": repository.get("default_branch"),
        }
        for repository in repositories_found
    ]


__all__ = [
    "BADGE_BACKGROUND",
    "CREATED_PATH",
    "LOGO_PATH",
    "SETUP_PATH",
    "AppAlreadyConfigured",
    "FrontendUrlMissing",
    "InstallationNotFound",
    "InvalidState",
    "SlugMissing",
    "app_status",
    "complete_manifest",
    "consume_state",
    "http_client",
    "list_installations",
    "list_repositories",
    "record_installation",
    "refresh_installation",
    "remove_installation",
    "start_install",
    "start_manifest",
]
