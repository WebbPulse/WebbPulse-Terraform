"""The control plane's e2e plugin: the deployed document, the UI contract and cleanup.

Everything the shared `webbpulse.e2e` plugin needs that only this product can answer. The
OpenAPI document is built from this commit's own app factory, so the suite compares the
gateway against the code that was deployed rather than against a checked-in copy.

Nothing here prints a password, a token or a gate value.
"""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import pytest
from runs_cleanup import end_runs_for_workspace

pytest_plugins = ["webbpulse.e2e"]

RUN_ROLE_ARN_VARIABLE = "E2E_RUN_ROLE_ARN"

JOURNEY_RUN_ROLE_ARN_VARIABLE = "E2E_JOURNEY_RUN_ROLE_ARN"

STALE_SECONDS = 3600

_DOCUMENT_ENVIRONMENT = {
    "ENVIRONMENT": "staging",
    "IDENTITY_ENVIRONMENT": "local",
    "IDENTITY_SIGNER": "local",
    "IDENTITY_LOCAL_SIGNER_SEED": "openapi-build-only-seed",
    "IDENTITY_SIGNING_KEY_ARNS": '["arn:aws:kms:us-west-2:000000000000:key/openapi-build-only"]',
    "IDENTITY_ISSUER": "https://api.staging.terraform.webbpulse.com/api/auth",
    "IDENTITY_AUDIENCE": "webbpulse-terraform-staging-api",
    "IDENTITY_TABLE_PREFIX": "webbpulse-terraform-staging",
    "IDENTITY_REGISTRATION_ENABLED": "false",
    "IDENTITY_EPHEMERAL_USERS_ENABLED": "true",
    "IDENTITY_PASSKEYS_ENABLED": "true",
    "IDENTITY_PASSKEYS_PASSWORDLESS": "true",
    "IDENTITY_WEBAUTHN_ORIGINS": '["https://staging.terraform.webbpulse.com"]',
}


@contextmanager
def _document_environment() -> Iterator[None]:
    """Run a block with the settings the app factory needs to build offline.

    The identity package builds a signing client while mounting its routes, so a real
    KMS client would be constructed and the document could not be built without AWS
    credentials. The local signer avoids that, and the package itself refuses the local
    signer in production, so this can never be what a deployed function runs.
    """
    previous = {name: os.environ.get(name) for name in _DOCUMENT_ENVIRONMENT}
    os.environ.update(_DOCUMENT_ENVIRONMENT)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def e2e_openapi_document() -> dict[str, Any]:
    """This commit's OpenAPI document, built with no AWS call."""
    with _document_environment():
        from app.common.composition.app import build_app

        return dict(build_app().openapi())


@pytest.fixture(scope="session")
def openapi_document() -> dict[str, Any]:
    """The document the coverage and reachability groups read."""
    return e2e_openapi_document()


def pytest_e2e_login_form(env: Any) -> Any:
    """The sign-in locators, which are CSS rather than test ids.

    The frontend carries no `data-testid` attributes, so every locator here is a CSS
    selector over the markup the pages already render.

    The signed-in chrome renders twice, as a rail for wide viewports and a header for
    narrow ones, and only one of the two is ever visible. These select the rail, which
    is the copy shown at the viewport the browser suite runs at, so the marker the
    plugin waits on is the one actually on screen.
    """
    from webbpulse.e2e import LoginForm

    del env
    return LoginForm(
        path="/sign-in",
        email="input[type=email]",
        password="input[type=password]",
        submit="button[type=submit]:has-text('Sign in')",
        signed_in_marker="aside nav[aria-label='Primary'] a[href='/workspaces']",
        sign_out="aside button:has-text('Sign out')",
        signed_out_marker="button[type=submit]:has-text('Sign in')",
        protected_redirect="/sign-in",
        guest_redirect="/workspaces",
    )


def pytest_e2e_routes(env: Any) -> Any:
    """Every route the SPA mounts, and who may see it."""
    from webbpulse.e2e import RouteSpec

    del env
    return [
        RouteSpec(path="/sign-in", access="guest-only", name="sign-in"),
        RouteSpec(path="/workspaces", access="protected", name="workspaces"),
        RouteSpec(path="/runs", access="protected", name="runs"),
    ]


