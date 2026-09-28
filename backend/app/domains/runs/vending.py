"""Vending a run phase its AWS credentials, so no runner task can reach a workspace role itself.

Everything the engine can execute during a plan can also reach the runner task's
own role, through the container credential endpoint. That role therefore holds
nothing but its log stream. Instead the runs function, when it serves the phase's
bundle, assumes the dedicated vending role, which is the only principal a
workspace run role trusts, and from there assumes two roles:

- the workspace's run role, with the workspace id as the external id and the
  phase's session policy, whose keys reach the providers;
- the control plane's state role, narrowed by a session policy to this
  workspace's state prefix, whose keys only the S3 backend reads.

Both sessions last at most an hour, the role chaining ceiling, which a long
phase outlives. The runner therefore asks for a fresh pair before they expire
through `POST /runs/{id}/credentials`, which vends again exactly as the bundle
did, for the phase the run is still in.

Google and Azure are reached the way HCP Terraform's dynamic credentials reach
them: the control plane is an OIDC issuer, and a workspace that sets
`TFC_GCP_PROVIDER_AUTH` or `TFC_AZURE_PROVIDER_AUTH` gets an identity token per
phase, signed with the issuer's KMS key, which the cloud trades for its own
credentials. The tokens are minted beside the AWS sessions and refreshed with
them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from ...common.composition.settings import Settings
from . import session_policy
from .schemas.run import Phase

VENDING_SESSION_NAME: Final = "webbpulse-terraform-vending"
"""The session name the runs function holds the vending role under."""

_log = logging.getLogger(__name__)


class VendingUnavailable(Exception):
    """This deployment names no vending or state role, so no phase can get credentials."""


class RunRoleAssumeFailed(Exception):
    """The workspace's run role refused the vending role, which is the run role check's failure."""


class StateCredentialsFailed(Exception):
    """The state role could not be assumed, which is a fault in this deployment, not the workspace's."""


class WorkloadIdentityMisconfigured(Exception):
    """The workspace asks for Google or Azure workload identity without what that needs."""


class WorkloadIdentityUnavailable(VendingUnavailable):
    """This deployment has no OIDC issuer, or its signing key refused, so no token can be minted."""


@dataclass(frozen=True)
class VendedCredentials:
    """One session's keys, as the bundle hands them to the runner."""

    access_key_id: str
    secret_access_key: str
    session_token: str
    expiration: str

    def as_dict(self) -> dict[str, str]:
        """The keys as the bundle's JSON object."""
        return {
            "access_key_id": self.access_key_id,
            "secret_access_key": self.secret_access_key,
            "session_token": self.session_token,
            "expiration": self.expiration,
        }


def _credentials(response: dict[str, Any]) -> VendedCredentials:
    """The keys an `AssumeRole` response carries."""
    raw = response["Credentials"]
    expiration = raw.get("Expiration")
    return VendedCredentials(
        access_key_id=str(raw["AccessKeyId"]),
        secret_access_key=str(raw["SecretAccessKey"]),
        session_token=str(raw["SessionToken"]),
        expiration=expiration.isoformat() if hasattr(expiration, "isoformat") else str(expiration or ""),
    )


def _sts(settings: Settings, credentials: VendedCredentials | None = None) -> Any:
    """An STS client, as the function's own role or as the given session. Imported late."""
    import boto3

    region = settings.AWS_REGION_NAME or None
    if credentials is None:
        return boto3.client("sts", region_name=region)
    return boto3.client(
        "sts",
        region_name=region,
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key=credentials.secret_access_key,
        aws_session_token=credentials.session_token,
    )


def _error_text(error: Exception) -> str:
    """A botocore failure as its code and message, which carry no key material."""
    from botocore.exceptions import ClientError

    if isinstance(error, ClientError):
        detail = error.response.get("Error", {})
        return f"{detail.get('Code', 'ClientError')}: {detail.get('Message', '')}".strip()
    return type(error).__name__


