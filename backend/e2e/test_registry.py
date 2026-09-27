"""The module registry protocol against the deployed staging stage.

Read only against a durable fixture: `WebbPulse/registry-proof/null` 0.1.0, published
from the `v0.1.0` tag in `WebbPulse/webbpulse-terraform-staging-e2e` by the Actions
upload route that tag webhook publishing has since replaced. The tag points at commit
`a8e56530de047bac44a2b5404dadd461a254f3e2` on the `registry-proof` branch, which holds
the module under `registry-proof/`. The version rows remain with no module row, so no
repository is connected to it and nothing republishes it. Keep the branch, the tag and
the rows: a published version is immutable, so these cases always compare against the
same bytes, and a new fixture needs a new version.

The cases mint `wpk_` keys through `POST /api/v1/api-keys` as the run's signed-in user
and revoke them on teardown, whatever the outcome. The protocol routes are exposed past
the staging gate with no gateway authorizer, so they are called with plain `httpx` and
no gate header, exactly as `terraform init` calls them. Skipped outside staging, since
only staging carries the fixture, and on the read-only production smoke because a key
is minted. No key, token or presigned URL is ever printed.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

REGISTRY_HOST = "staging.terraform.webbpulse.com"
API_HOST = "api.staging.terraform.webbpulse.com"
NAMESPACE, NAME, PROVIDER = "WebbPulse", "registry-proof", "null"
VERSION = "0.1.0"
FIXTURE_REPOSITORY = "WebbPulse/webbpulse-terraform-staging-e2e"
FIXTURE_SHA = "a8e56530de047bac44a2b5404dadd461a254f3e2"
FIXTURE_TAG = f"v{VERSION}"
FIXTURE_DIRECTORY = "registry-proof"
REGISTRY_READ = "registry:read"
OTHER_SCOPE = "workspaces:read"
TOKEN_VARIABLE = "TF_TOKEN_" + REGISTRY_HOST.replace(".", "_").replace("-", "__")
MODULES_URL = f"https://{API_HOST}/v1/modules/{NAMESPACE}/{NAME}/{PROVIDER}"
TIMEOUT_SECONDS = 30
INIT_TIMEOUT_SECONDS = 300
DOWNLOAD_TIMEOUT_SECONDS = 120
VERSIONS_FILE = Path(__file__).resolve().parents[2] / "runner" / "versions.env"
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.xdist_group("registry"),
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="the registry fixture module is published on staging only",
    ),
]


def _mint(api: Any, e2e_env: Any, label: str, scopes: list[str]) -> dict[str, Any]:
    """Mint one key carrying `scopes`, failing without ever echoing a minted plaintext."""
    response = api.post("/api/v1/api-keys", json={"name": f"{e2e_env.resource_prefix}{label}", "scopes": scopes})
    if response.status_code != 201:
        pytest.fail(f"minting the {label} key answered {response.status_code}: {response.text[:400]}")
    body = dict(response.json())
    assert body["key"].startswith("wpk_"), "the minted key is not a wpk_ key"
    assert sorted(body["scopes"]) == sorted(scopes), f"the {label} key carries {body['scopes']}"
    return body


@pytest.fixture(scope="module")
def registry_keys(api: Any, e2e_env: Any) -> Iterator[dict[str, dict[str, Any]]]:
    """A `registry:read` key and a key without it, both revoked on teardown."""
    minted: dict[str, dict[str, Any]] = {}
    try:
        minted["read"] = _mint(api, e2e_env, "registry-read", [REGISTRY_READ])
        minted["other"] = _mint(api, e2e_env, "registry-none", [OTHER_SCOPE])
        yield minted
    finally:
        failures = []
        for label, body in minted.items():
            response = api.delete(f"/api/v1/api-keys/{body['key_id']}")
            if response.status_code != 200 or not response.json().get("revoked_at"):
                failures.append(f"{label} ({response.status_code})")
        if failures:
            pytest.fail(f"e2e teardown could not revoke the registry keys: {', '.join(failures)}")


def _get(url: str, key: str | None = None) -> httpx.Response:
    """A plain GET, as Terraform sends it, with the key as a bearer when given."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return httpx.get(url, headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=False)


