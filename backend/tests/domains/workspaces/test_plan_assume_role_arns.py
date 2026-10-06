"""The workspace setting naming the reader roles a plan session may assume."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.domains.runs import vending

READER = "arn:aws:iam::488386929690:role/WebbPulse-Terraform-Route53-Reader"
OTHER_READER = "arn:aws:iam::123456789012:role/path/Other-Reader"
READ_ONLY = [{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}]


def patch(auth_client, workspace: dict[str, Any], value: Any):
    """PATCH the workspace's reader roles."""
    return auth_client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"plan_assume_role_arns": value})


def test_a_new_workspace_names_no_reader_roles(workspace):
    """The list starts empty."""
    assert workspace["plan_assume_role_arns"] == []


def test_create_stores_trimmed_exact_arns(auth_client):
    """Create keeps the ARNs as given, trimmed."""
    from tests.conftest import WORKSPACE_PAYLOAD

    response = auth_client.post(
        "/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"plan_assume_role_arns": [f" {READER} ", OTHER_READER]}
    )
    assert response.status_code == 201, response.text
    assert response.json()["plan_assume_role_arns"] == [READER, OTHER_READER]


def test_patch_sets_and_null_clears_the_list(auth_client, workspace):
    """A PATCH replaces the list and an explicit null empties it."""
    response = patch(auth_client, workspace, [READER])
    assert response.status_code == 200, response.text
    assert response.json()["plan_assume_role_arns"] == [READER]
    response = patch(auth_client, workspace, None)
    assert response.status_code == 200, response.text
    assert response.json()["plan_assume_role_arns"] == []


@pytest.mark.parametrize(
    "value",
    [
        ["arn:aws:iam::488386929690:role/*"],
        ["arn:aws:iam::488386929690:role/Reader-?"],
        ["arn:aws:iam::*:role/Reader"],
        ["arn:aws:iam::48838692969:role/Reader"],
        ["arn:aws:iam::488386929690:user/Reader"],
        ["arn:aws-us-gov:iam::488386929690:role/Reader"],
        ["arn:aws:iam::488386929690:role/"],
        [""],
        [READER, READER],
        [f"arn:aws:iam::488386929690:role/{'r' * 140}"],
        [f"arn:aws:iam::123456789012:role/R{index}" for index in range(11)],
    ],
)
def test_anything_but_a_short_list_of_exact_role_arns_is_refused(auth_client, workspace, value):
    """Wildcards, malformed ARNs, repeats, oversized entries and long lists are all 422s."""
    assert patch(auth_client, workspace, value).status_code == 422


@pytest.fixture
def sts_requests(monkeypatch) -> list[dict[str, Any]]:
    """The AssumeRole requests the bundle route makes, answered without AWS."""
    requests: list[dict[str, Any]] = []

    class RecordingSTS:
        def assume_role(self, **kwargs: Any) -> dict[str, Any]:
            """Record the request and hand back keys."""
            requests.append(kwargs)
            return {
                "Credentials": {
                    "AccessKeyId": "ASIA",
                    "SecretAccessKey": "secret",
                    "SessionToken": "token",
                    "Expiration": "2026-09-28T13:00:00Z",
                }
            }

    monkeypatch.setattr(vending, "_sts", lambda settings, credentials=None: RecordingSTS())
    return requests


def test_a_plan_session_may_assume_the_named_readers(auth_client, runner_client, workspace, created_run, sts_requests):
    """The plan's run role request keeps ReadOnlyAccess and adds `sts:AssumeRole` on the list alone."""
    assert patch(auth_client, workspace, [READER]).status_code == 200
    body = runner_client.get(f"/api/v1/runs/{created_run['run_id']}/bundle").json()
    assert body["phase"] == "plan"
    run_role_request = sts_requests[1]
    assert run_role_request["PolicyArns"] == READ_ONLY
    document = json.loads(run_role_request["Policy"])
    assert document["Statement"][-1] == {
        "Sid": "PlanAssumeReaderRoles",
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": [READER],
    }
    assert [statement for statement in document["Statement"] if statement["Action"] == "sts:AssumeRole"] == [
        document["Statement"][-1]
    ]


def test_the_apply_session_is_not_touched_by_the_list(
    auth_client, runner_client, workspace, awaiting_confirmation, sts_requests
):
    """An apply session carries no session policy whatever the list holds."""
    assert patch(auth_client, workspace, [READER]).status_code == 200
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"/api/v1/runs/{run_id}/confirm")
    body = runner_client.get(f"/api/v1/runs/{run_id}/bundle").json()
    assert body["phase"] == "apply"
    run_role_request = sts_requests[-2]
    assert "Policy" not in run_role_request
    assert "PolicyArns" not in run_role_request