def session_name(run_id: str, phase: Phase) -> str:
    """The session name a phase's sessions carry, so CloudTrail names the run."""
    return f"{run_id}-{phase}"[:64]


def run_role_request(
    role_arn: str, workspace_id: str, run_id: str, phase: Phase, *, duration_seconds: int
) -> dict[str, Any]:
    """The `AssumeRole` request for a workspace's run role in one phase."""
    policy = session_policy.for_phase(phase)
    request: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": session_name(run_id, phase),
        "ExternalId": workspace_id,
        "DurationSeconds": duration_seconds,
    }
    if policy.document is not None:
        request["Policy"] = json.dumps(policy.document)
    if policy.policy_arns:
        request["PolicyArns"] = [{"arn": arn} for arn in policy.policy_arns]
    return request


def state_role_request(workspace_id: str, run_id: str, phase: Phase, *, settings: Settings) -> dict[str, Any]:
    """The `AssumeRole` request for the state role, narrowed to one workspace's prefix."""
    return {
        "RoleArn": settings.RUN_STATE_ROLE_ARN,
        "RoleSessionName": session_name(run_id, phase),
        "DurationSeconds": settings.run_credentials_duration_seconds,
        "Policy": json.dumps(
            session_policy.state_policy(settings.STATE_BUCKET, workspace_id, settings.STATE_KMS_KEY_ARN, phase)
        ),
    }


def vend(
    *,
    role_arn: str,
    workspace_id: str,
    run_id: str,
    phase: Phase,
    settings: Settings,
) -> tuple[VendedCredentials, VendedCredentials]:
    """The provider and state credentials for one phase of one run.

    Raises:
        VendingUnavailable: No vending or state role is configured.
        RunRoleAssumeFailed: The workspace's run role is unset or refused the vending role.
        StateCredentialsFailed: The vending role could not reach the state role.
    """
    if not settings.RUN_CREDENTIALS_ROLE_ARN or not settings.RUN_STATE_ROLE_ARN:
        raise VendingUnavailable("RUN_CREDENTIALS_ROLE_ARN and RUN_STATE_ROLE_ARN must both be set")
    if not role_arn:
        raise RunRoleAssumeFailed("The workspace has no run role.")
    try:
        vending = _credentials(
            _sts(settings).assume_role(
                RoleArn=settings.RUN_CREDENTIALS_ROLE_ARN,
                RoleSessionName=VENDING_SESSION_NAME,
                DurationSeconds=settings.run_credentials_duration_seconds,
            )
        )
    except Exception as error:
        raise VendingUnavailable(f"the vending role could not be assumed: {_error_text(error)}") from error
    client = _sts(settings, vending)
    try:
        provider = _credentials(
            client.assume_role(
                **run_role_request(
                    role_arn,
                    workspace_id,
                    run_id,
                    phase,
                    duration_seconds=settings.run_credentials_duration_seconds,
                )
            )
        )
    except Exception as error:
        raise RunRoleAssumeFailed(f"assume role failed: {_error_text(error)}") from error
    try:
        state = _credentials(client.assume_role(**state_role_request(workspace_id, run_id, phase, settings=settings)))
    except Exception as error:
        raise StateCredentialsFailed(f"the state role could not be assumed: {_error_text(error)}") from error
    _log.info(
        "Vended a run phase its credentials.",
        extra={"event": "runs.credentials.vended", "run_id": run_id, "phase": phase, "workspace_id": workspace_id},
    )
    return provider, state


WORKLOAD_IDENTITY_LIFETIME_SECONDS: Final = 3600
"""How long a workload identity token is valid, HCP's hour. The runner refreshes it with the AWS sessions."""

KMS_SIGNING_ALGORITHM: Final = "RSASSA_PKCS1_V1_5_SHA_256"
"""The KMS algorithm behind RS256."""

KMS_RAW_MESSAGE_LIMIT: Final = 4096
"""The largest message KMS signs raw; a longer signing input is sent as its SHA-256 digest."""

