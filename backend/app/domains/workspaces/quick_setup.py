"""AWS quick setup: one CloudFormation stack that creates a workspace's run role.

The equivalent of HCP Terraform's AWS quick setup for this control plane's model,
where the runner assumes a role in the customer's account. The person gives the
account id and picks a permissions policy; the API derives the role ARN from the
account id and the workspace's deterministic role name, saves it on the workspace,
or stages it beside a role already in use, and answers with an AWS CloudFormation
quick create link. Creating that stack is the only thing left to do in AWS, and
nothing has to be copied back.

The template is rendered per environment with the runner task roles baked in as
the only trusted principals, so a stack cannot be pointed at anything else by
editing a parameter. The role name and the external id are parameters, pinned by
patterns to this environment's role prefix and to a workspace id. CloudFormation
only reads templates from S3, so the rendered body is written once to the
artifacts bucket under a key derived from its hash and handed out as a short
lived presigned GET: the bucket stays private and nothing is made public.

Where the environment has an AWS connect topic, the template also carries a custom
resource that reports the stack back, and the link carries a one-time connect
token. The account id is then optional: the stack's own ARN names the account, so
the person only clicks the link. See `app.common.workspaces.aws_connect`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, Literal
from urllib.parse import quote

from ...common.composition.settings import Settings, get_settings
from ...common.workspaces import aws_connect
from ...common.workspaces.reads import get_workspace
from . import service

PermissionsChoice = Literal["administrator", "power_user", "read_only", "none"]
"""Which AWS managed policy the stack attaches to the role, if any."""

PERMISSIONS_POLICY_ARNS: Final[dict[str, str | None]] = {
    "administrator": "arn:aws:iam::aws:policy/AdministratorAccess",
    "power_user": "arn:aws:iam::aws:policy/PowerUserAccess",
    "read_only": "arn:aws:iam::aws:policy/ReadOnlyAccess",
    "none": None,
}
"""The managed policy each choice attaches. `none` leaves the role for the person to scope."""

NO_POLICY: Final = "none"
"""The template parameter value that attaches no managed policy."""

TEMPLATE_KEY_PREFIX: Final = "templates/run-role/"
"""Where rendered templates live in the artifacts bucket, outside every lifecycle rule."""

TEMPLATE_CONTENT_TYPE: Final = "application/json"
"""The content type the template is stored and served with."""

TEMPLATE_URL_EXPIRES_IN: Final = 3600
"""One hour, long enough for CloudFormation to read the template at load and at create."""

MAX_SESSION_DURATION: Final = 3600
"""The role's session ceiling, matching the one hour the runner asks for per phase."""

WORKSPACE_TAG_KEY: Final = "webbpulse-terraform:workspace"
"""The tag that names the workspace a role belongs to, for finding it in the account."""

_uploaded_keys: set[str] = set()
"""Template keys this process already knows are in the bucket, to skip the HEAD."""


def reset_template_cache() -> None:
    """Forget which templates are known to be uploaded. Tests need this between moto contexts."""
    _uploaded_keys.clear()


class QuickSetupUnavailable(Exception):
    """This deployment cannot render a template: no runner principals or no bucket."""


CONNECTION_RESOURCE_TYPE: Final = "Custom::WebbPulseTerraformConnection"
"""The custom resource that reports the stack back to this environment."""


def _report_back(template: dict[str, Any], topic_arn: str) -> dict[str, Any]:
    """The template with the connect token parameter and the reporting custom resource.

    The token defaults to empty and the resource only exists when one is given, so
    a stack created from an older link or by hand still creates the role.
    """
    parameters = template["Parameters"] | {
        "ConnectToken": {
            "Type": "String",
            "Description": "One-time token that reports this stack back to the workspace. Keep the prefilled value.",
            "Default": "",
            "NoEcho": True,
            "AllowedPattern": f"^({aws_connect.TOKEN_PATTERN.strip('^$')})?$",
            "ConstraintDescription": "must be the token the link carried",
        }
    }
    conditions = template["Conditions"] | {
        "ReportBack": {"Fn::Not": [{"Fn::Equals": [{"Ref": "ConnectToken"}, ""]}]},
    }
    resources = template["Resources"] | {
        "Connection": {
            "Type": CONNECTION_RESOURCE_TYPE,
            "Condition": "ReportBack",
            "Properties": {
                "ServiceToken": topic_arn,
                "ConnectToken": {"Ref": "ConnectToken"},
                "WorkspaceId": {"Ref": "ExternalId"},
                "RoleArn": {"Fn::GetAtt": ["RunRole", "Arn"]},
            },
        }
    }
    interface = template["Metadata"]["AWS::CloudFormation::Interface"]
    groups = [*interface["ParameterGroups"], {"Label": {"default": "Connection"}, "Parameters": ["ConnectToken"]}]
    labels = interface["ParameterLabels"] | {"ConnectToken": {"default": "Connect token"}}
    metadata = {"AWS::CloudFormation::Interface": {"ParameterGroups": groups, "ParameterLabels": labels}}
    return template | {
        "Metadata": metadata,
        "Parameters": parameters,
        "Conditions": conditions,
        "Resources": resources,
    }


