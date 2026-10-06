"""The planned working directory carried from the plan phase into the apply."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path
from typing import Callable

import pytest

from app import workspace
from app.engine import DATA_DIRECTORY, PLAN_FILE, build_environment
from app.main import run
from tests.conftest import (
    PLAN_JSON_NO_CHANGES,
    SECRET_HCL_TFVAR,
    SECRET_TFVAR,
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
)

WORKDIR_PATH = "/runs/workdir.tar.gz"
WORKDIR_URL = "https://artifacts.example.invalid/runs/workdir.tar.gz?sig=7"
PLAN_URL = "https://artifacts.example.invalid/runs/plan.tfplan?sig=5"
GENERATED = (".build/handler.zip", ".terraform/modules/bundle/handler.js.tftpl", ".terraform/modules/modules.json")
LEFT_OUT = (".terraform/providers/registry.terraform.io/hashicorp/archive/binary", ".terraform/terraform.tfstate")


def archive_names(payload: bytes) -> set[str]:
    """The member names of a gzipped tarball."""
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        return set(archive.getnames())


def test_a_plan_with_changes_archives_the_working_directory(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The plan uploads its working directory with generated files and installed modules,
    and without the provider binaries, the backend record, the plan or any variable value."""
    fake_engine(plan_writes=GENERATED + LEFT_OUT)
    recorder = ApiRecorder()
    clients = make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, recorder))

    assert run(make_env("plan"), clients, tmp_path / "run") == 0

    assert "workdir" in {request["artifact"] for request in recorder.upload_requests}
    payload = recorder.uploads[WORKDIR_PATH]
    names = archive_names(payload)
    assert {"main.tf", *GENERATED} <= names
    assert not names & {*LEFT_OUT, "plan.tfplan", workspace.BACKEND_FILENAME}
    assert not names & {workspace.TFVARS_FILENAME, workspace.HCL_TFVARS_FILENAME}
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        contents = b"".join(
            handle.read() for member in archive.getmembers() if (handle := archive.extractfile(member)) is not None
        )
    assert SECRET_TFVAR.encode() not in contents
    assert SECRET_HCL_TFVAR.encode() not in contents


def test_a_plan_without_changes_archives_nothing(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """No apply follows a plan without changes, so it uploads no working directory."""
    fake_engine(plan_exit=0, plan_json=PLAN_JSON_NO_CHANGES)
    recorder = ApiRecorder()
    clients = make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, recorder))

    assert run(make_env("plan"), clients, tmp_path / "run") == 0

    assert "workdir" not in {request["artifact"] for request in recorder.upload_requests}


