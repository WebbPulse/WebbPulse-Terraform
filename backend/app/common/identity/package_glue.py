"""Mounts the `webbpulse.identity` package router onto the workspaces function.

The router carries the issuer's own path, so it is mounted with no prefix; a
prefix would double every path to `/api/auth/api/auth/...`.

The signing client follows the package's own `IDENTITY_SIGNER` switch rather than
being a `boto3.client("kms")` this module names, so a local stack signs in process
with no AWS credential at all. The package refuses the local signer in production,
so the switch cannot put a seed derived key in front of real users.

Every package import happens in a function body. Importing this module therefore builds
no AWS client, and the runs image never reaches it at all.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

from app.common.core.auth import ADMIN, ALL_SCOPES, STATE_DOWNLOAD, STATE_READ_OUTPUTS, STATE_WRITE

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from app.common.composition.settings import Settings

IDENTITY_ROUTER_VERSION: Final = "1.0.0"
"""The version the identity router reports, independent of the backend's own."""

IDENTITY_SERVICE_NAME: Final = "webbpulse-terraform-identity"
"""The service name the identity routes report to logs and traces. Distinct from
the workspaces function's own, so an auth failure is legible in the logs even
though one function serves both surfaces."""


DEVICE_CLIENTS: Final = {"wp-tf": "wp-tf CLI"}
"""The one client allowed to start a device login, and the name the approval page shows."""

DEVICE_EXPLICIT_SCOPES: Final = (STATE_DOWNLOAD, STATE_WRITE, STATE_READ_OUTPUTS, ADMIN)
"""Scopes a device login gets only when `wp-tf login` names them, never by default.

The default is therefore every other scope, read and write on workspaces, variables,
configs, runs and the registry plus `runs:apply`, which is `wp-tf`'s `STANDARD_SCOPES`.
Raw state, state writes and operator settings stay out of a session that sits on a workstation."""

SIGN_IN_PATH: Final = "/sign-in"
"""The SPA's sign-in page, where a signed-out device approval is sent."""

WEBAUTHN_ORIGINS_ENV: Final = "IDENTITY_WEBAUTHN_ORIGINS"
"""Set only where the passkey origin differs from the SPA's, such as the local e2e stack."""

SESSION_IDLE_TTL: Final = timedelta(hours=12)
"""How long a browser session lives without a refresh. Every refresh while the app is
open pushes it out again, so only a session nobody uses for this long ends."""

SESSION_ABSOLUTE_TTL: Final = timedelta(days=7)
"""The most a browser session can slide to, counted from its sign-in, after which the
person signs in again however active they were. Matches the access gate's session."""


def consent_theme() -> Any:
    """The device and consent pages' look, matching the SPA's dark default palette and type."""
    from webbpulse.identity.consent_page import ConsentPalette, ConsentTheme

    return ConsentTheme(
        light=ConsentPalette(
            background="#fafafa",
            surface="#ffffff",
            raised="#f1f2f3",
            line="#e5e6e8",
            line_strong="#d5d7db",
            text="#0c0c0e",
            text_muted="#656a76",
            text_faint="#737884",
            accent="#1060ff",
            accent_foreground="#ffffff",
            accent_ring="#cce3fe",
            danger="#c00005",
        ),
        dark=ConsentPalette(
            background="#0f1116",
            surface="#161920",
            raised="#1d212a",
            line="#262b36",
            line_strong="#333946",
            text="#f4f6fa",
            text_muted="#98a0b0",
            text_faint="#7b8394",
            accent="#4d9fff",
            accent_foreground="#08101c",
            accent_ring="#1e3c5f",
            danger="#ff6b63",
        ),
        color_scheme="dark",
        font_family="Inter, ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
        revoke_note="Run wp-tf logout on that machine to end the session early.",
    )


def build_identity_settings(settings: "Settings") -> Any:
    """`IdentitySettings` for this product: the `IDENTITY_*` environment plus the fixed facts.

    Device login's client, scopes and sign-in page are product facts rather than
    deployment config, so they are set here instead of in the function environment,
    which Lambda caps at 4KB, and so is the session's sliding window. The WebAuthn
    origin defaults to the SPA's origin. Raises `ValidationError` on a bad environment.
    """
    from webbpulse.identity import IdentitySettings

    overrides: dict[str, Any] = {
        "refresh_token_ttl": SESSION_IDLE_TTL,
        "refresh_absolute_ttl": SESSION_ABSOLUTE_TTL,
    }
    frontend = settings.IDENTITY_FRONTEND_BASE_URL.strip().rstrip("/")
    if frontend and not os.environ.get(WEBAUTHN_ORIGINS_ENV, "").strip():
        overrides["webauthn_origins"] = [frontend]
    if settings.IDENTITY_DEVICE_GRANT_ENABLED:
        overrides.update(
            device_grant_enabled=True,
            device_clients=dict(DEVICE_CLIENTS),
            device_scopes_supported=list(ALL_SCOPES),
            device_explicit_scopes=list(DEVICE_EXPLICIT_SCOPES),
        )
        if frontend:
            overrides["device_login_url"] = f"{frontend}{SIGN_IN_PATH}"
    return IdentitySettings(**overrides)  # pyright: ignore[reportCallIssue]


