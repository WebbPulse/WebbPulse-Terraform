"""Google and Azure workload identity: tokens the control plane mints and its issuer vouches for.

A token is only useful if it verifies against the JWKS the issuer publishes, so
the tests sign with a KMS key and verify against the JWKS the issuer's own
handler builds from that key, loaded from the terraform directory it deploys from.
"""

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import boto3
import jwt
import pytest

from app.common.composition import settings as settings_module
from app.domains.runs import phase_tasks, vending
from app.domains.runs import service as runs_service
from tests.conftest import REGION
from tests.domains.runs.test_runner_routes import BASE, TASK_TOKEN, RecordingStepFunctions, RecordingSTS

ISSUER = "https://oidc.example.test"
PROVIDER = "projects/123/locations/global/workloadIdentityPools/runs/providers/control-plane"
HANDLER = Path(__file__).resolve().parents[4] / "terraform" / "oidc_issuer" / "handler.py"


def _issuer_handler() -> ModuleType:
    """The issuer Lambda's handler, loaded by path since it deploys as a lone file."""
    spec = importlib.util.spec_from_file_location("oidc_issuer_handler", HANDLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _published_jwks(kms: Any, key_arns: list[str]) -> dict[str, Any]:
    """The JWKS the issuer serves when Terraform hands it the public keys of `key_arns`."""
    keys = [
        {"kid": arn, "der": base64.b64encode(kms.get_public_key(KeyId=arn)["PublicKey"]).decode()} for arn in key_arns
    ]
    return _issuer_handler().build_jwks(keys)


@pytest.fixture
def sts_requests(monkeypatch) -> list[dict[str, Any]]:
    """The AssumeRole requests vending makes, answered without AWS."""
    requests: list[dict[str, Any]] = []
    monkeypatch.setattr(vending, "_sts", lambda settings, credentials=None: RecordingSTS(requests))
    return requests


@pytest.fixture
def stepfunctions(monkeypatch) -> RecordingStepFunctions:
    """The phase's task token resolves, so a refresh finds its live runner task."""
    recorder = RecordingStepFunctions()
    monkeypatch.setattr(phase_tasks, "phase_token", lambda run, phase, *, settings: TASK_TOKEN)
    monkeypatch.setattr(phase_tasks, "_stepfunctions", lambda settings: recorder)
    return recorder


def _configure(settings, monkeypatch, **values: str) -> None:
    """Set issuer settings on this test's settings and on those a route resolves afresh."""
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
        monkeypatch.setenv(name, value)
    settings_module.reset_settings_cache()


@pytest.fixture
def signing_key(settings, monkeypatch) -> str:
    """An RS256 KMS signing key, configured as this deployment's issuer key."""
    kms = boto3.client("kms", region_name=REGION)
    key_arn = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")["KeyMetadata"]["Arn"]
    _configure(settings, monkeypatch, OIDC_ISSUER_URL=ISSUER, OIDC_SIGNING_KEY_ARN=key_arn)
    return key_arn


def _verify(token: str, key_arn: str, audience: str) -> dict[str, Any]:
    """The claims of `token`, verified against the JWKS the issuer publishes for `key_arn`."""
    jwks = _published_jwks(boto3.client("kms", region_name=REGION), [key_arn])
    header = jwt.get_unverified_header(token)
    (published,) = [key for key in jwks["keys"] if key["kid"] == header["kid"]]
    public_key = jwt.PyJWK.from_dict(published).key
    return jwt.decode(token, key=public_key, algorithms=["RS256"], audience=audience, issuer=ISSUER)


def _set(auth_client, workspace_id: str, key: str, value: str) -> None:
    """Set one env variable on the workspace through the API."""
    response = auth_client.put(
        f"/api/v1/workspaces/{workspace_id}/variables/{key}",
        json={"value": value, "category": "env", "sensitive": False},
    )
    assert response.status_code in (200, 201), response.text


def _ask_for_both(auth_client, workspace_id: str) -> None:
    """Ask for Google and Azure workload identity the way an HCP workspace does."""
    _set(auth_client, workspace_id, "TFC_GCP_PROVIDER_AUTH", "true")
    _set(auth_client, workspace_id, "TFC_GCP_WORKLOAD_PROVIDER_NAME", PROVIDER)
    _set(auth_client, workspace_id, "TFC_GCP_RUN_SERVICE_ACCOUNT_EMAIL", "runs@example.iam.gserviceaccount.com")
    _set(auth_client, workspace_id, "TFC_AZURE_PROVIDER_AUTH", "true")
    _set(auth_client, workspace_id, "TFC_AZURE_RUN_CLIENT_ID", "00000000-0000-0000-0000-000000000001")


def _mint(environment: dict[str, str], settings, phase: str = "plan", now: int | None = None):
    """Mint for a fixed workspace and run."""
    return vending.mint_workload_identity(
        environment=environment,
        workspace_id="ws-abc",
        workspace_name="network",
        run_id="run-xyz",
        phase=phase,  # type: ignore[arg-type]
        settings=settings,
        now=now,
    )


def test_a_minted_token_verifies_against_the_published_jwks(signing_key, settings):
    """The whole point: the issuer's JWKS vouches for what the runs function signs."""
    minted = _mint(
        {"TFC_GCP_PROVIDER_AUTH": "true", "TFC_GCP_WORKLOAD_PROVIDER_NAME": PROVIDER},
        settings,
    )
    assert minted is not None
    claims = _verify(minted["gcp"]["token"], signing_key, f"//iam.googleapis.com/{PROVIDER}")

    assert claims["sub"] == "workspace:ws-abc:run_phase:plan"
    assert claims["terraform_workspace_id"] == "ws-abc"
    assert claims["terraform_workspace_name"] == "network"
    assert claims["terraform_run_id"] == "run-xyz"
    assert claims["terraform_run_phase"] == "plan"
    assert claims["nbf"] == claims["iat"]
    assert 0 < claims["exp"] - claims["iat"] <= 3600


def test_the_kid_is_the_signing_key_id(signing_key, settings):
    """The issuer publishes each key under its KMS key id, and the header names the same."""
    minted = _mint({"TFC_AZURE_PROVIDER_AUTH": "true", "TFC_AZURE_RUN_CLIENT_ID": "client"}, settings)
    assert minted is not None
    assert jwt.get_unverified_header(minted["azure"]["token"])["kid"] == signing_key.rsplit("/", 1)[-1]


def test_a_token_signed_by_another_key_does_not_verify(signing_key, settings):
    """A JWKS that does not publish the signer refuses its tokens."""
    minted = _mint({"TFC_AZURE_PROVIDER_AUTH": "true", "TFC_AZURE_RUN_CLIENT_ID": "client"}, settings)
    assert minted is not None
    other = boto3.client("kms", region_name=REGION).create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")
    jwks = _published_jwks(boto3.client("kms", region_name=REGION), [other["KeyMetadata"]["Arn"]])
    public_key = jwt.PyJWK.from_dict({**jwks["keys"][0], "kid": signing_key.rsplit("/", 1)[-1]}).key

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            minted["azure"]["token"], key=public_key, algorithms=["RS256"], audience="api://AzureADTokenExchange"
        )


