"""The workspace setting naming which secrets a plan may read, and the admin gate on every plan access field.

A confirmable plan of a workspace naming no secrets reads any secret its role can; a
speculative plan, `plan_only` or from a pull request, reads none. Naming secrets bounds every plan to them.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import ADMIN, ALL_SCOPES
from app.common.runs import session_policy
from app.domains.runs import service as runs_service
from app.domains.runs import vending
from app.domains.workspaces.router import PLAN_SESSION_POLICY_TOO_LARGE_CODE
from tests.conftest import WORKSPACE_PAYLOAD, person_headers, runner_token, seed_user

SECRET = "arn:aws:secretsmanager:us-west-2:870550636948:secret:example-prod/app-*"
OTHER_SECRET = "arn:aws:secretsmanager:*:870550636948:secret:example-prod/github-??????"
READER = "arn:aws:iam::488386929690:role/WebbPulse-Terraform-Route53-Reader"
PLAN_ROLE = "arn:aws:iam::870550636948:role/webbpulse-terraform-test-plan"
PERSON = "user-plan-access"


def _path(workspace: dict[str, Any]) -> str:
    """The workspace's API path."""
    return f"/api/v1/workspaces/{workspace['workspace_id']}"


def _person(app: Any, *, admin: bool) -> TestClient:
    """A person who just signed in, holding every scope but admin unless `admin`."""
    seed_user(PERSON, is_admin=admin)
    scopes = ALL_SCOPES if admin else tuple(scope for scope in ALL_SCOPES if scope != ADMIN)
    roles = ("admin",) if admin else ()
    return TestClient(app, headers=person_headers(user_id=PERSON, scopes=scopes, roles=roles))


def test_a_new_workspace_names_no_plan_secrets(workspace):
    """The list starts empty."""
    assert workspace["plan_secret_arns"] == []


def test_patch_sets_and_null_clears_the_list(auth_client, workspace):
    """A PATCH replaces the list, trimmed, and an explicit null empties it."""
    response = auth_client.patch(_path(workspace), json={"plan_secret_arns": [f" {SECRET} ", OTHER_SECRET]})
    assert response.status_code == 200, response.text
    assert response.json()["plan_secret_arns"] == [SECRET, OTHER_SECRET]
    response = auth_client.patch(_path(workspace), json={"plan_secret_arns": None})
    assert response.status_code == 200, response.text
    assert response.json()["plan_secret_arns"] == []


@pytest.mark.parametrize(
    "value",
    [
        ["arn:aws:secretsmanager:*:*:secret:*"],
        ["arn:aws:secretsmanager:us-west-2:*:secret:app"],
        ["arn:aws:secretsmanager:us-west-2:87055063694:secret:app"],
        ["arn:aws:ssm:us-west-2:870550636948:parameter/app"],
        ["arn:aws-us-gov:secretsmanager:us-west-2:870550636948:secret:app"],
        ["arn:aws:secretsmanager:us-west-2:870550636948:secret:"],
        ["*"],
        [""],
        [SECRET, SECRET],
        [f"arn:aws:secretsmanager:us-west-2:870550636948:secret:{'s' * 200}"],
        [f"arn:aws:secretsmanager:us-west-2:870550636948:secret:s{index}" for index in range(11)],
    ],
)
def test_anything_but_account_pinned_secret_patterns_is_refused(auth_client, workspace, value):
    """Another account's or any account's secrets, other services, repeats and long lists are all 422s."""
    assert auth_client.patch(_path(workspace), json={"plan_secret_arns": value}).status_code == 422


def test_readers_and_secrets_that_overflow_the_session_policy_are_refused(auth_client, workspace):
    """Each list may be within its own cap and the two together still too long for STS."""
    head = "arn:aws:iam::123456789012:role/"
    readers = [f"{head}{index:02d}{'r' * (140 - len(head) - 2)}" for index in range(10)]
    assert auth_client.patch(_path(workspace), json={"plan_assume_role_arns": readers}).status_code == 200

    secrets = [f"arn:aws:secretsmanager:us-west-2:870550636948:secret:{index}{'s' * 130}" for index in range(3)]

    response = auth_client.patch(_path(workspace), json={"plan_secret_arns": secrets})

    assert response.status_code == 422, response.text
    assert response.json()["error_code"] == PLAN_SESSION_POLICY_TOO_LARGE_CODE
    assert auth_client.get(_path(workspace)).json()["plan_secret_arns"] == []


@pytest.mark.parametrize(
    "body",
    [{"plan_secret_arns": [SECRET]}, {"plan_assume_role_arns": [READER]}, {"plan_role_arn": PLAN_ROLE}],
    ids=["plan secrets", "reader roles", "plan role"],
)
def test_a_non_admin_cannot_change_what_a_plan_reaches(app, auth_client, workspace, body):
    """Workspace write is not enough, on an edit or on a create, and a refusal writes nothing."""
    with _person(app, admin=False) as client:
        patched = client.patch(_path(workspace), json=body)
        created = client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"name": "planned"} | body)

    assert patched.status_code == 403, patched.text
    assert created.status_code == 403, created.text
    stored = auth_client.get(_path(workspace)).json()
    assert {field: stored[field] for field in body} != body
    assert "planned" not in [item["name"] for item in auth_client.get("/api/v1/workspaces").json()["items"]]


