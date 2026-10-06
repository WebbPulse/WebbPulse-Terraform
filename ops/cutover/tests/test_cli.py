"""The subcommands end to end against fakes."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from cutover.cli import Deps, main
from cutover.hcp import HcpClient, VariableShape
from cutover.output import Console, Redactor
from cutover.plane import PlaneWorkspace

from .fakes import (
    ATTRIBUTE_SECRET,
    HCP_TOKEN,
    HCP_WS,
    KMS_ARN,
    LINEAGE,
    OUTPUT_SECRET,
    PLANE_WS,
    World,
    make_state,
    make_world,
)

KEY = f"workspaces/{PLANE_WS}/terraform.tfstate"
SECRETS = (OUTPUT_SECRET, ATTRIBUTE_SECRET, HCP_TOKEN)


@pytest.fixture
def world(tmp_path: Path) -> World:
    """A fresh world per test."""
    return make_world(tmp_path)


def run(world: World, *argv: str) -> int:
    """Run the CLI against the world."""
    return main(list(argv), world.deps())


def move(world: World, *extra: str) -> int:
    """move-state against staging with the fake engine binary."""
    return run(world, "move-state", HCP_WS, PLANE_WS, "--env", "staging", "--engine-bin", str(world.engine_bin), *extra)


def rollback(world: World, *extra: str) -> int:
    """rollback-state against staging with the fake engine binary."""
    return run(
        world, "rollback-state", HCP_WS, PLANE_WS, "--env", "staging", "--engine-bin", str(world.engine_bin), *extra
    )


def assert_no_leak(world: World, captured: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    """No secret reaches either stream, the process streams or a log record."""
    seen = captured.readouterr()
    everything = world.output() + seen.out + seen.err + caplog.text
    for secret in SECRETS:
        assert secret not in everything


def assert_private_empty(world: World) -> None:
    """Every private directory was shredded away."""
    assert [path for path in world.private_parent.iterdir() if path.name != "cache"] == []


class TestPreflight:
    """Read only readiness checks."""

    def test_ready(self, world: World) -> None:
        """Everything lines up."""
        assert run(world, "preflight", HCP_WS, PLANE_WS, "--env", "staging") == 0
        assert "preflight ok" in world.output()
        assert world.hcp.downloads == 0

    def test_engine_mismatch_blocks(self, world: World) -> None:
        """A plane engine that is not the state's exact version fails."""
        world.plane.found = PlaneWorkspace(PLANE_WS, "x", "terraform", "1.16.5", None)
        assert run(world, "preflight", HCP_WS, PLANE_WS, "--env", "staging") == 1
        assert "1.16.5 does not equal" in world.output()

    def test_vcs_repo_blocks(self, world: World) -> None:
        """A plane workspace already bound to a repository fails."""
        world.plane.found = PlaneWorkspace(PLANE_WS, "x", "terraform", "1.16.4", "WebbPulse/WebbPulse-Terraform")
        assert run(world, "preflight", HCP_WS, PLANE_WS, "--env", "staging") == 1
        assert "vcs_repo" in world.output()

    def test_missing_plane_workspace(self, world: World) -> None:
        """A plane workspace that does not exist fails."""
        world.plane.found = None
        assert run(world, "preflight", HCP_WS, PLANE_WS, "--env", "staging") == 1
        assert "does not exist" in world.output()

    def test_hcp_run_in_flight(self, world: World) -> None:
        """A run in flight on HCP fails."""
        world.hcp.runs = ["run-abc"]
        assert run(world, "preflight", HCP_WS, PLANE_WS, "--env", "staging") == 1
        assert "run-abc" in world.output()

    def test_no_plane_makes_no_plane_calls(self, world: World) -> None:
        """--no-plane never builds a plane client."""
        assert run(world, "preflight", HCP_WS, PLANE_WS, "--env", "staging", "--no-plane") == 0
        assert world.plane_factory_calls == 0

    def test_bad_ids_are_usage_errors(self, world: World) -> None:
        """Malformed ids exit 2 before any call."""
        assert run(world, "preflight", "ws-short", PLANE_WS, "--env", "staging") == 2
        assert run(world, "preflight", HCP_WS, "ws-lowercase", "--env", "staging") == 2


