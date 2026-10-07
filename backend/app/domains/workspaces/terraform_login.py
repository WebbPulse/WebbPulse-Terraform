"""`terraform login` for this host, Terraform's `login.v1` authorization code flow with PKCE.

Terraform CLI opens `/oauth/authorize` on the SPA with its PKCE challenge and a
loopback redirect, a signed-in person approves there, and the SPA asks the API for a
code. The CLI then exchanges the code at `/v1/oauth/token` and stores the access
token, an ordinary `wpk_` key named "terraform login", in `credentials.tfrc.json`.
That is the same outcome as HCP Terraform's own `terraform login`, which also leaves a
user API token behind.

The code rides the identity module's `authorization-codes` table and the package's
store, so it is hashed at rest, spent by one conditional delete and expired by TTL.
The client is fixed rather than registered: `terraform-cli` is a public client whose
only redirect is a loopback port Terraform chose from the discovery document's range.
"""

from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode, urlsplit

from webbpulse.dynamodb import Repository, now_iso
from webbpulse.identity.oauth import pkce_challenge
from webbpulse.identity.oauth_server_storage import (
    AUTHORIZATION_CODES_TABLE,
    AuthorizationCodeRecord,
    AuthorizationCodeStore,
    DynamoAuthorizationCodeStore,
)
from webbpulse.identity.storage import constant_time_equals, hash_token

from ...common.composition.settings import get_settings
from ...common.core.auth import (
    CONFIGS_READ,
    CONFIGS_WRITE,
    REGISTRY_READ,
    RUN_TOKEN_TENANT,
    RUNS_APPLY,
    RUNS_READ,
    RUNS_WRITE,
    STATE_DOWNLOAD,
    STATE_WRITE,
    VARIABLES_READ,
    WORKSPACES_READ,
)
from ...common.db.identity_tables import identity_table_prefix
from . import api_keys_service

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from ...common.composition.settings import Settings

CLIENT_ID: Final = "terraform-cli"
"""The one client, as the discovery document's `login.v1.client` names it."""

PORTS: Final = range(10000, 10011)
"""The loopback ports Terraform may listen on, the document's `ports` range inclusive."""

REDIRECT_HOSTS: Final = frozenset({"localhost", "127.0.0.1"})
"""Terraform redirects to `localhost`; the address form is accepted for the same listener."""

REDIRECT_PATH: Final = "/login"
"""The path Terraform's loopback listener serves."""

CODE_TTL: Final = timedelta(minutes=10)
"""How long an approval waits for the CLI's exchange."""

VERIFIER_PATTERN: Final = re.compile(r"^[A-Za-z0-9._~-]{43,128}$")
"""RFC 7636's code verifier: unreserved characters, 43 to 128 of them."""

KEY_NAME: Final = "terraform login"
"""The label the minted key carries in the API keys list."""

LOGIN_SCOPES: Final = (
    WORKSPACES_READ,
    VARIABLES_READ,
    CONFIGS_READ,
    CONFIGS_WRITE,
    RUNS_READ,
    RUNS_WRITE,
    RUNS_APPLY,
    STATE_DOWNLOAD,
    STATE_WRITE,
    REGISTRY_READ,
)
"""What a login key may carry: reading, the registry, runs, and the state a `cloud {}` block needs.

Applying from the CLI is ordinary, as it is with an HCP user token, so `runs:apply` is
included. `state:download` and `state:write` are what `terraform output`, `state mv`,
`import` and `force-unlock` need through the cloud block. The person's own scopes still
limit all of it, so someone without state access gets none. Never an admin or workspace
and variable writes, which stay behind an explicitly minted key.
"""


class LoginRefused(Exception):
    """An authorization or token request that the flow refuses, with its OAuth error code."""

    def __init__(self, error: str, description: str) -> None:
        """Keep the RFC 6749 error code and a human readable reason."""
        super().__init__(description)
        self.error = error
        self.description = description


@dataclass(frozen=True)
class Issued:
    """A token exchange's result: the key's plaintext and when it stops working."""

    access_token: str
    expires_at: datetime


def code_store(settings: "Settings | None" = None) -> AuthorizationCodeStore:
    """The identity module's `authorization-codes` table, resolved per call."""
    resolved = settings or get_settings()
    return DynamoAuthorizationCodeStore(
        Repository(
            AUTHORIZATION_CODES_TABLE,
            prefix=identity_table_prefix(resolved),
            region_name=resolved.AWS_REGION_NAME or None,
            endpoint_url=resolved.dynamodb_endpoint_url,
        )
    )


def login_scopes(held: "Iterable[str]") -> tuple[str, ...]:
    """The login scopes the person holds, in a stable order."""
    held_set = set(held)
    return tuple(scope for scope in LOGIN_SCOPES if scope in held_set)


