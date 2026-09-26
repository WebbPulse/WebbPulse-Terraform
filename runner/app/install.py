"""Installing the engine version a workspace pins when the image bakes a different one.

The image carries one Terraform and one OpenTofu release. A workspace that pins
another version gets that release downloaded into the run's temporary directory,
checked against the release's own SHA256SUMS file, and run from there, the way
HCP Terraform runs the version a workspace names rather than whatever it has.
"""

from __future__ import annotations

import hashlib
import io
import json
import platform
import re
import subprocess
import zipfile
from pathlib import Path
from typing import cast

import httpx

from app.engine import EngineError, resolve_binary
from app.logs import LogSink
from app.models import Engine

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
ARCHITECTURES = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)


class InstallError(RuntimeError):
    """The pinned engine version could not be resolved, fetched or verified."""


def release_urls(engine: Engine, version: str, architecture: str) -> tuple[str, str, str]:
    """The archive URL, the SHA256SUMS URL and the archive's file name for one release."""
    if engine == "terraform":
        base = f"https://releases.hashicorp.com/terraform/{version}"
        archive = f"terraform_{version}_linux_{architecture}.zip"
        return f"{base}/{archive}", f"{base}/terraform_{version}_SHA256SUMS", archive
    base = f"https://github.com/opentofu/opentofu/releases/download/v{version}"
    archive = f"tofu_{version}_linux_{architecture}.zip"
    return f"{base}/{archive}", f"{base}/tofu_{version}_SHA256SUMS", archive


def binary_version(binary: str) -> str | None:
    """The version a binary reports through `version -json`, or None when it cannot say."""
    try:
        completed = subprocess.run(  # noqa: S603
            [binary, "version", "-json"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        document: object = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    reported: object = cast(dict[str, object], document).get("terraform_version")
    return reported if isinstance(reported, str) and reported else None


def _architecture() -> str:
    """The release architecture name for this machine."""
    machine = platform.machine().lower()
    architecture = ARCHITECTURES.get(machine)
    if architecture is None:
        raise InstallError(f"no engine release for architecture {machine}")
    return architecture


def _fetch(client: httpx.Client, url: str) -> bytes:
    """GET one release file, raising `InstallError` on anything but a 200."""
    try:
        response = client.get(url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as error:
        raise InstallError(f"download of {url} failed: {type(error).__name__}") from error
    if response.status_code != 200:
        raise InstallError(f"download of {url} returned {response.status_code}")
    return response.content


def _expected_sum(sums: str, archive: str) -> str:
    """The SHA256 the release's SUMS file lists for one archive."""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == archive:
            return parts[0].lower()
    raise InstallError(f"{archive} is not listed in the release checksums")


def install(engine: Engine, version: str, directory: Path, client: httpx.Client) -> str:
    """Download, verify and unpack one engine release, returning the binary's path."""
    architecture = _architecture()
    archive_url, sums_url, archive_name = release_urls(engine, version, architecture)
    expected = _expected_sum(_fetch(client, sums_url).decode(errors="replace"), archive_name)
    archive = _fetch(client, archive_url)
    if hashlib.sha256(archive).hexdigest() != expected:
        raise InstallError(f"{archive_name} does not match its published checksum")
    target = directory / engine / version
    target.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            payload = bundle.read(engine)
    except (zipfile.BadZipFile, KeyError) as error:
        raise InstallError(f"{archive_name} carries no {engine} binary") from error
    binary = target / engine
    binary.write_bytes(payload)
    binary.chmod(0o755)
    return str(binary)


def ensure_engine(
    engine: Engine,
    version: str | None,
    directory: Path,
    client: httpx.Client,
    sink: LogSink,
) -> str:
    """The binary to run for a workspace's pinned version, installing it when the image lacks it.

    No pin, or a pin equal to the baked release, runs the binary on PATH. Any other
    pin must be an exact release version, since a constraint has nothing to resolve
    it against here.
    """
    try:
        baked = resolve_binary(engine)
    except EngineError as error:
        raise InstallError(str(error)) from error
    wanted = (version or "").strip().removeprefix("v")
    baked_version = binary_version(baked)
    if not wanted or wanted == baked_version:
        sink.write(f"using {engine} {baked_version or 'unknown version'}")
        return baked
    if not VERSION_PATTERN.match(wanted):
        raise InstallError(f"engine version {wanted!r} is not an exact release version")
    sink.write(f"installing {engine} {wanted}")
    binary = install(engine, wanted, directory, client)
    sink.write(f"using {engine} {binary_version(binary) or wanted}")
    return binary
