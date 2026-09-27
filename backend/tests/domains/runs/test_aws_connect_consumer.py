"""The AWS connect consumer: a Quick setup stack reporting its account and role back.

Driven through the runs function's event route where the dispatch matters, and
through `handle_record` otherwise. CloudFormation's answer is a PUT to a presigned
URL, so the HTTP client is replaced with a mock transport that records each answer.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from webbpulse.events import BATCH_FAILURES_KEY, FAILURE_ITEM_KEY, events_path

from app.common.composition import settings as settings_module
from app.common.composition.wiring import build_domain_app
from app.common.db import repositories
from app.common.workspaces import aws_connect
from app.domains.runs.consumers import aws_connect as consumer
from app.domains.workspaces import quick_setup
from tests.conftest import RUN_ROLE_NAME_PREFIX, WORKSPACE_PAYLOAD

TOPIC_ARN = "arn:aws:sns:us-west-2:870550636948:webbpulse-terraform-test-aws-connect"
ACCOUNT = "123456789012"
OTHER_ACCOUNT = "210987654321"
RESPONSE_URL = (
    "https://cloudformation-custom-resource-response-uswest2.s3-us-west-2.amazonaws.com/answer?X-Amz-Signature=x"
)


@pytest.fixture(autouse=True)
def connect_topic():
    """A deployment with a connect topic, so templates and quick setup report back."""
    quick_setup.reset_template_cache()
    os.environ["AWS_CONNECT_TOPIC_ARN"] = TOPIC_ARN
    settings_module.reset_settings_cache()
    yield TOPIC_ARN
    os.environ.pop("AWS_CONNECT_TOPIC_ARN", None)
    settings_module.reset_settings_cache()
    quick_setup.reset_template_cache()


@pytest.fixture
def answers(monkeypatch):
    """Every answer PUT, in order, with the status the mock bucket replies with settable."""
    sent: list[dict[str, Any]] = []
    reply = {"status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the answer and reply as the bucket would."""
        sent.append({"url": str(request.url), "method": request.method, "body": json.loads(request.content)})
        return httpx.Response(reply["status"])

    monkeypatch.setattr(consumer, "http_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    return {"sent": sent, "reply": reply}


def _workspace(auth_client, *, with_role: bool = False, name: str = "example") -> dict[str, Any]:
    """A workspace, without a run role unless asked."""
    payload = {**WORKSPACE_PAYLOAD, "name": name}
    if not with_role:
        payload.pop("run_role_arn")
    response = auth_client.post("/api/v1/workspaces", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _token(auth_client, workspace_id: str) -> str:
    """Start Quick setup with no account and return the token its link carries."""
    response = auth_client.post(f"/api/v1/workspaces/{workspace_id}/run-role/quick-setup", json={})
    assert response.status_code == 200, response.text
    fragment = urlsplit(response.json()["console_url"]).fragment
    return dict(parse_qsl(fragment.partition("?")[2]))["param_ConnectToken"]


def _stack(account: str = ACCOUNT) -> str:
    """A stack ARN in `account`."""
    return f"arn:aws:cloudformation:us-west-2:{account}:stack/webbpulse-run-role/1b2c3d4e-0000-0000-0000-000000000000"


def _role(workspace_id: str, account: str = ACCOUNT) -> str:
    """The run role ARN the workspace's stack creates in `account`."""
    return f"arn:aws:iam::{account}:role/{RUN_ROLE_NAME_PREFIX}{workspace_id.removeprefix('ws-')}"


def _request(
    kind: str,
    workspace_id: str,
    token: str,
    *,
    account: str = ACCOUNT,
    role_arn: str | None = None,
    request_id: str = "req-1",
    physical_id: str | None = None,
    old: dict[str, str] | None = None,
    response_url: str = RESPONSE_URL,
    trust_version: str | None = aws_connect.TRUST_VERSION,
) -> dict[str, Any]:
    """One CloudFormation custom resource request as the topic delivers it.

    `trust_version=None` is a stack built from a template older than the vended trust.
    """
    body: dict[str, Any] = {
        "RequestType": kind,
        "ServiceToken": TOPIC_ARN,
        "ResponseURL": response_url,
        "StackId": _stack(account),
        "RequestId": request_id,
        "LogicalResourceId": "Connection",
        "ResourceType": quick_setup.CONNECTION_RESOURCE_TYPE,
        "ResourceProperties": {
            "ServiceToken": TOPIC_ARN,
            "ConnectToken": token,
            "WorkspaceId": workspace_id,
            "RoleArn": role_arn or _role(workspace_id, account),
        },
    }
    if trust_version is not None:
        body["ResourceProperties"]["TrustVersion"] = trust_version
    if physical_id is not None:
        body["PhysicalResourceId"] = physical_id
    if old is not None:
        body["OldResourceProperties"] = old
    return body


def _record(body: dict[str, Any], message_id: str = "msg-1") -> dict[str, Any]:
    """An SQS record carrying `body` raw, as the subscription delivers it."""
    return {"messageId": message_id, "body": json.dumps(body)}


def _handle(body: dict[str, Any]) -> None:
    """Hand one request to the consumer."""
    consumer.handle_record(_record(body), settings=settings_module.get_settings())


def _row(workspace_id: str) -> dict[str, Any]:
    """The stored workspace row, private attributes included."""
    item = repositories.workspaces(settings_module.get_settings()).get({"workspace_id": workspace_id})
    assert item is not None
    return dict(item)


def test_issue_stores_only_the_hash(auth_client):
    """The link carries the token; the row keeps its hash, and the API returns neither."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)

    assert token.startswith(aws_connect.TOKEN_PREFIX)
    row = _row(workspace_id)
    assert row[aws_connect.TOKEN_HASH_ATTRIBUTE] == aws_connect.hash_token(token)
    assert token not in json.dumps(row, default=str)

    rendered = auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()
    assert aws_connect.TOKEN_HASH_ATTRIBUTE not in rendered
    assert aws_connect.TOKEN_EXPIRES_ATTRIBUTE not in rendered
    assert rendered["aws_connection"]["status"] == "waiting"


def test_quick_setup_without_an_account_reports_back(auth_client):
    """No account id is needed: the link carries a token and nothing is staged yet."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    body = auth_client.post(f"/api/v1/workspaces/{workspace_id}/run-role/quick-setup", json={}).json()

    assert body["reports_back"] is True
    assert body["account_id"] is None
    assert body["role_arn"] is None
    assert body["connect_expires_at"]
    assert auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["run_role_arn"] in (None, "")


def test_template_carries_the_connection_resource(settings):
    """With a topic configured the template reports back to it through a custom resource."""
    template = quick_setup.template_body(settings_module.get_settings())
    resource = template["Resources"]["Connection"]
    assert resource["Type"] == quick_setup.CONNECTION_RESOURCE_TYPE
    assert resource["Properties"]["ServiceToken"] == TOPIC_ARN
    assert resource["Properties"]["RoleArn"] == {"Fn::GetAtt": ["RunRole", "Arn"]}
    assert template["Parameters"]["ConnectToken"]["NoEcho"] is True
    assert resource["Properties"]["TrustVersion"] == aws_connect.TRUST_VERSION


def test_template_without_a_topic_has_no_connection(monkeypatch):
    """Unset, the template is the plain one and asks for no token."""
    monkeypatch.delenv("AWS_CONNECT_TOPIC_ARN")
    settings_module.reset_settings_cache()
    template = quick_setup.template_body(settings_module.get_settings())
    assert "Connection" not in template["Resources"]
    assert "ConnectToken" not in template["Parameters"]


def test_create_connects_and_starts_the_verification_run(auth_client, answers, state_machine):
    """A valid create takes the role at once, consumes the token and verifies with a plan only run."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)

    _handle(_request("Create", workspace_id, token))

    (answer,) = answers["sent"]
    assert answer["method"] == "PUT"
    assert answer["url"] == RESPONSE_URL
    assert answer["body"]["Status"] == "SUCCESS"
    assert answer["body"]["PhysicalResourceId"].startswith(aws_connect.PHYSICAL_ID_PREFIX)
    assert answer["body"]["Data"] == {"WorkspaceId": workspace_id, "AccountId": ACCOUNT}

    row = _row(workspace_id)
    assert row["run_role_arn"] == _role(workspace_id)
    assert aws_connect.TOKEN_HASH_ATTRIBUTE not in row
    connection = row["aws_connection"]
    assert connection["status"] == "connected"
    assert connection["account_id"] == ACCOUNT
    assert connection["pending"] is False

    run = auth_client.get(f"/api/v1/runs/{connection['run_id']}").json()
    assert run["plan_only"] is True
    assert run["source"] == "aws_connect"
    assert ACCOUNT in run["message"]


def test_create_beside_a_working_role_stages_it(auth_client, answers, state_machine):
    """A workspace already running as another role keeps it until the check run proves the new one."""
    workspace_id = _workspace(auth_client, with_role=True)["workspace_id"]
    token = _token(auth_client, workspace_id)

    _handle(_request("Create", workspace_id, token))

    assert answers["sent"][0]["body"]["Status"] == "SUCCESS"
    row = _row(workspace_id)
    assert row["run_role_arn"] == WORKSPACE_PAYLOAD["run_role_arn"]
    assert row["pending_run_role_arn"] == _role(workspace_id)
    assert row["aws_connection"]["pending"] is True
    run = auth_client.get(f"/api/v1/runs/{row['aws_connection']['run_id']}").json()
    assert run["run_role_check"] is True


def test_create_with_no_config_uses_a_starter(auth_client, answers, state_machine):
    """A workspace with no upload still verifies, from a starter config the consumer writes."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    _handle(_request("Create", workspace_id, token))

    run_id = _row(workspace_id)["aws_connection"]["run_id"]
    run = auth_client.get(f"/api/v1/runs/{run_id}").json()
    config = repositories.config_versions(settings_module.get_settings()).get(
        {"config_version_id": run["config_version_id"]}
    )
    assert config is not None
    assert config["source"] == "aws_connect"


def test_create_uses_the_latest_upload(auth_client, answers, state_machine, uploaded_config_version, workspace):
    """A workspace with an upload verifies against it rather than a starter."""
    workspace_id = workspace["workspace_id"]
    token = _token(auth_client, workspace_id)
    _handle(_request("Create", workspace_id, token))

    run_id = _row(workspace_id)["aws_connection"]["run_id"]
    run = auth_client.get(f"/api/v1/runs/{run_id}").json()
    assert run["config_version_id"] == uploaded_config_version["config_version_id"]


def test_a_retried_create_answers_again_without_reconnecting(auth_client, answers, state_machine):
    """The same request delivered twice answers SUCCESS with the same id and starts one run."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    request = _request("Create", workspace_id, token)

    _handle(request)
    run_id = _row(workspace_id)["aws_connection"]["run_id"]
    _handle(request)

    first, second = answers["sent"]
    assert second["body"]["Status"] == "SUCCESS"
    assert second["body"]["PhysicalResourceId"] == first["body"]["PhysicalResourceId"]
    assert _row(workspace_id)["aws_connection"]["run_id"] == run_id


def test_a_used_token_cannot_connect_another_stack(auth_client, answers, state_machine):
    """The token is single use: a second stack carrying it is refused and rolls back."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    _handle(_request("Create", workspace_id, token))

    _handle(_request("Create", workspace_id, token, account=OTHER_ACCOUNT, request_id="req-2"))

    refused = answers["sent"][1]["body"]
    assert refused["Status"] == "FAILED"
    assert not refused["PhysicalResourceId"].startswith(aws_connect.PHYSICAL_ID_PREFIX)
    assert _row(workspace_id)["aws_connection"]["account_id"] == ACCOUNT


def test_an_unknown_token_is_refused(auth_client, answers):
    """A token the workspace never issued answers FAILED and changes nothing."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    _token(auth_client, workspace_id)
    forged = f"{aws_connect.TOKEN_PREFIX}{'A' * 43}"

    _handle(_request("Create", workspace_id, forged))

    assert answers["sent"][0]["body"]["Status"] == "FAILED"
    row = _row(workspace_id)
    assert row.get("run_role_arn") in (None, "")
    assert row["aws_connection"]["status"] == "waiting"


def test_an_expired_token_is_refused(auth_client, answers):
    """A token past its expiry answers FAILED and the connection reads expired."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    repositories.workspaces(settings_module.get_settings()).update(
        {"workspace_id": workspace_id},
        update_expression="SET #expires = :past",
        expression_names={"#expires": aws_connect.TOKEN_EXPIRES_ATTRIBUTE},
        expression_values={":past": past},
    )

    _handle(_request("Create", workspace_id, token))

    answer = answers["sent"][0]["body"]
    assert answer["Status"] == "FAILED"
    assert "expired" in answer["Reason"]
    row = _row(workspace_id)
    assert row["aws_connection"]["status"] == "expired"
    assert aws_connect.TOKEN_HASH_ATTRIBUTE not in row
    assert row.get("run_role_arn") in (None, "")


def test_a_reissued_token_replaces_the_old_one(auth_client, answers):
    """Starting Quick setup again invalidates the earlier link."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    first = _token(auth_client, workspace_id)
    _token(auth_client, workspace_id)

    _handle(_request("Create", workspace_id, first))
    assert answers["sent"][0]["body"]["Status"] == "FAILED"


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::123456789012:role/somebody-else",
        "arn:aws:iam::210987654321:role/{name}",
    ],
)
def test_a_role_that_is_not_the_workspaces_is_refused(auth_client, answers, role_arn):
    """The role must be this workspace's, in the stack's own account."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    name = f"{RUN_ROLE_NAME_PREFIX}{workspace_id.removeprefix('ws-')}"

    _handle(_request("Create", workspace_id, token, role_arn=role_arn.format(name=name)))

    assert answers["sent"][0]["body"]["Status"] == "FAILED"
    assert aws_connect.TOKEN_HASH_ATTRIBUTE in _row(workspace_id)


def test_an_unknown_workspace_is_refused(answers):
    """A workspace id nobody holds answers FAILED."""
    _handle(_request("Create", "ws-01J0000000000000000000000Z", f"wct_{'A' * 43}"))
    assert answers["sent"][0]["body"]["Status"] == "FAILED"


def _connected(auth_client, answers) -> tuple[str, str, str]:
    """A workspace connected by one stack: its id, token and the physical id answered."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    _handle(_request("Create", workspace_id, token))
    return workspace_id, token, answers["sent"][-1]["body"]["PhysicalResourceId"]


def test_delete_disconnects_the_stack_that_connected(auth_client, answers, state_machine):
    """Deleting the connecting stack forgets its role and reads disconnected."""
    workspace_id, token, physical_id = _connected(auth_client, answers)

    _handle(_request("Delete", workspace_id, token, physical_id=physical_id, request_id="req-del"))

    assert answers["sent"][-1]["body"]["Status"] == "SUCCESS"
    row = _row(workspace_id)
    assert "run_role_arn" not in row
    assert row["aws_connection"]["status"] == "disconnected"


def test_delete_after_the_role_changed_keeps_the_new_role(auth_client, answers, state_machine):
    """A person who moved the workspace to another role does not lose it when the old stack goes."""
    workspace_id, token, physical_id = _connected(auth_client, answers)
    replacement = WORKSPACE_PAYLOAD["run_role_arn"]
    assert (
        auth_client.patch(f"/api/v1/workspaces/{workspace_id}", json={"run_role_arn": replacement}).status_code == 200
    )

    _handle(_request("Delete", workspace_id, token, physical_id=physical_id, request_id="req-del"))

    assert answers["sent"][-1]["body"]["Status"] == "SUCCESS"
    assert _row(workspace_id)["run_role_arn"] == replacement


def test_delete_of_an_unrelated_stack_changes_nothing(auth_client, answers, state_machine):
    """A physical id this workspace never answered is a no-op SUCCESS."""
    workspace_id, token, _ = _connected(auth_client, answers)

    _handle(_request("Delete", workspace_id, token, physical_id="wpc-somebody-else", request_id="req-del"))

    assert answers["sent"][-1]["body"]["Status"] == "SUCCESS"
    assert _row(workspace_id)["aws_connection"]["status"] == "connected"


def test_delete_of_a_refused_create_answers_success(auth_client, answers):
    """The rollback of a refused create deletes a resource that never existed, which must not fail."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    _handle(_request("Delete", workspace_id, "", physical_id="failed-req-1"))
    assert answers["sent"][0]["body"]["Status"] == "SUCCESS"


def test_an_unchanged_update_keeps_the_connection(auth_client, answers, state_machine):
    """A stack update that leaves the connection as recorded answers SUCCESS with the same id."""
    workspace_id, token, physical_id = _connected(auth_client, answers)
    properties = _request("Create", workspace_id, token)["ResourceProperties"]

    _handle(
        _request("Update", workspace_id, token, physical_id=physical_id, old=properties, request_id="req-up"),
    )

    answer = answers["sent"][-1]["body"]
    assert answer["Status"] == "SUCCESS"
    assert answer["PhysicalResourceId"] == physical_id


def test_an_update_to_another_account_is_refused(auth_client, answers, state_machine):
    """The same token cannot carry the connection to a role in another account."""
    workspace_id, token, physical_id = _connected(auth_client, answers)
    old = _request("Create", workspace_id, token)["ResourceProperties"]

    _handle(
        _request(
            "Update",
            workspace_id,
            token,
            role_arn=_role(workspace_id, OTHER_ACCOUNT),
            physical_id=physical_id,
            old=old,
            request_id="req-up",
        ),
    )

    assert answers["sent"][-1]["body"]["Status"] == "FAILED"
    assert _row(workspace_id)["run_role_arn"] == _role(workspace_id)


def test_an_update_to_another_workspace_is_refused(auth_client, answers, state_machine):
    """A connected stack cannot be pointed at a different workspace."""
    workspace_id, token, physical_id = _connected(auth_client, answers)
    other_id = _workspace(auth_client, name="other")["workspace_id"]
    other_token = _token(auth_client, other_id)
    old = _request("Create", workspace_id, token)["ResourceProperties"]

    _handle(
        _request("Update", other_id, other_token, physical_id=physical_id, old=old, request_id="req-up"),
    )

    assert answers["sent"][-1]["body"]["Status"] == "FAILED"
    assert _row(other_id)["aws_connection"]["status"] == "waiting"


def test_an_update_with_a_fresh_token_reconnects(auth_client, answers, state_machine):
    """A new link applied as an update to the same stack connects again under the same id."""
    workspace_id, token, physical_id = _connected(auth_client, answers)
    old = _request("Create", workspace_id, token)["ResourceProperties"]
    auth_client.patch(f"/api/v1/workspaces/{workspace_id}", json={"run_role_arn": WORKSPACE_PAYLOAD["run_role_arn"]})
    fresh = _token(auth_client, workspace_id)

    _handle(_request("Update", workspace_id, fresh, physical_id=physical_id, old=old, request_id="req-up"))

    answer = answers["sent"][-1]["body"]
    assert answer["Status"] == "SUCCESS"
    assert answer["PhysicalResourceId"] == physical_id
    assert _row(workspace_id)["pending_run_role_arn"] == _role(workspace_id)


def test_a_stack_with_the_current_trust_needs_no_reconnect(auth_client, answers, state_machine):
    """A create from the current template records its trust version and shows no reconnect."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    _handle(_request("Create", workspace_id, _token(auth_client, workspace_id)))

    assert _row(workspace_id)["aws_connection"][aws_connect.TRUST_VERSION_FIELD] == aws_connect.TRUST_VERSION
    assert auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["run_role_reconnect_required"] is False


def test_a_stack_from_an_older_template_requires_a_reconnect(auth_client, answers, state_machine):
    """A stack whose trust predates the vending role still trusts the runner task roles, so it must be updated."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    _handle(_request("Create", workspace_id, _token(auth_client, workspace_id), trust_version=None))

    assert auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["run_role_reconnect_required"] is True


def test_updating_an_old_stack_to_the_current_template_clears_the_reconnect(auth_client, answers, state_machine):
    """An in place update with the current template records the new trust version."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)
    _handle(_request("Create", workspace_id, token, trust_version=None))
    physical_id = answers["sent"][-1]["body"]["PhysicalResourceId"]
    old = _request("Create", workspace_id, token, trust_version=None)["ResourceProperties"]

    _handle(_request("Update", workspace_id, token, physical_id=physical_id, old=old, request_id="req-up"))

    assert answers["sent"][-1]["body"]["Status"] == "SUCCESS"
    assert _row(workspace_id)["aws_connection"][aws_connect.TRUST_VERSION_FIELD] == aws_connect.TRUST_VERSION
    assert auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["run_role_reconnect_required"] is False


@pytest.mark.parametrize(
    ("workspace", "expected"),
    [
        ({"run_role_arn": "arn:aws:iam::1:role/r"}, False),
        ({"run_role_arn": "arn:aws:iam::1:role/r", "aws_connection": {"role_arn": "arn:aws:iam::1:role/other"}}, False),
        ({"run_role_arn": "arn:aws:iam::1:role/r", "aws_connection": {"role_arn": "arn:aws:iam::1:role/r"}}, True),
        (
            {
                "pending_run_role_arn": "arn:aws:iam::1:role/r",
                "aws_connection": {"role_arn": "arn:aws:iam::1:role/r", "trust_version": "1"},
            },
            True,
        ),
        (
            {
                "run_role_arn": "arn:aws:iam::1:role/r",
                "aws_connection": {"role_arn": "arn:aws:iam::1:role/r", "trust_version": aws_connect.TRUST_VERSION},
            },
            False,
        ),
    ],
)
def test_reconnect_is_required_only_for_a_stale_stack_that_still_provides_the_role(workspace, expected):
    """A hand built role or a connection that no longer provides the role is never flagged."""
    assert aws_connect.reconnect_required(workspace) is expected


@pytest.mark.parametrize(
    "url",
    [
        "http://cloudformation-custom-resource-response-uswest2.s3-us-west-2.amazonaws.com/x",
        "https://attacker.example.com/cloudformation-custom-resource-response-uswest2.amazonaws.com",
        "https://cloudformation-custom-resource-response-uswest2.s3.amazonaws.com.evil.example/x",
        "https://user@cloudformation-custom-resource-response-uswest2.s3.amazonaws.com/x",
        "https://cloudformation-custom-resource-response-uswest2.s3.amazonaws.com:8443/x",
    ],
)
def test_a_response_url_outside_cloudformation_is_dropped(auth_client, answers, url):
    """Nothing is sent anywhere but a CloudFormation response bucket, and nothing is connected."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)

    _handle(_request("Create", workspace_id, token, response_url=url))

    assert answers["sent"] == []
    assert _row(workspace_id)["aws_connection"]["status"] == "waiting"


def test_a_refused_answer_raises_for_a_retry(auth_client, answers):
    """A bucket that refuses the PUT raises, so SQS redelivers the request."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    answers["reply"]["status"] = 403
    with pytest.raises(consumer.ResponseFailed):
        _handle(_request("Create", workspace_id, "wct_bad"))


@pytest.fixture
def events_client(settings):
    """A client over the runs function, where the consumer route lives."""
    with TestClient(build_domain_app("runs", settings=settings_module.get_settings())) as client:
        yield client


def test_the_event_route_dispatches_connect_requests(auth_client, answers, state_machine, events_client):
    """A raw CloudFormation request on the shared route reaches this consumer."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    token = _token(auth_client, workspace_id)

    response = events_client.post(events_path(), json={"Records": [_record(_request("Create", workspace_id, token))]})

    assert response.status_code == 200
    assert response.json()[BATCH_FAILURES_KEY] == []
    assert answers["sent"][0]["body"]["Status"] == "SUCCESS"
    assert _row(workspace_id)["aws_connection"]["status"] == "connected"


def test_an_undelivered_answer_is_retried_through_the_route(auth_client, answers, events_client):
    """A failed PUT names the record in the batch failures, so SQS keeps it."""
    workspace_id = _workspace(auth_client)["workspace_id"]
    answers["reply"]["status"] = 500

    response = events_client.post(
        events_path(), json={"Records": [_record(_request("Create", workspace_id, "wct_bad"), "msg-retry")]}
    )

    assert {item[FAILURE_ITEM_KEY] for item in response.json()[BATCH_FAILURES_KEY]} == {"msg-retry"}


def test_is_connect_request_recognises_only_the_shape():
    """Other queue bodies, which carry a `kind`, are never mistaken for a stack request."""
    body = _request("Create", "ws-x", "wct_x")
    assert consumer.is_connect_request(_record(body))
    assert not consumer.is_connect_request(_record({**body, "kind": "confirmation"}))
    assert not consumer.is_connect_request({"messageId": "m", "body": "not json"})
    assert not consumer.is_connect_request(_record({"RequestType": "Create"}))


def _verifying(auth_client, *, with_role: bool = False) -> tuple[str, str]:
    """A workspace a stack just connected, with the id of the verification run it started."""
    workspace_id = _workspace(auth_client, with_role=with_role)["workspace_id"]
    _handle(_request("Create", workspace_id, _token(auth_client, workspace_id)))
    return workspace_id, str(_row(workspace_id)["aws_connection"]["run_id"])


def _plan_succeeds(run_id: str) -> None:
    """Report a clean plan for a plan only run, which the API ends `planned_and_finished`."""
    from app.domains.runs import service

    service.record_phase_result(
        run_id, {"phase": "plan", "exit_code": 0, "changes": {"add": 0, "change": 0, "destroy": 0}, "error": ""}
    )


def _state_machine_errors(run_id: str, error: str) -> None:
    """End a run the way `MarkErrored` does, then settle it as the stream would."""
    from app.domains.runs import service

    repositories.runs(settings_module.get_settings()).update(
        {"run_id": run_id},
        update_expression="SET #status = :status, #error = :error",
        expression_names={"#status": "status", "#error": "error"},
        expression_values={":status": "errored", ":error": error},
    )
    assert service.settle_run(run_id) is not None


def test_a_reported_stack_is_pending_verification(auth_client, answers, state_machine):
    """Reporting back only says the role exists, so the connection waits on its run."""
    workspace_id, _run_id = _verifying(auth_client)
    connection = auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["aws_connection"]
    assert connection["status"] == "connected"
    assert connection["verification"] == "pending"
    assert connection["verification_error"] is None


def test_a_clean_verification_run_verifies_the_connection(auth_client, answers, state_machine):
    """The run finishing its plan is what shows the connection verified."""
    workspace_id, run_id = _verifying(auth_client)
    _plan_succeeds(run_id)
    connection = auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["aws_connection"]
    assert connection["verification"] == "verified"
    assert connection["verified_at"]
    assert connection["run_id"] == run_id


def test_a_verification_run_that_errors_fails_the_connection(auth_client, answers, state_machine):
    """A run that assumed the role but could not initialise shows failed, with why and which run."""
    workspace_id, run_id = _verifying(auth_client)
    _state_machine_errors(run_id, "The run failed with InitFailed.")
    connection = auth_client.get(f"/api/v1/workspaces/{workspace_id}").json()["aws_connection"]
    assert connection["status"] == "connected"
    assert connection["verification"] == "failed"
    assert connection["verification_error"] == "The run failed with InitFailed."
    assert connection["run_id"] == run_id


def test_a_verification_run_that_never_starts_fails_the_connection(auth_client, answers, state_machine, monkeypatch):
    """The stack still succeeds, and the connection says the run did not start."""
    from app.domains.runs import service

    def refused(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        """Fail the way a throttled table would."""
        raise RuntimeError("throttled")

    monkeypatch.setattr(service, "create_run", refused)
    workspace_id = _workspace(auth_client)["workspace_id"]
    _handle(_request("Create", workspace_id, _token(auth_client, workspace_id)))

    assert answers["sent"][0]["body"]["Status"] == "SUCCESS"
    connection = _row(workspace_id)["aws_connection"]
    assert connection["verification"] == "failed"
    assert connection["verification_error"] == consumer.VERIFY_START_FAILED_MESSAGE


def test_a_later_clean_run_on_the_role_settles_a_failed_verification(auth_client, answers, state_machine):
    """Fixing the cause and running again verifies the connection without reconnecting."""
    workspace_id, run_id = _verifying(auth_client)
    _state_machine_errors(run_id, "The run failed with InitFailed.")
    retry = auth_client.post(
        "/api/v1/runs",
        json={"workspace_id": workspace_id, "config_version_id": _config_of(auth_client, run_id), "plan_only": True},
    ).json()
    _plan_succeeds(retry["run_id"])
    connection = _row(workspace_id)["aws_connection"]
    assert connection["verification"] == "verified"
    assert connection["run_id"] == retry["run_id"]
    assert "verification_error" not in connection


def test_a_later_failure_never_unverifies_a_connection(auth_client, answers, state_machine):
    """Once verified, an unrelated run failing is that run's problem, not the connection's."""
    workspace_id, run_id = _verifying(auth_client)
    _plan_succeeds(run_id)
    later = auth_client.post(
        "/api/v1/runs",
        json={"workspace_id": workspace_id, "config_version_id": _config_of(auth_client, run_id), "plan_only": True},
    ).json()
    _state_machine_errors(later["run_id"], "The run failed with PlanFailed.")
    connection = _row(workspace_id)["aws_connection"]
    assert connection["verification"] == "verified"
    assert connection["run_id"] == run_id


def test_cancelling_the_verification_run_fails_the_verification(auth_client, answers, state_machine):
    """A cancelled verification proves nothing, and says so rather than staying pending."""
    workspace_id, run_id = _verifying(auth_client)
    assert auth_client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    connection = _row(workspace_id)["aws_connection"]
    assert connection["verification"] == "failed"
    assert "cancelled" in connection["verification_error"]


def test_a_staged_role_whose_verification_run_errors_is_not_switched_to(auth_client, answers, state_machine):
    """Assuming the role is not enough: a verification that fails after AssumeRole keeps the working role."""
    from app.common.workspaces import run_role_check

    workspace_id, run_id = _verifying(auth_client, with_role=True)
    _state_machine_errors(run_id, "The run failed with InitFailed.")

    row = _row(workspace_id)
    assert row["run_role_arn"] == WORKSPACE_PAYLOAD["run_role_arn"]
    assert row["pending_run_role_arn"] == _role(workspace_id)
    assert row["aws_connection"]["verification"] == "failed"
    pending = auth_client.get(f"/api/v1/workspaces/{workspace_id}/run-role/check").json()["pending"]
    assert pending["connected"] is False
    assert pending["status"] == "failed"
    assert pending["error"].startswith(run_role_check.PENDING_RUN_FAILED_PREFIX)
    assert "InitFailed" in pending["error"]
    assert pending["run_id"] == run_id


def test_a_staged_role_whose_verification_run_finishes_is_switched_to(auth_client, answers, state_machine):
    """The same run ending cleanly switches the workspace over and verifies the connection."""
    workspace_id, run_id = _verifying(auth_client, with_role=True)
    _plan_succeeds(run_id)
    row = _row(workspace_id)
    assert row["run_role_arn"] == _role(workspace_id)
    assert "pending_run_role_arn" not in row
    assert row["aws_connection"]["verification"] == "verified"


def _config_of(auth_client, run_id: str) -> str:
    """The config version a run planned, so a retry plans the same thing."""
    return str(auth_client.get(f"/api/v1/runs/{run_id}").json()["config_version_id"])
