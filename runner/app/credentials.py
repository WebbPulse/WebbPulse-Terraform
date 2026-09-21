"""Assuming the per workspace run role with the phase session policy."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.models import RunRole

if TYPE_CHECKING:
    from mypy_boto3_sts.client import STSClient
else:
    STSClient = object


class CredentialsError(RuntimeError):
    """The run role could not be assumed."""


def assume_run_role(client: STSClient, role: RunRole, run_id: str, phase: str) -> dict[str, str]:
    """Assume the run role and return the engine's AWS credential environment.

    The inline document and the managed policy ARNs are both session policies
    and the session gets their union intersected with the role. A failure is
    reported with the botocore exception's own type and message, which name the
    malformed field or the denied action and carry no credential material.
    """
    session_name = f"{run_id}-{phase}"[:64]
    request: dict[str, object] = {
        "RoleArn": role.role_arn,
        "RoleSessionName": session_name,
        "ExternalId": role.external_id,
        "DurationSeconds": role.duration_seconds,
    }
    if role.session_policy:
        request["Policy"] = json.dumps(role.session_policy)
    if role.session_policy_arns:
        request["PolicyArns"] = [{"arn": arn} for arn in role.session_policy_arns]
    try:
        response = client.assume_role(**request)  # type: ignore[arg-type]
    except Exception as error:
        raise CredentialsError(f"assume role failed: {type(error).__name__}: {error}") from error
    credentials = response["Credentials"]
    return {
        "AWS_ACCESS_KEY_ID": credentials["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": credentials["SecretAccessKey"],
        "AWS_SESSION_TOKEN": credentials["SessionToken"],
    }
