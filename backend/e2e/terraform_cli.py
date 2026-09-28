"""A `terraform` executable for the e2e cases that drive the real CLI."""

from __future__ import annotations

import hashlib
import io
import platform
import shutil
import stat
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

VERSIONS_FILE = Path(__file__).resolve().parents[2] / "runner" / "versions.env"
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
DOWNLOAD_TIMEOUT_SECONDS = 120


def _pinned_versions() -> dict[str, str]:
    """The `KEY=value` pins in `runner/versions.env`."""
    pins: dict[str, str] = {}
    for line in VERSIONS_FILE.read_text().splitlines():
        name, separator, value = line.strip().partition("=")
        if separator and not name.startswith("#"):
            pins[name.strip()] = value.strip()
    return pins


def find_or_fetch_terraform(tmp_path_factory: pytest.TempPathFactory) -> str:
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
