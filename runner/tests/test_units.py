"""Unit coverage for redaction, the backend override, tfvars and plan JSON parsing."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import boto3
import httpx
import pytest

from app import install, workspace
from app.api import ApiError, RunnerApi
from app.engine import build_environment, parse_apply_changes, parse_changes
from app.install import SIGNING_KEYS as PINNED_SIGNING_KEYS
from app.install import InstallError, ReleaseUrls, release_urls
from app.logs import REDACTED, CloudWatchLogSink, Redactor
from app.main import redact_outputs
from app.models import (
    ApiCredentials,
    BackendConfig,
    Bundle,
    Changes,
    Phase,
    PhaseResult,
    VendedCredentials,
    hcl_literal_fragments,
    token_variable,
)
from tests.conftest import (
    LOG_GROUP,
    PLAN_JSON_NO_CHANGES,
    PLAN_JSON_WITH_CHANGES,
    PROVIDER_SECRET_ACCESS_KEY,
    PROVIDER_SESSION_TOKEN,
    RUN_ID,
    SECRET_ENVVAR,
    SECRET_TFVAR,
    STATE_SECRET_ACCESS_KEY,
    STATE_SESSION_TOKEN,
    WORKSPACE_ID,
    ApiRecorder,
    ReleaseSigner,
    bundle_payload,
    make_env,
    make_signer,
    make_transport,
)


def test_redactor_masks_longest_first() -> None:
    """Overlapping secrets are masked longest first so no fragment survives."""
    redactor = Redactor(["abcdefgh", "abcd"])
    assert redactor.scrub("value abcdefgh here") == f"value {REDACTED} here"
    assert redactor.scrub("value abcd here") == f"value {REDACTED} here"


def test_redactor_ignores_short_values() -> None:
    """A value too short to be a secret is left alone rather than shredding the log."""
    redactor = Redactor(["ab", "", None])  # type: ignore[list-item]
    assert redactor.scrub("ab cd") == "ab cd"


def test_backend_override_sets_native_locking() -> None:
    """The backend override carries the bucket, key, region, KMS key and use_lockfile."""
    directory = Path(__import__("tempfile").mkdtemp())
    backend = BackendConfig(
        bucket="webbpulse-terraform-staging-state",
        key=f"workspaces/{WORKSPACE_ID}/terraform.tfstate",
        region="us-west-2",
        kms_key_id="arn:aws:kms:us-west-2:870550636948:key/abc",
        credentials=VendedCredentials(access_key_id="a", secret_access_key="b", session_token="c"),
    )
    body = workspace.write_backend_override(directory, backend).read_text()
    assert 'bucket       = "webbpulse-terraform-staging-state"' in body
    assert f'key          = "workspaces/{WORKSPACE_ID}/terraform.tfstate"' in body
    assert 'region       = "us-west-2"' in body
    assert 'kms_key_id   = "arn:aws:kms:us-west-2:870550636948:key/abc"' in body
    assert "use_lockfile = true" in body
    assert f'workspace_key_prefix = "workspaces/{WORKSPACE_ID}/env"' in body


def test_tfvars_are_written_owner_only(tmp_path: Path) -> None:
    """The tfvars file is JSON and readable only by the runner user."""
    path = workspace.write_tfvars(tmp_path, {"db_password": SECRET_TFVAR, "count": 2})
    assert path is not None
    assert json.loads(path.read_text()) == {"db_password": SECRET_TFVAR, "count": 2}
    assert path.stat().st_mode & 0o077 == 0


def test_no_tfvars_file_without_variables(tmp_path: Path) -> None:
    """No variables means no tfvars file at all."""
    assert workspace.write_tfvars(tmp_path, {}) is None


def test_escaping_tar_member_is_rejected(tmp_path: Path) -> None:
    """A member with a traversing path is refused rather than extracted."""
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("../escaped.tf")
        info.size = 0
        handle.addfile(info, io.BytesIO(b""))
    with pytest.raises(workspace.ConfigError):
        workspace.unpack_config(archive, tmp_path / "work")


def test_unreadable_archive_is_rejected(tmp_path: Path) -> None:
    """A corrupt archive is a config error, not a traceback."""
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(b"not a tarball")
    with pytest.raises(workspace.ConfigError):
        workspace.unpack_config(archive, tmp_path / "work")


def test_working_directory_defaults_to_the_configuration_root(tmp_path: Path) -> None:
    """An empty working directory runs the engine from the tarball root."""
    root = tmp_path / "config"
    root.mkdir()
    assert workspace.resolve_working_directory(root, "") == root
    assert workspace.resolve_working_directory(root, "  ") == root


def test_working_directory_selects_a_subdirectory(tmp_path: Path) -> None:
    """A non empty value points the engine at that directory in the configuration."""
    root = tmp_path / "config"
    (root / "infra" / "prod").mkdir(parents=True)
    assert workspace.resolve_working_directory(root, "infra/prod") == (root / "infra" / "prod").resolve()
    assert workspace.resolve_working_directory(root, "infra/") == (root / "infra").resolve()
    assert workspace.resolve_working_directory(root, "./infra") == (root / "infra").resolve()


def test_working_directory_escaping_the_configuration_is_rejected(tmp_path: Path) -> None:
    """A climbing or absolute value would point the engine at the task filesystem.

    The value comes from the workspace record rather than from the configuration,
    so it is refused rather than normalised into something that happens to exist.
    """
    root = tmp_path / "config"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    for escaping in ("../outside", "infra/../../outside", "/etc", "/infra"):
        with pytest.raises(workspace.ConfigError, match="escapes the configuration"):
            workspace.resolve_working_directory(root, escaping)


def test_working_directory_that_is_absent_is_rejected(tmp_path: Path) -> None:
    """A directory the configuration does not carry is a config error, not a cwd failure."""
    root = tmp_path / "config"
    root.mkdir()
    (root / "main.tf").write_text("")
    with pytest.raises(workspace.ConfigError, match="not in the configuration"):
        workspace.resolve_working_directory(root, "infra")
    with pytest.raises(workspace.ConfigError, match="not in the configuration"):
        workspace.resolve_working_directory(root, "main.tf")


def test_prepare_writes_the_files_into_the_working_directory(
    tmp_path: Path, run_role_arn: str, config_tarball: bytes
) -> None:
    """The override and the tfvars land where the engine runs, not at the root.

    Neither file is loaded from a parent directory, so writing them at the root
    while running the engine in a subdirectory would silently drop the backend.
    """
    archive = tmp_path / "config.tar.gz"
    archive.write_bytes(config_tarball)
    payload = bundle_payload(run_role_arn) | {"working_directory": "infra"}
    bundle = Bundle.model_validate(payload)
    root = tmp_path / "config"
    root.mkdir()
    (root / "infra").mkdir()

    target = workspace.prepare(root, bundle, archive)

    assert target == (root / "infra").resolve()
    assert (target / workspace.BACKEND_FILENAME).exists()
    assert (target / workspace.TFVARS_FILENAME).exists()
    assert not (root / workspace.BACKEND_FILENAME).exists()


def test_parse_changes_counts_each_action() -> None:
    """Creates, updates, deletes and replacements are counted; no-op and read are not."""
    changes, has_changes = parse_changes(json.dumps(PLAN_JSON_WITH_CHANGES))
    assert changes.add == 2
    assert changes.change == 1
    assert changes.destroy == 2
    assert has_changes is True


def test_parse_changes_on_an_empty_plan() -> None:
    """An empty resource_changes list is no changes."""
    changes, has_changes = parse_changes(json.dumps(PLAN_JSON_NO_CHANGES))
    assert changes.model_dump() == {"add": 0, "change": 0, "destroy": 0}
    assert has_changes is False


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"resource_changes": "nope"}'])
def test_parse_changes_tolerates_bad_json(payload: str) -> None:
    """Unparseable plan JSON yields zero counts rather than raising."""
    changes, has_changes = parse_changes(payload)
    assert changes.model_dump() == {"add": 0, "change": 0, "destroy": 0}
    assert has_changes is False


def test_build_environment_drops_the_runner_tokens(tmp_path: Path) -> None:
    """The engine never inherits the run token, task token or the task role credentials."""
    base = {
        "RUN_TOKEN": "run-token-value-aaaa",
        "TASK_TOKEN": "task-token-value-bbbb",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI": "/v2/credentials/xyz",
        "AWS_ACCESS_KEY_ID": "task-role-key",
        "HOME": "/home/runner",
    }
    environment = build_environment(
        base,
        {"PROVIDER_TOKEN": SECRET_ENVVAR},
        "us-west-2",
        {"AWS_PROFILE": "webbpulse-run"},
        run_phase="plan",
    )
    assert "RUN_TOKEN" not in environment
    assert "TASK_TOKEN" not in environment
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in environment
    assert "AWS_ACCESS_KEY_ID" not in environment
    assert environment["AWS_PROFILE"] == "webbpulse-run"
    assert environment["PROVIDER_TOKEN"] == SECRET_ENVVAR
    assert environment["HOME"] == "/home/runner"
    assert environment["TF_IN_AUTOMATION"] == "1"
    assert environment["AWS_REGION"] == "us-west-2"


@pytest.mark.parametrize("phase", ["plan", "apply"])
def test_build_environment_exports_the_run_phase_over_a_workspace_variable(tmp_path: Path, phase: Phase) -> None:
    """A configuration reads the phase as `var.webbpulse_run_phase`, and no workspace variable can set it."""
    environment = build_environment(
        {"TF_VAR_webbpulse_run_phase": "apply"},
        {"TF_VAR_webbpulse_run_phase": "apply"},
        "us-west-2",
        run_phase=phase,
    )
    assert environment["TF_VAR_webbpulse_run_phase"] == phase


PHASE_SELECTOR_CONFIG = """
variable "webbpulse_run_phase" {
  type      = string
  default   = "apply"
  ephemeral = true
}

