"""Installing the engine version a workspace pins when the image bakes a different one.

The image carries one Terraform and one OpenTofu release. A workspace that pins
another version gets that release downloaded into the run's temporary directory
and run from there, the way HCP Terraform runs the version a workspace names
rather than whatever it has. As HCP does, the release's SHA256SUMS file must carry
a good detached signature from the publisher's release key, which the image bakes
with its fingerprint pinned, before the archive is checked against it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx

from app.engine import EngineError, resolve_binary
from app.logs import LogSink
from app.models import Engine

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
ARCHITECTURES = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}
DOWNLOAD_TIMEOUT = httpx.Timeout(30.0, read=300.0)
KEYRING_DIRECTORY = Path(os.environ.get("ENGINE_KEYRING_DIRECTORY", "/usr/local/share/webbpulse-runner/keys"))
SIGNING_KEYS: dict[Engine, str] = {
    "terraform": "C874011F0AB405110D02105534365D9472D7468F",
    "tofu": "E3E6E43D84CB852EADB0051D0C0AF313E5FD9F80",
}


class InstallError(RuntimeError):
    """The pinned engine version could not be resolved, fetched or verified."""


@dataclass(frozen=True)
class ReleaseUrls:
    """Where one release's archive, SHA256SUMS file and SUMS signature are published."""

    archive: str
    sums: str
    signature: str
    archive_name: str


def release_urls(engine: Engine, version: str, architecture: str) -> ReleaseUrls:
    """The download locations for one engine release on this architecture."""
    if engine == "terraform":
        base = f"https://releases.hashicorp.com/terraform/{version}"
        archive = f"terraform_{version}_linux_{architecture}.zip"
        sums = f"{base}/terraform_{version}_SHA256SUMS"
        return ReleaseUrls(f"{base}/{archive}", sums, f"{sums}.sig", archive)
    base = f"https://github.com/opentofu/opentofu/releases/download/v{version}"
    archive = f"tofu_{version}_linux_{architecture}.zip"
    sums = f"{base}/tofu_{version}_SHA256SUMS"
    return ReleaseUrls(f"{base}/{archive}", sums, f"{sums}.gpgsig", archive)


def keyring_path(engine: Engine) -> Path:
    """The baked keyring holding one engine publisher's release key."""
    return KEYRING_DIRECTORY / f"{engine}.gpg"


def verify_signature(engine: Engine, sums: bytes, signature: bytes) -> None:
    """Require a good signature over `sums` from the publisher key pinned for `engine`.

    `gpgv` checks the detached signature against the baked keyring alone, and the
    primary key fingerprint it reports must equal the pinned one, so neither a
    swapped keyring nor a signature from another key passes.
    """
    keyring = keyring_path(engine)
    if not keyring.is_file():
        raise InstallError(f"no release key is baked for {engine} at {keyring}")
    with tempfile.TemporaryDirectory() as scratch:
        home = Path(scratch)
        sums_file = home / "SHA256SUMS"
        signature_file = home / "SHA256SUMS.sig"
        sums_file.write_bytes(sums)
        signature_file.write_bytes(signature)
        try:
            completed = subprocess.run(  # noqa: S603
                [
                    "gpgv",
                    "--homedir",
                    str(home),
                    "--keyring",
                    str(keyring.resolve()),
                    "--status-fd",
                    "1",
                    str(signature_file),
                    str(sums_file),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise InstallError(f"gpgv could not run: {type(error).__name__}") from error
    primaries = {
        fields[-1].upper()
        for fields in (line.split() for line in completed.stdout.splitlines())
        if len(fields) >= 3 and fields[:2] == ["[GNUPG:]", "VALIDSIG"]
    }
    if completed.returncode != 0 or primaries != {SIGNING_KEYS[engine]}:
        raise InstallError(f"the {engine} release checksums are not signed by the pinned release key")


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
    urls = release_urls(engine, version, _architecture())
    archive_name = urls.archive_name
    sums = _fetch(client, urls.sums)
    verify_signature(engine, sums, _fetch(client, urls.signature))
    expected = _expected_sum(sums.decode(errors="replace"), archive_name)
    archive = _fetch(client, urls.archive)
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
