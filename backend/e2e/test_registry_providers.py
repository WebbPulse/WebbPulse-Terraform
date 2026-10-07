"""The provider registry protocol against the deployed staging stage.

The fixture is `WebbPulse/webbpulse` 0.1.0-rc.1, published from the `v0.1.0-rc.1` GitHub
release of `WebbPulse/terraform-provider-webbpulse`, whose GoReleaser assets are signed
with the staging provider signing key. The provider stays connected between runs: a
published version is immutable, so connecting answers 409 after the first run and every
run compares against the same release. Connecting needs the staging GitHub App installed
on that repository; while it is not, the cases skip naming the owner step.

The protocol routes are exposed past the staging gate with no gateway authorizer, so they
are called with plain `httpx` and no gate header, and `terraform init` runs with an empty
CLI config and only `TF_TOKEN_<SPA host>`, so the install proves discovery, the key and
the signature check together. Skipped outside staging and on the read-only production
smoke, since a key is minted. No key, token or presigned URL is ever printed.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from terraform_cli import find_or_fetch_terraform

REGISTRY_HOST = "staging.terraform.webbpulse.com"
API_HOST = "api.staging.terraform.webbpulse.com"
REPOSITORY = "WebbPulse/terraform-provider-webbpulse"
NAMESPACE, TYPE = "WebbPulse", "webbpulse"
VERSION = "0.1.0-rc.1"
PROVIDERS = "/api/v1/registry/providers"
PROTOCOL_URL = f"https://{API_HOST}/v1/providers/{NAMESPACE}/{TYPE}"
TOKEN_VARIABLE = "TF_TOKEN_" + REGISTRY_HOST.replace(".", "_").replace("-", "__")
OWNER_STEP = (
    "the staging GitHub App (webbpulse-terraform-staging, installation 165274032) is not installed on "
    f"{REPOSITORY}: add that repository to the installation's selected repositories"
)
SETTLE_TIMEOUT_SECONDS = 300
POLL_SECONDS = 5
TIMEOUT_SECONDS = 30
INIT_TIMEOUT_SECONDS = 300

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.xdist_group("registry"),
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="the provider fixture release is signed with the staging key",
    ),
]


def _provider(api: Any) -> dict[str, Any] | None:
    """The fixture provider, or None when it is not connected."""
    response = api.get(f"{PROVIDERS}/{NAMESPACE}/{TYPE}")
    if response.status_code == 404:
        return None
    assert response.status_code == 200, f"reading the provider answered {response.status_code}: {response.text[:400]}"
    return dict(response.json())


@pytest.fixture(scope="module")
def published(api: Any, step_up_again: Callable[[], Any]) -> dict[str, Any]:
    """The fixture version's row once it is published, connecting the provider if it is not."""
    if _provider(api) is None:
        response = step_up_again().post(PROVIDERS, json={"vcs_repo": REPOSITORY})
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.status_code == 422 and "VCS_REPO_NOT_INSTALLED" in str(body):
            pytest.skip(OWNER_STEP)
        assert response.status_code in (201, 409), f"connecting answered {response.status_code}: {response.text[:400]}"
    started = time.monotonic()
    resynced = False
    while True:
        provider = _provider(api)
        assert provider is not None, "the provider disappeared while its releases were importing"
        row = next((row for row in provider["versions"] if row["version"] == VERSION), None)
        if row is not None and row["status"] != "pending":
            assert row["status"] == "published", f"{VERSION} settled {row['status']}: {row.get('error')}"
            return dict(row)
        elapsed = time.monotonic() - started
        if elapsed > SETTLE_TIMEOUT_SECONDS:
            pytest.fail(f"{VERSION} was not published within {SETTLE_TIMEOUT_SECONDS}s")
        if row is None and not resynced and elapsed > SETTLE_TIMEOUT_SECONDS / 3:
            resync = step_up_again().post(f"{PROVIDERS}/{NAMESPACE}/{TYPE}/resync")
            assert resync.status_code == 202, f"resyncing answered {resync.status_code}"
            resynced = True
        time.sleep(POLL_SECONDS)


@pytest.fixture(scope="module")
def read_key(e2e_env: Any, step_up_again: Callable[[], Any]) -> Iterator[str]:
    """A `registry:read` key, revoked on teardown whatever the outcome."""
    response = step_up_again().post(
        "/api/v1/api-keys", json={"name": f"{e2e_env.resource_prefix}provider-read", "scopes": ["registry:read"]}
    )
    assert response.status_code == 201, f"minting answered {response.status_code}: {response.text[:400]}"
    body = response.json()
    try:
        yield str(body["key"])
    finally:
        revoked = step_up_again().delete(f"/api/v1/api-keys/{body['key_id']}")
        if revoked.status_code != 200:
            pytest.fail(f"e2e teardown could not revoke the provider key ({revoked.status_code})")