def test_the_jwks_publishes_every_generation_newest_first(signing_key):
    """A rotation publishes the new key before it signs and keeps the old one until its tokens expire."""
    kms = boto3.client("kms", region_name=REGION)
    newer = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")["KeyMetadata"]["Arn"]
    jwks = _published_jwks(kms, [newer, signing_key])

    assert [key["kid"] for key in jwks["keys"]] == [newer.rsplit("/", 1)[-1], signing_key.rsplit("/", 1)[-1]]
    assert {key["alg"] for key in jwks["keys"]} == {"RS256"}


def test_the_issuer_serves_discovery_and_nothing_else(monkeypatch):
    """Discovery points at the JWKS under the issuer; any other path is a 404."""
    handler = _issuer_handler()
    monkeypatch.setenv("ISSUER", ISSUER)
    document = json.loads(handler.handler({"rawPath": "/.well-known/openid-configuration"}, None)["body"])

    assert document["issuer"] == ISSUER
    assert document["jwks_uri"] == f"{ISSUER}/.well-known/jwks.json"
    assert document["id_token_signing_alg_values_supported"] == ["RS256"]
    assert handler.handler({"rawPath": "/"}, None)["statusCode"] == 404


def test_the_audiences_follow_hcp(signing_key, settings):
    """Google gets its provider's resource name and Azure its token exchange audience."""
    minted = _mint(
        {
            "TFC_GCP_PROVIDER_AUTH": "true",
            "TFC_GCP_WORKLOAD_PROVIDER_NAME": PROVIDER,
            "TFC_AZURE_PROVIDER_AUTH": "true",
            "TFC_AZURE_RUN_CLIENT_ID": "client",
        },
        settings,
    )
    assert minted is not None
    assert minted["gcp"]["audience"] == f"//iam.googleapis.com/{PROVIDER}"
    _verify(minted["gcp"]["token"], signing_key, f"//iam.googleapis.com/{PROVIDER}")
    _verify(minted["azure"]["token"], signing_key, "api://AzureADTokenExchange")
    assert minted["azure"]["client_id"] == "client"


