"""AWS quick setup: the saved ARN, the quick create link and the served template.

The template is what the customer's CloudFormation reads, so these tests fetch it
back out of the bucket and assert on the trust policy it carries rather than on
the rendering function alone.
"""

import json
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import boto3
import pytest

from app.common.composition import settings as settings_module
from app.domains.workspaces import quick_setup
from tests.conftest import ARTIFACTS_BUCKET, REGION, RUN_ROLE_NAME_PREFIX, RUNNER_TASK_ROLE_ARNS, WORKSPACE_PAYLOAD

BASE = "/api/v1/workspaces"
ACCOUNT = "123456789012"


@pytest.fixture(autouse=True)
def fresh_template_cache():
    """Each moto context starts with an empty bucket, so the upload memo must start empty too."""
    quick_setup.reset_template_cache()
    yield
    quick_setup.reset_template_cache()


def _create(auth_client) -> dict[str, Any]:
    """A workspace with no run role, the state quick setup starts from."""
    payload = {key: value for key, value in WORKSPACE_PAYLOAD.items() if key != "run_role_arn"}
    response = auth_client.post(BASE, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _start(auth_client, workspace_id: str, **body: Any):
    """Call the quick setup route with `body`, defaulting the account id."""
    return auth_client.post(f"{BASE}/{workspace_id}/run-role/quick-setup", json={"account_id": ACCOUNT, **body})


def _fragment(console_url: str) -> dict[str, str]:
    """The quick create parameters carried in the console link's fragment."""
    fragment = urlsplit(console_url).fragment
    path, _, query = fragment.partition("?")
    assert path == "/stacks/quickcreate"
    return dict(parse_qsl(query))


def _stored_template(key: str) -> dict[str, Any]:
    """The template object as CloudFormation would read it."""
    body = boto3.client("s3", region_name=REGION).get_object(Bucket=ARTIFACTS_BUCKET, Key=key)["Body"].read()
    return json.loads(body)


def test_quick_setup_saves_the_derived_arn(auth_client):
    """The account id and the derived role name are all the ARN needs, so it is saved at once."""
    workspace = _create(auth_client)
    workspace_id = workspace["workspace_id"]
    role_name = workspace["run_role_setup"]["role_name"]

    response = _start(auth_client, workspace_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pending"] is False
    assert body["role_arn"] == f"arn:aws:iam::{ACCOUNT}:role/{role_name}"
    assert body["role_name"] == role_name
    assert body["stack_name"] == role_name
    assert body["account_id"] == ACCOUNT
    assert body["region"] == REGION
    assert body["permissions_policy_arn"] == "arn:aws:iam::aws:policy/AdministratorAccess"
    assert body["expires_in"] == quick_setup.TEMPLATE_URL_EXPIRES_IN

    saved = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert saved["run_role_arn"] == body["role_arn"]


def test_console_link_prefills_the_stack(auth_client):
    """The link opens CloudFormation quick create with the name, id and policy filled in."""
    workspace_id = _create(auth_client)["workspace_id"]
    body = _start(auth_client, workspace_id, permissions="read_only").json()

    parts = urlsplit(body["console_url"])
    assert parts.scheme == "https"
    assert parts.netloc == f"{REGION}.console.aws.amazon.com"
    assert parts.path == "/cloudformation/home"
    params = _fragment(body["console_url"])
    assert params["stackName"] == body["stack_name"]
    assert params["param_RoleName"] == body["role_name"]
    assert params["param_ExternalId"] == workspace_id
    assert params["param_PermissionsPolicyArn"] == "arn:aws:iam::aws:policy/ReadOnlyAccess"
    template_url = urlsplit(params["templateURL"])
    assert ARTIFACTS_BUCKET in template_url.netloc + template_url.path
    assert "X-Amz-Signature" in template_url.query


def test_no_policy_choice_passes_the_sentinel(auth_client):
    """Choosing no policy leaves the role with trust only, for a narrower policy attached by hand."""
    workspace_id = _create(auth_client)["workspace_id"]
    body = _start(auth_client, workspace_id, permissions="none").json()
    assert body["permissions_policy_arn"] is None
    assert _fragment(body["console_url"])["param_PermissionsPolicyArn"] == quick_setup.NO_POLICY


def test_template_trusts_only_the_runner_with_the_external_id(auth_client, settings):
    """The stored template names every runner task role and nothing else, behind the external id."""
    workspace_id = _create(auth_client)["workspace_id"]
    _start(auth_client, workspace_id)
    _, key = quick_setup.render_template(settings)
    template = _stored_template(key)

    role = template["Resources"]["RunRole"]["Properties"]
    (statement,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Principal"] == {"AWS": RUNNER_TASK_ROLE_ARNS}
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}
    assert role["RoleName"] == {"Ref": "RoleName"}
    assert role["MaxSessionDuration"] == 3600
    assert template["Outputs"]["RoleArn"]["Value"] == {"Fn::GetAtt": ["RunRole", "Arn"]}


def test_template_pins_the_name_and_id_patterns(settings):
    """A parameter edited in the console cannot move the role outside the runner's grant."""
    import re

    parameters = quick_setup.template_body(settings)["Parameters"]
    name_pattern = re.compile(parameters["RoleName"]["AllowedPattern"])
    assert name_pattern.match(f"{RUN_ROLE_NAME_PREFIX}01J00000000000000000000000")
    assert not name_pattern.match("AdminRole")
    assert not name_pattern.match(f"{RUN_ROLE_NAME_PREFIX}01J00000000000000000000000-extra")
    id_pattern = re.compile(parameters["ExternalId"]["AllowedPattern"])
    assert id_pattern.match("ws-01J00000000000000000000000")
    assert not id_pattern.match("*")
    assert parameters["PermissionsPolicyArn"]["AllowedValues"][-1] == quick_setup.NO_POLICY


def test_template_copy_has_no_em_dashes(settings):
    """Everything the AWS console shows from the template follows the copy rules."""
    assert "—" not in quick_setup.render_template(settings)[0]


def test_template_is_uploaded_once_per_content(auth_client, settings, monkeypatch):
    """The key is content addressed, so a second call neither rewrites nor changes it."""
    workspace_id = _create(auth_client)["workspace_id"]
    _start(auth_client, workspace_id)
    _, key = quick_setup.render_template(settings)
    listed = boto3.client("s3", region_name=REGION).list_objects_v2(
        Bucket=ARTIFACTS_BUCKET, Prefix=quick_setup.TEMPLATE_KEY_PREFIX
    )
    assert [item.get("Key") for item in listed.get("Contents", [])] == [key]

    quick_setup.reset_template_cache()
    calls: list[str] = []
    original = quick_setup._s3

    def counting(resolved):
        """Record each client handed out so a rewrite would show."""
        calls.append("client")
        return original(resolved)

    monkeypatch.setattr(quick_setup, "_s3", counting)
    assert quick_setup.ensure_template(settings) == key
    assert quick_setup.ensure_template(settings) == key
    assert calls == ["client"]


def test_reopening_keeps_the_check_outcome(auth_client, settings):
    """The same account again keeps the saved ARN, so a recorded success is not dropped."""
    from app.common.db import repositories

    workspace_id = _create(auth_client)["workspace_id"]
    _start(auth_client, workspace_id)
    repositories.workspaces(settings).update(
        {"workspace_id": workspace_id},
        update_expression="SET run_role_account_id = :account, run_role_checked_at = :at",
        expression_values={":account": ACCOUNT, ":at": "2026-09-26T00:00:00+00:00"},
    )
    assert _start(auth_client, workspace_id).status_code == 200
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["run_role_account_id"] == ACCOUNT


def test_a_new_account_is_staged_and_keeps_the_working_role(auth_client, settings):
    """Another account is staged, so the role runs use and its check outcome stay put."""
    from app.common.db import repositories

    workspace_id = _create(auth_client)["workspace_id"]
    first = _start(auth_client, workspace_id).json()
    assert first["pending"] is False
    repositories.workspaces(settings).update(
        {"workspace_id": workspace_id},
        update_expression="SET run_role_account_id = :account",
        expression_values={":account": ACCOUNT},
    )
    body = _start(auth_client, workspace_id, account_id="210987654321").json()
    saved = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert body["pending"] is True
    assert body["role_arn"].startswith("arn:aws:iam::210987654321:role/")
    assert saved["run_role_arn"] == first["role_arn"]
    assert saved["pending_run_role_arn"] == body["role_arn"]
    assert saved["run_role_account_id"] == ACCOUNT


def test_returning_to_the_current_account_drops_the_staged_role(auth_client):
    """Quick setup for the role already in use discards what another account staged."""
    workspace_id = _create(auth_client)["workspace_id"]
    first = _start(auth_client, workspace_id).json()
    _start(auth_client, workspace_id, account_id="210987654321")
    again = _start(auth_client, workspace_id).json()
    saved = auth_client.get(f"{BASE}/{workspace_id}").json()
    assert again["pending"] is False
    assert saved["run_role_arn"] == first["role_arn"]
    assert saved["pending_run_role_arn"] is None


@pytest.mark.parametrize("account_id", ["1234-5678-9012", "1234 5678 9012"])
def test_account_id_as_the_console_prints_it(auth_client, account_id):
    """The console shows the id with dashes, so a pasted one is accepted as is."""
    workspace_id = _create(auth_client)["workspace_id"]
    response = _start(auth_client, workspace_id, account_id=account_id)
    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == ACCOUNT


@pytest.mark.parametrize("account_id", ["12345678901", "1234567890123", "abcdefghijkl"])
def test_malformed_account_id_is_refused(auth_client, account_id):
    """Anything but twelve digits is a 422 and saves nothing."""
    workspace_id = _create(auth_client)["workspace_id"]
    assert _start(auth_client, workspace_id, account_id=account_id).status_code == 422
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["run_role_arn"] is None


@pytest.mark.parametrize("body", [{"account_id": ""}, {"account_id": None}])
def test_no_account_without_a_connect_topic_is_503(auth_client, body):
    """With no stack to report the account back, the account id is the only way to name it."""
    workspace_id = _create(auth_client)["workspace_id"]
    response = auth_client.post(f"{BASE}/{workspace_id}/run-role/quick-setup", json=body)
    assert response.status_code == 503
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["aws_connection"] is None


def test_unknown_permissions_choice_is_refused(auth_client):
    """Only the listed managed policies can be attached through the link."""
    workspace_id = _create(auth_client)["workspace_id"]
    assert _start(auth_client, workspace_id, permissions="iam_full_access").status_code == 422


def test_unknown_workspace_is_404(auth_client):
    """A well formed id with no row behind it is a 404."""
    assert _start(auth_client, "ws-01J00000000000000000000000").status_code == 404


def test_read_scope_cannot_start_quick_setup(auth_client, scoped_client):
    """Saving an ARN is a write, so the read scope is refused."""
    workspace_id = _create(auth_client)["workspace_id"]
    reader = scoped_client("workspaces:read")
    assert _start(reader, workspace_id).status_code == 403


def test_no_runner_roles_is_503(auth_client, monkeypatch):
    """A deployment with nothing to trust cannot render a template, and says so."""
    workspace_id = _create(auth_client)["workspace_id"]
    monkeypatch.setenv("RUNNER_TASK_ROLE_ARN", "")
    settings_module.reset_settings_cache()
    response = _start(auth_client, workspace_id)
    assert response.status_code == 503, response.text
    assert auth_client.get(f"{BASE}/{workspace_id}").json()["run_role_arn"] is None