check "phase" {
  assert {
    condition     = var.webbpulse_run_phase == "apply"
    error_message = "selected the reader"
  }
}
"""


@pytest.mark.skipif(shutil.which("terraform") is None, reason="needs a terraform binary")
def test_an_ephemeral_phase_variable_selects_the_writer_when_the_saved_plan_is_applied(tmp_path: Path) -> None:
    """The plan reads `plan` and the apply of that saved plan reads `apply`, since an ephemeral value is not saved."""
    (tmp_path / "main.tf").write_text(PHASE_SELECTOR_CONFIG)
    binary = str(shutil.which("terraform"))

    def engine(phase: Phase, *arguments: str) -> subprocess.CompletedProcess[str]:
        """Run the engine with the environment the runner builds for `phase`."""
        environment = build_environment(
            {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            {"TF_PLUGIN_CACHE_DIR": ""},
            "us-west-2",
            run_phase=phase,
        )
        return subprocess.run(  # noqa: S603
            [binary, *arguments], cwd=tmp_path, env=environment, capture_output=True, text=True, check=False
        )

    assert engine("plan", "init", "-input=false").returncode == 0
    planned = engine("plan", "plan", "-input=false", "-out=plan.tfplan")
    assert planned.returncode == 0, planned.stderr
    assert "selected the reader" in planned.stdout + planned.stderr
    applied = engine("apply", "apply", "-input=false", "plan.tfplan")
    assert applied.returncode == 0, applied.stderr
    assert "selected the reader" not in applied.stdout + applied.stderr


def test_build_environment_uses_a_relative_data_directory() -> None:
    """`TF_DATA_DIR` is relative to the working directory, whatever the runner's own environment says."""
    environment = build_environment({"TF_DATA_DIR": "/tmp/elsewhere"}, {}, "us-west-2", run_phase="plan")
    assert environment["TF_DATA_DIR"] == ".terraform"


