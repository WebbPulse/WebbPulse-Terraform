"""The run role: optional at create, its setup on every response, and the check.

The success path runs against moto, which assumes any role and reports the account
from the ARN, so the happy case is exercised as a real pair of STS calls. The
failure paths are driven through a fake client, because moto has no way to refuse
an AssumeRole the way a missing trust policy does.
"""

from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.domains.workspaces import service
from tests.conftest import RUN_ROLE_NAME_PREFIX, RUNNER_TASK_ROLE_ARNS, WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"

ROLE_ARN = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-workspace-other"


class _RefusingSts:
    """An STS client whose AssumeRole raises the error code it was built with."""

    def __init__(self, code: str) -> None:
        """Hold the STS error code every AssumeRole will raise."""
        self.code = code

    def assume_role(self, **_: Any) -> dict[str, Any]:
        """Raise the configured error, the way STS refuses an untrusting role."""
        raise ClientError(
            {"Error": {"Code": self.code, "Message": f"User is not authorized: {ROLE_ARN}"}},
            "AssumeRole",
        )


@pytest.fixture
def refusing_sts(monkeypatch):
    """A factory swapping the service's STS client for one that refuses."""

    def install(code: str) -> None:
        """Make every AssumeRole raise `code`."""
        monkeypatch.setattr(service, "_sts", lambda _settings: _RefusingSts(code))

    return install


def _create(auth_client, **overrides: Any) -> dict[str, Any]:
    """Create a workspace from the shared payload with `overrides` applied."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_without_a_run_role_is_accepted(auth_client):
    """The role cannot exist before the workspace, so create does not demand one."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    response = auth_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["run_role_arn"] is None


def test_create_with_an_explicit_null_run_role_is_accepted(auth_client):
    """A client that sends the field as null is treated as one that omitted it."""
    body = _create(auth_client, run_role_arn=None)
    assert body["run_role_arn"] is None


def test_create_still_rejects_a_too_short_run_role(auth_client):
    """The ARN format check survives the field becoming optional."""
    response = auth_client.post(BASE, json={**WORKSPACE_PAYLOAD, "run_role_arn": "arn:aws"})
    assert response.status_code == 422


def test_every_workspace_response_carries_the_run_role_setup(auth_client, workspace):
    """The three values needed to build the role ride on the create response."""
    setup = workspace["run_role_setup"]
    assert setup["principal_arns"] == RUNNER_TASK_ROLE_ARNS
    assert setup["principal_arn"] == RUNNER_TASK_ROLE_ARNS[0]
    assert setup["external_id"] == workspace["workspace_id"]
    assert setup["role_name"] == RUN_ROLE_NAME_PREFIX + workspace["workspace_id"].removeprefix("ws-")


