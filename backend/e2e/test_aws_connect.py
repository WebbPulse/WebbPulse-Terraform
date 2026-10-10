"""Connect AWS against the deployed stage and a real AWS CloudFormation stack.

Quick setup hands back a quick create link carrying a one-time connect token. The case
creates that stack itself, waits for the stack's custom resource to report back through
the connect topic, and checks that the workspace recorded the account, the run role, the
read only plan role and a verification run that plans with it. Deleting the stack must then disconnect the workspace.

The stack is created in the staging plane's own account, so the case also proves the
plane never manages itself: both roles carry the plane's run role boundary, and an IAM
policy simulation shows neither of them, nor an administrator bounded the same way, can
read or change the plane's resources while an unrelated resource stays reachable.

A real stack creates IAM roles, so the case runs only where `E2E_AWS_CONNECT_ACCOUNT`
names the account and the credentials resolve to exactly that account. Where
`E2E_AWS_CONNECT_ROLE_ARN` is set, the job assumes that role first: it can create only
roles that carry the boundary, and attach only ReadOnlyAccess. Only free IAM resources
are made, and the stack is deleted however the case ends.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

import pytest
from botocore.exceptions import WaiterError

ACCOUNT_VARIABLE = "E2E_AWS_CONNECT_ACCOUNT"
ROLE_VARIABLE = "E2E_AWS_CONNECT_ROLE_ARN"
BOUNDARY_VARIABLE = "E2E_RUN_ROLE_BOUNDARY_ARN"
RUN_ROLE_VARIABLE = "E2E_RUN_ROLE_ARN"
UNRELATED_OBJECT_ARN = "arn:aws:s3:::webbpulse-e2e-unrelated-bucket/key"
ALLOW_EVERYTHING = json.dumps(
    {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]}
)
POLL_SECONDS = 5
REPORT_TIMEOUT_SECONDS = 300
DISCONNECT_TIMEOUT_SECONDS = 300

pytestmark = pytest.mark.skipif(
    not os.environ.get(ACCOUNT_VARIABLE, "").strip(),
    reason=f"{ACCOUNT_VARIABLE} is unset, so there is no account to create a stack in",
)


def quick_create_request(console_url: str) -> tuple[str, str, list[dict[str, str]]]:
    """The template URL, stack name and parameters a quick create link carries."""
    fragment = urlsplit(console_url).fragment
    query = fragment.split("?", 1)[1]
    fields = parse_qsl(query, keep_blank_values=True)
    template_url = next(value for key, value in fields if key == "templateURL")
    stack_name = next(value for key, value in fields if key == "stackName")
    parameters = [
        {"ParameterKey": key.removeprefix("param_"), "ParameterValue": value}
        for key, value in fields
        if key.startswith("param_")
    ]
    return template_url, stack_name, parameters


def aws_session(region: str) -> Any:
    """A session in the e2e account, through the Quick setup role where one is named.

    Refused unless the credentials resolve to exactly `E2E_AWS_CONNECT_ACCOUNT`.
    """
    import boto3

    session = boto3.session.Session(region_name=region)
    role_arn = os.environ.get(ROLE_VARIABLE, "").strip()
    if role_arn:
        credentials = session.client("sts").assume_role(RoleArn=role_arn, RoleSessionName="e2e-aws-connect")[
            "Credentials"
        ]
        session = boto3.session.Session(
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
    account = str(session.client("sts").get_caller_identity()["Account"])
    expected = os.environ[ACCOUNT_VARIABLE].strip()
    if account != expected:
        pytest.fail(f"the job's AWS credentials are for {account}, not the e2e account {expected}")
    return session


def role_name(arn: str) -> str:
    """The role name an ARN ends with, past any path."""
    return arn.rsplit("/", 1)[-1]


def plane_prefix(run_role_arn: str) -> str:
    """The plane's resource name prefix, which every Quick setup run role name starts with."""
    return role_name(run_role_arn).rsplit("-workspace-", 1)[0]


def plane_resources(account: str, region: str, prefix: str) -> dict[str, str]:
    """One action and resource per kind of plane resource the boundary must close off."""
    return {
        "s3:GetObject": f"arn:aws:s3:::{prefix}-state/workspaces/ws-x/env/terraform.tfstate",
        "dynamodb:PutItem": f"arn:aws:dynamodb:{region}:{account}:table/{prefix}-workspaces",
        "lambda:UpdateFunctionCode": f"arn:aws:lambda:{region}:{account}:function:{prefix}-runs",
        "iam:UpdateAssumeRolePolicy": f"arn:aws:iam::{account}:role/{prefix}-run-credentials",
        "states:StartExecution": f"arn:aws:states:{region}:{account}:stateMachine:{prefix}-run",
    }