def module_repository(root: Path) -> Path:
    """A one-commit git repository holding a module that outputs its own `path.module`."""
    repository = root / "module"
    repository.mkdir()
    (repository / "main.tf").write_text('output "path" {\n  value = path.module\n}\n')
    git = ["git", "-c", "user.name=runner", "-c", "user.email=runner@example.test", "-C", str(repository)]
    for arguments in (["init", "-q"], ["add", "main.tf"], ["commit", "-q", "-m", "module"]):
        subprocess.run([*git, *arguments], check=True)  # noqa: S603
    return repository


@pytest.mark.skipif(
    shutil.which("terraform") is None or shutil.which("git") is None, reason="needs terraform and git binaries"
)
def test_installed_module_paths_match_across_run_directories(tmp_path: Path) -> None:
    """Installed modules keep a relative `path.module` in every run directory, so `filename` arguments never diff."""
    binary = str(shutil.which("terraform"))
    repository = module_repository(tmp_path)
    config = (
        f'module "child" {{\n  source = "git::file://{repository}"\n}}\n\n'
        'output "module_path" {\n  value = module.child.path\n}\n'
    )
    paths = []
    for name in ("webbpulse-run-first", "webbpulse-run-second"):
        directory = tmp_path / name / "config"
        directory.mkdir(parents=True)
        (directory / "main.tf").write_text(config)
        environment = build_environment(
            {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path)},
            {"TF_PLUGIN_CACHE_DIR": ""},
            "us-west-2",
            run_phase="plan",
        )
        for arguments in (["init", "-input=false"], ["apply", "-input=false", "-auto-approve"]):
            result = subprocess.run(  # noqa: S603
                [binary, *arguments], cwd=directory, env=environment, capture_output=True, text=True, check=False
            )
            assert result.returncode == 0, result.stderr
        output = subprocess.run(  # noqa: S603
            [binary, "output", "-raw", "module_path"],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        paths.append(output.stdout)
    assert paths[0] == paths[1] == ".terraform/modules/child"


def test_bundle_sensitive_values_cover_variables_and_vended_keys(run_role_arn: str) -> None:
    """Every variable value and both vended sessions' secrets are registered as sensitive."""
    bundle = Bundle.model_validate(bundle_payload(run_role_arn))
    values = bundle.sensitive_values()
    assert SECRET_TFVAR in values
    assert SECRET_ENVVAR in values
    for secret in (PROVIDER_SECRET_ACCESS_KEY, PROVIDER_SESSION_TOKEN, STATE_SECRET_ACCESS_KEY, STATE_SESSION_TOKEN):
        assert secret in values


def test_bundle_ignores_the_api_only_top_level_fields(run_role_arn: str) -> None:
    """The runs domain states the phase it served; the runner takes it from PHASE.

    The backend puts `phase`, `plan_only`, `working_directory` and
    `engine_version` on the bundle. Only `engine_version` is modelled here, so
    the other three have to pass through validation rather than reject it.
    """
    payload = bundle_payload(run_role_arn)
    payload |= {"phase": "plan", "plan_only": False, "working_directory": "infra"}
    bundle = Bundle.model_validate(payload)
    assert bundle.engine_version == "1.16.4"
    assert bundle.run_role_arn == run_role_arn
    assert bundle.is_destroy is False


def test_log_sink_creates_the_stream_and_redacts(aws: None, capsys: pytest.CaptureFixture[str]) -> None:
    """The sink creates `<run_id>/<phase>`, masks secrets and mirrors to stdout."""
    redactor = Redactor(["secret-value-here"])
    stream = "run-01JTEST/plan"
    with CloudWatchLogSink(boto3.client("logs", region_name="us-west-2"), LOG_GROUP, stream, redactor) as sink:
        sink.write("token is secret-value-here\n")
        sink.write("")
    client = boto3.client("logs", region_name="us-west-2")
    events = client.get_log_events(logGroupName=LOG_GROUP, logStreamName=stream, startFromHead=True)["events"]
    assert events[0]["message"] == f"token is {REDACTED}"
    assert "secret-value-here" not in capsys.readouterr().out
    assert sink.text().startswith(f"token is {REDACTED}")


def test_log_sink_swallows_delivery_failures(aws: None) -> None:
    """A missing log group loses the batch rather than failing the phase."""
    sink = CloudWatchLogSink(
        boto3.client("logs", region_name="us-west-2"),
        "/webbpulse-terraform/absent/runner",
        "run-x/plan",
        Redactor(),
    )
    sink.write("a line")
    sink.flush()
    assert sink.lines == ["a line"]


def test_upload_requests_the_url_for_the_exact_size(aws: None, config_tarball: bytes) -> None:
    """The client declares the real byte count and PUTs with the returned headers.

    `Content-Length` is inside the presigned URL's signature, so a runner that
    declared a ceiling and sent a shorter body is refused by S3. Asking per
    artifact, after the bytes exist, is what makes the PUT match the signature.
    """
    recorder = ApiRecorder()
    api = RunnerApi(make_env("plan"), httpx.Client(transport=make_transport(None, config_tarball, recorder)))

    assert api.upload_text("log", "a transcript") is True

    assert recorder.upload_requests == [{"artifact": "log", "size_bytes": len(b"a transcript")}]
    headers = recorder.upload_headers["/runs/plan.log"]
    assert headers["content-type"] == "text/plain"
    assert headers["content-length"] == str(len(b"a transcript"))
    assert recorder.uploads["/runs/plan.log"] == b"a transcript"


def test_upload_sends_only_the_signed_headers(aws: None, config_tarball: bytes) -> None:
    """The signed headers go over verbatim, with no content type of the client's own."""
    recorder = ApiRecorder()
    api = RunnerApi(make_env("plan"), httpx.Client(transport=make_transport(None, config_tarball, recorder)))

    body = b'{"format_version": "1.2"}'
    api.upload_artifact("plan_json", body)

    headers = recorder.upload_headers["/runs/plan.json"]
    assert headers["content-type"] == "application/json"
    assert headers["content-length"] == str(len(body))


def test_upload_of_an_absent_file_is_not_an_error(aws: None, config_tarball: bytes, tmp_path: Path) -> None:
    """A missing artifact is nothing to send rather than a failure."""
    recorder = ApiRecorder()
    api = RunnerApi(make_env("plan"), httpx.Client(transport=make_transport(None, config_tarball, recorder)))

    assert api.upload_file("plan", tmp_path / "never-written.tfplan") is False
    assert recorder.upload_requests == []


def test_a_clean_phase_result_carries_no_error_key(aws: None, config_tarball: bytes) -> None:
    """A `None` error is dropped from the payload rather than posted as null.

    The API's phase result schema reads an absent error as the empty default, so
    omitting the key is what a successful phase reports. Sending null was a 422,
    which turned every successful plan into an errored run.
    """
    recorder = ApiRecorder()
    api = RunnerApi(make_env("plan"), httpx.Client(transport=make_transport(None, config_tarball, recorder)))

    api.post_phase_result(
        PhaseResult(
            run_id=RUN_ID,
            phase="plan",
            exit_code=0,
            changes=Changes(add=1),
            has_changes=True,
        )
    )

    assert len(recorder.phase_results) == 1
    posted = recorder.phase_results[0]
    assert "error" not in posted
    assert posted["exit_code"] == 0
    assert posted["changes"] == {"add": 1, "change": 0, "destroy": 0}


def test_a_failed_phase_result_still_carries_its_error(aws: None, config_tarball: bytes) -> None:
    """A real failure text is posted, since only `None` is dropped."""
    recorder = ApiRecorder()
    api = RunnerApi(make_env("plan"), httpx.Client(transport=make_transport(None, config_tarball, recorder)))

    api.post_phase_result(PhaseResult(run_id=RUN_ID, phase="plan", exit_code=1, error="boom"))

    assert recorder.phase_results[0]["error"] == "boom"


def test_a_refused_upload_request_is_an_api_error(aws: None, config_tarball: bytes) -> None:
    """A rejected mint raises rather than uploading to nowhere."""
    recorder = ApiRecorder()
    transport = make_transport(None, config_tarball, recorder, upload_request_status=413)
    api = RunnerApi(make_env("plan"), httpx.Client(transport=transport))

    with pytest.raises(ApiError, match="413"):
        api.upload_text("log", "far too many bytes")


def test_a_refused_put_is_an_api_error(aws: None, config_tarball: bytes) -> None:
    """S3 refusing the PUT itself surfaces as an `ApiError` carrying the status."""
    recorder = ApiRecorder()
    transport = make_transport(None, config_tarball, recorder, upload_status=403)
    api = RunnerApi(make_env("plan"), httpx.Client(transport=transport))

    with pytest.raises(ApiError, match="403"):
        api.upload_text("log", "a transcript")


def test_hcl_tfvars_are_written_unquoted(tmp_path: Path) -> None:
    """An HCL expression reaches the native tfvars file exactly as it was stored.

    Quoting it would turn a list into an eight character string and hand a
    `list(string)` input variable the wrong type without anything failing.
    """
    path = workspace.write_hcl_tfvars(tmp_path, {"subnets": '["a", "b"]', "count": "2"})
    assert path is not None
    assert path.name == workspace.HCL_TFVARS_FILENAME
    body = path.read_text()
    assert 'subnets = (\n["a", "b"]\n)' in body
    assert "count = (\n2\n)" in body
    assert '"["a", "b"]"' not in body
    assert path.stat().st_mode & 0o777 == 0o600


def test_hcl_tfvars_keep_a_multi_line_expression_on_one_assignment(tmp_path: Path) -> None:
    """A map or heredoc spans lines without running into the next assignment."""
    expression = '{\n  env  = "staging"\n  size = 2\n}'
    path = workspace.write_hcl_tfvars(tmp_path, {"a": expression, "settings": expression})
    assert path is not None
    body = path.read_text()
    assert f"settings = (\n{expression}\n)" in body
    assert body.endswith("\n")


def test_no_hcl_tfvars_file_without_hcl_variables(tmp_path: Path) -> None:
    """A workspace with only literal variables gets no native tfvars file at all."""
    assert workspace.write_hcl_tfvars(tmp_path, {}) is None


def test_hcl_tfvars_refuse_a_name_that_is_not_an_identifier(tmp_path: Path) -> None:
    """A name is written unquoted, so anything but an identifier is refused."""
    with pytest.raises(workspace.ConfigError, match="valid HCL identifier"):
        workspace.write_hcl_tfvars(tmp_path, {'a" = "b\nevil': '"x"'})


def test_a_literal_with_hcl_punctuation_stays_literal(tmp_path: Path) -> None:
    """A literal goes to the JSON file, where its punctuation cannot be reread.

    JSON is what makes this safe: the value is the JSON string it is, so quotes,
    braces and `${` in it are characters rather than syntax.
    """
    value = '${var.nope} "quoted" {braces} ["a"]'
    path = workspace.write_tfvars(tmp_path, {"literal": value})
    assert path is not None
    assert json.loads(path.read_text()) == {"literal": value}


def test_prepare_writes_both_variable_files(tmp_path: Path, run_role_arn: str, config_tarball: bytes) -> None:
    """Literal and HCL variables land in their own files in the working directory."""
    archive = tmp_path / "config.tar.gz"
    archive.write_bytes(config_tarball)
    payload = bundle_payload(run_role_arn) | {"hcl_variables": {"subnets": '["a", "b"]'}}
    bundle = Bundle.model_validate(payload)
    root = tmp_path / "config"
    root.mkdir()

    target = workspace.prepare(root, bundle, archive)

    assert (target / workspace.TFVARS_FILENAME).exists()
    assert 'subnets = (\n["a", "b"]\n)' in (target / workspace.HCL_TFVARS_FILENAME).read_text()


@pytest.mark.parametrize(
    "expression",
    [
        '# leading comment\n["a", "b"]',
        '["a", "b"] // trailing comment',
        'true ?\n["a"] :\n["b"]',
        "<<-END-TEXT\n  body\n  END-TEXT",
        '"$${unfinished"',
        '"%%{unfinished"',
    ],
)
def test_hcl_file_parses_with_terraform(tmp_path: Path, expression: str) -> None:
    """The real parser accepts grouped values beside a second assignment."""
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Terraform is not installed")
    path = workspace.write_hcl_tfvars(tmp_path, {"example": expression, "neighbor": "true"})
    assert path is not None
    result = subprocess.run(
        [terraform, "fmt", "-write=false", "-list=false", str(path)],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0


def test_prepare_writes_no_hcl_file_for_a_bundle_without_the_field(
    tmp_path: Path, run_role_arn: str, config_tarball: bytes
) -> None:
    """A bundle from a control plane that predates the flag is unchanged."""
    archive = tmp_path / "config.tar.gz"
    archive.write_bytes(config_tarball)
    bundle = Bundle.model_validate(bundle_payload(run_role_arn))
    root = tmp_path / "config"
    root.mkdir()

    target = workspace.prepare(root, bundle, archive)

    assert bundle.hcl_variables == {}
    assert not (target / workspace.HCL_TFVARS_FILENAME).exists()


def test_bundle_sensitive_values_cover_hcl_variables(run_role_arn: str) -> None:
    """A sensitive HCL expression is registered for redaction like any other value.

    The expression itself is the secret when the variable is sensitive, so it is
    the expression that has to be masked before any line is emitted.
    """
    payload = bundle_payload(run_role_arn) | {"hcl_variables": {"secrets": f'["{SECRET_TFVAR}"]'}}
    bundle = Bundle.model_validate(payload)
    assert f'["{SECRET_TFVAR}"]' in bundle.sensitive_values()

    redactor = Redactor(bundle.sensitive_values())
    assert SECRET_TFVAR not in redactor.scrub(f'subnets = ["{SECRET_TFVAR}"]')


def test_a_sensitive_hcl_member_is_masked_when_printed_alone(run_role_arn: str) -> None:
    """The engine prints a list member or map value on its own line, so each is masked."""
    payload = bundle_payload(run_role_arn) | {
        "hcl_variables": {"secrets": f'{{ token = "{SECRET_TFVAR}", other = "second-secret" }}'}
    }
    redactor = Redactor(Bundle.model_validate(payload).sensitive_values())
    assert SECRET_TFVAR not in redactor.scrub(f'      + token = "{SECRET_TFVAR}"')
    assert "second-secret" not in redactor.scrub("second-secret")


def test_escaped_quoted_literals_are_masked_as_the_engine_prints_them() -> None:
    """A string with escapes is registered both raw and decoded."""
    fragments = hcl_literal_fragments('["tab\\there", "quote\\"d"]')
    assert "tab\\there" in fragments
    assert "tab\there" in fragments
    assert 'quote"d' in fragments


def test_heredoc_bodies_and_lines_are_masked() -> None:
    """A heredoc body is registered whole and line by line, trimmed of indentation."""
    fragments = hcl_literal_fragments("<<-EOT\n  first-line\n  second-line\n  EOT")
    assert "first-line" in fragments
    assert "second-line" in fragments
    assert "  first-line\n  second-line" in fragments


@pytest.mark.parametrize(
    "expression",
    ["1", "true", '"${var.x}"', '"\\u12"', "<<EOT\nno end", '"unterminated', '"\\', "<<-\n"],
)
def test_fragment_extraction_never_refuses_a_value(expression: str) -> None:
    """Extraction is best effort: a short, dynamic or odd expression still yields itself."""
    assert expression in hcl_literal_fragments(expression)


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Apply complete! Resources: 1 added, 2 changed, 3 destroyed."], Changes(add=1, change=2, destroy=3)),
        (["Apply complete! Resources: 4 imported, 1 added, 0 changed, 0 destroyed."], Changes(add=1)),
        (["Destroy complete! Resources: 5 destroyed."], Changes(destroy=5)),
        (["Apply complete! Resources: 1 added, 0 changed, 0 destroyed.", "noise"], Changes(add=1)),
        (["no summary here"], None),
    ],
)
def test_apply_counts_come_from_the_engine_summary(lines: list[str], expected: Changes | None) -> None:
    """The closing summary line is the apply's own record of what it changed."""
    assert parse_apply_changes(lines) == expected