def build_router(settings: "Settings") -> "APIRouter":
    """The identity router, mounted by the caller with no prefix of its own.

    Which route groups mount depends on what is supplied: credentials mount the flow
    routes, an email sender and token store the email routes, and so on. This
    deployment configures no sender, so the email routes are not declared rather
    than declared and answering 503.

    The `api-keys` store is supplied so the users stream purge deletes a deleted user's
    keys as well. The identity module creates that table and grants the workspaces role
    the table policy actions on it, delete and the user index query included.

    With `device_grant_enabled` on, the device stores back `wp-tf login`, and the same
    stores sit on `IdentityStores` so the purge removes a deleted user's device grants.
    The grants store records each new grant in the audit trail.
    """
    from webbpulse.dynamodb import Repository
    from webbpulse.identity import (
        CREDENTIALS_TABLE,
        DEVICE_CODES_TABLE,
        DEVICE_GRANTS_TABLE,
        IDENTITY_TOKENS_TABLE,
        LOGIN_ATTEMPTS_TABLE,
        OAUTH_LINKS_TABLE,
        OAUTH_STATES_TABLE,
        PASSKEYS_TABLE,
        RECOVERY_CODES_TABLE,
        REFRESH_TOKENS_TABLE,
        TOTP_FACTORS_TABLE,
        WEBAUTHN_CHALLENGES_TABLE,
        DeviceGrantStores,
        DynamoCredentialStore,
        DynamoDeviceCodeStore,
        DynamoIdentityTokenStore,
        DynamoLoginAttemptStore,
        DynamoOAuthLinkStore,
        DynamoOAuthStateStore,
        DynamoPasskeyStore,
        DynamoRecoveryCodeStore,
        DynamoRefreshTokenStore,
        DynamoTotpFactorStore,
        DynamoWebAuthnChallengeStore,
        IdentityStores,
        build_identity_router,
        signing_client,
    )
    from webbpulse.identity.api_keys import API_KEYS_TABLE, DynamoApiKeyStore

    from app.common.db.identity_tables import identity_table_prefix
    from app.common.identity.audited_device_grants import AuditedDeviceGrantStore
    from app.common.identity.identity_hooks import ControlPlaneIdentityHooks

    prefix = identity_table_prefix(settings)

    def repository(logical_name: str) -> Repository:
        """A package repository for one of the identity module's tables.

        Prefix, region and endpoint are passed explicitly so this reads the same
        `Settings` as the rest of the backend, and table names are the package's own
        constants rather than strings restated here.
        """
        return Repository(
            logical_name,
            prefix=prefix,
            region_name=settings.AWS_REGION_NAME or None,
            endpoint_url=settings.dynamodb_endpoint_url,
        )

    identity_settings = build_identity_settings(settings)
    device_stores = (
        DeviceGrantStores(
            codes=DynamoDeviceCodeStore(repository(DEVICE_CODES_TABLE)),
            grants=AuditedDeviceGrantStore(repository(DEVICE_GRANTS_TABLE)),
        )
        if identity_settings.device_grant_enabled
        else None
    )

    stores = IdentityStores(
        credentials=DynamoCredentialStore(repository(CREDENTIALS_TABLE)),
        refresh_tokens=DynamoRefreshTokenStore(repository(REFRESH_TOKENS_TABLE)),
        identity_tokens=DynamoIdentityTokenStore(repository(IDENTITY_TOKENS_TABLE)),
        totp_factors=DynamoTotpFactorStore(repository(TOTP_FACTORS_TABLE)),
        recovery_codes=DynamoRecoveryCodeStore(repository(RECOVERY_CODES_TABLE)),
        oauth_states=DynamoOAuthStateStore(repository(OAUTH_STATES_TABLE)),
        oauth_links=DynamoOAuthLinkStore(repository(OAUTH_LINKS_TABLE)),
        passkeys=DynamoPasskeyStore(repository(PASSKEYS_TABLE)),
        webauthn_challenges=DynamoWebAuthnChallengeStore(repository(WEBAUTHN_CHALLENGES_TABLE)),
        api_keys=DynamoApiKeyStore(repository(API_KEYS_TABLE)),
        device_grants=device_stores.grants if device_stores else None,
        device_codes=device_stores.codes if device_stores else None,
    )

    return build_identity_router(
        identity_settings,
        ControlPlaneIdentityHooks(),
        stores,
        kms_client=signing_client(identity_settings),
        service=IDENTITY_SERVICE_NAME,
        version=IDENTITY_ROUTER_VERSION,
        attempts=DynamoLoginAttemptStore(repository(LOGIN_ATTEMPTS_TABLE)),
        email_sender=None,
        oauth_client_secrets={},
        consent_theme=consent_theme(),
        device_grant_stores=device_stores,
    )