def pytest_e2e_journeys(env: Any) -> Any:
    """The UI journeys that would have caught the regressions found by hand.

    Each journey creates its own workspace and then proves one outcome about it. They
    cannot share one, because the journeys are separate parametrised cases distributed by
    `--dist loadgroup` and each xdist worker signs in as an ephemeral user of its own, so
    a row one journey creates is neither ordered before nor even visible to the other.

    The locators are roles and accessible names rather than structure, so restyling the
    shell or the table does not break them. The create form lives in a dialog behind the
    "New workspace" button, and creating a workspace navigates straight to its detail
    page, which is where the run role ARN is now saved through the setup checklist.

    The names are resolved here from the run's own prefix rather than left as `{run_id}`,
    because only `Fill`, `ExpectText` and `ExpectUrl` expand that placeholder: a `Click`
    locator carrying it would be searched for literally and never match a row.
    """
    from webbpulse.e2e import Click, ExpectText, ExpectUrl, ExpectVisible, Fill, Goto, Journey, Record

    if not resolve_journey_run_role_arn(env):
        return []

    prefix = getattr(env, "resource_prefix", "") or "e2e-{run_id}-"
    created = f"{prefix}ui-created"
    opened = f"{prefix}ui-opened"

    def create(name: str) -> list[Any]:
        """The steps that open the dialog and create a workspace called `name`."""
        return [
            Goto("/workspaces"),
            Click("button:has-text('New workspace')"),
            ExpectVisible("form[aria-label='Create a workspace']"),
            Fill('form[aria-label="Create a workspace"] >> internal:label="Name"i', name),
            Record({"kind": "workspace-name", "name": name}),
            Click("form[aria-label='Create a workspace'] button:has-text('Create workspace')"),
            ExpectUrl(r"/workspaces/ws-"),
        ]

    return [
        Journey(
            name="create a workspace through the form",
            signed_in=True,
            mutates=True,
            steps=[
                *create(created),
                ExpectText("h1", created),
                Goto("/workspaces"),
                ExpectText("table[aria-label='Workspaces']", created),
            ],
        ),
        Journey(
            name="open a workspace detail page",
            signed_in=True,
            mutates=True,
            steps=[
                *create(opened),
                Goto("/workspaces"),
                Click(f"table[aria-label='Workspaces'] a:has-text('{opened}')"),
                ExpectUrl(r"/workspaces/ws-"),
                ExpectText("h1", opened),
                ExpectVisible("section[aria-labelledby='setup-checklist-title']"),
                ExpectVisible("form[aria-label='Run role']"),
                ExpectVisible("[role=tablist][aria-label='Workspace sections']"),
            ],
        ),
    ]


def _cleanup_client(env: Any) -> Any:
    """An authenticated client for the sweep, built outside the fixture graph.

    The cleanup hook runs at session start, before any fixture has signed in, and at
    session end, after they have torn down, so it cannot borrow the `api` fixture.
    """
    from webbpulse.e2e import GATE_HEADER
    from webbpulse.e2e.client import DEFAULT_PER_MINUTE, E2EClient
    from webbpulse.e2e.identity import login

    gate: dict[str, str] = {}
    if env.gate_ssm_parameter:
        import boto3

        parameter = (
            boto3.session.Session(region_name=env.aws_region)
            .client("ssm")
            .get_parameter(Name=env.gate_ssm_parameter, WithDecryption=True)
        )
        gate = {GATE_HEADER: str(parameter["Parameter"]["Value"])}

    client = E2EClient(base_url=env.api_base_url, gate_headers=gate, per_minute=DEFAULT_PER_MINUTE)
    try:
        return login(client, env.user_email, env.user_password).client
    except Exception:
        client.close()
        return None


def _delete_workspace(client: Any, workspace_id: str) -> str:
    """End this workspace's runs, then delete it, describing failures rather than raising.

    The runs go first because deleting the workspace leaves them untouched, and a run
    still planning or applying holds an execution open. A run that will not end is
    reported but does not stop the delete, since leaving the workspace behind as well
    would only add to what is orphaned.
    """
    problems = [f"{workspace_id}: run {item}" for item in end_runs_for_workspace(client, workspace_id)]
    try:
        response = client.delete(f"/api/v1/workspaces/{workspace_id}")
    except Exception as error:
        problems.append(f"{workspace_id} ({type(error).__name__})")
        return "; ".join(problems)
    if response.status_code not in (200, 204, 404):
        problems.append(f"{workspace_id} ({response.status_code})")
    return "; ".join(problems)


def _workspace_ids(client: Any) -> list[dict[str, Any]]:
    """Every workspace this user can see, or an empty list when the read fails."""
    try:
        response = client.get("/api/v1/workspaces")
    except Exception:
        return []
    if response.status_code != 200:
        return []
    body = response.json()
    items = body.get("items", body) if isinstance(body, dict) else body
    return [dict(item) for item in items] if isinstance(items, list) else []


def _is_stale(item: dict[str, Any]) -> bool:
    """Whether a workspace is an e2e leftover old enough to sweep."""
    name = str(item.get("name", ""))
    if not name.startswith("e2e-"):
        return False
    created = item.get("created_at")
    if not created:
        return True
    try:
        from datetime import datetime

        stamp = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return True
    return (time.time() - stamp.timestamp()) > STALE_SECONDS