def _download_url(key: str) -> str:
    """The `X-Terraform-Get` location the download route answers for the fixture."""
    response = _get(f"{MODULES_URL}/{VERSION}/download", key)
    assert response.status_code == 204, f"download answered {response.status_code}: {response.text[:400]}"
    location = response.headers.get("X-Terraform-Get", "")
    assert location.startswith("https://"), "download carried no X-Terraform-Get URL"
    return location


def _fixture_source() -> bytes:
    """The fixture's `main.tf` as the tagged commit holds it."""
    url = f"https://raw.githubusercontent.com/{FIXTURE_REPOSITORY}/{FIXTURE_TAG}/{FIXTURE_DIRECTORY}/main.tf"
    response = httpx.get(url, timeout=TIMEOUT_SECONDS, follow_redirects=True)
    assert response.status_code == 200, f"reading the fixture source answered {response.status_code}"
    return response.content


def _terraform_files(payload: bytes) -> dict[str, bytes]:
    """Every `.tf` file in a gzipped tarball, by its path relative to the module root."""
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.endswith(".tf"):
                extracted = archive.extractfile(member)
                assert extracted is not None
                files[member.name.removeprefix("./")] = extracted.read()
    return files


def _pinned_versions() -> dict[str, str]:
    """The `KEY=value` pins in `runner/versions.env`."""
    pins: dict[str, str] = {}
    for line in VERSIONS_FILE.read_text().splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and not name.startswith("#"):
            pins[name.strip()] = value.strip()
    return pins