def test_the_get_and_list_responses_carry_the_setup_too(auth_client, workspace):
    """A client that reloads a workspace sees the same setup the create returned."""
    fetched = auth_client.get(f"{BASE}/{workspace['workspace_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["run_role_setup"] == workspace["run_role_setup"]

    listed = auth_client.get(BASE)
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["run_role_setup"] == workspace["run_role_setup"]


def test_the_derived_role_name_fits_inside_the_iam_ceiling(workspace, settings):
    """A name past sixty four characters could not be created at all."""
    name = service.run_role_name(workspace["workspace_id"], settings=settings)
    assert len(name) <= service.IAM_ROLE_NAME_MAX_LENGTH


def test_the_derived_role_name_fits_for_the_longest_stack_prefix(settings, monkeypatch):
    """The production prefix plus a ULID is the longest name the stack can derive."""
    monkeypatch.setattr(settings, "RUN_ROLE_NAME_PREFIX", "webbpulse-terraform-staging-workspace-")
    name = service.run_role_name("ws-01JBQ0000000000000000000AA", settings=settings)
    assert len(name) <= service.IAM_ROLE_NAME_MAX_LENGTH


def test_a_new_workspace_has_never_been_checked(workspace):
    """Nothing claims a connection before one was made."""
    assert workspace["run_role_checked_at"] is None
    assert workspace["run_role_account_id"] is None


def test_the_check_records_the_account_it_reached(auth_client, workspace):
    """A role that answers stamps the row with the account and the moment."""
    workspace_id = workspace["workspace_id"]
    response = auth_client.post(f"{BASE}/{workspace_id}/run-role/check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"connected": True, "account_id": "870550636948", "error": None}

    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_account_id"] == "870550636948"
    assert stored["run_role_checked_at"]


def test_the_check_returns_no_credentials(auth_client, workspace):
    """The temporary credentials never leave the function."""
    response = auth_client.post(f"{BASE}/{workspace['workspace_id']}/run-role/check")
    assert set(response.json()) == {"connected", "account_id", "error"}


def test_an_access_denied_reads_as_a_trust_problem(auth_client, workspace, refusing_sts):
    """STS will not say which half failed, so the message names both."""
    refusing_sts("AccessDenied")
    response = auth_client.post(f"{BASE}/{workspace['workspace_id']}/run-role/check")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is False
    assert body["account_id"] is None
    assert body["error"] == service.RUN_ROLE_ACCESS_DENIED_MESSAGE


def test_a_refused_check_never_echoes_the_sts_message(auth_client, workspace, refusing_sts):
    """The ARN the error carried is not handed back to the caller."""
    refusing_sts("AccessDenied")
    response = auth_client.post(f"{BASE}/{workspace['workspace_id']}/run-role/check")
    assert ROLE_ARN not in response.text


def test_a_refused_check_clears_an_earlier_success(auth_client, workspace, refusing_sts):
    """A stale success must not outlive the trust policy that earned it."""
    workspace_id = workspace["workspace_id"]
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True

    refusing_sts("AccessDenied")
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is False

    stored = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert stored["run_role_checked_at"] is None
    assert stored["run_role_account_id"] is None


def test_an_absent_role_reads_as_a_missing_role(auth_client, workspace, refusing_sts):
    """A role that is not there yet is named as such rather than as a trust failure."""
    refusing_sts("ValidationError")
    response = auth_client.post(f"{BASE}/{workspace['workspace_id']}/run-role/check")
    assert response.json()["error"] == "No role with that ARN exists."


def test_the_check_is_400_when_no_role_is_configured(auth_client):
    """A workspace with nothing to assume is a request error, not a failed check."""
    created = _create(auth_client, run_role_arn=None)
    response = auth_client.post(f"{BASE}/{created['workspace_id']}/run-role/check")
    assert response.status_code == 400, response.text
    assert response.json()["error_code"] == "RUN_ROLE_MISSING"


def test_the_check_is_404_for_an_absent_workspace(auth_client):
    """A well formed id that names nothing is a 404."""
    response = auth_client.post(f"{BASE}/ws-01JBQ0000000000000000000AA/run-role/check")
    assert response.status_code == 404


def test_the_check_needs_the_write_scope(scoped_client):
    """Reading a workspace does not entitle a caller to assume its role."""
    with scoped_client("workspaces:read") as client:
        response = client.post(f"{BASE}/ws-01JBQ0000000000000000000AA/run-role/check")
    assert response.status_code == 403


def test_a_patched_run_role_clears_the_check(auth_client, workspace):
    """The previous success belonged to the previous role."""
    workspace_id = workspace["workspace_id"]
    assert auth_client.post(f"{BASE}/{workspace_id}/run-role/check").json()["connected"] is True

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"run_role_arn": ROLE_ARN})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_role_arn"] == ROLE_ARN
    assert body["run_role_checked_at"] is None
    assert body["run_role_account_id"] is None


def test_an_unrelated_patch_keeps_the_check(auth_client, workspace):
    """Editing the description says nothing about the role."""
    workspace_id = workspace["workspace_id"]
    auth_client.post(f"{BASE}/{workspace_id}/run-role/check")

    response = auth_client.patch(f"{BASE}/{workspace_id}", json={"description": "Edited."})
    assert response.status_code == 200, response.text
    assert response.json()["run_role_account_id"] == "870550636948"


def test_patching_the_same_run_role_keeps_the_check(auth_client, workspace):
    """A no-op write is not a reason to forget a connection that still holds."""
    workspace_id = workspace["workspace_id"]
    auth_client.post(f"{BASE}/{workspace_id}/run-role/check")

    response = auth_client.patch(
        f"{BASE}/{workspace_id}",
        json={"run_role_arn": WORKSPACE_PAYLOAD["run_role_arn"]},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_account_id"] == "870550636948"


def test_a_run_role_can_be_attached_after_the_fact(auth_client):
    """The setup the create returned is followed by a patch carrying the ARN."""
    created = _create(auth_client, run_role_arn=None)
    response = auth_client.patch(
        f"{BASE}/{created['workspace_id']}",
        json={"run_role_arn": ROLE_ARN},
    )
    assert response.status_code == 200, response.text
    assert response.json()["run_role_arn"] == ROLE_ARN


def test_the_setup_names_every_runner_task_role(workspace):
    """A trust policy naming only one phase's role would break the other phase."""
    assert len(workspace["run_role_setup"]["principal_arns"]) == len(RUNNER_TASK_ROLE_ARNS)
