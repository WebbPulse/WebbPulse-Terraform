"""The runner token exchange: a runner task trades its signed AWS identity for its run token.

STS and ECS are stubbed at the edges the exchange calls, so each check is
driven from the answer AWS would give rather than from a mock of the check.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.common.composition import settings as settings_module
from app.domains.runs import runner_tokens
from app.domains.runs import service as runs_service
from tests.conftest import TABLE_PREFIX

BASE = "/api/v1/runs"
ACCOUNT = "870550636948"
CLUSTER_ARN = f"arn:aws:ecs:us-west-2:{ACCOUNT}:cluster/{TABLE_PREFIX}-runner"
TASK_ID = "63182c22aaaabbbbccccddddeeeeffff"
PLAN_ROLE_SESSION = f"arn:aws:sts::{ACCOUNT}:assumed-role/{TABLE_PREFIX}-runner-plan/{TASK_ID}"


class _Response:
    """What urllib3 hands back: a status and the raw body."""

    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self.data = data


class _Sts:
    """Answers the replayed request as STS would and records what was sent."""

    def __init__(self, arn: str = PLAN_ROLE_SESSION, status: int = 200) -> None:
        self.arn = arn
        self.status = status
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> _Sts:
        """Stand in for `urllib3.PoolManager(...)`."""
        return self

    def request(self, method: str, url: str, *, body: bytes, headers: dict[str, str]) -> _Response:
        """Record the replay and answer with the configured caller."""
        self.calls.append({"method": method, "url": url, "body": body, "headers": headers})
        if self.status != 200:
            return _Response(self.status, b"<ErrorResponse/>")
        payload = f"<GetCallerIdentityResponse><GetCallerIdentityResult><Arn>{self.arn}</Arn>"
        return _Response(200, (payload + "</GetCallerIdentityResult></GetCallerIdentityResponse>").encode())


class _Ecs:
    """Describes one runner task the way ECS does."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self.tasks = tasks
        self.calls: list[dict[str, Any]] = []

    def describe_tasks(self, **kwargs: Any) -> dict[str, Any]:
        """Record the lookup and return the configured tasks."""
        self.calls.append(kwargs)
        return {"tasks": self.tasks}


def _task(run_id: str, phase: str = "plan", status: str = "RUNNING") -> dict[str, Any]:
    """A runner task as DescribeTasks returns it, started for `run_id` and `phase`."""
    return {
        "lastStatus": status,
        "desiredStatus": status,
        "overrides": {
            "containerOverrides": [
                {
                    "name": "runner",
                    "environment": [{"name": "RUN_ID", "value": run_id}, {"name": "PHASE", "value": phase}],
                }
            ]
        },
    }


def _signed(
    run_id: str, *, signed: str = "content-type;host;x-amz-date;x-amz-security-token;x-webbpulse-run-id"
) -> dict:
    """Headers shaped like the runner's signed `GetCallerIdentity`."""
    return {
        "Authorization": f"AWS4-HMAC-SHA256 Credential=AKID/20260927/us-west-2/sts/aws4_request, "
        f"SignedHeaders={signed}, Signature=abc",
        "X-Amz-Date": "20260927T000000Z",
        "X-Amz-Security-Token": "session",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "x-webbpulse-run-id": run_id,
        "X-Smuggled": "dropped",
    }


@pytest.fixture
def cluster(monkeypatch):
    """The runner cluster the exchange describes tasks on."""
    monkeypatch.setenv("RUNNER_CLUSTER_ARN", CLUSTER_ARN)
    settings_module.reset_settings_cache()
    yield CLUSTER_ARN
    settings_module.reset_settings_cache()


@pytest.fixture
def sts(monkeypatch):
    """A stubbed STS answering as the plan task role session of `TASK_ID`."""
    import urllib3

    fake = _Sts()
    monkeypatch.setattr(urllib3, "PoolManager", fake)
    return fake


@pytest.fixture
def ecs(monkeypatch, created_run):
    """A stubbed ECS whose one task is the plan task of `created_run`."""
    fake = _Ecs([_task(created_run["run_id"])])
    monkeypatch.setattr(runner_tokens, "_ecs", lambda settings: fake)
    return fake


def _exchange(client: TestClient, run_id: str, headers: dict) -> Any:
    """Post a signed identity to the exchange route."""
    return client.post(f"{BASE}/{run_id}/runner-token", json={"headers": headers})


def test_a_running_task_gets_a_token_that_replaces_the_old_one(app, client, created_run, cluster, sts, ecs, caplog):
    """The exchanged token opens the bundle and the token it replaced no longer does."""
    run_id = created_run["run_id"]
    with caplog.at_level(logging.INFO):
        response = _exchange(client, run_id, _signed(run_id))
    assert response.status_code == 200, response.text
    token = response.json()["run_token"]
    assert token.startswith("wpk_") and token != created_run["run_token"]

    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as runner:
        assert runner.get(f"{BASE}/{run_id}/bundle").status_code == 200
    with TestClient(app, headers={"Authorization": f"Bearer {created_run['run_token']}"}) as old:
        assert old.get(f"{BASE}/{run_id}/bundle").status_code == 401

    assert ecs.calls == [{"cluster": CLUSTER_ARN, "tasks": [TASK_ID]}]
    events = [getattr(record, "event", "") for record in caplog.records]
    assert "runs.runner_token.exchanged" in events
    assert token not in caplog.text