def test_a_plan_only_run_archives_nothing(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A plan only run never applies, so it uploads no working directory."""
    fake_engine()
    recorder = ApiRecorder()
    bundle = {**bundle_payload(run_role_arn), "plan_only": True}
    clients = make_clients(make_transport(bundle, config_tarball, recorder))

    assert run(make_env("plan"), clients, tmp_path / "run") == 0

    assert "workdir" not in {request["artifact"] for request in recorder.upload_requests}


def test_the_apply_restores_what_the_plan_generated(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """An apply in a fresh run directory finds every file the plan generated, as an
    `archive_file` zip or a module's template must be there when the saved plan applies."""
    fake_engine(plan_writes=GENERATED, apply_reads=GENERATED)
    run_directory = tmp_path / "run"
    plan_recorder = ApiRecorder()
    plan_clients = make_clients(make_transport(bundle_payload(run_role_arn), config_tarball, plan_recorder))
    assert run(make_env("plan"), plan_clients, run_directory) == 0
    shutil.rmtree(run_directory)

    recorder = ApiRecorder()
    bundle = bundle_payload(run_role_arn, plan_get_url=PLAN_URL, workdir_get_url=WORKDIR_URL)
    transport = make_transport(bundle, b"not the config", recorder, workdir_tarball=plan_recorder.uploads[WORKDIR_PATH])

    assert run(make_env("apply"), make_clients(transport), run_directory) == 0

    assert recorder.phase_results[0]["exit_code"] == 0
    assert (run_directory / "config" / workspace.TFVARS_FILENAME).is_file()


def test_an_apply_without_a_workdir_reads_the_config(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """An apply whose plan predates the carry falls back to the uploaded configuration,
    so it fails only where the plan generated a file the apply needs."""
    fake_engine(apply_reads=GENERATED[:1])
    recorder = ApiRecorder()
    bundle = bundle_payload(run_role_arn, plan_get_url=PLAN_URL)
    clients = make_clients(make_transport(bundle, config_tarball, recorder))

    assert run(make_env("apply"), clients, tmp_path / "run") != 0

    assert recorder.phase_results[0]["exit_code"] != 0


def test_a_workdir_that_cannot_be_fetched_fails_by_name(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A missing working directory archive fails the apply as such, not as a config fault."""
    fake_engine()
    recorder = ApiRecorder()
    bundle = bundle_payload(run_role_arn, plan_get_url=PLAN_URL, workdir_get_url=WORKDIR_URL)
    clients = make_clients(make_transport(bundle, config_tarball, recorder))

    assert run(make_env("apply"), clients, tmp_path / "run") != 0

    assert "WorkdirDownloadFailed" in recorder.failure_names()


def test_pack_workdir_keeps_links_as_links(tmp_path: Path) -> None:
    """A module's link is archived as a link and restored inside the directory, never followed."""
    config = tmp_path / "config"
    module = config / ".terraform" / "modules" / "bundle"
    module.mkdir(parents=True)
    (module / "real.txt").write_text("real")
    os.symlink("real.txt", module / "alias.txt")
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    os.symlink(secret, config / "escape.txt")
    destination = tmp_path / "workdir.tar.gz"

    workspace.pack_workdir(config, config, destination, ".terraform")

    with tarfile.open(destination, "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers()}
    assert members[".terraform/modules/bundle/alias.txt"].issym()
    assert members["escape.txt"].issym()
    assert b"outside" not in destination.read_bytes()
    with pytest.raises(workspace.ConfigError):
        workspace.unpack_config(destination, tmp_path / "restored", allow_links=True)


def test_restored_inner_links_unpack(tmp_path: Path) -> None:
    """A link that stays inside the working directory restores when links are allowed,
    and a config upload still refuses it."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "real.txt").write_text("real")
    os.symlink("real.txt", config / "alias.txt")
    destination = tmp_path / "workdir.tar.gz"
    workspace.pack_workdir(config, config, destination, ".terraform")

    restored = workspace.unpack_config(destination, tmp_path / "restored", allow_links=True)

    assert (restored / "alias.txt").read_text() == "real"
    with pytest.raises(workspace.ConfigError):
        workspace.unpack_config(destination, tmp_path / "refused")


def test_pack_workdir_scopes_exclusions_to_the_working_directory(tmp_path: Path) -> None:
    """With a nested working directory the archive keeps the whole configuration root and
    drops the runner's files only where the runner wrote them."""
    config = tmp_path / "config"
    infra = config / "infra"
    (infra / ".terraform" / "providers").mkdir(parents=True)
    (config / "shared").mkdir()
    (config / "shared" / "main.tf").write_text("")
    (infra / "main.tf").write_text("")
    (infra / workspace.TFVARS_FILENAME).write_text("{}")
    (infra / ".terraform" / "providers" / "binary").write_text("")
    (config / workspace.TFVARS_FILENAME).write_text("{}")
    destination = tmp_path / "workdir.tar.gz"

    workspace.pack_workdir(config, infra, destination, ".terraform")

    names = archive_names(destination.read_bytes())
    assert {"shared/main.tf", "infra/main.tf", workspace.TFVARS_FILENAME} <= names
    assert f"infra/{workspace.TFVARS_FILENAME}" not in names
    assert "infra/.terraform/providers/binary" not in names


def template_module(root: Path) -> Path:
    """A one-commit git repository holding a module that renders a template beside it."""
    repository = root / "template-module"
    repository.mkdir()
    (repository / "handler.js.tftpl").write_text("exports.name = '${name}'\n")
    (repository / "main.tf").write_text(
        'output "rendered" {\n  value = templatefile("${path.module}/handler.js.tftpl", { name = "carried" })\n}\n'
    )
    git = ["git", "-c", "user.name=runner", "-c", "user.email=runner@example.test", "-C", str(repository)]
    for arguments in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "module"]):
        subprocess.run([*git, *arguments], check=True)  # noqa: S603
    return repository


@pytest.mark.skipif(
    shutil.which("terraform") is None or shutil.which("git") is None, reason="needs terraform and git binaries"
)
def test_a_saved_plan_applies_in_the_restored_working_directory(tmp_path: Path) -> None:
    """A module's template the plan read through `path.module` is there when the saved
    plan applies in the restored directory, with no module source to fetch again."""
    binary = str(shutil.which("terraform"))
    repository = template_module(tmp_path)
    config = tmp_path / "run" / "config"
    config.mkdir(parents=True)
    (config / "main.tf").write_text(
        f'module "bundle" {{\n  source = "git::file://{repository}"\n}}\n\n'
        'resource "terraform_data" "handler" {\n  input = sha256(module.bundle.rendered)\n}\n'
    )
    environment = build_environment(
        {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
        {"TF_PLUGIN_CACHE_DIR": ""},
        "us-west-2",
        run_phase="plan",
    )

    def terraform(*arguments: str) -> None:
        result = subprocess.run(  # noqa: S603
            [binary, *arguments], cwd=config, env=environment, capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr

    terraform("init", "-input=false")
    terraform("plan", "-input=false", f"-out={PLAN_FILE}")
    plan = tmp_path / PLAN_FILE
    shutil.copy(config / PLAN_FILE, plan)
    archive = workspace.pack_workdir(config, config, tmp_path / "workdir.tar.gz", DATA_DIRECTORY, (PLAN_FILE,))
    shutil.rmtree(tmp_path / "run")
    shutil.rmtree(repository)

    workspace.unpack_config(archive, config, allow_links=True)
    shutil.copy(plan, config / PLAN_FILE)
    terraform("init", "-input=false")
    terraform("apply", "-input=false", PLAN_FILE)

    assert (config / ".terraform" / "modules" / "bundle" / "handler.js.tftpl").is_file()


def test_a_refused_workdir_upload_leaves_the_plan_standing(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A control plane that predates the archive refuses the kind, and the plan still succeeds."""
    fake_engine()
    recorder = ApiRecorder()
    transport = make_transport(
        bundle_payload(run_role_arn), config_tarball, recorder, refused_uploads=frozenset({"workdir"})
    )

    assert run(make_env("plan"), make_clients(transport), tmp_path / "run") == 0

    assert recorder.phase_results[0]["exit_code"] == 0
    assert WORKDIR_PATH not in recorder.uploads
