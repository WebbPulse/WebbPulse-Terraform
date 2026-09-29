"""Answer a Quick setup stack's custom resource, connecting the workspace it names.

CloudFormation publishes each lifecycle event of the stack's `Connection` resource
to the AWS connect topic, which delivers it raw to a queue this function consumes.
The body is CloudFormation's own request, so it carries no `kind`; `is_connect_request`
recognises it by shape instead.

Nothing here trusts the request beyond the connect token. The account comes from
the stack's ARN, which CloudFormation writes, and the reported role has to be the
one this workspace's stack in that account creates. A create with a valid token
stages the role, consumes the token, starts the verification run and answers
SUCCESS; an unknown or expired token answers FAILED, so the stack rolls back and
leaves no role behind. A delete always answers SUCCESS, quickly, and forgets the
role only if this stack's connection is still the workspace's.

The answer is a PUT to the presigned `ResponseURL`. Only a failed PUT raises, so
SQS redelivers and the retry answers again; everything else is answered, since a
stack left without an answer waits an hour before it fails.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
from typing import Any, Final, Mapping
from urllib.parse import urlsplit

from boto3.dynamodb.conditions import Key
from webbpulse.dynamodb import ConditionFailed, new_ulid, now_iso

from ....common.composition.settings import Settings, get_settings
from ....common.db import repositories
from ....common.db.tables import CONFIG_VERSIONS_BY_WORKSPACE_INDEX
from ....common.workspaces import aws_connect
from ....common.workspaces import reads as workspace_reads
from .. import service

_log = logging.getLogger(__name__)

RESPONSE_TIMEOUT_SECONDS: Final = 10.0
"""How long the answer PUT may take before the delivery is retried."""

RESPONSE_HOST_PREFIX: Final = "cloudformation-custom-resource-response-"
"""Every CloudFormation response bucket's name starts with this."""

ACTOR: Final = {"kind": "system", "id": "aws-connect", "display_name": "AWS Quick setup"}
"""Who the verification run is recorded as started by."""

SOURCE: Final = "aws_connect"
"""The run source the verification run carries."""

STARTER_CONFIG: Final = "terraform {}\n"
"""The configuration a workspace with none yet verifies with: nothing to plan but the role."""

_REQUIRED: Final = ("RequestType", "ResponseURL", "StackId", "RequestId", "LogicalResourceId")


class ResponseFailed(Exception):
    """CloudFormation's response bucket refused the answer, so the delivery is retried."""


