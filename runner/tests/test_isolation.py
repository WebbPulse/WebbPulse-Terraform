"""The engine user split, the sealed plan copy and the refusal of a plan document that is not one."""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path
from typing import Callable

import pytest

from app import isolation
from app.credential_files import CredentialFiles
from app.isolation import EngineUser, IsolationError, engine_user, share
from app.main import PhaseFailure, plan_document, run, seal_plan
from tests.conftest import (
    ApiRecorder,
    bundle_payload,
    make_clients,
    make_env,
    make_transport,
)
from tests.test_credential_files import RUN_KEYS, STATE_KEYS


def current_user() -> EngineUser:
    """The user running the suite, standing in for the engine user."""
    entry = pwd.getpwuid(os.geteuid())
    return EngineUser(name=entry.pw_name, uid=entry.pw_uid, gid=entry.pw_gid, home=entry.pw_dir)


def test_no_engine_user_runs_the_engine_as_the_runner() -> None:
    """Unset, the engine runs as the runner, which only the suite relies on."""
    assert engine_user({}) is None
    assert engine_user({"ENGINE_USER": "  "}) is None


def test_a_missing_engine_user_is_refused() -> None:
    """A name that resolves to no user fails closed."""
    with pytest.raises(IsolationError, match="does not exist"):
        engine_user({"ENGINE_USER": "no-such-user-tf14"})


def test_root_is_refused_as_the_engine_user() -> None:
    """Running the engine as root would undo the split."""
    with pytest.raises(IsolationError, match="must not be root"):
        engine_user({"ENGINE_USER": "root"})


def test_a_runner_that_is_not_root_cannot_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named engine user with an unprivileged runner fails rather than run the engine as the runner."""
    name = "nobody"
    monkeypatch.setattr(isolation.os, "geteuid", lambda: 1000)
    with pytest.raises(IsolationError, match="must start as root"):
        engine_user({"ENGINE_USER": name})


def test_the_engine_user_environment_names_its_home() -> None:
    """Git and the engine write under the engine user's home, not the runner's."""
    user = EngineUser(name="runner", uid=10001, gid=10001, home="/home/runner")
    assert user.environment() == {"HOME": "/home/runner", "USER": "runner", "LOGNAME": "runner"}


def test_seal_plan_copies_a_regular_file(tmp_path: Path) -> None:
    """The sealed copy holds the plan bytes and is readable but not writable by others."""
    source = tmp_path / "plan.tfplan"
    source.write_bytes(b"real-plan")
    destination = tmp_path / "artifacts.tfplan"

    seal_plan(source, destination, current_user())

    assert destination.read_bytes() == b"real-plan"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o644


def test_seal_plan_refuses_a_link(tmp_path: Path) -> None:
    """A plan file swapped for a link is not followed to whatever it names."""
    target = tmp_path / "elsewhere"
    target.write_bytes(b"forged")
    source = tmp_path / "plan.tfplan"
    source.symlink_to(target)

    with pytest.raises(PhaseFailure) as caught:
        seal_plan(source, tmp_path / "sealed.tfplan", None)
    assert caught.value.error == "PlanUnavailable"


def test_seal_plan_refuses_a_file_another_user_owns(tmp_path: Path) -> None:
    """A hard link to a file the engine user does not own is refused."""
    source = tmp_path / "plan.tfplan"
    source.write_bytes(b"real-plan")
    stranger = EngineUser(name="stranger", uid=os.geteuid() + 1, gid=os.getegid(), home="/")

    with pytest.raises(PhaseFailure) as caught:
        seal_plan(source, tmp_path / "sealed.tfplan", stranger)
    assert caught.value.error == "PlanUnavailable"


@pytest.mark.parametrize("output", ["", "not json", "[]", "null", '"text"'])
def test_plan_document_refuses_anything_but_an_object(output: str) -> None:
    """Output that is not one JSON object fails the plan instead of reading as no changes."""
    with pytest.raises(PhaseFailure) as caught:
        plan_document(output)
    assert caught.value.error == "PlanShowFailed"


def test_plan_document_passes_an_object_through() -> None:
    """A real plan document is returned unchanged."""
    assert plan_document('{"resource_changes": []}') == '{"resource_changes": []}'


def test_share_lets_the_group_read_only(tmp_path: Path) -> None:
    """Shared files are group readable, shared directories group traversable, neither group writable."""
    directory = tmp_path / "shared"
    directory.mkdir()
    file = directory / "credentials"
    file.write_text("x")

    share(directory, os.getegid())
    share(file, os.getegid())

    assert stat.S_IMODE(directory.stat().st_mode) == 0o750
    assert stat.S_IMODE(file.stat().st_mode) == 0o640


def test_credential_files_are_shared_with_the_engine_group(tmp_path: Path) -> None:
    """With a group, every credential file the engine reads is group readable and nothing more."""
    files = CredentialFiles(tmp_path / "credentials", group=os.getegid())
    files.write(STATE_KEYS, RUN_KEYS)

    assert stat.S_IMODE((tmp_path / "credentials").stat().st_mode) == 0o750
    for path in (tmp_path / "credentials").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_a_plan_file_left_as_a_link_fails_the_plan(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """A plan that leaves a link where its plan file should be reports no plan at all."""
    fake_engine(plan_link="/etc/hostname")
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)

    assert run(make_env("plan"), make_clients(transport), tmp_path) == 1
    assert recorder.failure_names() == ["PlanUnavailable"]


def test_show_output_that_is_not_a_plan_fails_the_plan(
    aws: None,
    run_role_arn: str,
    config_tarball: bytes,
    fake_engine: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Show output that is not a plan document is a failure, never a plan with no changes."""
    fake_engine(show_output="forged")
    recorder = ApiRecorder()
    transport = make_transport(bundle_payload(run_role_arn), config_tarball, recorder)

    assert run(make_env("plan"), make_clients(transport), tmp_path) == 1
    assert recorder.failure_names() == ["PlanShowFailed"]