def _with_task_token(task: dict[str, Any], token: str) -> dict[str, Any]:
    """The same task with a Step Functions task token among its overrides."""
    task["overrides"]["containerOverrides"][0]["environment"].append({"name": "TASK_TOKEN", "value": token})
    return task


def test_the_exchange_records_the_task_whose_token_the_phase_resolves(client, created_run, cluster, sts, ecs):
    """The phase result resolves the task token from the exchanged task's own overrides, never the caller."""
    run_id = created_run["run_id"]
    ecs.tasks = [_with_task_token(_task(run_id), "sfn-token")]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 200

    settings = settings_module.get_settings()
    run = runs_service.get_run(run_id, settings=settings)
    assert run[runner_tokens.RUNNER_TASK_ATTRIBUTE] == TASK_ID
    assert runner_tokens.phase_task_token(run, "plan", settings=settings) == "sfn-token"
    with pytest.raises(runner_tokens.ExchangeRefused):
        runner_tokens.phase_task_token(run, "apply", settings=settings)


def test_a_run_no_task_exchanged_for_resolves_no_token(created_run, cluster, ecs):
    """With no exchanged task there is nothing to resolve, so no report can complete the phase."""
    settings = settings_module.get_settings()
    run = runs_service.get_run(created_run["run_id"], settings=settings)
    with pytest.raises(runner_tokens.ExchangeRefused):
        runner_tokens.phase_task_token(run, "plan", settings=settings)
    assert ecs.calls == []


def test_a_task_with_no_task_token_resolves_none(client, created_run, cluster, sts, ecs):
    """A task started without a task token is refused rather than resolved to an empty token."""
    run_id = created_run["run_id"]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 200
    settings = settings_module.get_settings()
    run = runs_service.get_run(run_id, settings=settings)
    with pytest.raises(runner_tokens.ExchangeRefused):
        runner_tokens.phase_task_token(run, "plan", settings=settings)


def test_only_the_signed_headers_and_the_fixed_body_reach_sts(client, created_run, cluster, sts, ecs):
    """The replay is exactly one regional `GetCallerIdentity`, whatever else the caller sent."""
    run_id = created_run["run_id"]
    _exchange(client, run_id, _signed(run_id))
    (call,) = sts.calls
    assert call["url"] == "https://sts.us-west-2.amazonaws.com/"
    assert call["body"] == runner_tokens.STS_BODY.encode()
    assert set(call["headers"]) == runner_tokens.FORWARDED_HEADERS
    assert "x-smuggled" not in call["headers"]


def test_the_exchanged_token_is_revoked_when_the_run_ends(app, client, created_run, cluster, sts, ecs):
    """A terminal transition revokes the token the runner is actually holding."""
    run_id = created_run["run_id"]
    token = _exchange(client, run_id, _signed(run_id)).json()["run_token"]
    runs_service.finish_run(run_id, "applied")
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as runner:
        assert runner.get(f"{BASE}/{run_id}/bundle").status_code == 401


def test_an_unsigned_run_id_is_refused_before_sts(client, created_run, cluster, sts, ecs):
    """A run id header outside SignedHeaders could be rewritten, so STS is never asked."""
    run_id = created_run["run_id"]
    response = _exchange(client, run_id, _signed(run_id, signed="content-type;host;x-amz-date"))
    assert response.status_code == 401
    assert sts.calls == []


def test_a_proof_for_another_run_is_refused(client, created_run, cluster, sts, ecs):
    """A proof signed for one run cannot be presented on another run's route."""
    other = "run-01JBOTHERRUNAAAAAAAAAAAAAA"
    response = _exchange(client, created_run["run_id"], _signed(other))
    assert response.status_code == 401
    assert sts.calls == []


def test_a_signature_sts_rejects_is_refused(client, created_run, cluster, sts, ecs):
    """STS is the authority on the signature, and a 403 from it is a 401 here."""
    sts.status = 403
    run_id = created_run["run_id"]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401
    assert ecs.calls == []


@pytest.mark.parametrize(
    "arn",
    [
        f"arn:aws:sts::{ACCOUNT}:assumed-role/someone-else/{TASK_ID}",
        f"arn:aws:sts::111111111111:assumed-role/{TABLE_PREFIX}-runner-plan/{TASK_ID}",
        f"arn:aws:sts::{ACCOUNT}:assumed-role/{TABLE_PREFIX}-runner-plan/a-person",
        f"arn:aws:iam::{ACCOUNT}:user/someone",
    ],
)
def test_a_caller_that_is_not_a_runner_task_is_refused(client, created_run, cluster, sts, ecs, arn):
    """Only a runner task role session, in the runner's account, named by a task id passes."""
    sts.arn = arn
    run_id = created_run["run_id"]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401
    assert ecs.calls == []