def template_body(settings: Settings) -> dict[str, Any]:
    """The CloudFormation template for this environment's run roles.

    Raises:
        QuickSetupUnavailable: No runner task role is configured to trust.
    """
    principals = settings.runner_task_role_arns
    if not principals or not settings.RUN_ROLE_NAME_PREFIX:
        raise QuickSetupUnavailable("No runner task roles are configured for this environment.")
    policy_values = [arn for arn in PERMISSIONS_POLICY_ARNS.values() if arn is not None]
    template: dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            "WebbPulse Terraform run role. Trusts only the WebbPulse Terraform runner, "
            "and only with this workspace id as the external id."
        ),
        "Metadata": {
            "AWS::CloudFormation::Interface": {
                "ParameterGroups": [
                    {"Label": {"default": "Workspace"}, "Parameters": ["RoleName", "ExternalId"]},
                    {"Label": {"default": "Permissions"}, "Parameters": ["PermissionsPolicyArn"]},
                ],
                "ParameterLabels": {
                    "RoleName": {"default": "Role name"},
                    "ExternalId": {"default": "Workspace id (external id)"},
                    "PermissionsPolicyArn": {"default": "Permissions policy"},
                },
            }
        },
        "Parameters": {
            "RoleName": {
                "Type": "String",
                "Description": "The runner only assumes roles with this prefix. Keep the prefilled name.",
                "AllowedPattern": f"^{re.escape(settings.RUN_ROLE_NAME_PREFIX)}[0-9A-Za-z]{{26}}$",
                "ConstraintDescription": f"must start with {settings.RUN_ROLE_NAME_PREFIX}",
            },
            "ExternalId": {
                "Type": "String",
                "Description": "The runner sends the workspace id as the external id when it assumes the role.",
                "AllowedPattern": "^ws-[0-9A-Za-z]{26}$",
                "ConstraintDescription": "must be a workspace id",
            },
            "PermissionsPolicyArn": {
                "Type": "String",
                "Description": (
                    "The AWS managed policy the role gets. Choose none to attach a narrower policy yourself."
                ),
                "Default": policy_values[0],
                "AllowedValues": [*policy_values, NO_POLICY],
            },
        },
        "Conditions": {
            "AttachPolicy": {"Fn::Not": [{"Fn::Equals": [{"Ref": "PermissionsPolicyArn"}, NO_POLICY]}]},
        },
        "Resources": {
            "RunRole": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": {"Ref": "RoleName"},
                    "Description": {"Fn::Sub": "Assumed by WebbPulse Terraform runs for workspace ${ExternalId}"},
                    "MaxSessionDuration": MAX_SESSION_DURATION,
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "WebbPulseTerraformRunner",
                                "Effect": "Allow",
                                "Principal": {"AWS": principals},
                                "Action": "sts:AssumeRole",
                                "Condition": {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}},
                            }
                        ],
                    },
                    "ManagedPolicyArns": {
                        "Fn::If": ["AttachPolicy", [{"Ref": "PermissionsPolicyArn"}], {"Ref": "AWS::NoValue"}]
                    },
                    "Tags": [{"Key": WORKSPACE_TAG_KEY, "Value": {"Ref": "ExternalId"}}],
                },
            }
        },
        "Outputs": {
            "RoleArn": {
                "Description": "The role WebbPulse Terraform runs assume. It is already saved on the workspace.",
                "Value": {"Fn::GetAtt": ["RunRole", "Arn"]},
            }
        },
    }
    if settings.AWS_CONNECT_TOPIC_ARN:
        return _report_back(template, settings.AWS_CONNECT_TOPIC_ARN)
    return template