class TestLock:
    """Locking and unlocking HCP."""

    def test_lock_names_the_issue(self, world: World) -> None:
        """The reason carries the issue key."""
        world.hcp.locked = False
        world.hcp.locked_by = None
        assert run(world, "lock", HCP_WS, "--issue", "TF-26") == 0
        assert world.hcp.lock_reasons == ["TF-26: cutover to the owned control plane"]

    def test_lock_needs_an_issue_key(self, world: World) -> None:
        """A malformed key is a usage error and nothing is locked."""
        world.hcp.locked = False
        assert run(world, "lock", HCP_WS, "--issue", "tf26") == 2
        assert world.hcp.lock_reasons == []

    def test_lock_is_idempotent_for_us(self, world: World) -> None:
        """Already locked by us is fine."""
        assert run(world, "lock", HCP_WS, "--issue", "TF-26") == 0
        assert world.hcp.lock_reasons == []

    def test_lock_held_by_someone_else(self, world: World) -> None:
        """Someone else's lock is refused."""
        world.hcp.locked_by = "user-other"
        assert run(world, "lock", HCP_WS, "--issue", "TF-26") == 1

    def test_lock_warns_about_runs(self, world: World) -> None:
        """Locking with a run in flight warns."""
        world.hcp.locked = False
        world.hcp.runs = ["run-busy"]
        assert run(world, "lock", HCP_WS, "--issue", "TF-26") == 0
        assert "run-busy" in world.err.getvalue()

    def test_unlock(self, world: World) -> None:
        """Our lock is released."""
        assert run(world, "unlock", HCP_WS) == 0
        assert world.hcp.unlocks == 1

    def test_unlock_refuses_someone_elses_lock(self, world: World) -> None:
        """Another user's lock is left alone."""
        world.hcp.locked_by = "user-other"
        assert run(world, "unlock", HCP_WS) == 1
        assert world.hcp.unlocks == 0


