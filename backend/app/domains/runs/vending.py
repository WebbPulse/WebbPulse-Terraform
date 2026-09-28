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
"""

from __future__ import annotations

import json
import logging
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


__all__ = [
    "RunRoleAssumeFailed",
    "StateCredentialsFailed",
    "VendedCredentials",
    "VendingUnavailable",
    "run_role_request",
    "session_name",
    "state_role_request",
    "vend",
]
