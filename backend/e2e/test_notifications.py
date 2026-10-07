"""Run notifications reach a real receiver, signed, through the queue.

The receiver is the staging only `<prefix>-e2e-notification-receiver` function, created by
`terraform/e2e_notification_receiver.tf` behind a public function URL. It checks the
HMAC-SHA512 signature against the token the suite repeats as its `token` query parameter
and answers with what it saw, which the control plane keeps as the delivery's
`response_excerpt`. Nothing is posted to Slack or Discord and nothing billable is created.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from typing import Any

import pytest
from test_product_flows import PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS, POLL_SECONDS, _create_run, _upload, _wait_for

DELIVERY_TIMEOUT_SECONDS = 180


def _receiver_url(e2e_env: Any) -> str:
    """The receiver's function URL, skipping where the stage has no receiver."""
    if str(e2e_env.environment).strip().lower() != "staging":
        pytest.skip("the notification receiver exists in staging only")
    import boto3

    client = boto3.session.Session(region_name=e2e_env.aws_region).client("lambda")
    try:
        answer = client.get_function_url_config(FunctionName="webbpulse-terraform-staging-e2e-notification-receiver")
    except client.exceptions.ResourceNotFoundException:
        pytest.skip("the notification receiver is not deployed yet")
    return str(answer["FunctionUrl"])


def _excerpt(delivery: dict[str, Any]) -> dict[str, Any]:
    """The receiver's verdict, parsed from the delivery's response excerpt."""
    try:
        return dict(json.loads(str(delivery.get("response_excerpt") or "{}")))
    except ValueError:
        pytest.fail(f"the receiver's answer was not JSON: {delivery}")


def _create(api: Any, workspace_id: str, url: str, token: str, triggers: list[str]) -> dict[str, Any]:
    """Create a generic configuration pointing at the receiver."""
    response = api.post(
        f"/api/v1/workspaces/{workspace_id}/notification-configurations",
        json={
            "name": "e2e receiver",
            "destination_type": "generic",
            "url": f"{url}?token={token}",
            "token": token,
            "triggers": triggers,
        },
    )
    assert response.status_code == 201, (
        f"creating the configuration answered {response.status_code}: {response.text[:400]}"
    )
    created = dict(response.json())
    assert token not in json.dumps(created), "the configuration response carried the token"
    assert "url" not in created and created["url_masked"].endswith("/****")
    return created


@pytest.mark.e2e_writes
def test_send_test_reaches_the_receiver_signed(
    e2e_env: Any, workspace: dict[str, Any], step_up_again: Callable[[], Any]
) -> None:
    """Send test delivers HCP's version 1 payload with a signature the receiver verifies."""
    url = _receiver_url(e2e_env)
    api = step_up_again()
    workspace_id = str(workspace["workspace_id"])
    token = secrets.token_hex(24)
    created = _create(api, workspace_id, url, token, ["run:completed"])

    verified = api.post(f"/api/v1/workspaces/{workspace_id}/notification-configurations/{created['id']}/actions/verify")
    assert verified.status_code == 200, verified.text[:400]
    delivery = dict(verified.json())
    assert delivery["status"] == "succeeded", delivery
    verdict = _excerpt(delivery)
    assert verdict.get("signature_valid") is True, verdict
    assert verdict.get("trigger") == "verification" and verdict.get("payload_version") == 1, verdict

    listed = api.get(f"/api/v1/workspaces/{workspace_id}/notification-configurations")
    assert listed.status_code == 200, listed.text[:400]
    assert listed.json()["items"][0]["last_delivery"]["status"] == "succeeded"

    deleted = step_up_again().delete(f"/api/v1/workspaces/{workspace_id}/notification-configurations/{created['id']}")
    assert deleted.status_code == 204, deleted.text[:400]


@pytest.mark.e2e_writes
def test_a_finished_plan_notifies_through_the_queue(
    api: Any, e2e_env: Any, workspace: dict[str, Any], step_up_again: Callable[[], Any]
) -> None:
    """A plan only run that finishes sends run:completed, signed, naming the run."""
    url = _receiver_url(e2e_env)
    workspace_id = str(workspace["workspace_id"])
    token = secrets.token_hex(24)
    created = _create(step_up_again(), workspace_id, url, token, ["run:completed", "run:errored"])

    run_id = _create_run(api, workspace_id, _upload(api, workspace_id), plan_only=True)
    run = _wait_for(api, run_id, PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS)
    assert run.get("status") == "planned_and_finished", run

    path = f"/api/v1/workspaces/{workspace_id}/notification-configurations/{created['id']}"
    deadline = time.monotonic() + DELIVERY_TIMEOUT_SECONDS
    delivery: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = api.get(path)
        assert response.status_code == 200, response.text[:400]
        delivery = dict(response.json().get("last_delivery") or {})
        if delivery.get("run_id") == run_id and delivery.get("status") != "retrying":
            break
        time.sleep(POLL_SECONDS)
    assert delivery.get("run_id") == run_id, f"no delivery for {run_id} within {DELIVERY_TIMEOUT_SECONDS}s: {delivery}"
    assert delivery["status"] == "succeeded" and delivery["trigger"] == "run:completed", delivery
    verdict = _excerpt(delivery)
    assert verdict.get("signature_valid") is True, verdict
    assert verdict.get("run_id") == run_id and verdict.get("trigger") == "run:completed", verdict