def test_redact_outputs_drops_sensitive_values() -> None:
    """A sensitive output keeps its name and type but loses its value."""
    raw = json.dumps(
        {
            "name": {"sensitive": False, "type": "string", "value": "plain"},
            "token": {"sensitive": True, "type": "string", "value": "hidden"},
        }
    )
    redacted = json.loads(redact_outputs(raw) or "{}")
    assert redacted["name"]["value"] == "plain"
    assert redacted["token"] == {"sensitive": True, "type": "string", "value": None}
    assert redact_outputs("not json") is None
    assert redact_outputs("[]") is None


def test_release_urls_follow_each_projects_layout() -> None:
    """Terraform and OpenTofu publish their archives, sums and signatures under different paths."""
    assert release_urls("terraform", "1.11.0", "arm64") == ReleaseUrls(
        "https://releases.hashicorp.com/terraform/1.11.0/terraform_1.11.0_linux_arm64.zip",
        "https://releases.hashicorp.com/terraform/1.11.0/terraform_1.11.0_SHA256SUMS",
        "https://releases.hashicorp.com/terraform/1.11.0/terraform_1.11.0_SHA256SUMS.sig",
        "terraform_1.11.0_linux_arm64.zip",
    )
    assert release_urls("tofu", "1.9.0", "amd64") == ReleaseUrls(
        "https://github.com/opentofu/opentofu/releases/download/v1.9.0/tofu_1.9.0_linux_amd64.zip",
        "https://github.com/opentofu/opentofu/releases/download/v1.9.0/tofu_1.9.0_SHA256SUMS",
        "https://github.com/opentofu/opentofu/releases/download/v1.9.0/tofu_1.9.0_SHA256SUMS.gpgsig",
        "tofu_1.9.0_linux_amd64.zip",
    )


