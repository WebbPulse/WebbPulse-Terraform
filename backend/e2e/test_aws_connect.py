"""Connect AWS against the deployed stage and a real AWS CloudFormation stack.

Quick setup hands back a quick create link carrying a one-time connect token. The case
creates that stack itself, waits for the stack's custom resource to report back through
the connect topic, and checks that the workspace recorded the account, the run role, the
read only plan role and a verification run that plans with it. Deleting the stack must then disconnect the workspace.

A real stack creates an IAM role, so the case runs only where `E2E_AWS_CONNECT_ACCOUNT`
names the throwaway e2e account and the job's own credentials resolve to exactly that
account. It never runs against a project account.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

ACCOUNT_VARIABLE = "E2E_AWS_CONNECT_ACCOUNT"
POLL_SECONDS = 5
REPORT_TIMEOUT_SECONDS = 300
DISCONNECT_TIMEOUT_SECONDS = 300

pytestmark = pytest.mark.skipif(
    not os.environ.get(ACCOUNT_VARIABLE, "").strip(),
    reason=f"{ACCOUNT_VARIABLE} is unset, so there is no throwaway account to create a stack in",
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


def cloudformation(region: str) -> Any:
    """A CloudFormation client, refused unless the credentials are the throwaway account."""
    import boto3

    session = boto3.session.Session(region_name=region)
    account = str(session.client("sts").get_caller_identity()["Account"])
    expected = os.environ[ACCOUNT_VARIABLE].strip()
    if account != expected:
        pytest.fail(f"the job's AWS credentials are for {account}, not the e2e account {expected}")
    return session.client("cloudformation")


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

    client = cloudformation(str(setup["region"]))
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
    finally:
        client.delete_stack(StackName=stack_name)
        client.get_waiter("stack_delete_complete").wait(StackName=stack_name)

    wait_for_connection(api, workspace_id, "disconnected", DISCONNECT_TIMEOUT_SECONDS)
