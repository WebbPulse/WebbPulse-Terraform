"""Workload identity end to end: a real plan verifies the Google and Azure tokens it was handed.

The workspace asks for both clouds with HCP Terraform's variable names, and the
configuration's `external` data source runs `verify.py` inside the plan, where it sees
exactly what a provider would. It checks each token's RS256 signature against the
issuer's anonymous JWKS and each claim, and prints only that it verified. So the plan
finishing is the proof, and no token ever reaches the suite, the run's logs or its plan.

No cloud is called: the provider name and client id are placeholders, since an STS
exchange needs a workload identity pool that trusts this issuer.
"""

from __future__ import annotations

import io
import os
import tarfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from test_product_flows import PLAN_TERMINAL, PLAN_TIMEOUT_SECONDS, POLL_SECONDS

CONFIGURATION = Path(__file__).resolve().parent / "workload_identity"

ENVIRONMENT = {
    "TFC_GCP_PROVIDER_AUTH": "true",
    "TFC_GCP_WORKLOAD_PROVIDER_NAME": "projects/0/locations/global/workloadIdentityPools/e2e/providers/e2e",
    "TFC_GCP_RUN_SERVICE_ACCOUNT_EMAIL": "e2e@e2e.iam.gserviceaccount.com",
    "TFC_AZURE_PROVIDER_AUTH": "true",
    "TFC_AZURE_RUN_CLIENT_ID": "00000000-0000-0000-0000-000000000e2e",
}
"""The workspace's HCP style settings, placeholders since nothing is exchanged."""


def _issuer(api_base_url: str) -> str:
    """The issuer beside the API: `api.<stage host>` becomes `oidc.<stage host>`."""
    override = os.environ.get("E2E_OIDC_ISSUER", "").strip()
    if override:
        return override.rstrip("/")
    host = urlsplit(api_base_url).hostname or ""
    if not host.startswith("api."):
        pytest.skip(f"no issuer can be derived from {api_base_url}; set E2E_OIDC_ISSUER")
    return f"https://oidc.{host.removeprefix('api.')}"


def _tarball() -> bytes:
    """The verifier configuration as a gzipped tar, built in memory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name in ("main.tf", "verify.py"):
            archive.add(CONFIGURATION / name, arcname=name)
    return buffer.getvalue()


def _upload(api: Any, workspace_id: str) -> str:
    """Create a config version, PUT the verifier to it and return the config version id."""
    payload = _tarball()
    response = api.post(f"/api/v1/workspaces/{workspace_id}/config-versions", json={"size_bytes": len(payload)})
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the config version answered {response.status_code}: {response.text[:400]}")
    body = response.json()
    put = httpx.put(body["upload_url"], content=payload, headers=body["headers"], timeout=60)
    assert put.status_code in (200, 204), f"the presigned PUT answered {put.status_code}: {put.text[:400]}"
    return str(body["config_version"]["config_version_id"])


def _set(api: Any, workspace_id: str, key: str, value: str, category: str) -> None:
    """Write one plain workspace variable."""
    response = api.put(
        f"/api/v1/workspaces/{workspace_id}/variables/{key}",
        json={"value": value, "category": category, "sensitive": False},
    )
    assert response.status_code in (200, 201), f"setting {key} answered {response.status_code}: {response.text[:400]}"


def _plan(api: Any, workspace_id: str, config_version_id: str) -> dict[str, Any]:
    """Start a plan only run and return it once it settles, failing on the timeout."""
    response = api.post(
        "/api/v1/runs",
        json={
            "workspace_id": workspace_id,
            "config_version_id": config_version_id,
            "plan_only": True,
            "message": "e2e",
        },
    )
    if response.status_code not in (200, 201):
        pytest.fail(f"creating the run answered {response.status_code}: {response.text[:400]}")
    run_id = str(response.json()["run_id"])
    deadline = time.monotonic() + PLAN_TIMEOUT_SECONDS
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        polled = api.get(f"/api/v1/runs/{run_id}")
        assert polled.status_code == 200, polled.text[:400]
        run = dict(polled.json())
        if run.get("status") in PLAN_TERMINAL:
            return run
        time.sleep(POLL_SECONDS)
    pytest.fail(f"run {run_id} was still {run.get('status')!r} after {PLAN_TIMEOUT_SECONDS}s")


@pytest.mark.e2e_writes
def test_a_plan_verifies_its_workload_identity_tokens(api: Any, e2e_env: Any, workspace: dict[str, Any]) -> None:
    """The plan finishes only if both tokens verify against the published JWKS with the right claims."""
    issuer = _issuer(e2e_env.api_base_url)
    discovery = httpx.get(f"{issuer}/.well-known/openid-configuration", timeout=15)
    assert discovery.status_code == 200, f"the discovery document answered {discovery.status_code}"
    assert discovery.json()["issuer"] == issuer

    workspace_id = str(workspace["workspace_id"])
    for key, value in ENVIRONMENT.items():
        _set(api, workspace_id, key, value, "env")
    _set(api, workspace_id, "issuer", issuer, "terraform")
    _set(api, workspace_id, "workspace_id", workspace_id, "terraform")

    finished = _plan(api, workspace_id, _upload(api, workspace_id))
    assert finished.get("status") in ("planned", "planned_and_finished"), (
        f"the workload identity plan did not verify: run={finished.get('run_id')} "
        f"status={finished.get('status')!r} error={finished.get('error') or 'none'!r}"
    )