AZURE_DEFAULT_AUDIENCE: Final = "api://AzureADTokenExchange"
"""The audience an Azure federated identity credential expects unless the workspace names another."""

GCP_AUDIENCE_PREFIX: Final = "//iam.googleapis.com/"
"""What a Google workload identity provider's full resource name is prefixed with as an audience."""


def _flag(environment: Mapping[str, str], key: str) -> bool:
    """Whether a workspace variable is set to true, as HCP reads its `TFC_*_PROVIDER_AUTH` flags."""
    return environment.get(key, "").strip().lower() == "true"


def _phase_value(environment: Mapping[str, str], provider: str, suffix: str, phase: Phase) -> str:
    """HCP's per phase override, `TFC_<P>_<PHASE>_<suffix>`, falling back to `TFC_<P>_RUN_<suffix>`."""
    specific = environment.get(f"TFC_{provider}_{phase.upper()}_{suffix}", "").strip()
    return specific or environment.get(f"TFC_{provider}_RUN_{suffix}", "").strip()


def _b64url(data: bytes) -> str:
    """Base64url without padding, as JOSE encodes each part."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _json_part(value: Mapping[str, Any]) -> str:
    """One JOSE header or claims part."""
    return _b64url(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def key_id(key_arn: str) -> str:
    """The `kid` a KMS key is published under by the issuer: its key id."""
    return key_arn.rsplit("/", 1)[-1]


def workload_identity_claims(
    *,
    issuer: str,
    audience: str,
    workspace_id: str,
    workspace_name: str,
    run_id: str,
    phase: Phase,
    now: int,
) -> dict[str, Any]:
    """The claims of one phase's identity token, named as HCP Terraform names them."""
    return {
        "iss": issuer,
        "sub": f"workspace:{workspace_id}:run_phase:{phase}",
        "aud": audience,
        "iat": now,
        "nbf": now,
        "exp": now + WORKLOAD_IDENTITY_LIFETIME_SECONDS,
        "jti": uuid.uuid4().hex,
        "terraform_workspace_id": workspace_id,
        "terraform_workspace_name": workspace_name,
        "terraform_run_id": run_id,
        "terraform_run_phase": phase,
    }


def _kms(settings: Settings) -> Any:
    """A KMS client as the function's own role. Imported late."""
    import boto3

    return boto3.client("kms", region_name=settings.AWS_REGION_NAME or None)


def sign_token(claims: Mapping[str, Any], *, key_arn: str, kms: Any) -> str:
    """A compact RS256 JWT over `claims`, signed by KMS so the private key never leaves it."""
    header = {"alg": "RS256", "kid": key_id(key_arn), "typ": "JWT"}
    signing_input = f"{_json_part(header)}.{_json_part(claims)}"
    message = signing_input.encode("ascii")
    if len(message) <= KMS_RAW_MESSAGE_LIMIT:
        request = {"Message": message, "MessageType": "RAW"}
    else:
        request = {"Message": hashlib.sha256(message).digest(), "MessageType": "DIGEST"}
    response = kms.sign(KeyId=key_arn, SigningAlgorithm=KMS_SIGNING_ALGORITHM, **request)
    return f"{signing_input}.{_b64url(bytes(response['Signature']))}"