def decision(result: dict[str, Any]) -> str:
    """The single evaluation decision a one action, one resource simulation answers."""
    (evaluation,) = result["EvaluationResults"]
    return str(evaluation["EvalDecision"])


def boundary_document(iam: Any, boundary_arn: str) -> str:
    """The boundary's default version as JSON text."""
    version = iam.get_policy(PolicyArn=boundary_arn)["Policy"]["DefaultVersionId"]
    document = iam.get_policy_version(PolicyArn=boundary_arn, VersionId=version)["PolicyVersion"]["Document"]
    if isinstance(document, str):
        document = json.loads(unquote(document))
    return json.dumps(document)


def assert_the_plane_is_out_of_reach(session: Any, role_arns: list[str]) -> None:
    """Every vendable role carries the boundary, and the boundary keeps the plane out of reach.

    ReadOnlyAccess alone would allow the reads, so a denied read of a plane resource beside
    an allowed read of an unrelated one is the boundary at work.
    """
    iam = session.client("iam")
    boundary_arn = os.environ[BOUNDARY_VARIABLE].strip()
    account = os.environ[ACCOUNT_VARIABLE].strip()
    region = str(session.region_name)
    prefix = plane_prefix(role_arns[0])
    targets = plane_resources(account, region, prefix)

    durable = os.environ.get(RUN_ROLE_VARIABLE, "").strip()
    for arn in [*role_arns, *([durable] if durable else [])]:
        role = iam.get_role(RoleName=role_name(arn))["Role"]
        assert role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn") == boundary_arn, (
            f"{role_name(arn)} does not carry the run role boundary"
        )

    reads = {"s3:GetObject": targets["s3:GetObject"], "lambda:GetFunction": targets["lambda:UpdateFunctionCode"]}
    for arn in role_arns:
        for action, resource in reads.items():
            result = iam.simulate_principal_policy(PolicySourceArn=arn, ActionNames=[action], ResourceArns=[resource])
            assert decision(result) != "allowed", f"{role_name(arn)} may {action} on the plane"
        control = iam.simulate_principal_policy(
            PolicySourceArn=arn, ActionNames=["s3:GetObject"], ResourceArns=[UNRELATED_OBJECT_ARN]
        )
        assert decision(control) == "allowed", f"{role_name(arn)} cannot read an unrelated object"

    boundary = boundary_document(iam, boundary_arn)
    bounded_admin = {"PolicyInputList": [ALLOW_EVERYTHING], "PermissionsBoundaryPolicyInputList": [boundary]}
    for action, resource in [*targets.items(), ("iam:CreatePolicyVersion", boundary_arn)]:
        result = iam.simulate_custom_policy(**bounded_admin, ActionNames=[action], ResourceArns=[resource])
        assert decision(result) == "explicitDeny", f"a bounded administrator may {action} on {resource}"
    unbounded_role = f"arn:aws:iam::{account}:role/e2e-unbounded-{secrets.token_hex(3)}"
    result = iam.simulate_custom_policy(**bounded_admin, ActionNames=["iam:CreateRole"], ResourceArns=[unbounded_role])
    assert decision(result) == "explicitDeny", "a bounded administrator may create an unbounded role"
    result = iam.simulate_custom_policy(
        **bounded_admin, ActionNames=["s3:GetObject"], ResourceArns=[UNRELATED_OBJECT_ARN]
    )
    assert decision(result) == "allowed", "the boundary also denies an unrelated resource"


def remove_role(iam: Any, name: str) -> None:
    """Delete one role the stack could not, with its policies, tolerating a role that is already gone."""
    try:
        for attached in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
            iam.detach_role_policy(RoleName=name, PolicyArn=attached["PolicyArn"])
        for policy_name in iam.list_role_policies(RoleName=name)["PolicyNames"]:
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        iam.delete_role(RoleName=name)
    except iam.exceptions.NoSuchEntityException:
        return


