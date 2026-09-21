"""The control plane's settings, on the shared package's base.

Every field is an environment variable Terraform sets on the function. The one
secret field, `variables_master_key`, resolves from the `APP_SECRETS_ARN` blob on
first read, so constructing this makes no Secrets Manager call and importing it
needs no credentials.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import model_validator
from webbpulse.config import BaseServiceSettings

VARIABLES_MASTER_KEY_ENTRY = "variables_master_key"
"""The app secret's key holding the base64 HKDF master key for sensitive variables."""

LOCALHOST_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
]

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

    STATE_BUCKET: str = ""
    ARTIFACTS_BUCKET: str = ""
    RUN_STATE_MACHINE_ARN: str = ""
    RUNNER_LOG_GROUP: str = ""
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
    """The prefix the identity module's own tables carry, set by Terraform to
    `local.prefix`.

    It cannot be derived from `ENVIRONMENT`: the stack slugs production to `prod`
    while `ENVIRONMENT` is the word `production`, so a derived prefix would name
    tables that do not exist there. `identity_table_prefix` falls back to the derived
    form only for the local stack and the suite, where the two do agree."""

    RUNNER_TASK_ROLE_ARN: str = ""
    """The runner task roles a workspace run role has to trust, comma separated.

    One role per phase, so a trust policy that names only the first leaves the
    apply phase unable to assume. `runner_task_role_arns` splits it."""

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
        """Derive the base class's lower case fields from this project's own."""
        object.__setattr__(
            self,
            "environment",
            ENVIRONMENT_ALIASES.get(self.ENVIRONMENT.strip().lower(), "local"),
        )
        object.__setattr__(self, "log_level", self.LOG_LEVEL.strip().upper())
        origins = [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]
        object.__setattr__(self, "cors_allow_origins", sorted(set(origins + LOCALHOST_ORIGINS)))
        if self.app_secret_arn and not self.app_secrets_arn:
            object.__setattr__(self, "app_secrets_arn", self.app_secret_arn)
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
