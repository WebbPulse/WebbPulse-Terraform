"""Mounts the `webbpulse.identity` package router onto the workspaces function.

The router carries the issuer's own path, so it is mounted with no prefix; a
prefix would double every path to `/api/auth/api/auth/...`.

The signing client follows the package's own `IDENTITY_SIGNER` switch rather than
being a `boto3.client("kms")` this module names, so a local stack signs in process
with no AWS credential at all. The package refuses the local signer in production,
so the switch cannot put a seed derived key in front of real users.

Every import happens in a function body. Importing this module therefore builds no
AWS client, and the runs image never reaches it at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:  # pragma: no cover
    from fastapi import APIRouter

    from app.common.composition.settings import Settings

IDENTITY_ROUTER_VERSION: Final = "1.0.0"
"""The version the identity router reports, independent of the backend's own."""

IDENTITY_SERVICE_NAME: Final = "webbpulse-terraform-identity"
"""The service name the identity routes report to logs and traces. Distinct from
the workspaces function's own, so an auth failure is legible in the logs even
though one function serves both surfaces."""


def build_identity_settings(settings: "Settings") -> Any:
    """`IdentitySettings` for this product, read straight from the environment.

    Every field arrives through an `IDENTITY_*` variable Terraform sets, so this is
    a bare constructor call. Raises `ValidationError` on a bad environment.
    """
    from webbpulse.identity import IdentitySettings

    del settings
    return IdentitySettings()  # pyright: ignore[reportCallIssue]


def build_router(settings: "Settings") -> "APIRouter":
    """The identity router, mounted by the caller with no prefix of its own.

    Which route groups mount depends on what is supplied: credentials mount the flow
    routes, an email sender and token store the email routes, and so on. This
    deployment configures no sender, so the email routes are not declared rather
    than declared and answering 503.
    """
    from webbpulse.dynamodb import Repository
    from webbpulse.identity import (
        CREDENTIALS_TABLE,
        IDENTITY_TOKENS_TABLE,
        LOGIN_ATTEMPTS_TABLE,
        OAUTH_LINKS_TABLE,
        OAUTH_STATES_TABLE,
        PASSKEYS_TABLE,
        RECOVERY_CODES_TABLE,
        REFRESH_TOKENS_TABLE,
        TOTP_FACTORS_TABLE,
        WEBAUTHN_CHALLENGES_TABLE,
        DynamoCredentialStore,
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

    from app.common.db.identity_tables import identity_table_prefix
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
    )
