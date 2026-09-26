"""Request and response models for the GitHub App, its installations and repositories."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

GITHUB_LOGIN_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
"""A GitHub account or organization login."""

STATE_PATTERN = r"^[A-Za-z0-9_-]{16,128}$"
"""A state this API issued: URL-safe base64."""


class GitHubAppStatus(BaseModel):
    """Whether this environment's App exists, and what the page may offer next."""

    configured: bool
    """The `app` secret holds a usable App id and private key."""
    slug: Optional[str] = None
    app_id: Optional[str] = None
    name: Optional[str] = None
    html_url: Optional[str] = None
    owner_login: Optional[str] = None
    settings_url: Optional[str] = None
    """The App's settings page on GitHub, where its logo is uploaded by hand."""
    created_at: Optional[str] = None
    can_create: bool
    """No App yet, so the manifest flow may create one."""
    can_install: bool
    """An App with a known slug, so it can be installed."""
    logo_path: str
    """The SPA path of the App logo to upload on GitHub."""
    badge_background: str
    """The badge background hex to set beside the logo."""


class ManifestStartRequest(BaseModel):
    """Where the App is created."""

    organization: Optional[str] = Field(default=None, pattern=GITHUB_LOGIN_PATTERN)
    """The organization to own the App; the signed-in GitHub account when omitted."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=34)
    """The App's name; the product and environment when omitted."""


class ManifestStart(BaseModel):
    """What the SPA posts to GitHub: the form action and the manifest."""

    action_url: str
    """GitHub's new App page, carrying the state."""
    manifest: dict[str, Any]
    state: str


class ManifestConversionRequest(BaseModel):
    """The create callback's query, forwarded by the SPA."""

    code: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    state: str = Field(pattern=STATE_PATTERN)


class InstallStart(BaseModel):
    """The GitHub URL that installs the App, carrying a one-time state."""

    install_url: str
    state: str


class InstallationCallback(BaseModel):
    """The setup callback's query, forwarded by the SPA."""

    installation_id: int = Field(gt=0)
    setup_action: Optional[str] = Field(default=None, max_length=32)
    state: Optional[str] = Field(default=None, pattern=STATE_PATTERN)
    """Required for an install. An `update` from GitHub carries none of ours."""


class Installation(BaseModel):
    """One installation of the App, as GitHub last confirmed it."""

    installation_id: str
    account_login: str
    account_type: str
    account_avatar_url: Optional[str] = None
    repository_selection: str
    """`all` or `selected`."""
    html_url: Optional[str] = None
    """The installation's configuration page on GitHub."""
    suspended: bool
    suspended_at: Optional[str] = None
    installed_at: str
    updated_at: str


class InstallationList(BaseModel):
    """Every stored installation."""

    items: list[Installation]


class Repository(BaseModel):
    """One repository an installation covers."""

    id: int
    name: str
    full_name: str
    private: bool
    html_url: Optional[str] = None
    default_branch: Optional[str] = None


class RepositoryList(BaseModel):
    """Every repository an installation covers."""

    items: list[Repository]
