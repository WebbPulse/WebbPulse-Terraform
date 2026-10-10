"""The control plane's settings, on the shared package's base.

Every field is an environment variable Terraform sets on the function, or derived
from the stack prefix where Terraform leaves it unset. The one
secret field, `variables_master_key`, resolves from the `APP_SECRETS_ARN` blob on
first read, so constructing this makes no Secrets Manager call and importing it
needs no credentials.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import model_validator
from webbpulse.config import BaseServiceSettings

from ..db import tables

VARIABLES_MASTER_KEY_ENTRY = "variables_master_key"
"""The app secret's key holding the base64 HKDF master key for sensitive variables."""

LOCALHOST_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]
"""The Vite dev server's origins, allowed only where `LOCALHOST_ORIGIN_ENVIRONMENTS` names
the raw `ENVIRONMENT`, so no deployed stack answers a page served from a workstation."""

LOCALHOST_ORIGIN_ENVIRONMENTS = frozenset({"local", "dev", "development"})
"""The explicit spellings of a workstation stack. An unrecognised value maps to "local"
for the base class but is not listed here, so a typo on a deployed function stays closed."""

STACK_TABLE_FIELDS = {
    "WORKSPACES_TABLE": tables.WORKSPACES,
    "RUNS_TABLE": tables.RUNS,
    "VARIABLES_TABLE": tables.VARIABLES,
    "CONFIG_VERSIONS_TABLE": tables.CONFIG_VERSIONS,
    "USERS_TABLE": tables.USERS,
    "GITHUB_TABLE": tables.GITHUB,
    "VCS_UPLOADS_TABLE": tables.VCS_UPLOADS,
    "REGISTRY_TABLE": tables.REGISTRY,
    "NOTIFICATION_CONFIGURATIONS_TABLE": tables.NOTIFICATION_CONFIGURATIONS,
    "PROJECTS_TABLE": tables.PROJECTS,
    "AUDIT_TABLE": tables.AUDIT,
}
"""Each table setting and the logical name the stack prefixes to name that table."""

ENVIRONMENT_ALIASES = {
    "development": "local",
    "dev": "local",
    "local": "local",
    "test": "test",
    "testing": "test",
    "staging": "staging",
    "production": "production",
    "prod": "production",
}
"""Maps the free-text `ENVIRONMENT` onto the base class's Literal. Anything
unrecognised lands on "local", the value that grants the least."""


