"""Shared fixtures: a mocked AWS environment, the four tables, buckets and auth.

Every test runs against moto rather than a stub layer, so a query that works here
is one a deployed table can serve. The environment is set before `app` is
imported, because the settings object reads it at import.

Authorization is exercised through real `wpk_` API keys rather than a forged
authorizer context. That is deliberate: the key path and the JWT path converge on
the same claims object, so a key-authenticated test covers `require_scopes` as it
runs in production, and the run token gate can only be tested this way.
"""

from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from webbpulse.testing import CheckedKey

pytest_plugins = ["webbpulse.testing"]
"""The package's own fixtures."""

ENVIRONMENT = "test"
TABLE_PREFIX = f"webbpulse-terraform-{ENVIRONMENT}"
STATE_BUCKET = f"{TABLE_PREFIX}-state"
STATE_KMS_KEY_ARN = "arn:aws:kms:us-west-2:870550636948:key/00000000-0000-0000-0000-000000000000"
ARTIFACTS_BUCKET = f"{TABLE_PREFIX}-artifacts"
RUNNER_LOG_GROUP = f"/aws/ecs/{TABLE_PREFIX}-runner"
STATE_MACHINE_NAME = f"{TABLE_PREFIX}-run"
VARIABLES_MASTER_KEY = base64.b64encode(b"k" * 32).decode()
RUNNER_TASK_ROLE_ARNS = [
    f"arn:aws:iam::870550636948:role/{TABLE_PREFIX}-runner-apply",
    f"arn:aws:iam::870550636948:role/{TABLE_PREFIX}-runner-plan",
]
RUNNER_TASK_ROLE_ARN = ",".join(RUNNER_TASK_ROLE_ARNS)
RUN_ROLE_NAME_PREFIX = f"{TABLE_PREFIX}-workspace-"
RUN_CREDENTIALS_ROLE_ARN = f"arn:aws:iam::870550636948:role/{TABLE_PREFIX}-run-credentials"
RUN_STATE_ROLE_ARN = f"arn:aws:iam::870550636948:role/{TABLE_PREFIX}-run-state"

os.environ.update(
    {
        "TESTING": "1",
        "AWS_DEFAULT_REGION": "us-west-2",
        "AWS_REGION_NAME": "us-west-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "ENVIRONMENT": ENVIRONMENT,
        "LOG_LEVEL": "WARNING",
        "WORKSPACES_TABLE": f"{TABLE_PREFIX}-workspaces",
        "RUNS_TABLE": f"{TABLE_PREFIX}-runs",
        "VARIABLES_TABLE": f"{TABLE_PREFIX}-variables",
        "CONFIG_VERSIONS_TABLE": f"{TABLE_PREFIX}-config-versions",
        "USERS_TABLE": f"{TABLE_PREFIX}-users",
        "GITHUB_TABLE": f"{TABLE_PREFIX}-github",
        "REGISTRY_TABLE": f"{TABLE_PREFIX}-registry",
        "STATE_BUCKET": STATE_BUCKET,
        "STATE_KMS_KEY_ARN": STATE_KMS_KEY_ARN,
        "ARTIFACTS_BUCKET": ARTIFACTS_BUCKET,
        "RUNNER_LOG_GROUP": RUNNER_LOG_GROUP,
        "VARIABLES_MASTER_KEY": VARIABLES_MASTER_KEY,
        "RUNNER_TASK_ROLE_ARN": RUNNER_TASK_ROLE_ARN,
        "RUN_ROLE_NAME_PREFIX": RUN_ROLE_NAME_PREFIX,
        "RUN_CREDENTIALS_ROLE_ARN": RUN_CREDENTIALS_ROLE_ARN,
        "RUN_STATE_ROLE_ARN": RUN_STATE_ROLE_ARN,
    }
)
os.environ.pop("APP_SECRET_ID", None)
os.environ.pop("APP_SECRETS_ARN", None)
os.environ.pop("DYNAMODB_ENDPOINT_URL", None)
os.environ.pop("S3_ENDPOINT_URL", None)
os.environ.pop("IDENTITY_ISSUER", None)

import boto3  # noqa: E402
import pytest  # noqa: E402
import webbpulse.storage  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from moto import mock_aws  # noqa: E402
from webbpulse.identity.api_keys import API_KEY_TABLE, mint  # noqa: E402

from app.common.composition import settings as settings_module  # noqa: E402
from app.common.core.auth import ALL_SCOPES, RUN_TOKEN_TENANT, RUNNER_SCOPE, api_key_store  # noqa: E402
from app.common.db.tables import ALL_TABLES, local_table_name, table_definition  # noqa: E402

REGION = "us-west-2"


def create_all_tables() -> None:
    """Create the project tables plus the identity `api-keys` table.

    The identity table is created here rather than by a fixture a test opts into,
    because every guarded route verifies a key against it and an absent table
    reads as an invalid credential, which turns an authorization bug into a 401
    that looks deliberate.
    """
    client = boto3.client("dynamodb", region_name=REGION)
    for logical in ALL_TABLES:
        physical = local_table_name(logical, ENVIRONMENT)
        client.create_table(**table_definition(logical, physical))
    client.create_table(**API_KEY_TABLE.create_table_request(TABLE_PREFIX))