def test_a_workspace_may_name_its_own_audience(signing_key, settings):
    """HCP's `TFC_*_WORKLOAD_IDENTITY_AUDIENCE` overrides the token audience and nothing else."""
    minted = _mint(
        {
            "TFC_GCP_PROVIDER_AUTH": "true",
            "TFC_GCP_WORKLOAD_PROVIDER_NAME": PROVIDER,
            "TFC_GCP_WORKLOAD_IDENTITY_AUDIENCE": "custom-gcp",
            "TFC_AZURE_PROVIDER_AUTH": "true",
            "TFC_AZURE_RUN_CLIENT_ID": "client",
            "TFC_AZURE_WORKLOAD_IDENTITY_AUDIENCE": "custom-azure",
        },
        settings,
    )
    assert minted is not None
    _verify(minted["gcp"]["token"], signing_key, "custom-gcp")
    _verify(minted["azure"]["token"], signing_key, "custom-azure")
    assert minted["gcp"]["audience"] == f"//iam.googleapis.com/{PROVIDER}"


def test_a_phase_override_wins_over_the_run_value(signing_key, settings):
    """`TFC_AZURE_APPLY_CLIENT_ID` is the apply's client, and the plan keeps the run default."""
    environment = {
        "TFC_AZURE_PROVIDER_AUTH": "true",
        "TFC_AZURE_RUN_CLIENT_ID": "reader",
        "TFC_AZURE_APPLY_CLIENT_ID": "writer",
        "TFC_GCP_PROVIDER_AUTH": "true",
        "TFC_GCP_WORKLOAD_PROVIDER_NAME": PROVIDER,
        "TFC_GCP_RUN_SERVICE_ACCOUNT_EMAIL": "reader@example",
        "TFC_GCP_APPLY_SERVICE_ACCOUNT_EMAIL": "writer@example",
    }
    plan = _mint(environment, settings, phase="plan")
    apply = _mint(environment, settings, phase="apply")
    assert plan is not None and apply is not None

    assert (plan["azure"]["client_id"], apply["azure"]["client_id"]) == ("reader", "writer")
    assert (plan["gcp"]["service_account_email"], apply["gcp"]["service_account_email"]) == (
        "reader@example",
        "writer@example",
    )
    assert _verify(apply["azure"]["token"], signing_key, "api://AzureADTokenExchange")["sub"].endswith(
        ":run_phase:apply"
    )


def test_nothing_is_minted_unless_asked(settings):
    """No flag, no token, and no need for an issuer."""
    assert _mint({"TFC_GCP_PROVIDER_AUTH": "false", "OTHER": "true"}, settings) is None


@pytest.mark.parametrize(
    "environment",
    [
        {"TFC_GCP_PROVIDER_AUTH": "true"},
        {"TFC_AZURE_PROVIDER_AUTH": "true"},
    ],
)
def test_a_flag_without_its_details_is_misconfigured(environment, signing_key, settings):
    """The workspace's mistake, told apart from the deployment's."""
    with pytest.raises(vending.WorkloadIdentityMisconfigured):
        _mint(environment, settings)


def test_asking_without_an_issuer_is_unavailable(settings, monkeypatch):
    """A deployment with no issuer cannot mint, which is its fault, not the workspace's."""
    monkeypatch.setattr(settings, "OIDC_ISSUER_URL", "")
    with pytest.raises(vending.WorkloadIdentityUnavailable):
        _mint({"TFC_AZURE_PROVIDER_AUTH": "true", "TFC_AZURE_RUN_CLIENT_ID": "client"}, settings)


class RefusingKMS:
    """A KMS whose key policy denies this caller `kms:Sign`."""

    def sign(self, **kwargs: Any) -> dict[str, Any]:
        """Refuse as KMS does."""
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "Sign")