@pytest.mark.parametrize(
    "body",
    [{"plan_secret_arns": [SECRET]}, {"plan_assume_role_arns": [READER]}, {"plan_role_arn": PLAN_ROLE}],
    ids=["plan secrets", "reader roles", "plan role"],
)
def test_an_admin_changes_what_a_plan_reaches(app, body):
    """An admin who just signed in sets each field on a create."""
    with _person(app, admin=True) as client:
        response = client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD | {"name": "planned"} | body)

    assert response.status_code == 201, response.text


def test_resending_the_stored_lists_is_not_a_change(app, auth_client, workspace):
    """A settings form resending what is stored needs no admin."""
    auth_client.patch(_path(workspace), json={"plan_secret_arns": [SECRET], "plan_assume_role_arns": [READER]})

    with _person(app, admin=False) as client:
        response = client.patch(
            _path(workspace),
            json={"plan_secret_arns": [SECRET], "plan_assume_role_arns": [READER], "description": "edited"},
        )

    assert response.status_code == 200, response.text


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


def _secret_resource(app: Any, run: dict[str, Any], sts_requests: list[dict[str, Any]]) -> Any:
    """What the plan's run role session may read in Secrets Manager: a resource, or None for nothing."""
    with TestClient(app, headers={"Authorization": f"Bearer {runner_token(run['run_id'])}"}) as runner:
        body = runner.get(f"/api/v1/runs/{run['run_id']}/bundle").json()
    assert body["phase"] == "plan"
    request = sts_requests[1]
    assert request["PolicyArns"] == [{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}]
    if "Policy" not in request:
        return None
    by_sid = {statement["Sid"]: statement for statement in json.loads(request["Policy"])["Statement"]}
    if "PlanReadSecretValues" not in by_sid:
        assert "PlanDecryptViaSecretsAndSsm" not in by_sid
        return None
    return by_sid["PlanReadSecretValues"]["Resource"]


def test_a_confirmable_plan_with_no_list_reads_any_secret(app, created_run, sts_requests):
    """The default leaves a plan that may apply the reads its apply already holds."""
    assert _secret_resource(app, created_run, sts_requests) == session_policy.ANY_SECRET_ARN


def test_a_speculative_plan_with_no_list_reads_no_secret(app, plan_only_run, sts_requests):
    """A plan only run on a workspace naming no secrets gets no secret read and no decrypt."""
    assert _secret_resource(app, plan_only_run, sts_requests) is None


def test_a_speculative_plan_reads_only_its_allowlist(app, auth_client, workspace, plan_only_run, sts_requests):
    """A named list is the whole of what a speculative plan reads."""
    auth_client.patch(_path(workspace), json={"plan_secret_arns": [SECRET]})

    assert _secret_resource(app, plan_only_run, sts_requests) == [SECRET]


def test_a_confirmable_plan_reads_only_its_allowlist(app, auth_client, workspace, created_run, sts_requests):
    """A named list narrows a plan that may apply too."""
    auth_client.patch(_path(workspace), json={"plan_secret_arns": [SECRET, OTHER_SECRET]})

    assert _secret_resource(app, created_run, sts_requests) == [SECRET, OTHER_SECRET]


@pytest.mark.parametrize(
    ("run", "listed", "expected"),
    [
        ({"source": "vcs_pr", "plan_only": True}, [], []),
        ({"source": "vcs_pr", "plan_only": False}, [], []),
        ({"source": "vcs_pr", "plan_only": True}, [SECRET], [SECRET]),
        ({"source": "vcs", "plan_only": False}, [], None),
        ({"source": "api", "plan_only": True, "run_role_check": True}, [], None),
    ],
    ids=["pull request", "pull request not plan only", "pull request with a list", "push", "run role check"],
)
def test_which_runs_are_speculative(run, listed, expected):
    """A pull request plan is speculative whatever its flags; a run role check plans the real configuration."""
    assert runs_service._plan_secret_arns(run, {"plan_secret_arns": listed}) == expected


def test_the_apply_session_ignores_the_list(auth_client, workspace, awaiting_confirmation, sts_requests, app):
    """An apply carries no session policy whatever the list holds."""
    auth_client.patch(_path(workspace), json={"plan_secret_arns": [SECRET]})
    run_id = awaiting_confirmation["run_id"]
    auth_client.post(f"/api/v1/runs/{run_id}/confirm")

    with TestClient(app, headers={"Authorization": f"Bearer {runner_token(run_id)}"}) as runner:
        assert runner.get(f"/api/v1/runs/{run_id}/bundle").json()["phase"] == "apply"

    run_role_request = sts_requests[-2]
    assert "Policy" not in run_role_request
    assert "PolicyArns" not in run_role_request