class TestMoveState:
    """Runbook step 3."""

    def test_moves_exact_bytes_and_verifies(
        self, world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The object is the downloaded body under the resolved key, and the engine reads it back."""
        caplog.set_level(logging.DEBUG)
        world.runner.pull = lambda: world.store.objects[KEY]
        assert move(world) == 0
        assert world.store.objects[KEY] == world.hcp.states[-1]
        assert world.store.puts == [(KEY, KMS_ARN)]
        assert world.runner.verbs() == ["version", "init", "state pull"]
        assert f"lineage={LINEAGE}" in world.output()
        assert "verified" in world.output()
        assert_no_leak(world, capsys, caplog)
        assert_private_empty(world)

    def test_backend_matches_the_runner(self, world: World) -> None:
        """The engine sees an S3 backend with the runner's prefix, KMS key, encryption and lockfile."""
        seen: list[str] = []

        def pull() -> bytes:
            """Capture the backend file while the private directory still exists."""
            for path in world.private_parent.rglob("backend.tf"):
                seen.append(path.read_text())
            return world.store.objects[KEY]

        world.runner.pull = pull
        assert move(world) == 0
        backend = seen[0]
        assert '"webbpulse-terraform-staging-state"' in backend
        assert f'"{KEY}"' in backend
        assert f'"{KMS_ARN}"' in backend
        assert f'"workspaces/{PLANE_WS}/env"' in backend
        assert "encrypt              = true" in backend
        assert "use_lockfile         = true" in backend
        init_env = world.runner.calls[1][1]
        assert init_env["AWS_PROFILE"] == "WebbPulse-Terraform-Staging/AdministratorAccess"
        assert "TF_TOKEN_app_terraform_io" not in init_env

    def test_engine_mismatch_blocks_before_download(self, world: World) -> None:
        """A plane engine off by one patch release refuses before state is fetched."""
        world.plane.found = PlaneWorkspace(PLANE_WS, "x", "terraform", "1.16.5", None)
        assert move(world) == 1
        assert world.hcp.downloads == 0
        assert world.store.puts == []
        assert "REFUSED" in world.output()

    def test_engine_binary_at_the_wrong_version_blocks(self, world: World) -> None:
        """A local binary that reports another version is refused before anything is written."""
        world.runner.versions[str(world.engine_bin)] = "1.16.5"
        assert move(world) == 1
        assert world.store.puts == []
        assert "reports Terraform 1.16.5, expected 1.16.4" in world.output()
        assert_private_empty(world)

    def test_engine_pull_with_another_lineage_fails(self, world: World) -> None:
        """A read back through the engine with a different lineage is a failure."""
        world.runner.pull = lambda: make_state(lineage="00000000-0000-0000-0000-000000000000")
        assert move(world) == 1
        assert "LineageMismatch" in world.output()
        assert_private_empty(world)

    def test_engine_pull_with_another_serial_fails(self, world: World) -> None:
        """A read back through the engine at another serial is a failure."""
        world.runner.pull = lambda: make_state(serial=1)
        assert move(world) == 1
        assert "SerialMismatch" in world.output()

    def test_s3_read_back_mismatch_fails(self, world: World) -> None:
        """An S3 read back with another lineage fails before the engine pull."""
        world.store.corrupt_on_read = lambda body: make_state(lineage="11111111-0000-0000-0000-000000000000")
        assert move(world) == 1
        assert "LineageMismatch" in world.output()
        assert "state pull" not in world.runner.verbs()

    def test_dry_run_writes_nothing_and_skips_the_plane(
        self, world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A dry run downloads, resolves and inits, but never calls the plane or writes."""
        assert move(world, "--dry-run") == 0
        assert world.plane_factory_calls == 0
        assert world.plane.calls == 0
        assert world.store.puts == []
        assert world.runner.verbs() == ["version", "init"]
        assert "nothing was written" in world.output()
        assert_no_leak(world, capsys, caplog)
        assert_private_empty(world)

    def test_refuses_without_our_lock(self, world: World) -> None:
        """An unlocked HCP workspace is refused before anything is read."""
        world.hcp.locked = False
        assert move(world) == 1
        assert world.hcp.downloads == 0

    def test_refuses_with_hcp_run_in_flight(self, world: World) -> None:
        """A run in flight is refused."""
        world.hcp.runs = ["run-x"]
        assert move(world) == 1
        assert world.hcp.downloads == 0

    @pytest.mark.parametrize("suffix", ["", ".tflock"])
    def test_refuses_to_overwrite(self, world: World, suffix: str) -> None:
        """Existing state or lock at the key is never overwritten."""
        world.store.objects[KEY + suffix] = b"{}"
        assert move(world) == 1
        assert world.store.puts == []
        assert "already exists" in world.output()

    def test_engine_stderr_is_redacted(
        self, world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """An engine error that echoes a state value is printed masked."""
        world.runner.fail["init"] = (1, f"Error: bad value {ATTRIBUTE_SECRET} near {OUTPUT_SECRET}\n".encode())
        assert move(world) == 1
        assert "[redacted]" in world.output()
        assert_no_leak(world, capsys, caplog)
        assert_private_empty(world)

    def test_body_disagreeing_with_version_is_refused(self, world: World) -> None:
        """A body whose engine version differs from the state version's is refused."""
        world.hcp.states = [make_state(version="1.16.3")]
        assert move(world) == 1
        assert world.store.puts == []


class TestCompareVars:
    """Runbook step 4."""

    def shapes(self) -> list[VariableShape]:
        """A typical HCP variable set."""
        return [
            VariableShape("env", "TFC_AWS_RUN_ROLE_ARN", False, False, "varset:aws"),
            VariableShape("terraform", "bootstrap_image_tag", False, False, "workspace"),
            VariableShape("terraform", "allowed_cidrs", False, True, "workspace"),
            VariableShape("env", "GITHUB_TOKEN", True, False, "workspace"),
        ]

    def test_match_ignores_tfc_variables(self, world: World) -> None:
        """TFC_ variables have no plane equivalent and are ignored by default."""
        world.hcp.variable_shapes = self.shapes()
        world.plane.variable_shapes = [
            VariableShape("terraform", "bootstrap_image_tag", False, False, "plane"),
            VariableShape("terraform", "allowed_cidrs", False, True, "plane"),
            VariableShape("env", "GITHUB_TOKEN", True, False, "plane"),
        ]
        assert run(world, "compare-vars", HCP_WS, PLANE_WS, "--env", "staging") == 0
        assert "variables match" in world.output()
        assert "TFC_AWS_RUN_ROLE_ARN" not in world.output()

    def test_missing_and_differing(self, world: World) -> None:
        """A missing variable and a flag mismatch both fail."""
        world.hcp.variable_shapes = self.shapes()
        world.plane.variable_shapes = [VariableShape("terraform", "allowed_cidrs", False, False, "plane")]
        assert run(world, "compare-vars", HCP_WS, PLANE_WS, "--env", "staging") == 1
        assert "MISSING on plane: env GITHUB_TOKEN" in world.output()
        assert "DIFFERS terraform allowed_cidrs" in world.output()

    def test_include_tfc(self, world: World) -> None:
        """--include-tfc compares TFC_ variables too."""
        world.hcp.variable_shapes = self.shapes()
        world.plane.variable_shapes = []
        assert run(world, "compare-vars", HCP_WS, PLANE_WS, "--env", "staging", "--include-tfc") == 1
        assert "MISSING on plane: env TFC_AWS_RUN_ROLE_ARN" in world.output()

    def test_values_from_hcp_are_never_printed(
        self, world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The real HCP client drops values that the API returns, so they cannot be printed."""
        caplog.set_level(logging.DEBUG)
        plain_value = "plain-value-that-must-not-print"

        def handler(request: httpx.Request) -> httpx.Response:
            """Workspace variables only, one sensitive with a value, one plain."""
            if request.url.path.endswith("/varsets"):
                return httpx.Response(200, json={"data": [], "meta": {"pagination": {"next-page": None}}})
            variables = [
                {
                    "id": "var-1",
                    "attributes": {
                        "key": "db_password",
                        "value": ATTRIBUTE_SECRET,
                        "category": "terraform",
                        "sensitive": False,
                        "hcl": False,
                    },
                },
                {
                    "id": "var-2",
                    "attributes": {
                        "key": "region",
                        "value": plain_value,
                        "category": "env",
                        "sensitive": False,
                        "hcl": False,
                    },
                },
            ]
            return httpx.Response(200, json={"data": variables})

        client = HcpClient(HCP_TOKEN, transport=httpx.MockTransport(handler))
        deps = world.deps()
        deps.hcp_factory = lambda token: client
        assert main(["compare-vars", HCP_WS, PLANE_WS, "--env", "staging", "--no-plane"], deps) == 0
        assert "db_password" in world.output()
        assert plain_value not in world.output()
        assert_no_leak(world, capsys, caplog)


class TestRollback:
    """Putting the plane's newest state back on HCP."""

    def prepare(self, world: World, plane_serial: int = 74) -> None:
        """The plane has advanced the moved state, and pushing appends an HCP version at the next serial."""
        world.store.objects[KEY] = make_state(serial=plane_serial)

        def push(body: bytes) -> None:
            """HCP stores the pushed state at one past its serial, as Terraform does."""
            document = json.loads(body)
            document["serial"] += 1
            world.hcp.states.append(json.dumps(document).encode())

        world.runner.push = push

    def test_pushes_verifies_and_unlocks(
        self, world: World, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The push runs without taking a lock, then HCP is verified and unlocked."""
        self.prepare(world)
        assert rollback(world) == 0
        assert world.runner.verbs() == ["version", "init", "state push"]
        push_args, push_env = world.runner.calls[-1]
        assert "-lock=false" in push_args
        assert push_env["TF_TOKEN_app_terraform_io"] == HCP_TOKEN
        assert json.loads(world.hcp.states[-1])["serial"] == 75
        assert world.hcp.unlocks == 1
        assert_no_leak(world, capsys, caplog)
        assert_private_empty(world)

    def test_cloud_block_names_the_workspace(self, world: World) -> None:
        """The engine is pointed at exactly the HCP workspace being rolled back."""
        self.prepare(world)
        seen: list[str] = []
        original = world.runner.push

        def push(body: bytes) -> None:
            """Capture the backend while the private directory exists."""
            seen.extend(path.read_text() for path in world.private_parent.rglob("backend.tf"))
            original(body)

        world.runner.push = push
        assert rollback(world) == 0
        assert 'organization = "WebbPulse"' in seen[0]
        assert 'name = "WebbPulse-Terraform-staging"' in seen[0]

    def test_refuses_another_lineage(self, world: World) -> None:
        """A plane state from another history is never pushed and HCP stays locked."""
        self.prepare(world)
        world.store.objects[KEY] = make_state(serial=80, lineage="22222222-0000-0000-0000-000000000000")
        assert rollback(world) == 1
        assert "LineageMismatch" in world.output()
        assert "state push" not in world.runner.verbs()
        assert world.hcp.unlocks == 0
        assert_private_empty(world)

    def test_refuses_a_plane_state_behind_hcp(self, world: World) -> None:
        """A plane serial behind HCP's is refused."""
        self.prepare(world, plane_serial=60)
        assert rollback(world) == 1
        assert world.hcp.unlocks == 0

    def test_same_serial_only_unlocks(self, world: World) -> None:
        """Nothing changed on the plane, so only the unlock happens."""
        self.prepare(world, plane_serial=70)
        assert rollback(world) == 0
        assert "state push" not in world.runner.verbs()
        assert world.hcp.unlocks == 1

    def test_dry_run_keeps_the_lock(self, world: World) -> None:
        """A dry run inits but never pushes or unlocks."""
        self.prepare(world)
        assert rollback(world, "--dry-run") == 0
        assert world.runner.verbs() == ["version", "init"]
        assert world.hcp.unlocks == 0
        assert len(world.hcp.states) == 1

    def test_uses_hcps_engine_version(self, world: World) -> None:
        """The engine is resolved at HCP's state version, not the plane's."""
        self.prepare(world)
        world.store.objects[KEY] = make_state(serial=74, version="1.16.5")
        assert rollback(world) == 0
        assert "terraform 1.16.4" in world.output()

    def test_refuses_without_our_lock(self, world: World) -> None:
        """Rollback needs HCP still locked by us."""
        self.prepare(world)
        world.hcp.locked = False
        assert rollback(world) == 1
        assert "state push" not in world.runner.verbs()


def test_unexpected_errors_are_redacted(tmp_path: Path) -> None:
    """Even an unexpected exception's message passes through the redactor."""
    world = make_world(tmp_path)

    def boom(token: str) -> HcpClient:
        """Fail with the token in the message."""
        raise RuntimeError(f"exploded with {token}")

    deps = Deps(
        console=Console(Redactor(), world.out, world.err),
        token_loader=lambda: HCP_TOKEN,
        hcp_factory=boom,
    )
    assert main(["unlock", HCP_WS], deps) == 1
    assert HCP_TOKEN not in world.output()
    assert "RuntimeError" in world.output()
