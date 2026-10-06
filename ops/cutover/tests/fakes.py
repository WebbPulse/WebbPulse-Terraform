"""In memory stand-ins for HCP, the plane, S3 and the engine."""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from cutover.cli import Deps
from cutover.config import Environment
from cutover.engine import Completed, EngineResolver
from cutover.hcp import HcpApi, HcpStateVersion, HcpWorkspace, VariableShape
from cutover.output import Console, Redactor
from cutover.plane import PlaneApi, PlaneWorkspace
from cutover.s3state import StateStore, StoreError

HCP_WS = "ws-xVqd4ioXLARZhGd4"
PLANE_WS = "ws-01K7Z3Y9V2B4N6M8P0Q2R4S6T8"
LINEAGE = "e69b10a1-8b15-9d3a-3687-a4354cbf5636"
OUTPUT_SECRET = "s3cr3t-output-value-1234"
ATTRIBUTE_SECRET = "hunter2-db-password-5678"
HCP_TOKEN = "atlasv1.fake-hcp-token-abcdefghijklmnop"
ME = "user-me"
KMS_ARN = "arn:aws:kms:us-west-2:897427573432:key/11111111-2222-3333-4444-555555555555"


def make_state(serial: int = 70, lineage: str = LINEAGE, version: str = "1.16.4") -> bytes:
    """A state body holding secrets in an output and a resource attribute."""
    document = {
        "version": 4,
        "terraform_version": version,
        "serial": serial,
        "lineage": lineage,
        "outputs": {"db": {"value": OUTPUT_SECRET, "type": "string", "sensitive": True}},
        "resources": [
            {
                "mode": "managed",
                "type": "aws_db_instance",
                "name": "main",
                "provider": 'provider["registry.terraform.io/hashicorp/aws"]',
                "instances": [{"schema_version": 0, "attributes": {"id": "db-1", "password": ATTRIBUTE_SECRET}}],
            }
        ],
        "check_results": None,
    }
    return json.dumps(document, indent=2).encode()


@dataclass
class FakeHcp(HcpApi):
    """HCP with one workspace whose state versions are a list, newest last."""

    states: list[bytes] = field(default_factory=lambda: [make_state()])
    locked: bool = True
    locked_by: str | None = ME
    runs: list[str] = field(default_factory=lambda: [])
    variable_shapes: list[VariableShape] = field(default_factory=lambda: [])
    engine_versions: list[str] = field(default_factory=lambda: ["1.16.4"])
    downloads: int = 0
    lock_reasons: list[str] = field(default_factory=lambda: [])
    unlocks: int = 0

    def workspace(self, workspace_id: str) -> HcpWorkspace:
        """The one workspace."""
        return HcpWorkspace(
            workspace_id, "WebbPulse-Terraform-staging", "WebbPulse", self.locked, self.locked_by, "~> 1.10"
        )

    def account_id(self) -> str:
        """The token's user."""
        return ME

    def active_runs(self, workspace_id: str) -> list[str]:
        """Configured in-flight runs."""
        return list(self.runs)

    def current_state_version(self, workspace_id: str) -> HcpStateVersion | None:
        """The newest state, with its serial read from the body as HCP records it."""
        if not self.states:
            return None
        index = len(self.states) - 1
        serial = int(json.loads(self.states[index])["serial"])
        version = self.engine_versions[min(index, len(self.engine_versions) - 1)]
        return HcpStateVersion(f"sv-{index}", serial, version, f"https://app.terraform.io/sv-{index}")

    def download_state(self, version: HcpStateVersion) -> bytes:
        """The body of one version."""
        self.downloads += 1
        return self.states[int(version.id.removeprefix("sv-"))]

    def lock(self, workspace_id: str, reason: str) -> None:
        """Record the lock."""
        self.locked = True
        self.locked_by = ME
        self.lock_reasons.append(reason)

    def unlock(self, workspace_id: str) -> None:
        """Record the unlock."""
        self.locked = False
        self.locked_by = None
        self.unlocks += 1

    def variables(self, workspace_id: str) -> list[VariableShape]:
        """Configured shapes."""
        return sorted(self.variable_shapes)