def test_the_pinned_release_keys_match_the_image_build() -> None:
    """The fingerprints the runner requires are the ones the Dockerfile checks the baked keys against."""
    pinned = dict(
        line.split("=", 1)
        for line in (Path(__file__).parent.parent / "versions.env").read_text().splitlines()
        if "=" in line
    )
    assert pinned["TERRAFORM_KEY_FINGERPRINT"] == PINNED_SIGNING_KEYS["terraform"]
    assert pinned["TOFU_KEY_FINGERPRINT"] == PINNED_SIGNING_KEYS["tofu"]


def test_a_good_signature_from_the_pinned_key_verifies(release_signer: ReleaseSigner) -> None:
    """A SUMS file signed by the pinned key passes."""
    sums = b"abc  terraform_1.11.0_linux_arm64.zip\n"
    install.verify_signature("terraform", sums, release_signer.sign(sums))


def test_a_signature_over_other_sums_is_refused(release_signer: ReleaseSigner) -> None:
    """A signature made over different bytes does not vouch for these ones."""
    signature = release_signer.sign(b"abc  terraform_1.11.0_linux_arm64.zip\n")
    with pytest.raises(InstallError, match="not signed by the pinned release key"):
        install.verify_signature("terraform", b"def  terraform_1.11.0_linux_arm64.zip\n", signature)