def pytest_e2e_cleanup(env: Any, phase: str, created: Sequence[Any]) -> Any:
    """End this run's runs and delete its workspaces at the end, and stale ones at the start.

    Deleting a workspace takes its variables and config versions with it, but not its
    runs, so every non-terminal run is cancelled or discarded and waited out first.
    Nothing a case started is still executing once this returns.
    """
    if env.read_only:
        return ""

    client = _cleanup_client(env)
    if client is None:
        return "the cleanup client could not sign in, so nothing was swept"

    try:
        failures: list[str] = []
        listed = _workspace_ids(client)

        if phase == "start":
            targets = [str(item["workspace_id"]) for item in listed if _is_stale(item)]
        else:
            from webbpulse.e2e.journeys import expand

            recorded = {
                expand(str(item.get("name")), env.run_id)
                for item in created
                if isinstance(item, dict) and item.get("kind") == "workspace-name"
            }
            direct = [
                str(item.get("id"))
                for item in created
                if isinstance(item, dict) and item.get("kind") == "workspace" and item.get("id")
            ]
            targets = direct + [str(item["workspace_id"]) for item in listed if str(item.get("name", "")) in recorded]

        for workspace_id in dict.fromkeys(targets):
            failure = _delete_workspace(client, workspace_id)
            if failure:
                failures.append(failure)

        if failures:
            return f"{len(failures)} workspace(s) could not be deleted: {', '.join(failures)}"
        return ""
    finally:
        client.close()


@pytest.fixture(scope="session")
def ephemeral_user_attributes() -> dict[str, Any]:
    """The attributes this run's own login user is created with.

    This product grants write scopes only to a user whose row carries `is_admin`, so a
    user created without it would hold read scopes alone and every write journey would
    be refused. The plugin owns the fixture's lifecycle and passes this through.
    """
    return {"is_admin": True, "email_verified": True}


def resolve_run_role_arn(env: Any) -> str:
    """The ARN of the run role a created workspace carries, or an empty string.

    Read from `E2E_RUN_ROLE_ARN` alone. The reusable workflow forwards every `E2E_*`
    Environment variable, so the ARN the environment was applied with arrives directly
    and nothing has to be rebuilt from an account id and a naming convention that
    Terraform is free to change.
    """
    del env
    return os.environ.get(RUN_ROLE_ARN_VARIABLE, "").strip()


def resolve_journey_run_role_arn(env: Any) -> str:
    """The ARN that decides whether the browser journeys are declared.

    The journeys never submit an ARN: the create form no longer carries one, and the role
    is saved afterwards through the setup checklist. So any syntactically real ARN is
    enough to declare them, and the local stack sets `E2E_JOURNEY_RUN_ROLE_ARN` to a
    placeholder to exercise them on every pull request.

    It is a separate variable from `E2E_RUN_ROLE_ARN` on purpose. That one also gates the
    API run lifecycle cases, which upload a configuration to S3 and really do assume the
    role, and the local stack has neither, so pointing it at a placeholder would turn
    their skips into failures.
    """
    return os.environ.get(JOURNEY_RUN_ROLE_ARN_VARIABLE, "").strip() or resolve_run_role_arn(env)


@pytest.fixture(scope="session")
def run_role_arn(e2e_env: Any) -> str:
    """The run role ARN a created workspace carries, skipping when there is none."""
    value = resolve_run_role_arn(e2e_env)
    if not value:
        pytest.skip("no e2e run role is available, so no workspace can be created")
    return value


@pytest.fixture
def workspace(api: Any, e2e_env: Any, run_role_arn: str, created_resources: list[Any]) -> Iterator[dict[str, Any]]:
    """A workspace this run owns, registered for cleanup before it is used.

    Teardown ends every run the case left behind, whether it passed or failed, so no
    execution outlives the case even though the session sweep would also catch it.
    """
    body = {
        "name": f"{e2e_env.resource_prefix}{secrets.token_hex(3)}",
        "engine": "terraform",
        "engine_version": "1.11.0",
        "run_role_arn": run_role_arn,
    }
    response = api.post("/api/v1/workspaces", json=body)
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the workspace answered {response.status_code}: {response.text[:400]}")
    created = dict(response.json())
    created_resources.append({"kind": "workspace", "id": created["workspace_id"]})
    try:
        yield created
    finally:
        unfinished = end_runs_for_workspace(api, str(created["workspace_id"]))
        if unfinished:
            print(f"e2e cleanup could not end run(s): {', '.join(unfinished)}")