def test_a_refused_signature_is_unavailable(signing_key, settings):
    """A key the function may not sign with surfaces as unavailable, never as a token."""
    with pytest.raises(vending.WorkloadIdentityUnavailable):
        vending.mint_workload_identity(
            environment={"TFC_AZURE_PROVIDER_AUTH": "true", "TFC_AZURE_RUN_CLIENT_ID": "client"},
            workspace_id="ws-abc",
            workspace_name="network",
            run_id="run-xyz",
            phase="plan",
            settings=settings,
            kms=RefusingKMS(),
        )


def test_the_bundle_carries_the_tokens(auth_client, runner_client, created_run, workspace, signing_key, sts_requests):
    """A workspace that asks gets both tokens in its bundle, bound to this run and phase."""
    _ask_for_both(auth_client, workspace["workspace_id"])
    response = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle")

    assert response.status_code == 200, response.text
    identity = response.json()["workload_identity"]
    claims = _verify(identity["gcp"]["token"], signing_key, f"//iam.googleapis.com/{PROVIDER}")
    assert claims["terraform_run_id"] == created_run["run_id"]
    assert claims["sub"] == f"workspace:{workspace['workspace_id']}:run_phase:plan"
    assert claims["terraform_workspace_name"] == workspace["name"]
    assert identity["gcp"]["service_account_email"] == "runs@example.iam.gserviceaccount.com"
    assert identity["azure"]["client_id"] == "00000000-0000-0000-0000-000000000001"
    _verify(identity["azure"]["token"], signing_key, "api://AzureADTokenExchange")


def test_a_bundle_without_the_flags_has_no_identity(runner_client, created_run, sts_requests):
    """The field is there and empty, so a runner never guesses."""
    assert runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()["workload_identity"] is None


def test_a_refresh_mints_fresh_tokens(
    auth_client, runner_client, created_run, workspace, signing_key, stepfunctions, sts_requests
):
    """The PR 151 refresh path renews the identity tokens with the AWS sessions."""
    _ask_for_both(auth_client, workspace["workspace_id"])
    first = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle").json()["workload_identity"]
    response = runner_client.post(f"{BASE}/{created_run['run_id']}/credentials", json={"phase": "plan"})

    assert response.status_code == 200, response.text
    refreshed = response.json()["workload_identity"]
    assert refreshed["azure"]["token"] != first["azure"]["token"]
    _verify(refreshed["azure"]["token"], signing_key, "api://AzureADTokenExchange")


@pytest.mark.parametrize("route", ["bundle", "credentials"])
def test_a_misconfigured_workspace_is_409(
    route, auth_client, runner_client, created_run, workspace, signing_key, stepfunctions, sts_requests
):
    """The runner reports the workspace's own mistake rather than a platform outage."""
    _set(auth_client, workspace["workspace_id"], "TFC_GCP_PROVIDER_AUTH", "true")
    if route == "bundle":
        response = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle")
    else:
        response = runner_client.post(f"{BASE}/{created_run['run_id']}/credentials", json={"phase": "plan"})

    assert response.status_code == 409
    assert response.json()["error_code"] == "WORKLOAD_IDENTITY_MISCONFIGURED"


def test_a_deployment_without_an_issuer_is_503(
    auth_client, runner_client, created_run, workspace, settings, sts_requests, monkeypatch
):
    """Asking where no issuer exists is a platform outage, which the runner retries."""
    _configure(settings, monkeypatch, OIDC_ISSUER_URL="")
    _ask_for_both(auth_client, workspace["workspace_id"])
    response = runner_client.get(f"{BASE}/{created_run['run_id']}/bundle")

    assert response.status_code == 503
    assert response.json()["error_code"] == "RUN_CREDENTIALS_UNAVAILABLE"


def test_the_service_passes_the_workspace_name(
    created_run, workspace, signing_key, settings, auth_client, sts_requests
):
    """The service reads the name from the workspace row, as HCP puts it in the claims."""
    _ask_for_both(auth_client, workspace["workspace_id"])
    bundle = runs_service.run_bundle(created_run["run_id"], settings=settings)
    claims = _verify(bundle["workload_identity"]["azure"]["token"], signing_key, "api://AzureADTokenExchange")
    assert claims["terraform_workspace_name"] == workspace["name"]
