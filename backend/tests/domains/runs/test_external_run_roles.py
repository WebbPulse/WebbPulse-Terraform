"""Run roles outside the `<prefix>-workspace-*` naming, such as the factory's `<Workspace>-Terraform` roles.

The IAM grant on the vending role (`external_run_role_arns`) is what admits them,
so the API must accept and vend any role ARN as given, with no name pattern of
its own that would quietly refuse a role the grant lists.
"""

from typing import Any

import pytest

from app.domains.runs import vending

FACTORY_ROLE_ARN = "arn:aws:iam::488386929690:role/WebbPulse-Platform-Terraform"
READ_ONLY = [{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}]


class RecordingSTS:
    """Records each AssumeRole and answers with keys named after the role."""

    def __init__(self, requests: list[dict[str, Any]]) -> None:
        self.requests = requests

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        """Record the request and hand back keys."""
        self.requests.append(kwargs)
        name = kwargs["RoleArn"].rsplit("/", 1)[-1]
        return {
            "Credentials": {
                "AccessKeyId": f"ASIA-{name}",
                "SecretAccessKey": f"secret-{name}",
                "SessionToken": f"token-{name}",
                "Expiration": "2026-09-26T13:00:00Z",
            }
        }


@pytest.fixture
def sts_requests(monkeypatch):
    """The AssumeRole requests the bundle route makes, answered without AWS."""
    requests: list[dict[str, Any]] = []
    monkeypatch.setattr(vending, "_sts", lambda settings, credentials=None: RecordingSTS(requests))
    return requests


@pytest.fixture
def workspace(auth_client):
    """A workspace whose run role is a factory role in another account."""
    from tests.conftest import WORKSPACE_PAYLOAD

    response = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"run_role_arn": FACTORY_ROLE_ARN})
    assert response.status_code == 201, response.text
    return response.json()


def test_a_factory_role_is_stored_as_given(workspace):
    """Create keeps a role ARN that matches no workspace role naming."""
    assert workspace["run_role_arn"] == FACTORY_ROLE_ARN


def test_a_factory_role_can_be_set_by_patch(auth_client):
    """A workspace created without a role can be pointed at a factory role later, as the factory does."""
    from tests.conftest import WORKSPACE_PAYLOAD

    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    created = auth_client.post("/api/v1/workspaces", json=payload).json()
    response = auth_client.patch(
        f"/api/v1/workspaces/{created['workspace_id']}",
        json={"run_role_arn": FACTORY_ROLE_ARN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] == FACTORY_ROLE_ARN


def test_a_plan_vends_the_factory_role_read_only(runner_client, created_run, workspace, sts_requests, settings):
    """The plan phase assumes the factory role through the vending role, keyed by workspace id, read only."""
    body = runner_client.get(f"/api/v1/runs/{created_run['run_id']}/bundle").json()
    assert body["phase"] == "plan"
    vending_request, run_role_request, _state_request = sts_requests
    assert vending_request["RoleArn"] == settings.RUN_CREDENTIALS_ROLE_ARN
    assert run_role_request["RoleArn"] == FACTORY_ROLE_ARN
    assert run_role_request["ExternalId"] == workspace["workspace_id"]
    assert run_role_request["PolicyArns"] == READ_ONLY
    assert body["aws_credentials"]["access_key_id"] == "ASIA-WebbPulse-Platform-Terraform"


PLAN_ROLE_ARN = "arn:aws:iam::488386929690:role/terraform/plan-reader"


def test_a_plan_with_a_plan_role_vends_only_the_plan_role(
    auth_client, runner_client, created_run, workspace, sts_requests, settings
):
    """The plan never assumes the run role; its keys come from the plan role, tagged with the plan phase."""
    auth_client.patch(f"/api/v1/workspaces/{workspace['workspace_id']}", json={"plan_role_arn": PLAN_ROLE_ARN})

    body = runner_client.get(f"/api/v1/runs/{created_run['run_id']}/bundle").json()

    _vending_request, plan_request, _state_request = sts_requests
    assert all(request["RoleArn"] != FACTORY_ROLE_ARN for request in sts_requests)
    assert {"Key": "run_phase", "Value": "plan"} in plan_request["Tags"]
    assert plan_request["RoleArn"] == PLAN_ROLE_ARN
    assert plan_request["ExternalId"] == workspace["workspace_id"]
    assert plan_request["PolicyArns"] == READ_ONLY
    assert body["aws_credentials"]["access_key_id"] == "ASIA-plan-reader"
    assert body["run_role_arn"] == PLAN_ROLE_ARN


def test_an_apply_ignores_the_plan_role(sts_requests, settings):
    """An apply assumes the run role alone, with no session policy."""
    provider, _state = vending.vend(
        role_arn=FACTORY_ROLE_ARN,
        workspace_id="ws-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        run_id="run-01ARZ3NDEKTSV4RRFFQ69G5FAV",
        phase="apply",
        settings=settings,
        workspace_name="example",
        plan_role_arn=PLAN_ROLE_ARN,
    )
    _vending_request, run_role_request, _state_request = sts_requests
    assert run_role_request["RoleArn"] == FACTORY_ROLE_ARN
    assert "PolicyArns" not in run_role_request
    assert provider.access_key_id == "ASIA-WebbPulse-Platform-Terraform"