def test_a_signature_from_another_key_is_refused(tmp_path: Path) -> None:
    """A key the keyring does not hold cannot sign a release."""
    impostor = make_signer(tmp_path / "impostor")
    sums = b"abc  tofu_1.9.0_linux_arm64.zip\n"
    with pytest.raises(InstallError, match="not signed by the pinned release key"):
        install.verify_signature("tofu", sums, impostor.sign(sums))


def test_a_keyring_key_that_is_not_the_pinned_one_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, release_signer: ReleaseSigner
) -> None:
    """A swapped keyring fails the fingerprint pin even when its key made the signature."""
    impostor = make_signer(tmp_path / "impostor")
    keyrings = tmp_path / "keyrings"
    keyrings.mkdir()
    impostor.export(keyrings / "terraform.gpg")
    monkeypatch.setattr(install, "KEYRING_DIRECTORY", keyrings)
    sums = b"abc  terraform_1.11.0_linux_arm64.zip\n"
    with pytest.raises(InstallError, match="not signed by the pinned release key"):
        install.verify_signature("terraform", sums, impostor.sign(sums))
    assert release_signer.fingerprint != impostor.fingerprint


def test_garbage_in_place_of_a_signature_is_refused() -> None:
    """Bytes that are not an OpenPGP signature fail rather than pass."""
    with pytest.raises(InstallError, match="not signed by the pinned release key"):
        install.verify_signature("terraform", b"abc\n", b"<html>not found</html>")


