"""Exact engine versions, resolved and verified, and the few commands the cutover runs with them."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import subprocess
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

import httpx

from cutover.config import check_engine_version

RELEASES = "https://releases.hashicorp.com/terraform"
DEFAULT_CACHE = Path.home() / ".cache" / "webbpulse-cutover" / "terraform"
PASSTHROUGH_ENV = (
    "PATH",
    "HOME",
    "USER",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "SSL_CERT_FILE",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)
STDERR_TAIL = 8


class EngineError(Exception):
    """An engine that could not be resolved, or a command it failed."""


@dataclass(frozen=True)
class Completed:
    """What a finished command returned; stdout may hold a state body and is never printed."""

    returncode: int
    stdout: bytes
    stderr: bytes


class CommandRunner(Protocol):
    """Runs one command; injectable so tests never start a real engine."""

    def __call__(self, args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
        """Run `args` in `cwd` with exactly `env` and capture both streams."""
        ...


def run_command(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
    """The real runner: no shell, no inherited environment, both streams captured."""
    finished = subprocess.run(list(args), cwd=cwd, env=dict(env), capture_output=True, check=False)
    return Completed(finished.returncode, finished.stdout, finished.stderr)


def engine_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A minimal engine environment, so no stray token or backend override in the shell leaks in."""
    env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
    env["TF_IN_AUTOMATION"] = "1"
    env["TF_INPUT"] = "0"
    env["CHECKPOINT_DISABLE"] = "1"
    if extra:
        env.update(extra)
    return env


def _stderr_tail(result: Completed) -> str:
    """The last lines of a failed command's stderr, for the redacting console to print."""
    lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
    return "\n".join(lines[-STDERR_TAIL:])


@dataclass(frozen=True)
class Engine:
    """One verified Terraform binary at one exact version."""

    binary: Path
    version: str
    runner: CommandRunner = run_command

    def _run(self, args: Sequence[str], cwd: Path, env: Mapping[str, str], what: str) -> Completed:
        """Run a subcommand, raising with the stderr tail on failure."""
        result = self.runner([str(self.binary), *args], cwd, env)
        if result.returncode != 0:
            raise EngineError(f"terraform {what} failed ({result.returncode}): {_stderr_tail(result)}")
        return result

    def init(self, workdir: Path, env: Mapping[str, str]) -> None:
        """Initialise the backend in `workdir`, never migrating or copying any state."""
        self._run(["init", "-input=false", "-no-color", "-reconfigure"], workdir, env, "init")

    def state_pull(self, workdir: Path, env: Mapping[str, str]) -> bytes:
        """The backend's current state body, captured and never echoed."""
        return self._run(["state", "pull"], workdir, env, "state pull").stdout

    def state_push(self, workdir: Path, state_file: Path, env: Mapping[str, str], *, lock: bool = True) -> None:
        """Push a state file to the backend; `lock=False` where the backend's lock is already held by us."""
        args = ["state", "push"]
        if not lock:
            args.append("-lock=false")
        args.append(str(state_file))
        self._run(args, workdir, env, "state push")


def _platform() -> tuple[str, str]:
    """The release platform pair for this machine."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if system not in ("linux", "darwin") or arch is None:
        raise EngineError(f"no Terraform release for {system}/{machine}")
    return system, arch


def _fetch(url: str) -> bytes:
    """GET a release file over HTTPS."""
    response = httpx.get(url, follow_redirects=True, timeout=120)
    response.raise_for_status()
    return response.content


class EngineResolver:
    """Finds or downloads the exact engine a workspace's state was written with."""

    def __init__(
        self,
        cache_root: Path = DEFAULT_CACHE,
        runner: CommandRunner = run_command,
        fetch: Callable[[str], bytes] = _fetch,
        platform_pair: tuple[str, str] | None = None,
    ) -> None:
        """Bind the cache, the command runner and the downloader."""
        self.cache_root = cache_root
        self.runner = runner
        self.fetch = fetch
        self.platform_pair = platform_pair

    def resolve(self, version: str, override: Path | None = None) -> Engine:
        """An `Engine` at exactly `version`, from `override`, the cache or a verified download."""
        check_engine_version(version)
        if override is not None:
            return self._verified(override, version)
        binary = self.cache_root / version / "terraform"
        if not binary.exists():
            self._download(version, binary)
        return self._verified(binary, version)

    def _verified(self, binary: Path, version: str) -> Engine:
        """Refuse a binary whose own `version -json` does not report exactly `version`."""
        if not binary.is_file():
            raise EngineError(f"no engine binary at {binary}")
        result = self.runner([str(binary), "version", "-json"], binary.parent, engine_env())
        if result.returncode != 0:
            raise EngineError(f"{binary} version failed: {_stderr_tail(result)}")
        try:
            reported = cast(dict[str, Any], json.loads(result.stdout)).get("terraform_version")
        except (ValueError, AttributeError):
            reported = None
        if reported != version:
            raise EngineError(f"{binary} reports Terraform {reported}, expected {version}")
        return Engine(binary, version, self.runner)

    def _download(self, version: str, binary: Path) -> None:
        """Download the release zip, check it against SHA256SUMS and unpack the binary into the cache."""
        system, arch = self.platform_pair or _platform()
        name = f"terraform_{version}_{system}_{arch}.zip"
        sums = self.fetch(f"{RELEASES}/{version}/terraform_{version}_SHA256SUMS").decode()
        expected = next((line.split()[0] for line in sums.splitlines() if line.endswith(f"  {name}")), None)
        if expected is None:
            raise EngineError(f"{name} is not listed in the release SHA256SUMS")
        archive = self.fetch(f"{RELEASES}/{version}/{name}")
        actual = hashlib.sha256(archive).hexdigest()
        if actual != expected:
            raise EngineError(f"{name} sha256 {actual} does not match the release sum {expected}")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            body = bundle.read("terraform")
        binary.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        staging = binary.with_suffix(".partial")
        staging.write_bytes(body)
        staging.chmod(0o755)
        staging.replace(binary)


def hcl_string(value: str) -> str:
    """`value` as an HCL string literal with interpolation and directives escaped."""
    return json.dumps(value).replace("${", "$${").replace("%{", "%%{")


def s3_backend(bucket: str, key: str, region: str, kms_key_id: str, workspace_key_prefix: str) -> str:
    """The same S3 backend block the plane's runner writes, so the engine reads the object the runner will."""
    return (
        "terraform {\n"
        '  backend "s3" {\n'
        f"    bucket               = {hcl_string(bucket)}\n"
        f"    key                  = {hcl_string(key)}\n"
        f"    region               = {hcl_string(region)}\n"
        f"    kms_key_id           = {hcl_string(kms_key_id)}\n"
        f"    workspace_key_prefix = {hcl_string(workspace_key_prefix)}\n"
        "    encrypt              = true\n"
        "    use_lockfile         = true\n"
        "  }\n"
        "}\n"
    )


def cloud_backend(hostname: str, organization: str, workspace: str) -> str:
    """A `cloud` block naming exactly one HCP workspace."""
    return (
        "terraform {\n"
        "  cloud {\n"
        f"    hostname     = {hcl_string(hostname)}\n"
        f"    organization = {hcl_string(organization)}\n"
        "    workspaces {\n"
        f"      name = {hcl_string(workspace)}\n"
        "    }\n"
        "  }\n"
        "}\n"
    )
