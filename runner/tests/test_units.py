"""Unit coverage for redaction, the backend override, tfvars and plan JSON parsing."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import boto3
import httpx
import pytest

from app import workspace
from app.api import ApiError, RunnerApi
from app.credentials import CredentialsError, assume_run_role
from app.engine import build_environment, parse_changes
from app.logs import REDACTED, CloudWatchLogSink, Redactor
from app.models import BackendConfig, Bundle, RunRole
from tests.conftest import (
    LOG_GROUP,
    PLAN_JSON_NO_CHANGES,
    PLAN_JSON_WITH_CHANGES,
    SECRET_ENVVAR,
    SECRET_TFVAR,
    WORKSPACE_ID,
    ApiRecorder,
    bundle_payload,
    make_env,
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
    )
    body = workspace.write_backend_override(directory, backend).read_text()
    assert 'bucket       = "webbpulse-terraform-staging-state"' in body
    assert f'key          = "workspaces/{WORKSPACE_ID}/terraform.tfstate"' in body
    assert 'region       = "us-west-2"' in body
    assert 'kms_key_id   = "arn:aws:kms:us-west-2:870550636948:key/abc"' in body
    assert "use_lockfile = true" in body


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
        {"AWS_ACCESS_KEY_ID": "run-role-key", "AWS_SECRET_ACCESS_KEY": "s", "AWS_SESSION_TOKEN": "t"},
        {"PROVIDER_TOKEN": SECRET_ENVVAR},
        "us-west-2",
        tmp_path,
    )
    assert "RUN_TOKEN" not in environment
    assert "TASK_TOKEN" not in environment
    assert "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI" not in environment
    assert environment["AWS_ACCESS_KEY_ID"] == "run-role-key"
    assert environment["PROVIDER_TOKEN"] == SECRET_ENVVAR
    assert environment["HOME"] == "/home/runner"
    assert environment["TF_IN_AUTOMATION"] == "1"
    assert environment["AWS_REGION"] == "us-west-2"


def test_bundle_sensitive_values_cover_variables_and_external_id(run_role_arn: str) -> None:
    """Every variable value and the external id are registered as sensitive."""
    bundle = Bundle.model_validate(bundle_payload(run_role_arn))
    values = bundle.sensitive_values()
    assert SECRET_TFVAR in values
    assert SECRET_ENVVAR in values
    assert WORKSPACE_ID in values


def test_bundle_ignores_the_api_only_top_level_fields(run_role_arn: str) -> None:
    """The runs domain states the phase it served; the runner takes it from PHASE.

    The backend puts `phase`, `plan_only`, `working_directory` and
    `engine_version` on the bundle. Only `engine_version` is modelled here, so
    the other three have to pass through validation rather than reject it.
    """
    payload = bundle_payload(run_role_arn)
    payload |= {"phase": "plan", "plan_only": False, "working_directory": "infra"}
    bundle = Bundle.model_validate(payload)
    assert bundle.engine_version == "1.16.3"
    assert bundle.run_role.duration_seconds == 3600


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


def test_assume_role_passes_the_external_id_and_policy(aws: None, run_role_arn: str) -> None:
    """The run role is assumed with the workspace id as the external id."""
    role = RunRole(
        role_arn=run_role_arn,
        external_id=WORKSPACE_ID,
        session_policy={
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "s3:Get*", "Resource": "*"}],
        },
    )
    exported = assume_run_role(boto3.client("sts", region_name="us-west-2"), role, "run-01JTEST", "plan")
    assert set(exported) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}


def test_assume_role_failure_is_wrapped(aws: None) -> None:
    """A rejected assume role surfaces as a CredentialsError naming the cause.

    The botocore message is carried through because it names the malformed
    field or the denied action and holds no credential material.
    """
    role = RunRole(role_arn="not-an-arn", external_id=WORKSPACE_ID)
    with pytest.raises(CredentialsError):
        assume_run_role(boto3.client("sts", region_name="us-west-2"), role, "run-01JTEST", "plan")


class _RecordingSTS:
    """An STS stand in that records the assume role request and returns credentials."""

    def __init__(self) -> None:
        """Start with no recorded request."""
        self.request: dict[str, object] = {}

    def assume_role(self, **kwargs: object) -> dict[str, dict[str, str]]:
        """Record the request and hand back a fixed credential triple."""
        self.request = kwargs
        return {
            "Credentials": {
                "AccessKeyId": "AKIAEXAMPLE",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
            }
        }


def test_assume_role_passes_the_managed_policy_arns() -> None:
    """A plan role's managed policies reach STS as PolicyArns.

    The plan phase expresses "read everything" with the managed ReadOnlyAccess
    policy, so losing this argument would silently strip a plan's reads.
    """
    client = _RecordingSTS()
    role = RunRole(
        role_arn="arn:aws:iam::870550636948:role/run",
        external_id=WORKSPACE_ID,
        session_policy={"Version": "2012-10-17", "Statement": []},
        session_policy_arns=["arn:aws:iam::aws:policy/ReadOnlyAccess"],
    )
    assume_run_role(client, role, "run-01JTEST", "plan")  # type: ignore[arg-type]
    assert client.request["PolicyArns"] == [{"arn": "arn:aws:iam::aws:policy/ReadOnlyAccess"}]


def test_assume_role_omits_policy_arns_when_there_are_none() -> None:
    """An apply role sends no PolicyArns key at all.

    STS rejects an empty PolicyArns list, so the key has to be absent rather
    than present and empty.
    """
    client = _RecordingSTS()
    role = RunRole(
        role_arn="arn:aws:iam::870550636948:role/run",
        external_id=WORKSPACE_ID,
        session_policy={"Version": "2012-10-17", "Statement": []},
    )
    assume_run_role(client, role, "run-01JTEST", "apply")  # type: ignore[arg-type]
    assert "PolicyArns" not in client.request
    assert "Policy" in client.request


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