class Settings(BaseServiceSettings):
    """The control plane's settings. Constructing this reads no AWS and no secret."""

    ENVIRONMENT: str = "development"

    WORKSPACES_TABLE: str = ""
    RUNS_TABLE: str = ""
    VARIABLES_TABLE: str = ""
    CONFIG_VERSIONS_TABLE: str = ""
    USERS_TABLE: str = ""
    """The account rows the identity hooks read and write. Separate from the identity
    module's own ten tables, which the package names from a prefix rather than from
    the environment."""
    VCS_UPLOADS_TABLE: str = ""
    """The ingest records a GitHub App delivery writes, expired by TTL."""

    REGISTRY_TABLE: str = ""
    """The module registry's one table: modules and their versions."""

    NOTIFICATION_CONFIGURATIONS_TABLE: str = ""
    """Each workspace's run notification configurations, with their sealed webhook URLs."""

    RUN_NOTIFICATIONS_QUEUE_URL: str = ""
    """The queue the runs stream sends each notification delivery to. Unset, the queue
    is found by its name under the stack prefix, which keeps it out of the function's
    environment."""

    PROJECTS_TABLE: str = ""
    """The projects workspaces are grouped into. The default project is never stored."""

    AUDIT_TABLE: str = ""
    """The durable audit trail of workspace, token, variable, state and run decisions."""

    REGISTRY_INGEST_QUEUE_URL: str = ""
    """The queue the webhook route sends each semantic version tag push to, for the
    registry function. Unset, tag pushes are acknowledged and dropped."""

    PROVIDER_SIGNING_KEY_PARAMETER: str = ""
    """The SSM parameter holding the ASCII armored public key provider releases must be
    signed with. Unset, no provider version can be published."""

    PROVIDER_SIGNING_KEY_ID_PARAMETER: str = ""
    """The SSM parameter holding that key's id, which a release's signature must name."""

    GITHUB_TABLE: str = ""
    """The GitHub domain's one table: the App row, one-time states and installations."""

    GITHUB_APP_SLUG: str = ""
    """Fallback App slug. The slug the manifest flow stores in the GitHub table wins."""

    IDENTITY_FRONTEND_BASE_URL: str = ""
    """The SPA's origin, which the App manifest's callback URLs are built on."""

    STATE_BUCKET: str = ""
    ARTIFACTS_BUCKET: str = ""
    RUN_STATE_MACHINE_ARN: str = ""
    RUNNER_LOG_GROUP: str = ""
    WORKSPACE_CLEANUP_QUEUE_URL: str = ""
    """The queue a workspace delete sends its S3 purge to. Unset, the delete purges inline."""
    GITHUB_WEBHOOKS_QUEUE_URL: str = ""
    """The queue the webhook route sends each verified delivery to, for the runs function."""
    AWS_CONNECT_TOPIC_ARN: str = ""
    """The SNS topic a Quick setup stack's custom resource reports to. Unset, the
    template carries no custom resource and the link needs the account id."""
    API_BASE_URL: str = ""
    """The API's public origin, which the App's webhook URL is built on."""
    ORIGIN_VERIFY_PARAMETER: str = ""
    """The access gate's SSM SecureString holding the `x-origin-verify` value, set on the
    runs function only. A run API token travels with it so the WebbPulse provider gets past
    the gate. Unset, the bundle carries no gate value."""
    APP_SECRETS_ARN: str = ""
    """The one JSON app secret every runtime key is read from.

    Named exactly as the shared package expects: `webbpulse.security.app_secrets`
    and `webbpulse.identity.crypto.resolve_totp_master_key` fall back to the
    `APP_SECRETS_ARN` environment variable directly, so a differently named
    variable leaves the package unable to find `mfa_master_key` however well this
    class resolves it.
    """

    APP_SECRET_ID: str = ""
    """Legacy spelling of `APP_SECRETS_ARN`, still read so a function running the
    previous environment keeps resolving its secret across a deploy. Prefer
    `app_secret_arn`, which takes the standard name first."""

    IDENTITY_ISSUER: str = ""
    """The identity issuer. Present exactly when a JWT authorizer fronts this
    deployment, which is how the composition root tests for it cheaply."""

    IDENTITY_TABLE_PREFIX: str = ""
    """The stack prefix, set by Terraform to `local.prefix`. The identity module's own
    tables carry it, every product table is `<prefix>-<logical>`, and run roles are
    named under `<prefix>-workspace-`, so an unset table or role prefix setting is
    derived from it and the function environment stays under Lambda's 4KB limit.

    It cannot be derived from `ENVIRONMENT`: the stack slugs production to `prod`
    while `ENVIRONMENT` is the word `production`, so a derived prefix would name
    tables that do not exist there. `identity_table_prefix` falls back to the derived
    form only for the local stack and the suite, where the two do agree."""

    IDENTITY_DEVICE_GRANT_ENABLED: bool = False
    """Whether `wp-tf login` device tokens are accepted. Set by Terraform on every
    function, so each one checks a device token's grant is still live before
    trusting it, not only the function that mounts the identity routes."""

    RUNNER_TASK_ROLE_ARN: str = ""
    """The runner task roles a workspace run role has to trust, comma separated.

    One role per phase, so a trust policy that names only the first leaves the
    apply phase unable to assume. `runner_task_role_arns` splits it."""

    RUNNER_CLUSTER_ARN: str = ""
    """The ECS cluster runner tasks run on. The runner token exchange describes the
    calling task there to learn which run and phase it was started for."""

    RUN_CREDENTIALS_ROLE_ARN: str = ""
    """The one principal every workspace run role trusts. The runs function
    assumes it to vend each run phase its credentials, so no runner task holds
    a path to a workspace role. `run_role_principal_arns` is what a trust names."""

    RUN_STATE_ROLE_ARN: str = ""
    """The state bucket role the vending role assumes, with a session policy
    narrowing it to one workspace's state, for the S3 backend's credentials."""

    RUN_CREDENTIALS_DURATION_SECONDS: int = 3600
    """How long each vended session lasts, clamped to STS's 900 second floor and the
    one hour role chaining ceiling. The runner refreshes before expiry, so a shorter
    session only means more refreshes; `run_credentials_duration_seconds` reads it."""

    OIDC_ISSUER_URL: str = ""
    """The control plane's OIDC issuer, `https://oidc.<host>`, the `iss` of every
    workload identity token a run gets for Google or Azure. Empty means the issuer
    is off, and a workspace asking for workload identity fails its run."""

    OIDC_SIGNING_KEY_ARN: str = ""
    """The KMS RSA key that signs workload identity tokens now. Its key policy lets
    only the runs function sign, and the issuer publishes its public half."""

    RUN_ROLE_NAME_PREFIX: str = ""
    """The prefix every workspace run role name carries, `${local.prefix}-workspace-`.
    The runner's AssumeRole grant is scoped to it, so a role named outside it cannot
    be assumed however its trust policy reads."""

    STATE_KMS_KEY_ARN: str = ""
    """The state bucket's KMS key ARN, which the runner writes into the S3 backend
    block. Required outside the local stack: `terraform init` rejects an empty
    `kms_key_id` rather than falling back to the bucket's default encryption."""

    AWS_REGION_NAME: str = "us-west-2"

    DYNAMODB_ENDPOINT_URL: str = ""
    S3_ENDPOINT_URL: str = ""

    CORS_ORIGINS: str = ""
    LOG_LEVEL: str = "INFO"

    _variables_master_key: str = ""

    @model_validator(mode="before")
    @classmethod
    def _map_environment_alias(cls, data: Any) -> Any:
        """Translate the raw `ENVIRONMENT` before the base's Literal validates it.

        Both fields are fed by the same case-insensitive variable, so the base
        would otherwise reject the free-text spellings this project uses.
        """
        if not isinstance(data, dict):
            return data
        keys = [key for key in data if key.lower() == "environment"]
        if not keys:
            return data
        raw = data[keys[0]]
        if not isinstance(raw, str):
            return data
        mapped = ENVIRONMENT_ALIASES.get(raw.strip().lower(), "local")
        data = dict(data)
        for key in keys:
            data.pop(key)
        data["ENVIRONMENT"] = raw
        data["environment"] = mapped
        return data

    @model_validator(mode="after")
    def _mirror_base_fields(self) -> "Settings":
        """Derive the base class's lower case fields from this project's own, and the
        table names and run role prefix the stack prefix implies when none is set."""
        object.__setattr__(
            self,
            "environment",
            ENVIRONMENT_ALIASES.get(self.ENVIRONMENT.strip().lower(), "local"),
        )
        object.__setattr__(self, "log_level", self.LOG_LEVEL.strip().upper())
        origins = [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
        if self.ENVIRONMENT.strip().lower() in LOCALHOST_ORIGIN_ENVIRONMENTS:
            origins += LOCALHOST_ORIGINS
        object.__setattr__(self, "cors_allow_origins", sorted(set(origins)))
        if self.app_secret_arn and not self.app_secrets_arn:
            object.__setattr__(self, "app_secrets_arn", self.app_secret_arn)
        prefix = self.IDENTITY_TABLE_PREFIX.strip()
        if prefix:
            for field, logical_name in STACK_TABLE_FIELDS.items():
                if not getattr(self, field):
                    object.__setattr__(self, field, f"{prefix}-{logical_name}")
            if not self.RUN_ROLE_NAME_PREFIX:
                object.__setattr__(self, "RUN_ROLE_NAME_PREFIX", f"{prefix}-workspace-")
        return self

    @property
    def app_secret_arn(self) -> str:
        """The app secret ARN, standard name ahead of the legacy one."""
        return self.APP_SECRETS_ARN or self.APP_SECRET_ID

    @property
    def runner_task_role_arns(self) -> list[str]:
        """Every runner task role ARN, in the order Terraform set them."""
        return [arn.strip() for arn in self.RUNNER_TASK_ROLE_ARN.split(",") if arn.strip()]

    @property
    def run_role_principal_arns(self) -> list[str]:
        """The principals a workspace run role trusts: the vending role alone."""
        return [self.RUN_CREDENTIALS_ROLE_ARN] if self.RUN_CREDENTIALS_ROLE_ARN else []

    @property
    def plane_account_id(self) -> str:
        """The AWS account the control plane runs in, read from the vending role's ARN."""
        parts = self.RUN_CREDENTIALS_ROLE_ARN.split(":")
        return parts[4] if len(parts) > 5 and parts[4] else ""

    @property
    def run_role_permissions_boundary_arn(self) -> str:
        """The boundary every run role in the plane's own account carries, so no role
        the plane can vend there can reach the plane's own resources. Terraform names it
        `<run role prefix>boundary`."""
        if not self.plane_account_id or not self.RUN_ROLE_NAME_PREFIX:
            return ""
        return f"arn:aws:iam::{self.plane_account_id}:policy/{self.RUN_ROLE_NAME_PREFIX}boundary"

    @property
    def run_credentials_duration_seconds(self) -> int:
        """The vended session length, within what STS accepts for a chained role."""
        return max(900, min(3600, self.RUN_CREDENTIALS_DURATION_SECONDS))

    @property
    def dynamodb_endpoint_url(self) -> str | None:
        """The DynamoDB endpoint override, or `None` when the real service is used."""
        return self.DYNAMODB_ENDPOINT_URL or None

    @property
    def s3_endpoint_url(self) -> str | None:
        """The S3 endpoint override, or `None` when the real service is used."""
        return self.S3_ENDPOINT_URL or None

    def variables_master_key(self) -> str:
        """The base64 HKDF master key for sensitive variable values.

        Read from the environment first so a workstation needs no AWS, then from
        the `APP_SECRETS_ARN` blob, and cached for the life of the process. Returns
        `""` when no source carries it, which the cipher factory turns into a
        named failure rather than a silent plaintext write.
        """
        if self._variables_master_key:
            return self._variables_master_key

        import os

        from webbpulse.security import app_secrets

        resolved = os.environ.get("VARIABLES_MASTER_KEY", "")
        if not resolved and self.app_secret_arn:
            resolved = str(app_secrets(self.app_secret_arn).get(VARIABLES_MASTER_KEY_ENTRY, "") or "")
        object.__setattr__(self, "_variables_master_key", resolved)
        return resolved


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, built on first use rather than at import."""
    return Settings()


def reset_settings_cache() -> None:
    """Drop the cached settings. For tests that change the environment."""
    get_settings.cache_clear()