@pytest.fixture(scope="module")
def terraform_binary(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A `terraform` executable, the one on PATH or the pinned release."""
    return find_or_fetch_terraform(tmp_path_factory)


def _get(url: str, key: str | None = None) -> httpx.Response:
    """A plain GET, as Terraform sends it, with the key as a bearer when given."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.get(url, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False)


def test_the_release_published_every_platform(published: dict[str, Any]) -> None:
    """The row records the release's platforms and the staging key id it was verified with."""
    assert published["tag"] == f"v{VERSION}"
    assert published["key_id"], "no signing key id was recorded"
    platforms = {(item["os"], item["arch"]) for item in published["platforms"]}
    assert ("linux", "amd64") in platforms, f"the release published {sorted(platforms)}"


def test_the_protocol_serves_versions_and_a_signed_download(published: dict[str, Any], read_key: str) -> None:
    """`versions` lists the fixture, and `download` names the sums, their signature and the key."""
    assert _get(f"{PROTOCOL_URL}/versions").status_code in (401, 403)
    versions = _get(f"{PROTOCOL_URL}/versions", read_key)
    assert versions.status_code == 200
    listed = {row["version"]: row for row in versions.json()["versions"]}
    assert VERSION in listed
    assert {"os": "linux", "arch": "amd64"} in listed[VERSION]["platforms"]

    download = _get(f"{PROTOCOL_URL}/{VERSION}/download/linux/amd64", read_key)
    assert download.status_code == 200
    body = download.json()
    assert body["filename"] == f"terraform-provider-{TYPE}_{VERSION}_linux_amd64.zip"
    assert body["shasums_url"] and body["shasums_signature_url"] and body["download_url"]
    keys = body["signing_keys"]["gpg_public_keys"]
    assert [key["key_id"] for key in keys] == [published["key_id"]]
    assert keys[0]["ascii_armor"].startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    sums = httpx.get(body["shasums_url"], timeout=TIMEOUT_SECONDS)
    assert sums.status_code == 200
    assert f"{body['shasum']}  {body['filename']}" in sums.text


def test_terraform_init_installs_the_signed_provider(
    published: dict[str, Any], read_key: str, terraform_binary: str, tmp_path: Path
) -> None:
    """A real `terraform init` installs the provider and verifies its signature against the served key."""
    workdir = tmp_path / "root"
    workdir.mkdir()
    (workdir / "main.tf").write_text(
        "terraform {\n  required_providers {\n    webbpulse = {\n"
        f'      source  = "{REGISTRY_HOST}/{NAMESPACE}/{TYPE}"\n      version = "{VERSION}"\n'
        "    }\n  }\n}\n"
    )
    config = tmp_path / "terraformrc"
    config.write_text("")
    env = {name: value for name, value in os.environ.items() if not name.startswith("TF_")}
    env.update({"TF_CLI_CONFIG_FILE": str(config), "TF_IN_AUTOMATION": "1", TOKEN_VARIABLE: read_key})

    result = subprocess.run(
        [terraform_binary, "init", "-input=false", "-no-color"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=INIT_TIMEOUT_SECONDS,
        check=False,
    )
    output = (result.stdout + result.stderr).replace(read_key, "[redacted]")
    assert result.returncode == 0, f"terraform init exited {result.returncode}: {output[-2000:]}"
    assert f"Installed {REGISTRY_HOST}/{NAMESPACE.lower()}/{TYPE} v{VERSION}" in output, output[-2000:]
    assert f"key id {published['key_id'].lower()}" in output.lower(), (
        f"the install was not signature checked: {output[-2000:]}"
    )
    lock = (workdir / ".terraform.lock.hcl").read_text()
    assert f'provider "{REGISTRY_HOST}/{NAMESPACE.lower()}/{TYPE}"' in lock


def _session_token(api: Any) -> str:
    """The signed-in user's current access token, refreshed by its source when one is set."""
    source = getattr(api, "token_source", None)
    token = source.bearer_token() if source is not None else getattr(api, "token", None)
    assert token and not str(token).startswith("wpk_"), "the run's client holds no session token"
    return str(token)


def test_a_session_token_downloads_the_provider(published: dict[str, Any], api: Any) -> None:
    """A person's own access token, not only a key, reads the protocol and the checksum list."""
    token = _session_token(api)
    versions = _get(f"{PROTOCOL_URL}/versions", token)
    assert versions.status_code == 200, f"a session token listing versions answered {versions.status_code}"
    download = _get(f"{PROTOCOL_URL}/{VERSION}/download/linux/amd64", token)
    assert download.status_code == 200, f"a session token downloading answered {download.status_code}"
    body = download.json()
    sums = httpx.get(body["shasums_url"], timeout=TIMEOUT_SECONDS)
    assert sums.status_code == 200
    assert f"{body['shasum']}  {body['filename']}" in sums.text


def test_terraform_providers_lock_records_both_hash_schemes(
    published: dict[str, Any], read_key: str, terraform_binary: str, tmp_path: Path
) -> None:
    """`terraform providers lock` with the `terraform login` credential records `h1:` and `zh:` hashes."""
    platforms = sorted(f"{item['os']}_{item['arch']}" for item in published["platforms"])
    wanted = [platform for platform in ("linux_amd64", "darwin_arm64") if platform in platforms]
    workdir = tmp_path / "lock"
    workdir.mkdir()
    (workdir / "main.tf").write_text(
        "terraform {\n  required_providers {\n    webbpulse = {\n"
        f'      source  = "{REGISTRY_HOST}/{NAMESPACE}/{TYPE}"\n      version = "{VERSION}"\n'
        "    }\n  }\n}\n"
    )
    config = tmp_path / "terraformrc-lock"
    config.write_text("")
    env = {name: value for name, value in os.environ.items() if not name.startswith("TF_")}
    env.update({"TF_CLI_CONFIG_FILE": str(config), "TF_IN_AUTOMATION": "1", TOKEN_VARIABLE: read_key})

    result = subprocess.run(
        [terraform_binary, "providers", "lock", "-no-color", *(f"-platform={platform}" for platform in wanted)],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=INIT_TIMEOUT_SECONDS,
        check=False,
    )
    output = (result.stdout + result.stderr).replace(read_key, "[redacted]")
    assert result.returncode == 0, f"terraform providers lock exited {result.returncode}: {output[-2000:]}"
    lock = (workdir / ".terraform.lock.hcl").read_text()
    assert f'provider "{REGISTRY_HOST}/{NAMESPACE.lower()}/{TYPE}"' in lock
    assert lock.count('"h1:') == len(wanted), f"expected one h1 hash per platform: {lock}"
    assert '"zh:' in lock, f"no zh hash was recorded: {lock}"