@pytest.fixture(scope="session")
def terraform_binary(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A `terraform` executable: the one on PATH, else the pinned release, SHA256 checked.

    The download is the version `runner/versions.env` pins, for this machine's OS and
    architecture, verified against the sum pinned there before it is unzipped.
    """
    found = shutil.which("terraform")
    if found is not None:
        return found
    architecture = ARCHITECTURES.get(platform.machine().lower())
    if sys.platform != "linux" or architecture is None:
        pytest.skip(f"no terraform on PATH and no pinned sum for {sys.platform} {platform.machine()}")
    pins = _pinned_versions()
    version = pins["TERRAFORM_VERSION"]
    expected = pins[f"TERRAFORM_SHA256_{architecture.upper()}"]
    archive = f"terraform_{version}_linux_{architecture}.zip"
    response = httpx.get(
        f"https://releases.hashicorp.com/terraform/{version}/{archive}",
        timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    assert response.status_code == 200, f"downloading {archive} answered {response.status_code}"
    actual = hashlib.sha256(response.content).hexdigest()
    assert actual == expected, f"{archive} hashed {actual}, versions.env pins {expected}"
    directory = tmp_path_factory.mktemp("terraform-bin")
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        bundle.extract("terraform", directory)
    binary = directory / "terraform"
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return str(binary)


def test_discovery_is_anonymous_and_points_at_the_api_host() -> None:
    """Service discovery on the SPA host answers without credentials or a gate cookie."""
    response = _get(f"https://{REGISTRY_HOST}/.well-known/terraform.json")
    assert response.status_code == 200, f"discovery answered {response.status_code}: {response.text[:400]}"
    assert response.json().get("modules.v1") == f"https://{API_HOST}/v1/modules/"


def test_versions_lists_the_fixture(registry_keys: dict[str, dict[str, Any]]) -> None:
    """A `registry:read` key sees the published fixture version."""
    response = _get(f"{MODULES_URL}/versions", registry_keys["read"]["key"])
    assert response.status_code == 200, f"versions answered {response.status_code}: {response.text[:400]}"
    versions = [item["version"] for module in response.json()["modules"] for item in module["versions"]]
    assert VERSION in versions, f"versions listed {versions}"


def test_download_serves_the_published_bytes(api: Any, registry_keys: dict[str, dict[str, Any]]) -> None:
    """The tarball behind `X-Terraform-Get` is the one the fixture tag published.

    Its length is the size the ingest recorded, the listing attributes it to the tag's
    commit in the fixture repository, and its only Terraform file is that commit's.
    """
    listing = api.get("/api/v1/registry/modules")
    assert listing.status_code == 200, f"the module listing answered {listing.status_code}: {listing.text[:400]}"
    source = f"{NAMESPACE}/{NAME}/{PROVIDER}".lower()
    modules = [module for module in listing.json()["modules"] if module["source"].lower() == source]
    assert modules, f"the listing has no {source}"
    rows = [row for row in modules[0]["versions"] if row["version"] == VERSION]
    assert rows, f"the listing has no {source} {VERSION}"
    row = rows[0]
    assert row["status"] == "published", f"{VERSION} is {row['status']}"
    assert row["repository"].lower() == FIXTURE_REPOSITORY.lower()
    assert row["sha"] == FIXTURE_SHA

    tarball = httpx.get(_download_url(registry_keys["read"]["key"]), timeout=TIMEOUT_SECONDS)
    assert tarball.status_code == 200, f"fetching the tarball answered {tarball.status_code}"
    assert len(tarball.content) == row["size_bytes"], (
        f"the tarball is {len(tarball.content)} bytes, the ingest recorded {row['size_bytes']}"
    )
    files = _terraform_files(tarball.content)
    assert sorted(files) == ["main.tf"], f"the tarball holds {sorted(files)}"
    assert files["main.tf"] == _fixture_source(), "the tarball's main.tf differs from the tagged fixture"


@pytest.mark.parametrize("route", ["versions", f"{VERSION}/download"])
def test_protocol_refuses_without_registry_read(registry_keys: dict[str, dict[str, Any]], route: str) -> None:
    """No key, and a key lacking `registry:read`, are both refused."""
    url = f"{MODULES_URL}/{route}"
    missing = _get(url)
    assert missing.status_code in (401, 403), f"{route} with no key answered {missing.status_code}"
    assert "X-Terraform-Get" not in missing.headers
    unscoped = _get(url, registry_keys["other"]["key"])
    assert unscoped.status_code in (401, 403), f"{route} with an unscoped key answered {unscoped.status_code}"
    assert "X-Terraform-Get" not in unscoped.headers


def test_terraform_init_resolves_the_module(
    registry_keys: dict[str, dict[str, Any]], terraform_binary: str, tmp_path: Path
) -> None:
    """A real `terraform init` installs the fixture from the SPA host's module source.

    The token is set only as `TF_TOKEN_<SPA host>`, and the protocol routes on the API
    host refuse a request without a `registry:read` key, so an install proves Terraform
    carried that host's credential to the API host discovery named. The CLI config is an
    empty file so no credential from the runner's home can stand in for it. The module
    must land under `.terraform/modules` with the tagged fixture's exact bytes.
    """
    key = registry_keys["read"]["key"]
    workdir = tmp_path / "root"
    workdir.mkdir()
    (workdir / "main.tf").write_text(
        f'module "proof" {{\n  source  = "{REGISTRY_HOST}/{NAMESPACE}/{NAME}/{PROVIDER}"\n  version = "{VERSION}"\n}}\n'
    )
    config = tmp_path / "terraformrc"
    config.write_text("")
    env = {name: value for name, value in os.environ.items() if not name.startswith("TF_")}
    env.update({"TF_CLI_CONFIG_FILE": str(config), "TF_IN_AUTOMATION": "1", TOKEN_VARIABLE: key})

    result = subprocess.run(
        [terraform_binary, "init", "-input=false", "-no-color"],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=INIT_TIMEOUT_SECONDS,
        check=False,
    )
    output = (result.stdout + result.stderr).replace(key, "[redacted]")
    assert result.returncode == 0, f"terraform init exited {result.returncode}: {output[-2000:]}"

    modules_root = (workdir / ".terraform" / "modules").resolve()
    manifest = json.loads((modules_root / "modules.json").read_text())
    installed = [module for module in manifest["Modules"] if module.get("Key") == "proof"]
    assert installed, f"terraform init installed no proof module: {output[-2000:]}"
    assert installed[0]["Source"] == f"{REGISTRY_HOST}/{NAMESPACE}/{NAME}/{PROVIDER}"
    assert installed[0]["Version"] == VERSION
    module_dir = (workdir / installed[0]["Dir"]).resolve()
    assert module_dir.is_relative_to(modules_root), f"the module landed at {installed[0]['Dir']}"
    tf_files = sorted(path.relative_to(module_dir).as_posix() for path in module_dir.rglob("*.tf"))
    assert tf_files == ["main.tf"], f"the installed module holds {tf_files}"
    assert (module_dir / "main.tf").read_bytes() == _fixture_source(), "the installed main.tf differs from the fixture"