def create_buckets() -> None:
    """Create the state and artifacts buckets."""
    client = boto3.client("s3", region_name=REGION)
    for bucket in (STATE_BUCKET, ARTIFACTS_BUCKET):
        client.create_bucket(
            Bucket=bucket,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )


@pytest.fixture(autouse=True)
def _primary_keys_only(primary_keys_only: list[CheckedKey]) -> list[CheckedKey]:
    """Hold every DynamoDB key in the suite to its table's primary key, as DynamoDB does."""
    return primary_keys_only


@pytest.fixture(autouse=True)
def aws_environment():
    """Run each test against freshly created tables and buckets in a mocked AWS.

    The settings cache and the storage client cache are both cleared on the way in
    and out: each holds a client bound to the previous moto context, which would
    otherwise answer from a socket that no longer exists.
    """
    with mock_aws():
        settings_module.reset_settings_cache()
        webbpulse.storage.reset_client_cache()
        create_all_tables()
        create_buckets()
        yield
        settings_module.reset_settings_cache()
        webbpulse.storage.reset_client_cache()


@pytest.fixture
def identity_tables():
    """Every identity module table, for a test that exercises the `/api/auth` routes.

    Created by the package's own specs rather than by shapes restated here, so a
    table the package renames is a failing import rather than a silent miss. Opt-in
    because only the identity tests need them, and creating eleven tables per test
    would slow the rest of the suite for nothing.
    """
    from webbpulse.identity import TABLES

    client = boto3.client("dynamodb", region_name=REGION)
    existing = set(client.list_tables()["TableNames"])
    for spec in TABLES:
        request = spec.create_table_request(TABLE_PREFIX)
        if request["TableName"] not in existing:
            client.create_table(**request)
    return TABLE_PREFIX


@pytest.fixture
def settings():
    """This test's settings, resolved inside the mocked environment."""
    return settings_module.get_settings()


@pytest.fixture
def app(settings):
    """The whole surface in one process, built from both domains' routers."""
    from app.common.composition.app import build_app

    return build_app(settings)


@pytest.fixture
def client(app):
    """A client with no credentials, for asserting that a route refuses one."""
    with TestClient(app) as test_client:
        yield test_client


def runner_token(run_id: str) -> str:
    """The run token a runner task of `run_id` holds, as the exchange leaves it.

    Minted and stored as the run's `run_token_hash` exactly as
    `runner_tokens.exchange` does once a task has proved its identity, which the
    exchange's own tests cover.
    """
    from app.common.db import repositories
    from app.domains.runs.service import RUN_TOKEN_TTL

    minted = mint(
        user_id=run_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=(RUNNER_SCOPE,),
        name=f"run token {run_id}",
        expires_at=datetime.now(timezone.utc) + RUN_TOKEN_TTL,
        store=api_key_store(),
    )
    repositories.runs(settings_module.get_settings()).update(
        {"run_id": run_id},
        update_expression="SET run_token_hash = :hash",
        expression_values={":hash": minted.record.key_hash},
    )
    return minted.plaintext


def seed_user(user_id: str, *, is_admin: bool = True, disabled: bool = False) -> None:
    """Write an enabled, verified `users` row for `user_id`, unless one is there.

    A key's scopes are intersected with its owner's on every request, so a key
    whose owner has no row holds nothing. An admin by default, so a key's own
    scopes are what a test asserts on.
    """
    from app.common.db.users import User, UserRepository

    users = UserRepository()
    if users.get(user_id) is None:
        users.create(
            User(
                id=user_id,
                email=f"{user_id}@example.test",
                email_verified=True,
                is_admin=is_admin,
                disabled=disabled,
            )
        )


def person_headers(
    *,
    user_id: str = "user-human",
    scopes: tuple[str, ...] | None = None,
    roles: tuple[str, ...] = (),
    auth_age: int | None = 0,
) -> dict[str, str]:
    """The request context header a route behind the JWT authorizer sees for a person.

    The gateway flattens every claim to a string, so `roles` goes down as the bracketed
    form and `scope` as the space-joined one, which is what `coerce_claims` parses back.
    `auth_age` is how many seconds ago the person signed in, stamped as `auth_time`; None
    leaves the claim out, as an MCP OAuth token does.
    """
    from webbpulse.http import REQUEST_CONTEXT_HEADER

    claims: dict[str, str] = {
        "sub": user_id,
        "scope": " ".join(ALL_SCOPES if scopes is None else scopes),
        "roles": json.dumps(list(roles)),
    }
    if auth_age is not None:
        claims["auth_time"] = str(int(time.time()) - auth_age)
    return {REQUEST_CONTEXT_HEADER: json.dumps({"authorizer": {"jwt": {"claims": claims}}})}