def _requested(environment: Mapping[str, str], phase: Phase) -> dict[str, dict[str, str]]:
    """What each requested cloud needs besides its token, checked before anything is signed.

    Raises:
        WorkloadIdentityMisconfigured: A requested cloud lacks its provider or client id.
    """
    wanted: dict[str, dict[str, str]] = {}
    if _flag(environment, "TFC_GCP_PROVIDER_AUTH"):
        provider_name = environment.get("TFC_GCP_WORKLOAD_PROVIDER_NAME", "").strip().strip("/")
        if not provider_name:
            raise WorkloadIdentityMisconfigured(
                "TFC_GCP_PROVIDER_AUTH is true but TFC_GCP_WORKLOAD_PROVIDER_NAME is not set."
            )
        provider_audience = f"{GCP_AUDIENCE_PREFIX}{provider_name}"
        wanted["gcp"] = {
            "token_audience": environment.get("TFC_GCP_WORKLOAD_IDENTITY_AUDIENCE", "").strip() or provider_audience,
            "audience": provider_audience,
            "service_account_email": _phase_value(environment, "GCP", "SERVICE_ACCOUNT_EMAIL", phase),
        }
    if _flag(environment, "TFC_AZURE_PROVIDER_AUTH"):
        client_id = _phase_value(environment, "AZURE", "CLIENT_ID", phase)
        if not client_id:
            raise WorkloadIdentityMisconfigured(
                f"TFC_AZURE_PROVIDER_AUTH is true but neither TFC_AZURE_RUN_CLIENT_ID nor "
                f"TFC_AZURE_{phase.upper()}_CLIENT_ID is set."
            )
        wanted["azure"] = {
            "token_audience": environment.get("TFC_AZURE_WORKLOAD_IDENTITY_AUDIENCE", "").strip()
            or AZURE_DEFAULT_AUDIENCE,
            "client_id": client_id,
        }
    return wanted


def mint_workload_identity(
    *,
    environment: Mapping[str, str],
    workspace_id: str,
    workspace_name: str,
    run_id: str,
    phase: Phase,
    settings: Settings,
    now: int | None = None,
    kms: Any = None,
) -> dict[str, dict[str, str]] | None:
    """One phase's Google and Azure identity tokens, for the clouds the workspace asks for.

    None when it asks for neither. Each entry carries the token, its expiry and
    what the runner needs to hand it to the provider: the workload provider
    audience and service account for Google, the client id for Azure. Nothing
    about a token is logged.

    Raises:
        WorkloadIdentityMisconfigured: A requested cloud lacks its provider or client id.
        WorkloadIdentityUnavailable: There is no issuer, or KMS refused to sign.
    """
    wanted = _requested(environment, phase)
    if not wanted:
        return None
    if not settings.OIDC_ISSUER_URL or not settings.OIDC_SIGNING_KEY_ARN:
        raise WorkloadIdentityUnavailable(
            "This control plane has no OIDC issuer, so Google and Azure workload identity are unavailable."
        )
    issued_at = int(time.time()) if now is None else now
    expiration = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(issued_at + WORKLOAD_IDENTITY_LIFETIME_SECONDS))
    client = kms or _kms(settings)
    minted: dict[str, dict[str, str]] = {}
    for cloud, details in wanted.items():
        claims = workload_identity_claims(
            issuer=settings.OIDC_ISSUER_URL.rstrip("/"),
            audience=details["token_audience"],
            workspace_id=workspace_id,
            workspace_name=workspace_name,
            run_id=run_id,
            phase=phase,
            now=issued_at,
        )
        try:
            token = sign_token(claims, key_arn=settings.OIDC_SIGNING_KEY_ARN, kms=client)
        except Exception as error:
            raise WorkloadIdentityUnavailable(f"the OIDC signing key could not sign: {_error_text(error)}") from error
        entry = {key: value for key, value in details.items() if key != "token_audience"}
        minted[cloud] = {"token": token, "expiration": expiration, **entry}
    _log.info(
        "Minted a run phase its workload identity tokens.",
        extra={
            "event": "runs.workload_identity.minted",
            "run_id": run_id,
            "phase": phase,
            "workspace_id": workspace_id,
            "clouds": sorted(minted),
        },
    )
    return minted


__all__ = [
    "RunRoleAssumeFailed",
    "StateCredentialsFailed",
    "VendedCredentials",
    "VendingUnavailable",
    "WorkloadIdentityMisconfigured",
    "WorkloadIdentityUnavailable",
    "key_id",
    "mint_workload_identity",
    "run_role_request",
    "session_name",
    "sign_token",
    "state_role_request",
    "vend",
    "workload_identity_claims",
]