@dataclass
class FakePlane(PlaneApi):
    """The plane with one workspace, or none."""

    found: PlaneWorkspace | None = field(
        default_factory=lambda: PlaneWorkspace(PLANE_WS, "WebbPulse-Terraform-staging", "terraform", "1.16.4", None)
    )
    runs: list[str] = field(default_factory=lambda: [])
    variable_shapes: list[VariableShape] = field(default_factory=lambda: [])
    calls: int = 0

    def workspace(self, workspace_id: str) -> PlaneWorkspace | None:
        """The configured workspace."""
        self.calls += 1
        return self.found

    def active_runs(self, workspace_id: str) -> list[str]:
        """Configured runs."""
        self.calls += 1
        return list(self.runs)

    def variables(self, workspace_id: str) -> list[VariableShape]:
        """Configured shapes."""
        self.calls += 1
        return sorted(self.variable_shapes)


@dataclass
class FakeStore(StateStore):
    """An S3 bucket in a dict."""

    objects: dict[str, bytes] = field(default_factory=lambda: {})
    puts: list[tuple[str, str]] = field(default_factory=lambda: [])
    corrupt_on_read: Callable[[bytes], bytes] | None = None

    def kms_key_arn(self) -> str:
        """A fixed key ARN."""
        return KMS_ARN

    def exists(self, key: str) -> bool:
        """Membership."""
        return key in self.objects

    def put_new(self, key: str, body: bytes, kms_key_arn: str) -> None:
        """Conditional create."""
        if key in self.objects:
            raise StoreError("PreconditionFailed")
        self.objects[key] = body
        self.puts.append((key, kms_key_arn))

    def get(self, key: str) -> bytes:
        """The body, optionally altered to simulate a bad read back."""
        if key not in self.objects:
            raise StoreError("NoSuchKey")
        body = self.objects[key]
        return self.corrupt_on_read(body) if self.corrupt_on_read else body


@dataclass
class FakeRunner:
    """Simulates the few terraform subcommands the tool runs."""

    versions: dict[str, str] = field(default_factory=lambda: {})
    pull: Callable[[], bytes] = lambda: b""
    push: Callable[[bytes], None] = lambda body: None
    fail: dict[str, tuple[int, bytes]] = field(default_factory=lambda: {})
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=lambda: [])

    def __call__(self, args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Completed:
        """Answer `version -json`, `init`, `state pull` and `state push`."""
        argv = list(args)
        self.calls.append((argv, dict(env)))
        verb = " ".join(argv[1:3]) if argv[1] == "state" else argv[1]
        if verb in self.fail:
            code, stderr = self.fail[verb]
            return Completed(code, b"", stderr)
        if verb == "version":
            version = self.versions.get(argv[0], "0.0.0")
            return Completed(0, json.dumps({"terraform_version": version}).encode(), b"")
        if verb == "state pull":
            return Completed(0, self.pull(), b"")
        if verb == "state push":
            self.push(Path(argv[-1]).read_bytes())
            return Completed(0, b"", b"")
        return Completed(0, b"Terraform has been successfully initialized!\n", b"")

    def verbs(self) -> list[str]:
        """The subcommands run, in order."""
        return [" ".join(argv[1:3]) if argv[1] == "state" else argv[1] for argv, _env in self.calls]


@dataclass
class World:
    """One wired set of fakes plus captured output."""

    hcp: FakeHcp
    plane: FakePlane
    store: FakeStore
    runner: FakeRunner
    out: io.StringIO
    err: io.StringIO
    private_parent: Path
    engine_bin: Path
    plane_factory_calls: int = 0

    def deps(self) -> Deps:
        """Deps wired to the fakes."""

        def plane_factory(environment: Environment) -> PlaneApi:
            """Count plane client construction."""
            self.plane_factory_calls += 1
            return self.plane

        return Deps(
            console=Console(Redactor(), self.out, self.err),
            token_loader=lambda: HCP_TOKEN,
            hcp_factory=lambda token: self.hcp,
            plane_factory=plane_factory,
            store_factory=lambda environment: self.store,
            resolver=EngineResolver(cache_root=self.private_parent / "cache", runner=self.runner),
            private_parent=self.private_parent,
        )

    def output(self) -> str:
        """Everything printed to either stream."""
        return self.out.getvalue() + self.err.getvalue()


def make_world(tmp_path: Path) -> World:
    """A world whose HCP is locked by us, with an engine binary at 1.16.4."""
    private_parent = tmp_path / "private"
    private_parent.mkdir()
    engine_bin = tmp_path / "bin" / "terraform"
    engine_bin.parent.mkdir()
    engine_bin.write_text("fake")
    runner = FakeRunner(versions={str(engine_bin): "1.16.4"})
    return World(FakeHcp(), FakePlane(), FakeStore(), runner, io.StringIO(), io.StringIO(), private_parent, engine_bin)
