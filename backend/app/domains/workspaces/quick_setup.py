"""AWS quick setup: one CloudFormation stack that creates a workspace's run role.

The equivalent of HCP Terraform's AWS quick setup for this control plane's model,
where the runner assumes a role in the customer's account. The person gives the
account id and picks a permissions policy; the API derives the role ARN from the
account id and the workspace's deterministic role name, saves it on the workspace,
and answers with an AWS CloudFormation quick create link. Creating that stack is
the only thing left to do in AWS, and nothing has to be copied back.

The template is rendered per environment with the runner task roles baked in as
the only trusted principals, so a stack cannot be pointed at anything else by
editing a parameter. The role name and the external id are parameters, pinned by
patterns to this environment's role prefix and to a workspace id. CloudFormation
only reads templates from S3, so the rendered body is written once to the
artifacts bucket under a key derived from its hash and handed out as a short
lived presigned GET: the bucket stays private and nothing is made public.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Final, Literal
from urllib.parse import quote

from ...common.composition.settings import Settings, get_settings
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


def template_body(settings: Settings) -> dict[str, Any]:
    """The CloudFormation template for this environment's run roles.

    Raises:
        QuickSetupUnavailable: No runner task role is configured to trust.
    """
    principals = settings.runner_task_role_arns
    if not principals or not settings.RUN_ROLE_NAME_PREFIX:
        raise QuickSetupUnavailable("No runner task roles are configured for this environment.")
    policy_values = [arn for arn in PERMISSIONS_POLICY_ARNS.values() if arn is not None]
    return {
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
    account_id: str,
    permissions: PermissionsChoice,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Save the derived role ARN on the workspace and return the quick create link.

    Saving first is what removes the copy back step: once the stack exists the
    next run assumes the role, and the run role check turns `connected`. Saving the
    same ARN again changes nothing, so the link can be reopened freely.

    Raises:
        WorkspaceNotFound: No such workspace.
        QuickSetupUnavailable: This deployment cannot serve the template.
    """
    from webbpulse.storage import presigned_get

    resolved = settings or get_settings()
    get_workspace(workspace_id, settings=resolved)
    key = ensure_template(resolved)
    role_name = service.run_role_name(workspace_id, settings=resolved)
    role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"
    service.update_workspace(workspace_id, {"run_role_arn": role_arn}, settings=resolved)

    region = resolved.AWS_REGION_NAME or "us-west-2"
    download = presigned_get(
        resolved.ARTIFACTS_BUCKET,
        key,
        TEMPLATE_URL_EXPIRES_IN,
        region_name=region,
        endpoint_url=resolved.s3_endpoint_url,
    )
    policy_arn = PERMISSIONS_POLICY_ARNS[permissions]
    name = role_name
    return {
        "account_id": account_id,
        "role_arn": role_arn,
        "role_name": role_name,
        "stack_name": name,
        "region": region,
        "permissions_policy_arn": policy_arn,
        "expires_in": TEMPLATE_URL_EXPIRES_IN,
        "console_url": quick_create_url(
            region=region,
            template_url=download.url,
            name=name,
            parameters={
                "RoleName": role_name,
                "ExternalId": workspace_id,
                "PermissionsPolicyArn": policy_arn or NO_POLICY,
            },
        ),
    }
