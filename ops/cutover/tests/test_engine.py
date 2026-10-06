"""Engine resolution, command failures and the backend blocks."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from cutover.config import UsageError
from cutover.engine import Completed, Engine, EngineError, EngineResolver, cloud_backend, engine_env, s3_backend


class VersionRunner:
    """Answers `version -json` with a fixed version for any binary."""

    def __init__(self, version: str, stderr: bytes = b"", returncode: int = 0) -> None:
        """Bind the answer."""
        self.version = version
        self.stderr = stderr
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
        """Record and answer."""
        self.calls.append(list(args))
        if self.returncode:
            return Completed(self.returncode, b"", self.stderr)
        return Completed(0, json.dumps({"terraform_version": self.version}).encode(), b"")


def release(version: str, body: bytes = b"#!binary") -> tuple[bytes, str]:
    """A release zip and its SHA256SUMS text."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("terraform", body)
    archive = buffer.getvalue()
    name = f"terraform_{version}_linux_amd64.zip"
    sums = f"{'0' * 64}  terraform_{version}_darwin_arm64.zip\n{hashlib.sha256(archive).hexdigest()}  {name}\n"
    return archive, sums


def test_override_at_the_wrong_version_is_refused(tmp_path: Path) -> None:
    """A local binary that reports another version is never used."""
    binary = tmp_path / "terraform"
    binary.write_text("x")
    resolver = EngineResolver(cache_root=tmp_path / "cache", runner=VersionRunner("1.16.5"))
    with pytest.raises(EngineError, match="reports Terraform 1.16.5, expected 1.16.4"):
        resolver.resolve("1.16.4", binary)


def test_override_at_the_right_version(tmp_path: Path) -> None:
    """A matching binary resolves."""
    binary = tmp_path / "terraform"
    binary.write_text("x")
    engine = EngineResolver(cache_root=tmp_path / "cache", runner=VersionRunner("1.14.8")).resolve("1.14.8", binary)
    assert (engine.binary, engine.version) == (binary, "1.14.8")


def test_missing_override(tmp_path: Path) -> None:
    """A missing binary is an error."""
    resolver = EngineResolver(cache_root=tmp_path, runner=VersionRunner("1.16.4"))
    with pytest.raises(EngineError, match="no engine binary"):
        resolver.resolve("1.16.4", tmp_path / "absent")


def test_floating_versions_are_refused(tmp_path: Path) -> None:
    """Only an exact version is accepted."""
    resolver = EngineResolver(cache_root=tmp_path, runner=VersionRunner("1.16.4"))
    with pytest.raises(UsageError):
        resolver.resolve("~> 1.10")


def test_download_verifies_and_caches(tmp_path: Path) -> None:
    """A verified download is installed executable in the cache, and the next resolve does not fetch."""
    archive, sums = release("1.16.4")
    fetched: list[str] = []

    def fetch(url: str) -> bytes:
        """Serve the release files."""
        fetched.append(url)
        return sums.encode() if url.endswith("SHA256SUMS") else archive

    resolver = EngineResolver(tmp_path, VersionRunner("1.16.4"), fetch, ("linux", "amd64"))
    engine = resolver.resolve("1.16.4")
    assert engine.binary == tmp_path / "1.16.4" / "terraform"
    assert engine.binary.read_bytes() == b"#!binary"
    assert engine.binary.stat().st_mode & 0o111
    count = len(fetched)
    resolver.resolve("1.16.4")
    assert len(fetched) == count


def test_download_with_a_bad_sum_installs_nothing(tmp_path: Path) -> None:
    """A tampered archive is refused and nothing lands in the cache."""
    archive, sums = release("1.16.4")

    def fetch(url: str) -> bytes:
        """Serve a sum file and a different archive."""
        return sums.encode() if url.endswith("SHA256SUMS") else archive + b"tampered"

    resolver = EngineResolver(tmp_path, VersionRunner("1.16.4"), fetch, ("linux", "amd64"))
    with pytest.raises(EngineError, match="does not match the release sum"):
        resolver.resolve("1.16.4")
    assert not (tmp_path / "1.16.4" / "terraform").exists()


def test_download_for_an_unlisted_platform(tmp_path: Path) -> None:
    """A platform missing from the sums is refused."""
    _archive, sums = release("1.16.4")
    resolver = EngineResolver(tmp_path, VersionRunner("1.16.4"), lambda url: sums.encode(), ("linux", "arm64"))
    with pytest.raises(EngineError, match="not listed"):
        resolver.resolve("1.16.4")


def test_failure_carries_only_the_stderr_tail(tmp_path: Path) -> None:
    """A failed command reports its last stderr lines, never stdout."""
    lines = b"\n".join(f"line {n}".encode() for n in range(20))

    def runner(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
        """Fail with stdout that must not be shown."""
        return Completed(1, b"stdout-state-body", lines)

    with pytest.raises(EngineError) as raised:
        Engine(tmp_path / "terraform", "1.16.4", runner).init(tmp_path, {})
    message = str(raised.value)
    assert "line 19" in message and "line 12" in message
    assert "line 11" not in message
    assert "stdout-state-body" not in message


def test_push_without_lock(tmp_path: Path) -> None:
    """`lock=False` adds -lock=false before the file."""
    seen: list[list[str]] = []

    def runner(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
        """Record."""
        seen.append(list(args))
        return Completed(0, b"", b"")

    engine = Engine(tmp_path / "terraform", "1.16.4", runner)
    engine.state_push(tmp_path, tmp_path / "s.tfstate", {}, lock=False)
    engine.state_push(tmp_path, tmp_path / "s.tfstate", {})
    assert seen[0][1:] == ["state", "push", "-lock=false", str(tmp_path / "s.tfstate")]
    assert seen[1][1:] == ["state", "push", str(tmp_path / "s.tfstate")]


def test_engine_env_drops_stray_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tokens and backend overrides in the shell never reach the engine."""
    monkeypatch.setenv("TF_TOKEN_app_terraform_io", "leak")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "leak")
    monkeypatch.setenv("TF_CLI_ARGS_init", "-backend-config=leak")
    env = engine_env({"AWS_PROFILE": "p"})
    assert "leak" not in env.values()
    assert env["AWS_PROFILE"] == "p"
    assert env["TF_INPUT"] == "0"


def test_s3_backend_block() -> None:
    """Encryption, the KMS key and the lockfile are always set."""
    block = s3_backend("bucket", "workspaces/ws/terraform.tfstate", "us-west-2", "arn:kms", "workspaces/ws/env")
    assert 'bucket               = "bucket"' in block
    assert 'kms_key_id           = "arn:kms"' in block
    assert "encrypt              = true" in block
    assert "use_lockfile         = true" in block


def test_backend_values_are_escaped() -> None:
    """Quotes and interpolation in a value cannot change the block."""
    block = cloud_backend("app.terraform.io", 'Org"${evil}', "ws%{x}")
    assert 'organization = "Org\\"$${evil}"' in block
    assert 'name = "ws%%{x}"' in block