def render_template(settings: Settings) -> tuple[str, str]:
    """The template as stable JSON text, with the bucket key its hash names."""
    text = json.dumps(template_body(settings), indent=2, sort_keys=True)
    digest = hashlib.sha256(text.encode()).hexdigest()[:32]
    return text, f"{TEMPLATE_KEY_PREFIX}{digest}.json"


def _s3(settings: Settings) -> Any:
    """An S3 client for the artifacts bucket."""
    import boto3

    return boto3.client("s3", region_name=settings.AWS_REGION_NAME or None, endpoint_url=settings.s3_endpoint_url)


def ensure_template(settings: Settings) -> str:
    """Write the rendered template to the bucket unless it is there, and return its key.

    The key is content addressed, so a changed template or runner role lands under a
    new key and an existing object never needs rewriting.

    Raises:
        QuickSetupUnavailable: No artifacts bucket or no runner principals.
    """
    if not settings.ARTIFACTS_BUCKET:
        raise QuickSetupUnavailable("ARTIFACTS_BUCKET is unset, so no template can be served.")
    text, key = render_template(settings)
    if key in _uploaded_keys:
        return key
    from botocore.exceptions import ClientError

    client = _s3(settings)
    try:
        client.head_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchKey", "NotFound"):
            raise
        client.put_object(
            Bucket=settings.ARTIFACTS_BUCKET,
            Key=key,
            Body=text.encode(),
            ContentType=TEMPLATE_CONTENT_TYPE,
        )
    _uploaded_keys.add(key)
    return key


def quick_create_url(*, region: str, template_url: str, name: str, parameters: dict[str, str]) -> str:
    """The AWS console link that opens the quick create page with everything filled in."""
    query = [("templateURL", template_url), ("stackName", name)]
    query.extend((f"param_{key}", value) for key, value in parameters.items())
    fragment = "&".join(f"{key}={quote(value, safe='')}" for key, value in query)
    return f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}#/stacks/quickcreate?{fragment}"


def start_quick_setup(
    workspace_id: str,
    account_id: str | None,
    permissions: PermissionsChoice,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Issue a connect token, stage the role for a given account, and return the link.

    With a connect topic the link carries a fresh one-time token, replacing any
    earlier one, and the stack reports its account and role back when it is
    created, so no account id is needed. A given account id still saves or stages
    the derived ARN at once, as it did before the topic existed: a workspace with
    no role takes it, and one already running as another role stages it as
    `pending_run_role_arn` until a verification run assumes it.

    Raises:
        WorkspaceNotFound: No such workspace.
        QuickSetupUnavailable: This deployment cannot serve the template, or has no
            topic to report back to and no account id was given.
    """
    from webbpulse.storage import presigned_get

    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    reports_back = bool(resolved.AWS_CONNECT_TOPIC_ARN)
    if not reports_back and not account_id:
        raise QuickSetupUnavailable("No connect topic is configured, so the account id is required.")
    key = ensure_template(resolved)
    role_name = service.run_role_name(workspace_id, settings=resolved)
    role_arn: str | None = None
    pending = False
    if account_id:
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
        updated = service.update_workspace(workspace_id, {"pending_run_role_arn": role_arn}, settings=resolved)
        pending = str(updated.get("pending_run_role_arn") or "") == role_arn

    region = resolved.AWS_REGION_NAME or "us-west-2"
    download = presigned_get(
        resolved.ARTIFACTS_BUCKET,
        key,
        TEMPLATE_URL_EXPIRES_IN,
        region_name=region,
        endpoint_url=resolved.s3_endpoint_url,
    )
    policy_arn = PERMISSIONS_POLICY_ARNS[permissions]
    parameters = {
        "RoleName": role_name,
        "ExternalId": workspace_id,
        "PermissionsPolicyArn": policy_arn or NO_POLICY,
    }
    connect_expires_at: str | None = None
    if reports_back:
        token, connect_expires_at = aws_connect.issue_token(workspace_id, settings=resolved)
        parameters["ConnectToken"] = token
    return {
        "account_id": account_id,
        "role_arn": role_arn,
        "pending": pending,
        "role_name": role_name,
        "stack_name": role_name,
        "region": region,
        "permissions_policy_arn": policy_arn,
        "expires_in": TEMPLATE_URL_EXPIRES_IN,
        "reports_back": reports_back,
        "connect_expires_at": connect_expires_at,
        "console_url": quick_create_url(
            region=region,
            template_url=download.url,
            name=role_name,
            parameters=parameters,
        ),
    }
