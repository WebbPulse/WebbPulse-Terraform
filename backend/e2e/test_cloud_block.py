"""The real `terraform` CLI driving a workspace through a `cloud {}` block on staging.

HCP compatibility is proven the only way that counts: an unmodified CLI, configured with
nothing but the cloud block and `TF_TOKEN_<SPA host>`, runs remote plans and applies with
`-var`, `-target`, `-replace` and `-destroy`, saves a plan with `plan -out` and applies the
planfile, reads outputs and state, moves, removes and imports state through locked local
operations, and clears a held lock with `force-unlock`, with no break-glass and no `wp-tf`.
No remote run logs an error from the cost estimate, policy or task stage reads, and a cloud
block naming a missing workspace fails saying where workspaces are created. The
configuration is `terraform_data` only, which is built into the CLI, so nothing is
installed and nothing billable exists.

The key carries exactly the scopes a `terraform login` key carries, so the case proves
what a person's login can do, and is revoked on teardown. Teardown also destroys whatever
the case left in state before the workspace fixture deletes the workspace, whatever the
outcome; a destroy that fails there leaves the fixture's force delete to fail the case,
naming the workspace. Skipped outside staging and on the read-only production smoke, since
a key is minted and runs apply. No key, state or plan JSON is ever printed: CLI output is
redacted before it reaches an assertion message.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from terraform_cli import find_or_fetch_terraform

SPA_HOST = "staging.terraform.webbpulse.com"
API_HOST = "api.staging.terraform.webbpulse.com"
ORGANIZATION = "WebbPulse"
TFE_API = f"https://{API_HOST}/api/v2"
TOKEN_VARIABLE = "TF_TOKEN_" + SPA_HOST.replace(".", "_").replace("-", "__")
API_KEYS = "/api/v1/api-keys"
KEY_SCOPES = [
    "workspaces:read",
    "variables:read",
    "configs:read",
    "configs:write",
    "runs:read",
    "runs:write",
    "runs:apply",
    "state:download",
    "state:write",
    "registry:read",
]
RUN_TIMEOUT_SECONDS = 1500
STAGE_ERRORS = ("Failed to retrieve", "Error:")
"""What the CLI prints when a stub it reads around a run, such as task stages or policy
evaluations, answers something it cannot use."""
LOCAL_TIMEOUT_SECONDS = 300
HTTP_TIMEOUT_SECONDS = 30
TAIL = 2500

CONFIGURATION = """terraform {{
  cloud {{
    hostname     = "{host}"
    organization = "{organization}"

    workspaces {{
      name = "{workspace}"
    }}
  }}
}}

variable "label" {{
  type    = string
  default = "unset"
}}

resource "terraform_data" "a" {{
  input = "a"
}}

resource "terraform_data" "b" {{
  input = "b"
}}

output "a_id" {{
  value = terraform_data.a.id
}}

output "b_id" {{
  value = terraform_data.b.id
}}

output "label" {{
  value = var.label
}}
"""

pytestmark = [
    pytest.mark.e2e_writes,
    pytest.mark.xdist_group("cloud-block"),
    pytest.mark.skipif(
        os.environ.get("E2E_ENVIRONMENT", "").strip().lower() != "staging",
        reason="the cloud block case mints a key and applies runs, so it runs on staging only",
    ),
]


@pytest.fixture(scope="module")
def terraform_binary(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The CLI under test."""
    return find_or_fetch_terraform(tmp_path_factory)


@pytest.fixture
def cloud_key(e2e_env: Any, step_up_again: Callable[[], Any]) -> Iterator[str]:
    """An agent key with the cloud backend's scopes, revoked on teardown whatever happened."""
    response = step_up_again().post(
        API_KEYS, json={"name": f"{e2e_env.resource_prefix}cloud-block", "scopes": KEY_SCOPES}
    )
    assert response.status_code == 201, f"minting answered {response.status_code}: {response.text[:400]}"
    body = response.json()
    try:
        yield str(body["key"])
    finally:
        revoked = step_up_again().delete(f"{API_KEYS}/{body['key_id']}")
        if revoked.status_code != 200:
            pytest.fail(f"e2e teardown could not revoke the cloud block key ({revoked.status_code})")


