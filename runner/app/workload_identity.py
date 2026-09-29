"""The engine's Google and Azure identity tokens, served from files the runner rewrites on refresh.

The control plane is an OIDC issuer and mints one token per cloud per phase, the
way HCP Terraform's dynamic credentials do. Neither token goes into the engine's
environment: each is written to an owner only file, and the providers are pointed
at the file, so a refresh only has to rewrite it.

Google reads an `external_account` credential through `GOOGLE_APPLICATION_CREDENTIALS`
whose `credential_source.file` is the token, and exchanges it at Google STS on each
refresh of its own, impersonating the service account when one is named. Azure reads
the token through `ARM_OIDC_TOKEN_FILE_PATH` with `ARM_USE_OIDC` and `ARM_CLIENT_ID`.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.credential_files import parse_expiration, private_directory, write_private
from app.models import GcpWorkloadIdentity, WorkloadIdentity

GOOGLE_STS_URL = "https://sts.googleapis.com/v1/token"
"""Where Google exchanges the identity token for a federated access token."""

GOOGLE_IMPERSONATION_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{email}:generateAccessToken"
)
"""Where the federated token is traded for the named service account's."""

JWT_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
"""The subject token type of an OIDC identity token at Google STS."""


def external_account(identity: GcpWorkloadIdentity, token_path: Path) -> dict[str, object]:
    """The Google `external_account` credential that reads the identity token from `token_path`."""
    document: dict[str, object] = {
        "type": "external_account",
        "audience": identity.audience,
        "subject_token_type": JWT_TOKEN_TYPE,
        "token_url": GOOGLE_STS_URL,
        "credential_source": {"file": str(token_path)},
    }
    if identity.service_account_email:
        document["service_account_impersonation_url"] = GOOGLE_IMPERSONATION_URL.format(
            email=identity.service_account_email
        )
    return document


class WorkloadIdentityFiles:
    """The token files and the Google credential file, in one owner only directory.

    It lives outside the configuration directory, so nothing here is packed into
    what the run uploads.
    """

    def __init__(self, directory: Path, group: int | None = None) -> None:
        self.directory = directory
        self._group = group
        self.expires_at: datetime | None = None
        self._environment: dict[str, str] = {}

    @property
    def gcp_token_path(self) -> Path:
        """The Google identity token."""
        return self.directory / "gcp-token.jwt"

    @property
    def gcp_credentials_path(self) -> Path:
        """The `external_account` credential Google's client libraries load."""
        return self.directory / "gcp-credentials.json"

    @property
    def azure_token_path(self) -> Path:
        """The Azure identity token."""
        return self.directory / "azure-token.jwt"

    def write(self, identity: WorkloadIdentity | None) -> None:
        """Write or rotate each token the phase was given; a phase given none gets no files.

        `expires_at` becomes the earliest expiry, which is when the next refresh is due.
        """
        if identity is None or (identity.gcp is None and identity.azure is None):
            return
        private_directory(self.directory, self._group)
        environment: dict[str, str] = {}
        expiries: list[datetime | None] = []
        if identity.gcp is not None:
            write_private(self.gcp_token_path, identity.gcp.token, self._group)
            write_private(
                self.gcp_credentials_path,
                json.dumps(external_account(identity.gcp, self.gcp_token_path)),
                self._group,
            )
            environment["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.gcp_credentials_path)
            expiries.append(parse_expiration(identity.gcp.expiration))
        if identity.azure is not None:
            write_private(self.azure_token_path, identity.azure.token, self._group)
            environment.update(
                {
                    "ARM_USE_OIDC": "true",
                    "ARM_OIDC_TOKEN_FILE_PATH": str(self.azure_token_path),
                    "ARM_CLIENT_ID": identity.azure.client_id,
                }
            )
            expiries.append(parse_expiration(identity.azure.expiration))
        self._environment = environment
        known = [expiry for expiry in expiries if expiry is not None]
        self.expires_at = min(known) if known else None

    def environment(self) -> dict[str, str]:
        """The engine's additions to its environment, empty when the phase has no tokens."""
        return dict(self._environment)
