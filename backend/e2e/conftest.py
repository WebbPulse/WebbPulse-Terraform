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

pytest_plugins = ["webbpulse.e2e"]

RUN_ROLE_ARN_VARIABLE = "E2E_RUN_ROLE_ARN"

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
    """
    from webbpulse.e2e import LoginForm

    del env
    return LoginForm(
        path="/sign-in",
        email="input[type=email]",
        password="input[type=password]",
        submit="button[type=submit]:has-text('Sign in')",
        signed_in_marker="header a[href='/workspaces']",
        sign_out="header button:has-text('Sign out')",
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

    The create journey fills the run role ARN field, which is the field whose absence
    from the form let a workspace be created that no run could ever assume.
    """
    from webbpulse.e2e import Click, ExpectText, ExpectUrl, ExpectVisible, Fill, Goto, Journey, Record

    run_role_arn = resolve_run_role_arn(env)
    if not run_role_arn:
        return []

    name = "e2e-{run_id}-ui"
    return [
        Journey(
            name="create a workspace through the form",
            signed_in=True,
            mutates=True,
            steps=[
                Goto("/workspaces"),
                ExpectVisible("form[aria-label='Create a workspace']"),
                Fill("form[aria-label='Create a workspace'] label:has-text('Name') input", name),
                Fill("form[aria-label='Create a workspace'] label:has-text('Run role ARN') input", run_role_arn),
                Click("form[aria-label='Create a workspace'] button[type=submit]"),
                ExpectText("table", name),
                Record({"kind": "workspace-name", "name": name}),
            ],
        ),
        Journey(
            name="open a workspace detail page",
            signed_in=True,
            steps=[
                Goto("/workspaces"),
                Click(f"table a:has-text('{name}')"),
                ExpectUrl(r"/workspaces/ws-"),
                ExpectVisible("[role=tablist]"),
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
    """Delete one workspace, describing the failure rather than raising."""
    try:
        response = client.delete(f"/api/v1/workspaces/{workspace_id}")
    except Exception as error:
        return f"{workspace_id} ({type(error).__name__})"
    if response.status_code not in (200, 204, 404):
        return f"{workspace_id} ({response.status_code})"
    return ""


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
    """Delete this run's workspaces at the end and stale ones at the start.

    A workspace is the only resource that outlives a case: variables, config versions
    and runs are all deleted with it.
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
            recorded = {
                str(item.get("name"))
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
def ephemeral_user(
    request: pytest.FixtureRequest,
    e2e_env: Any,
    anon: Any,
    admin_mint_token: str,
) -> Iterator[Any]:
    """This run's own login user, created with the admin flag this product needs.

    Overrides the plugin's fixture for one reason: the plugin creates an ephemeral user
    with no attributes, and this product grants write scopes only to a user whose row
    carries `is_admin`. A user created without it holds read scopes alone and every
    write journey would be refused. Everything else follows the plugin's own helpers.
    """
    from webbpulse.e2e.ephemeral import create_ephemeral_user, describe_delete_failure
    from webbpulse.e2e.xdist import worker_id

    if e2e_env.read_only or not admin_mint_token:
        yield None
        return

    run_id = f"{e2e_env.run_id}-{worker_id(request.config)}"
    user = create_ephemeral_user(
        anon,
        run_id=run_id,
        admin_token=admin_mint_token,
        attributes={"is_admin": True, "email_verified": True},
    )
    try:
        yield user
    finally:
        failure = describe_delete_failure(anon, user, admin_token=admin_mint_token) if user is not None else ""
        if user is not None and failure:
            request.config.issue_config_time_warning(
                UserWarning(
                    f"The ephemeral e2e user {user.user_id} could not be deleted: {failure} It "
                    "carries the e2e- prefix, so the next run's start sweep will collect it."
                ),
                stacklevel=2,
            )


def resolve_run_role_arn(env: Any) -> str:
    """The ARN of the run role a created workspace carries, or an empty string.

    Read from `E2E_RUN_ROLE_ARN` alone. The reusable workflow forwards every `E2E_*`
    Environment variable, so the ARN the environment was applied with arrives directly
    and nothing has to be rebuilt from an account id and a naming convention that
    Terraform is free to change.
    """
    del env
    return os.environ.get(RUN_ROLE_ARN_VARIABLE, "").strip()


@pytest.fixture(scope="session")
def run_role_arn(e2e_env: Any) -> str:
    """The run role ARN a created workspace carries, skipping when there is none."""
    value = resolve_run_role_arn(e2e_env)
    if not value:
        pytest.skip("no e2e run role is available, so no workspace can be created")
    return value


@pytest.fixture
def workspace(api: Any, e2e_env: Any, run_role_arn: str, created_resources: list[Any]) -> dict[str, Any]:
    """A workspace this run owns, registered for cleanup before it is used."""
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
    return created