def test_a_missing_keyring_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An image without the baked key installs nothing."""
    monkeypatch.setattr(install, "KEYRING_DIRECTORY", tmp_path / "absent")
    with pytest.raises(InstallError, match="no release key is baked"):
        install.verify_signature("terraform", b"abc\n", b"sig")


@pytest.mark.parametrize(
    ("host", "variable"),
    [
        ("terraform.webbpulse.com", "TF_TOKEN_terraform_webbpulse_com"),
        ("staging.terraform-e2e.webbpulse.com", "TF_TOKEN_staging_terraform__e2e_webbpulse_com"),
    ],
)
def test_token_variables_follow_the_engines_host_encoding(host: str, variable: str) -> None:
    """Dots become underscores and hyphens double underscores."""
    assert token_variable(host) == variable


def test_the_registry_token_is_a_sensitive_value(run_role_arn: str) -> None:
    """The redactor holds the registry token like any other secret in the bundle."""
    bundle = Bundle.model_validate(
        bundle_payload(run_role_arn) | {"registry": {"hosts": ["terraform.webbpulse.com"], "token": "wpk_abcdefghijkl"}}
    )
    assert "wpk_abcdefghijkl" in bundle.sensitive_values()
    assert bundle.init_environment() == {"TF_TOKEN_terraform_webbpulse_com": "wpk_abcdefghijkl"}


def test_the_api_token_and_gate_value_are_sensitive_values(run_role_arn: str) -> None:
    """The redactor holds the run's API token and the gate value; the host is not a secret."""
    bundle = Bundle.model_validate(
        bundle_payload(run_role_arn)
        | {"api": {"host": "https://api.example.test", "token": "wpk_apitokenabcdef", "origin_verify": "gate-abcdef"}}
    )
    values = bundle.sensitive_values()
    assert "wpk_apitokenabcdef" in values
    assert "gate-abcdef" in values
    assert "https://api.example.test" not in values


def test_the_api_environment_leaves_out_an_absent_gate_value() -> None:
    """Without the access gate the provider gets only the host and the token."""
    credentials = ApiCredentials(host="https://api.example.test", token="wpk_x")
    assert credentials.environment() == {"WEBBPULSE_TF_HOST": "https://api.example.test", "WEBBPULSE_TF_TOKEN": "wpk_x"}