def delete_the_stack(session: Any, stack_name: str) -> None:
    """Delete the stack, and if CloudFormation cannot, remove its roles directly and fail with the reason.

    A stack stuck in DELETE_FAILED would otherwise leave its roles behind in the plane's account, so the
    roles it could not delete are removed by hand, the stack is deleted retaining them, and the test fails.
    """
    client = session.client("cloudformation")
    client.delete_stack(StackName=stack_name)
    try:
        client.get_waiter("stack_delete_complete").wait(StackName=stack_name)
        return
    except WaiterError:
        pass
    resources = client.describe_stack_resources(StackName=stack_name)["StackResources"]
    stuck = [resource for resource in resources if resource["ResourceStatus"] == "DELETE_FAILED"]
    iam = session.client("iam")
    for resource in stuck:
        if resource["ResourceType"] == "AWS::IAM::Role" and resource.get("PhysicalResourceId"):
            remove_role(iam, role_name(str(resource["PhysicalResourceId"])))
    client.delete_stack(StackName=stack_name, RetainResources=[resource["LogicalResourceId"] for resource in stuck])
    client.get_waiter("stack_delete_complete").wait(StackName=stack_name)
    reasons = "; ".join(f"{r['LogicalResourceId']}: {r.get('ResourceStatusReason', '')}" for r in stuck)
    pytest.fail(f"CloudFormation could not delete {stack_name}, its roles were removed directly: {reasons}")


def wait_for_connection(api: Any, workspace_id: str, status: str, timeout: int) -> dict[str, Any]:
    """The workspace's connection record once it reaches `status`."""
    deadline = time.monotonic() + timeout
    connection: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api.get(f"/api/v1/workspaces/{workspace_id}")
        assert response.status_code == 200, f"reading the workspace answered {response.status_code}"
        connection = dict(response.json().get("aws_connection") or {})
        if connection.get("status") == status:
            return connection
        time.sleep(POLL_SECONDS)
    pytest.fail(f"the connection stayed {connection.get('status')!r}, never {status!r}")


@pytest.fixture
def unconnected_workspace(api: Any, e2e_env: Any, created_resources: list[Any]) -> Iterator[dict[str, Any]]:
    """A workspace with no run role, registered for the session sweep before it is used."""
    body = {
        "name": f"{e2e_env.resource_prefix}{secrets.token_hex(3)}",
        "engine": "terraform",
        "engine_version": "1.11.0",
    }
    response = api.post("/api/v1/workspaces", json=body)
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the workspace answered {response.status_code}")
    created = dict(response.json())
    created_resources.append({"kind": "workspace", "id": created["workspace_id"]})
    yield created


@pytest.mark.usefixtures("stepped_up_session")
def test_a_stack_connects_and_disconnects_the_workspace(api: Any, unconnected_workspace: dict[str, Any]) -> None:
    """The stack's report connects the workspace, and deleting the stack disconnects it.

    Quick setup is step-up gated, so the case starts from a stepped up session.
    """
    workspace_id = str(unconnected_workspace["workspace_id"])
    response = api.post(
        f"/api/v1/workspaces/{workspace_id}/run-role/quick-setup",
        json={"permissions": "read_only"},
    )
    assert response.status_code == 200, f"quick setup answered {response.status_code}"
    setup = response.json()
    assert setup["reports_back"] is True
    template_url, stack_name, parameters = quick_create_request(str(setup["console_url"]))

    session = aws_session(str(setup["region"]))
    client = session.client("cloudformation")
    client.create_stack(
        StackName=stack_name,
        TemplateURL=template_url,
        Parameters=parameters,
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )
    try:
        client.get_waiter("stack_create_complete").wait(StackName=stack_name)
        connection = wait_for_connection(api, workspace_id, "connected", REPORT_TIMEOUT_SECONDS)
        expected_account = os.environ[ACCOUNT_VARIABLE].strip()
        assert connection["account_id"] == expected_account
        assert str(connection["role_arn"]).startswith(f"arn:aws:iam::{expected_account}:role/")
        assert connection.get("run_id")
        assert str(connection["plan_role_arn"]).startswith(f"arn:aws:iam::{expected_account}:role/")
        assert "-workspace-plan/plan-" in str(connection["plan_role_arn"])
        workspace = api.get(f"/api/v1/workspaces/{workspace_id}").json()
        assert workspace["plan_role_arn"] == connection["plan_role_arn"]
        run = api.get(f"/api/v1/runs/{connection['run_id']}").json()
        assert run["plan_role_arn"] == connection["plan_role_arn"]
        assert_the_plane_is_out_of_reach(session, [str(connection["role_arn"]), str(connection["plan_role_arn"])])
    finally:
        delete_the_stack(session, stack_name)

    wait_for_connection(api, workspace_id, "disconnected", DISCONNECT_TIMEOUT_SECONDS)