class Cli:
    """One working directory's `terraform`, with an environment holding only the cloud block's token."""

    def __init__(self, binary: str, workdir: Path, home: Path, key: str) -> None:
        """Build the environment once: an empty CLI config, a throwaway HOME and the token."""
        config = home / "terraformrc"
        config.write_text("")
        self.binary = binary
        self.workdir = workdir
        self.key = key
        self.env = {name: value for name, value in os.environ.items() if not name.startswith("TF_")}
        self.env.update(
            {"HOME": str(home), "TF_CLI_CONFIG_FILE": str(config), "TF_IN_AUTOMATION": "1", TOKEN_VARIABLE: key}
        )

    def run(self, *arguments: str, timeout: int = LOCAL_TIMEOUT_SECONDS) -> tuple[int, str]:
        """Run one subcommand and return its exit code and its redacted, combined output."""
        completed = subprocess.run(
            [self.binary, *arguments],
            cwd=self.workdir,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return completed.returncode, (completed.stdout + "\n" + completed.stderr).replace(self.key, "[redacted]")

    def ok(self, *arguments: str, timeout: int = LOCAL_TIMEOUT_SECONDS) -> str:
        """Run one subcommand that must succeed, failing with the tail of its output."""
        code, output = self.run(*arguments, timeout=timeout)
        assert code == 0, f"terraform {arguments[0]} exited {code}: {output[-TAIL:]}"
        return output

    def outputs(self) -> dict[str, str]:
        """The root outputs as `terraform output -json` reads them from the current state version."""
        completed = subprocess.run(
            [self.binary, "output", "-json"],
            cwd=self.workdir,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=LOCAL_TIMEOUT_SECONDS,
            check=False,
        )
        assert completed.returncode == 0, f"terraform output exited {completed.returncode}: {completed.stderr[-TAIL:]}"
        return {name: str(value["value"]) for name, value in json.loads(completed.stdout).items()}

    def addresses(self) -> list[str]:
        """The resource addresses `terraform state list` reads."""
        return sorted(line.strip() for line in self.ok("state", "list").splitlines() if line.startswith("terraform_"))


def _cli_version(binary: str) -> str:
    """The CLI's own version, which the workspace must carry for local state operations."""
    completed = subprocess.run(
        [binary, "version", "-json"], capture_output=True, text=True, timeout=LOCAL_TIMEOUT_SECONDS, check=True
    )
    return str(json.loads(completed.stdout)["terraform_version"])


def _remote(output: str) -> str:
    """A remote run's output, failing on anything the CLI logged as an error around it."""
    for marker in STAGE_ERRORS:
        assert marker not in output, f"the run logged {marker!r}: {output[-TAIL:]}"
    return output


def _flat(output: str) -> str:
    """Output with its whitespace collapsed, so a wrapped diagnostic reads as one line."""
    return " ".join(output.split())


def _lock(workspace_id: str, key: str) -> None:
    """Hold the workspace lock as a crashed local operation would leave it."""
    response = httpx.post(
        f"{TFE_API}/workspaces/{workspace_id}/actions/lock",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/vnd.api+json"},
        json={"reason": "e2e stale lock"},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    assert response.status_code == 200, f"locking answered {response.status_code}: {response.text[:400]}"


def test_terraform_cli_drives_a_workspace_through_a_cloud_block(
    api: Any,
    workspace: dict[str, Any],
    cloud_key: str,
    terraform_binary: str,
    tmp_path: Path,
) -> None:
    """Init, apply with -var, target, replace, saved plans, outputs, state ops, import, force-unlock, destroy."""
    workspace_id = str(workspace["workspace_id"])
    name = str(workspace["name"])
    version = _cli_version(terraform_binary)
    patched = api.patch(f"/api/v1/workspaces/{workspace_id}", json={"engine_version": version})
    assert patched.status_code == 200, f"pinning the engine answered {patched.status_code}: {patched.text[:400]}"

    workdir = tmp_path / "root"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (workdir / "main.tf").write_text(CONFIGURATION.format(host=SPA_HOST, organization=ORGANIZATION, workspace=name))
    cli = Cli(terraform_binary, workdir, home, cloud_key)
    applied = False
    try:
        initialized = cli.ok("init", "-input=false", "-no-color")
        assert "successfully initialized" in initialized, initialized[-TAIL:]

        created = _remote(
            cli.ok(
                "apply",
                "-auto-approve",
                "-input=false",
                "-no-color",
                "-var=label=from-var",
                timeout=RUN_TIMEOUT_SECONDS,
            )
        )
        applied = True
        assert "Apply complete! Resources: 2 added, 0 changed, 0 destroyed." in created, created[-TAIL:]
        assert cli.addresses() == ["terraform_data.a", "terraform_data.b"]
        first = cli.outputs()
        assert first["a_id"] and first["b_id"] and first["a_id"] != first["b_id"]
        assert first["label"] == "from-var", "the remote run did not take the -var value"

        shown = cli.ok("state", "show", "-no-color", "terraform_data.a")
        assert 'resource "terraform_data" "a"' in shown, "state show did not render the resource"
        assert first["a_id"] in shown, "state show rendered another instance"

        targeted = _remote(
            cli.ok(
                "plan",
                "-input=false",
                "-no-color",
                "-var=label=from-var",
                "-target=terraform_data.a",
                "-replace=terraform_data.a",
                timeout=RUN_TIMEOUT_SECONDS,
            )
        )
        assert "Plan: 1 to add, 0 to change, 1 to destroy." in targeted, targeted[-TAIL:]
        actions = targeted.split("Terraform will perform")[-1]
        assert "terraform_data.b" not in actions, "the targeted plan touched an untargeted resource"
        assert cli.outputs() == first, "a speculative plan changed the state"

        replaced = _remote(
            cli.ok(
                "apply",
                "-auto-approve",
                "-input=false",
                "-no-color",
                "-var=label=from-var",
                "-replace=terraform_data.b",
                timeout=RUN_TIMEOUT_SECONDS,
            )
        )
        assert "Apply complete! Resources: 1 added, 0 changed, 1 destroyed." in replaced, replaced[-TAIL:]
        second = cli.outputs()
        assert second["a_id"] == first["a_id"], "the replace touched an untargeted resource"
        assert second["b_id"] != first["b_id"], "the replaced resource kept its id"

        saved = _remote(
            cli.ok(
                "plan",
                "-input=false",
                "-no-color",
                "-out=saved.tfplan",
                "-var=label=saved",
                "-replace=terraform_data.b",
                timeout=RUN_TIMEOUT_SECONDS,
            )
        )
        assert "Plan: 1 to add, 0 to change, 1 to destroy." in saved, saved[-TAIL:]
        assert (workdir / "saved.tfplan").is_file(), "plan -out wrote no planfile"
        assert cli.outputs() == second, "saving a plan changed the state"
        from_file = _remote(cli.ok("apply", "-input=false", "-no-color", "saved.tfplan", timeout=RUN_TIMEOUT_SECONDS))
        assert "Apply complete! Resources: 1 added, 0 changed, 1 destroyed." in from_file, from_file[-TAIL:]
        third = cli.outputs()
        assert third["label"] == "saved", "the saved plan's -var value did not apply"
        assert third["b_id"] != second["b_id"], "the saved plan's replacement did not apply"
        assert third["a_id"] == second["a_id"], "the saved plan touched an untargeted resource"
        code, again = cli.run("apply", "-input=false", "-no-color", "saved.tfplan", timeout=RUN_TIMEOUT_SECONDS)
        assert code != 0, "an applied saved plan applied a second time"
        assert "Saved plan is already applied" in again, again[-TAIL:]

        stale = _remote(
            cli.ok(
                "plan",
                "-input=false",
                "-no-color",
                "-out=stale.tfplan",
                "-var=label=saved",
                "-replace=terraform_data.b",
                timeout=RUN_TIMEOUT_SECONDS,
            )
        )
        assert "Plan: 1 to add, 0 to change, 1 to destroy." in stale, stale[-TAIL:]
        overtaken = _remote(
            cli.ok(
                "apply",
                "-auto-approve",
                "-input=false",
                "-no-color",
                "-var=label=saved",
                "-replace=terraform_data.a",
                timeout=RUN_TIMEOUT_SECONDS,
            )
        )
        assert "Apply complete! Resources: 1 added, 0 changed, 1 destroyed." in overtaken, overtaken[-TAIL:]
        fourth = cli.outputs()
        assert fourth["a_id"] != third["a_id"], "the run behind a saved plan did not apply"
        code, refused = cli.run("apply", "-input=false", "-no-color", "stale.tfplan", timeout=RUN_TIMEOUT_SECONDS)
        assert code != 0, "a saved plan applied over a state that moved on"
        assert "Saved plan is discarded" in refused or "stale" in refused.lower(), refused[-TAIL:]
        assert cli.outputs() == fourth, "a stale saved plan changed the state"

        moved = cli.ok("state", "mv", "-no-color", "terraform_data.a", "terraform_data.moved")
        assert "Successfully moved 1 object(s)." in moved, moved[-TAIL:]
        assert cli.addresses() == ["terraform_data.b", "terraform_data.moved"]

        removed = cli.ok("state", "rm", "-no-color", "terraform_data.moved")
        assert "Successfully removed 1 resource instance(s)." in removed, removed[-TAIL:]
        assert cli.addresses() == ["terraform_data.b"]

        _lock(workspace_id, cloud_key)
        code, held = cli.run("state", "rm", "-no-color", "-lock-timeout=0s", "terraform_data.b")
        assert code != 0, "a state write went through a held lock"
        assert "locked" in held.lower(), held[-TAIL:]
        unlocked = cli.ok("force-unlock", "-force", "-no-color", f"{ORGANIZATION}/{name}")
        assert "successfully unlocked" in unlocked.lower(), unlocked[-TAIL:]

        imported = cli.ok("import", "-input=false", "-no-color", "terraform_data.a", first["a_id"])
        assert "Import successful!" in imported, imported[-TAIL:]
        assert cli.addresses() == ["terraform_data.a", "terraform_data.b"]

        destroyed = _remote(
            cli.ok("apply", "-destroy", "-auto-approve", "-input=false", "-no-color", timeout=RUN_TIMEOUT_SECONDS)
        )
        assert "Apply complete! Resources: 0 added, 0 changed, 2 destroyed." in destroyed, destroyed[-TAIL:]
        applied = False
        assert cli.addresses() == []
    finally:
        if applied:
            cli.run("force-unlock", "-force", "-no-color", f"{ORGANIZATION}/{name}")
            cli.run("apply", "-destroy", "-auto-approve", "-input=false", "-no-color", timeout=RUN_TIMEOUT_SECONDS)


def test_a_cloud_block_naming_a_missing_workspace_says_where_to_create_it(
    workspace: dict[str, Any],
    cloud_key: str,
    terraform_binary: str,
    tmp_path: Path,
) -> None:
    """HCP creates a missing workspace; here the CLI fails with the reason and the fix."""
    missing = f"{workspace['name']}-missing"
    workdir = tmp_path / "missing"
    workdir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (workdir / "main.tf").write_text(CONFIGURATION.format(host=SPA_HOST, organization=ORGANIZATION, workspace=missing))
    cli = Cli(terraform_binary, workdir, home, cloud_key)
    code, output = cli.run("init", "-input=false", "-no-color")
    if code == 0:
        code, output = cli.run("plan", "-input=false", "-no-color", timeout=RUN_TIMEOUT_SECONDS)
    assert code != 0, "a cloud block naming a missing workspace went through"
    flat = _flat(output)
    assert f'Workspace "{missing}" does not exist.' in flat, output[-TAIL:]
    assert "Create it in the WebbPulse Terraform UI" in flat, output[-TAIL:]
