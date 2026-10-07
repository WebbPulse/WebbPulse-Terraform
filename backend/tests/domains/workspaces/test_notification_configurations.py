"""Workspace notification configurations: the API, sealed storage and the test delivery."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient

from app.common.core.auth import ALL_SCOPES, STEP_UP_MAX_AGE_SECONDS
from app.common.db import repositories
from app.common.notifications import sender, store
from tests.conftest import REGION, TABLE_PREFIX, person_headers, seed_user

SLACK_URL = "https://hooks.slack.com/services/T000/B000/slack-secret-path"
DISCORD_URL = "https://discord.com/api/webhooks/123/discord-secret-path"
GENERIC_URL = "https://receiver.example.com/hooks/generic-secret-path?k=v"
TOKEN = "generic-signing-token"
PUBLIC_ADDRESS = "93.184.216.34"


def _path(workspace: dict[str, Any], suffix: str = "") -> str:
    """The workspace's notification configurations path."""
    return f"/api/v1/workspaces/{workspace['workspace_id']}/notification-configurations{suffix}"


def _create(client: TestClient, workspace: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Create a configuration, defaulting to a Slack one on every trigger."""
    body = {
        "name": "Team channel",
        "destination_type": "slack",
        "url": SLACK_URL,
        "triggers": ["run:completed", "run:created", "run:errored"],
        **overrides,
    }
    response = client.post(_path(workspace), json=body)
    assert response.status_code == 201, response.text
    return response.json()


class Receiver:
    """An in-test webhook receiver behind httpx's mock transport."""

    def __init__(self, status_code: int = 200, text: str = "ok", headers: dict[str, str] | None = None) -> None:
        """Answer every request with `status_code` and `text`."""
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Record the request and answer it."""
        self.requests.append(request)
        return httpx.Response(self.status_code, text=self.text, headers=self.headers)


@pytest.fixture
def receiver(monkeypatch: pytest.MonkeyPatch) -> Receiver:
    """A receiver every delivery reaches, with every host resolving to a public address."""
    target = Receiver()
    monkeypatch.setattr(sender, "resolve", lambda host, port: [PUBLIC_ADDRESS])
    monkeypatch.setattr(sender, "_transport", lambda: httpx.MockTransport(target))
    return target


def _person(app, *, auth_age: int = 0) -> TestClient:
    """A person holding every scope who signed in `auth_age` seconds ago."""
    seed_user("user-notify")
    return TestClient(app, headers=person_headers(user_id="user-notify", scopes=ALL_SCOPES, auth_age=auth_age))


def test_create_masks_the_url_and_never_returns_it(auth_client, workspace):
    """The response shows the scheme and host only, and the stored row holds no plaintext URL."""
    created = _create(auth_client, workspace)

    assert created["url_masked"] == "https://hooks.slack.com/****"
    assert created["destination_type"] == "slack"
    assert created["enabled"] is True
    assert created["triggers"] == ["run:created", "run:completed", "run:errored"]
    assert created["has_token"] is False
    assert created["last_delivery"] is None
    assert "slack-secret-path" not in json.dumps(created)
    listed = auth_client.get(_path(workspace)).json()
    assert "slack-secret-path" not in json.dumps(listed)
    row = repositories.notification_configurations(None).get(
        {"workspace_id": workspace["workspace_id"], "notification_id": created["id"]}
    )
    assert row is not None
    assert "slack-secret-path" not in json.dumps(row, default=str)


def test_the_sealed_url_opens_only_for_its_own_row(auth_client, workspace):
    """The sealed URL round trips, and a copy under another configuration does not open."""
    created = _create(auth_client, workspace)
    row = store.get_configuration(workspace["workspace_id"], created["id"])

    assert store.secrets(row) == (SLACK_URL, None)
    with pytest.raises(Exception):
        store.secrets({**row, "notification_id": "nc-01ARZ3NDEKTSV4RRFFQ69G5FAV"})


def test_list_get_patch_and_delete(auth_client, workspace):
    """The whole lifecycle through the API, with a partial edit leaving other fields alone."""
    created = _create(auth_client, workspace)
    one = _path(workspace, f"/{created['id']}")

    assert auth_client.get(one).json()["name"] == "Team channel"
    assert [item["id"] for item in auth_client.get(_path(workspace)).json()["items"]] == [created["id"]]

    patched = auth_client.patch(one, json={"enabled": False, "triggers": ["run:needs_attention"]})
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is False
    assert patched.json()["triggers"] == ["run:needs_attention"]
    assert patched.json()["name"] == "Team channel"
    assert patched.json()["url_masked"] == created["url_masked"]

    assert auth_client.delete(one).status_code == 204
    assert auth_client.get(one).status_code == 404
    assert auth_client.delete(one).status_code == 404


def test_a_generic_webhook_keeps_its_token_secret(auth_client, workspace):
    """The token is sealed and reported only as present; an empty token clears it."""
    created = _create(auth_client, workspace, destination_type="generic", url=GENERIC_URL, token=TOKEN)

    assert created["has_token"] is True
    assert created["url_masked"] == "https://receiver.example.com/****"
    assert TOKEN not in json.dumps(created)
    row = store.get_configuration(workspace["workspace_id"], created["id"])
    assert store.secrets(row) == (GENERIC_URL, TOKEN)

    cleared = auth_client.patch(_path(workspace, f"/{created['id']}"), json={"token": ""})
    assert cleared.json()["has_token"] is False


def test_changing_the_destination_needs_a_new_url(auth_client, workspace):
    """A Slack URL was never checked as a Discord one, so the type cannot change alone."""
    created = _create(auth_client, workspace)
    one = _path(workspace, f"/{created['id']}")

    refused = auth_client.patch(one, json={"destination_type": "discord"})
    assert refused.status_code == 422

    moved = auth_client.patch(one, json={"destination_type": "discord", "url": DISCORD_URL})
    assert moved.status_code == 200, moved.text
    assert moved.json()["url_masked"] == "https://discord.com/****"


@pytest.mark.parametrize(
    ("destination_type", "url"),
    [
        ("slack", "https://example.com/services/T/B/x"),
        ("discord", "https://discord.com/channels/1/2"),
        ("generic", "http://receiver.example.com/hook"),
        ("generic", "https://user:pass@receiver.example.com/hook"),
        ("generic", "https://127.0.0.1/hook"),
        ("generic", "https://10.0.0.5/hook"),
        ("generic", "https://[::1]/hook"),
        ("generic", "https://169.254.169.254/latest"),
        ("generic", "https://localhost/hook"),
        ("generic", "https://metadata.internal/hook"),
        ("generic", "not a url"),
    ],
)
def test_refused_urls_are_422_without_echoing_the_url(auth_client, workspace, destination_type, url):
    """Each refused URL answers a 422 whose body never quotes the URL."""
    response = auth_client.post(
        _path(workspace),
        json={"name": "x", "destination_type": destination_type, "url": url, "triggers": []},
    )

    assert response.status_code == 422, response.text
    assert url not in response.text


def test_a_token_on_slack_is_refused(auth_client, workspace):
    """Only a generic webhook signs its deliveries."""
    response = auth_client.post(
        _path(workspace),
        json={"name": "x", "destination_type": "slack", "url": SLACK_URL, "token": TOKEN},
    )

    assert response.status_code == 422
    assert TOKEN not in response.text


def test_an_unknown_trigger_is_refused(auth_client, workspace):
    """Triggers are HCP's six."""
    response = auth_client.post(
        _path(workspace),
        json={"name": "x", "destination_type": "slack", "url": SLACK_URL, "triggers": ["run:exploded"]},
    )

    assert response.status_code == 422


def test_an_absent_workspace_is_404(auth_client):
    """A configuration needs a workspace to belong to."""
    response = auth_client.post(
        "/api/v1/workspaces/ws-01ARZ3NDEKTSV4RRFFQ69G5FAV/notification-configurations",
        json={"name": "x", "destination_type": "slack", "url": SLACK_URL},
    )

    assert response.status_code == 404


def test_the_per_workspace_limit_is_409(auth_client, workspace, monkeypatch):
    """Past the generous limit, a create is a conflict."""
    monkeypatch.setattr(store, "MAX_CONFIGURATIONS_PER_WORKSPACE", 1)
    _create(auth_client, workspace)

    response = auth_client.post(
        _path(workspace), json={"name": "y", "destination_type": "slack", "url": SLACK_URL}
    )

    assert response.status_code == 409


def test_reading_needs_only_read_scope_and_changing_needs_write(scoped_client, auth_client, workspace):
    """A read-only key lists but cannot create."""
    _create(auth_client, workspace)
    with scoped_client("workspaces:read") as reader:
        assert reader.get(_path(workspace)).status_code == 200
        refused = reader.post(
            _path(workspace), json={"name": "x", "destination_type": "slack", "url": SLACK_URL}
        )
    assert refused.status_code == 403


def test_a_stale_login_must_step_up_to_change_notifications(app, auth_client, workspace):
    """Where a workspace's run details go is a sensitive setting, behind step-up."""
    created = _create(auth_client, workspace)
    one = _path(workspace, f"/{created['id']}")
    with _person(app, auth_age=STEP_UP_MAX_AGE_SECONDS + 60) as stale:
        assert stale.get(_path(workspace)).status_code == 200
        create = stale.post(_path(workspace), json={"name": "x", "destination_type": "slack", "url": SLACK_URL})
        patch = stale.patch(one, json={"enabled": False})
        verify = stale.post(f"{one}/actions/verify")
        delete = stale.delete(one)

    for response in (create, patch, verify, delete):
        assert response.status_code == 401, response.text
        assert response.json()["error_code"] == "STEP_UP_REQUIRED"
    assert auth_client.get(one).json()["enabled"] is True


def test_a_fresh_login_may_change_notifications(app, workspace):
    """A person inside the step-up window creates one."""
    with _person(app) as fresh:
        response = fresh.post(_path(workspace), json={"name": "x", "destination_type": "slack", "url": SLACK_URL})

    assert response.status_code == 201, response.text


def test_verify_sends_a_signed_hcp_payload_and_records_it(auth_client, workspace, receiver):
    """The test delivery is HCP's version 1 payload with trigger `verification`, signed with the token."""
    created = _create(auth_client, workspace, destination_type="generic", url=GENERIC_URL, token=TOKEN)

    response = auth_client.post(_path(workspace, f"/{created['id']}/actions/verify"))

    assert response.status_code == 200, response.text
    outcome = response.json()
    assert outcome["status"] == "succeeded"
    assert outcome["trigger"] == "verification"
    assert outcome["status_code"] == 200
    assert outcome["response_excerpt"] == "ok"
    [request] = receiver.requests
    assert request.url.host == PUBLIC_ADDRESS
    assert request.url.path == "/hooks/generic-secret-path"
    assert request.url.query == b"k=v"
    assert request.headers["host"] == "receiver.example.com"
    assert request.extensions["sni_hostname"] == "receiver.example.com"
    expected = hmac.new(TOKEN.encode(), request.content, hashlib.sha512).hexdigest()
    assert request.headers["x-tfe-notification-signature"] == expected
    body = json.loads(request.content)
    assert body["payload_version"] == 1
    assert body["notification_configuration_id"] == created["id"]
    assert body["workspace_id"] == workspace["workspace_id"]
    assert body["workspace_name"] == workspace["name"]
    assert body["run_id"] is None
    assert body["notifications"][0]["trigger"] == "verification"
    assert body["notifications"][0]["message"] == "Verification of Team channel"
    stored = auth_client.get(_path(workspace, f"/{created['id']}")).json()
    assert stored["last_delivery"]["status"] == "succeeded"


def test_verify_reports_a_refusal_in_the_body(auth_client, workspace, receiver):
    """A receiver's 404 is the delivery's outcome, not the request's."""
    receiver.status_code = 404
    receiver.text = "no_service"
    created = _create(auth_client, workspace)

    response = auth_client.post(_path(workspace, f"/{created['id']}/actions/verify"))

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["status_code"] == 404
    assert response.json()["error"] == "http_error"
    assert "x-tfe-notification-signature" not in receiver.requests[0].headers


def test_verify_refuses_a_host_resolving_inside(auth_client, workspace, receiver, monkeypatch):
    """A name that resolves to a private address is refused at delivery, before any connection."""
    monkeypatch.setattr(sender, "resolve", lambda host, port: ["10.1.2.3"])
    created = _create(auth_client, workspace, destination_type="generic", url=GENERIC_URL)

    outcome = auth_client.post(_path(workspace, f"/{created['id']}/actions/verify")).json()

    assert outcome["status"] == "failed"
    assert outcome["error"] == "blocked_address"
    assert receiver.requests == []


def test_verify_never_follows_a_redirect(auth_client, workspace, receiver):
    """A 3xx is recorded as refused rather than followed somewhere else."""
    receiver.status_code = 302
    receiver.headers = {"location": "https://169.254.169.254/"}
    created = _create(auth_client, workspace, destination_type="generic", url=GENERIC_URL)

    outcome = auth_client.post(_path(workspace, f"/{created['id']}/actions/verify")).json()

    assert outcome["error"] == "redirect_refused"
    assert len(receiver.requests) == 1


def test_verify_is_rate_limited_per_configuration(auth_client, workspace, receiver):
    """Past twenty a minute, a test delivery is a 429 with Retry-After."""
    boto3.client("dynamodb", region_name=REGION).create_table(
        TableName=f"{TABLE_PREFIX}-rate-limits",
        KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    created = _create(auth_client, workspace)
    verify = _path(workspace, f"/{created['id']}/actions/verify")

    answers = [auth_client.post(verify).status_code for _ in range(21)]

    assert answers[:20] == [200] * 20
    assert answers[20] == 429


def test_deleting_the_workspace_deletes_its_configurations(auth_client, workspace):
    """No configuration outlives its workspace."""
    _create(auth_client, workspace)

    assert auth_client.delete(f"/api/v1/workspaces/{workspace['workspace_id']}").status_code in (200, 202, 204)

    assert store.list_configurations(workspace["workspace_id"]) == []