def _body(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The record's body as a mapping, or `None` when it is not a JSON object."""
    raw = record.get("body")
    if not isinstance(raw, str):
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def is_connect_request(record: Mapping[str, Any]) -> bool:
    """Whether a record is a CloudFormation custom resource request."""
    body = _body(record)
    return body is not None and "kind" not in body and all(key in body for key in _REQUIRED)


def _response_url_allowed(url: str) -> bool:
    """Whether `url` is a CloudFormation response bucket over HTTPS, the only place an answer goes."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return (
        parts.scheme == "https"
        and parts.port is None
        and not parts.username
        and host.startswith(RESPONSE_HOST_PREFIX)
        and (host.endswith(".amazonaws.com") or host.endswith(".amazonaws.com.cn"))
    )


def http_client() -> Any:
    """The HTTP client answers go out on. A seam the suite replaces with a mock transport."""
    import httpx

    return httpx.Client(timeout=RESPONSE_TIMEOUT_SECONDS, follow_redirects=False)


def _answer(
    request: Mapping[str, Any],
    status: str,
    physical_id: str,
    *,
    reason: str = "",
    data: Mapping[str, str] | None = None,
) -> None:
    """PUT the response CloudFormation waits on.

    Raises:
        ResponseFailed: The bucket did not accept it.
    """
    import httpx

    body = {
        "Status": status,
        "Reason": reason or status,
        "PhysicalResourceId": physical_id,
        "StackId": request["StackId"],
        "RequestId": request["RequestId"],
        "LogicalResourceId": request["LogicalResourceId"],
        "Data": dict(data or {}),
    }
    try:
        with http_client() as client:
            response = client.put(
                str(request["ResponseURL"]),
                content=json.dumps(body).encode(),
                headers={"Content-Type": ""},
            )
    except httpx.HTTPError as error:
        raise ResponseFailed(type(error).__name__) from error
    if response.status_code >= 300:
        raise ResponseFailed(str(response.status_code))


def _properties(request: Mapping[str, Any], key: str = "ResourceProperties") -> dict[str, str]:
    """The resource properties as strings, empty when absent."""
    raw = request.get(key)
    if not isinstance(raw, Mapping):
        return {}
    return {str(name): str(value) for name, value in raw.items()}


def _failed_id(request: Mapping[str, Any]) -> str:
    """A physical id for a resource that was never created, so its delete is a no-op."""
    return str(request.get("PhysicalResourceId") or f"failed-{request['RequestId']}")


def _latest_uploaded_config(workspace_id: str, *, settings: Settings) -> str | None:
    """The newest config version whose tarball is in the bucket, or `None`."""
    for item in repositories.config_versions(settings).iter_query(
        Key("workspace_id").eq(workspace_id),
        index_name=CONFIG_VERSIONS_BY_WORKSPACE_INDEX,
        ascending=False,
    ):
        reconciled = workspace_reads.reconcile_config_version(item, settings=settings)
        if str(reconciled.get("status", "")) == "uploaded":
            return str(reconciled["config_version_id"])
    return None


def _starter_tarball(working_directory: str) -> bytes:
    """A tarball holding only an empty `terraform {}` block in the working directory."""
    content = STARTER_CONFIG.encode()
    name = f"{working_directory}/main.tf" if working_directory else "main.tf"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.size = len(content)
        info.mode = 0o644
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _starter_config(workspace: Mapping[str, Any], *, settings: Settings) -> str:
    """Write a starter config version for a workspace with none, and return its id."""
    import boto3

    workspace_id = str(workspace["workspace_id"])
    config_version_id = f"{workspace_reads.CONFIG_VERSION_ID_PREFIX}{new_ulid()}"
    key = workspace_reads.config_key(workspace_id, config_version_id)
    body = _starter_tarball(str(workspace.get("working_directory") or ""))
    boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url).put_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=key, Body=body, ContentType="application/gzip"
    )
    try:
        repositories.config_versions(settings).put(
            {
                "config_version_id": config_version_id,
                "workspace_id": workspace_id,
                "key": key,
                "status": "uploaded",
                "size_bytes": len(body),
                "source": SOURCE,
                "created_at": now_iso(),
            }
        )
    except ConditionFailed:
        pass
    return config_version_id


VERIFY_START_FAILED_MESSAGE = "The verification run could not be started. Start a plan only run to verify the role."
"""What a connection whose verification run never started shows."""


def _verify(workspace_id: str, account_id: str, pending: bool, request_id: str, *, settings: Settings) -> None:
    """Start the plan only run that proves the runner can assume the new role.

    A staged role gets a `run_role_check` run, which switches the workspace over when
    it finishes; a role taken at once gets an ordinary plan only run, whose end
    records the check. Failing to start it shows the connection's verification as
    failed, which the workspace page offers to retry, and never fails the stack.
    """
    try:
        workspace = workspace_reads.get_workspace(workspace_id, settings=settings)
        config_version_id = _latest_uploaded_config(workspace_id, settings=settings) or _starter_config(
            workspace, settings=settings
        )
        run = service.create_run(
            {
                "workspace_id": workspace_id,
                "config_version_id": config_version_id,
                "plan_only": True,
                "run_role_check": pending,
                "message": f"Verify the connection to AWS account {account_id}",
            },
            actor=ACTOR,
            source=SOURCE,
            settings=settings,
        )
    except Exception as error:  # noqa: BLE001
        _log.exception(
            "Could not start the verification run for a connected AWS account.",
            extra={
                "event": "runs.aws_connect.verify_failed",
                "workspace_id": workspace_id,
                "error": type(error).__name__,
            },
        )
        aws_connect.fail_verification(workspace_id, request_id, VERIFY_START_FAILED_MESSAGE, settings=settings)
        return
    aws_connect.record_run(workspace_id, request_id, str(run["run_id"]), settings=settings)


def _plan_role_allowed(
    plan_role_arn: str, workspace_id: str, partition: str, account_id: str, *, settings: Settings
) -> bool:
    """Whether a reported plan role is none, or the one this workspace's stack in that account creates."""
    return not plan_role_arn or plan_role_arn == aws_connect.expected_plan_role_arn(
        workspace_id, partition, account_id, settings=settings
    )


def _create(request: Mapping[str, Any], *, settings: Settings, physical_id: str | None = None) -> None:
    """Connect the workspace a create names, or refuse it so the stack rolls back."""
    properties = _properties(request)
    workspace_id = properties.get("WorkspaceId", "")
    stack = aws_connect.stack_account(str(request["StackId"]))
    refused = physical_id or _failed_id(request)
    if stack is None or not workspace_id:
        _answer(request, "FAILED", refused, reason="The request does not name a stack and a workspace.")
        return
    partition, account_id = stack
    role_arn = properties.get("RoleArn", "")
    if role_arn != aws_connect.expected_role_arn(workspace_id, partition, account_id, settings=settings):
        _answer(request, "FAILED", refused, reason="The role is not this workspace's run role in this account.")
        return
    plan_role_arn = properties.get("PlanRoleArn", "")
    if not _plan_role_allowed(plan_role_arn, workspace_id, partition, account_id, settings=settings):
        _answer(request, "FAILED", refused, reason="The plan role is not this workspace's plan role in this account.")
        return
    result = aws_connect.connect(
        workspace_id,
        token=properties.get("ConnectToken", ""),
        role_arn=role_arn,
        account_id=account_id,
        stack_id=str(request["StackId"]),
        request_id=str(request["RequestId"]),
        physical_id=physical_id,
        trust_version=properties.get("TrustVersion", ""),
        plan_role_arn=plan_role_arn,
        settings=settings,
    )
    if result.outcome == "invalid":
        _answer(request, "FAILED", refused, reason="The connect token is not valid. Start Quick setup again.")
        return
    if result.outcome == "expired":
        _answer(request, "FAILED", refused, reason="The connect token expired. Start Quick setup again.")
        return
    if result.outcome == "connected":
        _log.info(
            "Connected a workspace to an AWS account.",
            extra={"event": "runs.aws_connect.connected", "workspace_id": workspace_id, "account_id": account_id},
        )
        _verify(workspace_id, account_id, result.pending, str(request["RequestId"]), settings=settings)
    _answer(
        request,
        "SUCCESS",
        result.physical_id,
        data={"WorkspaceId": workspace_id, "AccountId": account_id},
    )


def _update(request: Mapping[str, Any], *, settings: Settings) -> None:
    """Keep a connection through a stack update, reconnect with a new token, refuse anything else.

    An update that leaves the workspace, the stack and the role as the connection
    recorded them is answered SUCCESS unchanged, which also covers the rollback of a
    refused update, and records the trust version and plan role the template now
    grants. A new token is a fresh connect under the same physical id. A different
    workspace or a role in another account is refused.
    """
    physical_id = str(request.get("PhysicalResourceId") or "")
    new = _properties(request)
    old = _properties(request, "OldResourceProperties")
    workspace_id = new.get("WorkspaceId", "")
    stack = aws_connect.stack_account(str(request["StackId"]))
    if not physical_id.startswith(aws_connect.PHYSICAL_ID_PREFIX) or stack is None or not workspace_id:
        _answer(request, "FAILED", _failed_id(request), reason="This resource never connected a workspace.")
        return
    connection = aws_connect.current_connection(workspace_id, settings=settings)
    unchanged = (
        connection.get("status") == "connected"
        and connection.get("physical_id") == physical_id
        and connection.get("stack_id") == request["StackId"]
        and connection.get("account_id") == stack[1]
        and connection.get("role_arn") == new.get("RoleArn")
    )
    if unchanged:
        plan_role_arn = new.get("PlanRoleArn", "")
        if not _plan_role_allowed(plan_role_arn, workspace_id, stack[0], stack[1], settings=settings):
            _answer(request, "FAILED", physical_id, reason="The plan role is not this workspace's plan role.")
            return
        trust_version = new.get("TrustVersion", "")
        if trust_version != str(connection.get(aws_connect.TRUST_VERSION_FIELD) or ""):
            aws_connect.record_trust_version(workspace_id, physical_id, trust_version, settings=settings)
        if plan_role_arn != str(connection.get(aws_connect.PLAN_ROLE_ATTRIBUTE) or ""):
            aws_connect.record_plan_role(workspace_id, physical_id, plan_role_arn, settings=settings)
        _answer(request, "SUCCESS", physical_id, data={"WorkspaceId": workspace_id, "AccountId": stack[1]})
        return
    if workspace_id != old.get("WorkspaceId") or new.get("ConnectToken") == old.get("ConnectToken"):
        _answer(request, "FAILED", physical_id, reason="A connected stack cannot move to another workspace or role.")
        return
    _create(request, settings=settings, physical_id=physical_id)


def _delete(request: Mapping[str, Any], *, settings: Settings) -> None:
    """Answer SUCCESS at once, forgetting the role first if this stack still provides it."""
    physical_id = str(request.get("PhysicalResourceId") or "")
    workspace_id = _properties(request).get("WorkspaceId", "")
    if physical_id.startswith(aws_connect.PHYSICAL_ID_PREFIX) and workspace_id:
        try:
            if aws_connect.disconnect(
                workspace_id, stack_id=str(request["StackId"]), physical_id=physical_id, settings=settings
            ):
                _log.info(
                    "Disconnected a workspace whose Quick setup stack was deleted.",
                    extra={"event": "runs.aws_connect.disconnected", "workspace_id": workspace_id},
                )
        except Exception as error:  # noqa: BLE001
            _log.exception(
                "Could not record a deleted Quick setup stack.",
                extra={
                    "event": "runs.aws_connect.delete_failed",
                    "workspace_id": workspace_id,
                    "error": type(error).__name__,
                },
            )
    _answer(request, "SUCCESS", physical_id or _failed_id(request))


def handle_record(record: Mapping[str, Any], *, settings: Settings | None = None) -> None:
    """Answer one custom resource request.

    A request whose response URL is not a CloudFormation response bucket is dropped
    unanswered: nothing may be sent anywhere else. A processing error answers
    FAILED rather than leaving the stack waiting.

    Raises:
        ResponseFailed: The answer could not be delivered, so SQS retries.
    """
    resolved = settings or get_settings()
    request = _body(record)
    if request is None:
        return
    if not _response_url_allowed(str(request.get("ResponseURL", ""))):
        _log.warning(
            "Dropped a custom resource request whose response URL is not CloudFormation's.",
            extra={"event": "runs.aws_connect.bad_response_url"},
        )
        return
    kind = str(request.get("RequestType", ""))
    try:
        if kind == "Delete":
            _delete(request, settings=resolved)
        elif kind == "Update":
            _update(request, settings=resolved)
        elif kind == "Create":
            _create(request, settings=resolved)
        else:
            _answer(request, "FAILED", _failed_id(request), reason="Unknown request type.")
    except ResponseFailed:
        raise
    except Exception as error:  # noqa: BLE001
        _log.exception(
            "Could not process a custom resource request.",
            extra={"event": "runs.aws_connect.failed", "request_type": kind, "error": type(error).__name__},
        )
        status = "SUCCESS" if kind == "Delete" else "FAILED"
        _answer(request, status, _failed_id(request), reason="WebbPulse Terraform could not process the request.")


__all__ = ["ResponseFailed", "handle_record", "is_connect_request"]