def mint_key(*scopes: str, user_id: str = "user-test") -> str:
    """Mint a `wpk_` key carrying `scopes` and return its plaintext.

    Seeds the owner's `users` row too, except for a run token, whose subject is
    a run id rather than a person.
    """
    if RUNNER_SCOPE not in scopes:
        seed_user(user_id)
    minted = mint(
        user_id=user_id,
        tenant_id=RUN_TOKEN_TENANT,
        scopes=scopes or ALL_SCOPES,
        store=api_key_store(),
    )
    return minted.plaintext


@pytest.fixture
def auth_client(app):
    """A client authenticated as an agent holding every human scope.

    Every scope, because these tests assert a route's behaviour rather than its
    guard. The guard itself is asserted in `tests/common/test_auth.py`, which
    mints deliberately insufficient keys.
    """
    token = mint_key(*ALL_SCOPES)
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as test_client:
        yield test_client


@pytest.fixture
def scoped_client(app):
    """A factory building a client that holds exactly the scopes it is given."""

    def build(*scopes: str) -> TestClient:
        """A client authenticated with a key carrying only `scopes`."""
        token = mint_key(*scopes)
        return TestClient(app, headers={"Authorization": f"Bearer {token}"})

    return build


WORKSPACE_PAYLOAD = {
    "name": "example",
    "engine": "terraform",
    "engine_version": "1.11.4",
    "run_role_arn": "arn:aws:iam::870550636948:role/webbpulse-terraform-test-run",
    "working_directory": "",
    "description": "An example workspace.",
}
"""A valid create body, so a test that is not about validation does not restate one."""


@pytest.fixture
def workspace(auth_client):
    """One created workspace."""
    response = auth_client.post("/api/v1/workspaces", json=WORKSPACE_PAYLOAD)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def uploaded_config_version(auth_client, workspace):
    """A config version marked uploaded, which is what a run requires."""
    from app.domains.workspaces import service as workspaces_service

    workspace_id = workspace["workspace_id"]
    response = auth_client.post(
        f"/api/v1/workspaces/{workspace_id}/config-versions",
        json={"size_bytes": 1024},
    )
    assert response.status_code == 201, response.text
    config_version = response.json()["config_version"]
    workspaces_service.mark_config_version_uploaded(config_version["config_version_id"])
    return config_version


@pytest.fixture
def state_machine():
    """A state machine whose ARN the runs settings resolve to.

    Created through moto rather than faked, so `start_execution` and
    `stop_execution` are exercised as calls rather than as mocks.
    """
    client = boto3.client("stepfunctions", region_name=REGION)
    created = client.create_state_machine(
        name=STATE_MACHINE_NAME,
        definition='{"StartAt": "Done", "States": {"Done": {"Type": "Succeed"}}}',
        roleArn="arn:aws:iam::123456789012:role/StepFunctions",
    )
    arn = created["stateMachineArn"]
    os.environ["RUN_STATE_MACHINE_ARN"] = arn
    settings_module.reset_settings_cache()
    yield arn
    os.environ.pop("RUN_STATE_MACHINE_ARN", None)
    settings_module.reset_settings_cache()


@pytest.fixture
def runner_log_group():
    """The runner log group, so a logs read has something to read from."""
    client = boto3.client("logs", region_name=REGION)
    client.create_log_group(logGroupName=RUNNER_LOG_GROUP)
    return RUNNER_LOG_GROUP


@pytest.fixture
def created_run(auth_client, workspace, uploaded_config_version, state_machine):
    """A started run, carrying the run token its runner task would have exchanged for."""
    response = auth_client.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": False,
            "message": "An example run.",
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    return created | {"run_token": runner_token(created["run_id"])}


@pytest.fixture
def plan_only_run(auth_client, workspace, uploaded_config_version, state_machine):
    """A started run that will finish at the end of its plan."""
    response = auth_client.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace["workspace_id"],
            "config_version_id": uploaded_config_version["config_version_id"],
            "plan_only": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def runner_client(app, created_run):
    """A client authenticated as the runner for `created_run`."""
    token = created_run["run_token"]
    assert token, "a started run has to carry its token"
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as test_client:
        yield test_client


@pytest.fixture
def planned_with_changes(created_run):
    """A run whose plan found changes, before its task token has arrived.

    The state the confirmations queue message is sent against: the phase result has
    moved the run to `awaiting_confirmation` and the state machine has reached
    `AwaitConfirmation`, but nothing has consumed the message yet.
    """
    from app.domains.runs import service as runs_service

    runs_service.record_phase_result(
        created_run["run_id"],
        {
            "phase": "plan",
            "exit_code": 0,
            "changes": {"add": 2, "change": 1, "destroy": 0},
            "error": "",
        },
    )
    return runs_service.get_run(created_run["run_id"])


@pytest.fixture
def awaiting_confirmation(planned_with_changes):
    """A run that planned with changes and is holding its confirmation task token.

    Driven through the real phase-result transition rather than written into the
    table, so the state under test is one the state machine can actually produce.
    The token is stored the way the consumer stores it.
    """
    from app.domains.runs import service as runs_service

    run_id = planned_with_changes["run_id"]
    runs_service.store_confirm_task_token(
        run_id,
        "task-token-for-confirmation",
        expected_statuses=frozenset(runs_service.CONFIRMABLE_STATUSES),
    )
    return runs_service.get_run(run_id)