def check_redirect(redirect_uri: str) -> str:
    """Accept only Terraform's loopback listener, `http://localhost:<port>/login`.

    A loopback redirect is the whole of a public client's protection, since a code
    sent anywhere else could be exchanged by whoever received it.
    """
    try:
        parts = urlsplit(redirect_uri)
        port = parts.port
    except ValueError as error:
        raise LoginRefused("invalid_request", "The redirect URI is not a URL.") from error
    if (
        parts.scheme != "http"
        or parts.hostname not in REDIRECT_HOSTS
        or port not in PORTS
        or parts.path != REDIRECT_PATH
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise LoginRefused("invalid_request", "The redirect URI must be Terraform's loopback listener.")
    return redirect_uri


def authorize(
    *,
    user_id: str,
    held_scopes: "Iterable[str]",
    client_id: str,
    response_type: str,
    redirect_uri: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str,
    settings: "Settings | None" = None,
) -> str:
    """Issue a code for an approved request and return the redirect that carries it.

    The scopes are fixed now, from what the person holds at approval, so the exchange
    needs no session. The key is still intersected with the owner's live scopes on
    every request, so a later demotion narrows it.

    Raises:
        LoginRefused: The request is not Terraform's, or the person holds nothing a
            login key could carry.
    """
    if client_id != CLIENT_ID:
        raise LoginRefused("unauthorized_client", "Only Terraform CLI may use this flow.")
    if response_type != "code":
        raise LoginRefused("unsupported_response_type", "Only the authorization code flow is supported.")
    if code_challenge_method != "S256" or not 43 <= len(code_challenge) <= 128:
        raise LoginRefused("invalid_request", "A PKCE S256 code challenge is required.")
    check_redirect(redirect_uri)
    scopes = login_scopes(held_scopes)
    if not scopes:
        raise LoginRefused("access_denied", "Your account holds no scope a Terraform login can carry.")

    code = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    code_store(settings).put(
        AuthorizationCodeRecord(
            code_hash=hash_token(code),
            client_id=CLIENT_ID,
            user_id=user_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            resource="",
            tenant_id=RUN_TOKEN_TENANT,
            created_at=now_iso(),
            expires_at=int((now + CODE_TTL).timestamp()),
            scopes=scopes,
        )
    )
    query = {"code": code, **({"state": state} if state else {})}
    return f"{redirect_uri}?{urlencode(query)}"


def basic_client_id(authorization: str) -> str:
    """The client id from an HTTP Basic header, which Go's oauth2 tries before the body.

    Empty when the header is absent or is not Basic, so the body's `client_id` decides.
    """
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "basic" or not value:
        return ""
    try:
        decoded = base64.b64decode(value.strip(), validate=True).decode()
    except ValueError:
        return ""
    return decoded.partition(":")[0]


def exchange(
    *,
    grant_type: str,
    code: str,
    client_id: str,
    redirect_uri: str,
    code_verifier: str,
    settings: "Settings | None" = None,
) -> Issued:
    """Spend a code and mint the login key it was approved for.

    The code is deleted before anything is compared, so a code tried with a wrong
    verifier is gone, as RFC 6749 asks of a code seen twice.

    Raises:
        LoginRefused: The grant, code, client, redirect or verifier does not match.
    """
    if grant_type != "authorization_code":
        raise LoginRefused("unsupported_grant_type", "Only the authorization_code grant is supported.")
    if not code or not VERIFIER_PATTERN.fullmatch(code_verifier):
        raise LoginRefused("invalid_request", "A code and a well formed code_verifier are required.")
    record = code_store(settings).consume(hash_token(code))
    if record is None:
        raise LoginRefused("invalid_grant", "The code is unknown, spent or expired.")
    if client_id != record.client_id:
        raise LoginRefused("invalid_client", "The code was issued to another client.")
    if redirect_uri != record.redirect_uri:
        raise LoginRefused("invalid_grant", "The redirect URI does not match the authorization request.")
    if not constant_time_equals(pkce_challenge(code_verifier), record.code_challenge):
        raise LoginRefused("invalid_grant", "The code verifier does not match the challenge.")

    expires_at = datetime.now(UTC) + timedelta(days=api_keys_service.DEFAULT_EXPIRY_DAYS)
    try:
        minted = api_keys_service.mint_key(
            user_id=record.user_id,
            name=KEY_NAME,
            requested_scopes=record.scopes,
            held_scopes=record.scopes,
            expires_at=expires_at,
            settings=settings,
        )
    except api_keys_service.TooManyKeys as error:
        raise LoginRefused("invalid_grant", str(error)) from error
    return Issued(access_token=minted.plaintext, expires_at=expires_at)


__all__ = [
    "CLIENT_ID",
    "CODE_TTL",
    "KEY_NAME",
    "LOGIN_SCOPES",
    "PORTS",
    "Issued",
    "LoginRefused",
    "authorize",
    "basic_client_id",
    "check_redirect",
    "code_store",
    "exchange",
    "login_scopes",
]