@pytest.mark.parametrize(
    "task",
    [
        {"stopped": True},
        {"run_id": "run-01JBOTHERRUNAAAAAAAAAAAAAA"},
        {"phase": "apply"},
        {"phase": "destroy"},
    ],
)
def test_a_task_that_does_not_match_the_run_is_refused(client, created_run, cluster, sts, ecs, task):
    """A stopped task, another run's task, or a task for the wrong phase gets nothing."""
    run_id = created_run["run_id"]
    ecs.tasks = [
        _task(
            task.get("run_id", run_id),
            task.get("phase", "plan"),
            "STOPPED" if task.get("stopped") else "RUNNING",
        )
    ]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401


def test_a_task_the_cluster_does_not_know_is_refused(client, created_run, cluster, sts, ecs):
    """DescribeTasks answering nothing means the session is not a runner task."""
    ecs.tasks = []
    run_id = created_run["run_id"]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401


def test_no_cluster_configured_refuses_everything(client, created_run, sts, ecs):
    """Without `RUNNER_CLUSTER_ARN` no task can be checked, so no token is given."""
    run_id = created_run["run_id"]
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401
    assert ecs.calls == []


def test_a_finished_run_gets_no_token(client, created_run, cluster, sts, ecs):
    """A run past its phase is refused even for its own still running task."""
    run_id = created_run["run_id"]
    runs_service.finish_run(run_id, "errored")
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401


def test_a_run_that_ends_during_the_exchange_keeps_no_live_token(
    app, client, created_run, cluster, sts, ecs, monkeypatch
):
    """The swap is conditional on the phase, and a lost race revokes the token it minted."""
    run_id = created_run["run_id"]
    before = runs_service.get_run(run_id)
    runs_service.finish_run(run_id, "errored")
    monkeypatch.setattr(runs_service, "get_run", lambda *args, **kwargs: before)

    minted: list[str] = []
    from webbpulse.identity import api_keys

    original = api_keys.mint

    def recording_mint(**kwargs: Any) -> Any:
        """Mint as usual, remembering the plaintext the exchange never returns."""
        result = original(**kwargs)
        minted.append(result.plaintext)
        return result

    monkeypatch.setattr(api_keys, "mint", recording_mint)
    assert _exchange(client, run_id, _signed(run_id)).status_code == 401
    assert len(minted) == 1
    monkeypatch.undo()
    with TestClient(app, headers={"Authorization": f"Bearer {minted[0]}"}) as runner:
        assert runner.get(f"{BASE}/{run_id}/bundle").status_code == 401


def test_every_refusal_is_the_same_answer(client, created_run, cluster, sts, ecs, caplog):
    """The caller learns nothing about which check failed, and the reason is logged instead."""
    run_id = created_run["run_id"]
    sts.arn = f"arn:aws:sts::{ACCOUNT}:assumed-role/someone-else/{TASK_ID}"
    with caplog.at_level(logging.WARNING):
        first = _exchange(client, run_id, _signed(run_id))
    ecs.tasks = []
    sts.arn = PLAN_ROLE_SESSION
    second = _exchange(client, run_id, _signed(run_id))
    assert first.status_code == second.status_code == 401
    assert {**first.json(), "request_id": ""} == {**second.json(), "request_id": ""}
    refused = [record for record in caplog.records if getattr(record, "event", "") == "runs.runner_token.refused"]
    assert refused and getattr(refused[0], "reason", "")


def test_the_request_body_is_bounded(client, created_run, cluster, sts, ecs):
    """An oversized header value is refused before anything is replayed."""
    run_id = created_run["run_id"]
    headers = _signed(run_id) | {"Authorization": "x" * 5000}
    assert _exchange(client, run_id, headers).status_code == 401
    assert sts.calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/runs/x/runner-token", {"headers": {}}),
        ("/api/v1/runs/x/runner-token", {"nonsense": True}),
        ("/api/v1/runs/run-00000000000000000000000000/runner-token", {"unexpected": 1}),
        ("/api/v1/runs/run-00000000000000000000000000/runner-token", None),
    ],
)
def test_a_malformed_exchange_is_the_same_401_as_a_refused_one(client, path, body):
    """A stranger learns nothing about the route's schema from a malformed request."""
    response = client.post(path, json=body) if body is not None else client.post(path, content=b"{not json")

    assert response.status_code == 401
    assert "loc" not in response.text
    assert response.json()["error_code"] == "UNAUTHORIZED"


def test_a_refused_exchange_and_a_malformed_one_answer_alike(client, created_run, cluster, sts, ecs):
    """The body of a refusal does not say whether the request was malformed or unproven."""
    run_id = created_run["run_id"]
    refused = _exchange(client, run_id, {"Authorization": "unsigned"})
    malformed = client.post(f"{BASE}/{run_id}/runner-token", json={"nonsense": True})

    assert refused.status_code == malformed.status_code == 401
    assert {k: v for k, v in refused.json().items() if k != "request_id"} == {
        k: v for k, v in malformed.json().items() if k != "request_id"
    }
