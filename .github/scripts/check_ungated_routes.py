"""Fail when the stack exposes an entry point that skips the access gate without an allowlist entry.

Every route the control plane API declares sits behind the access gate's REQUEST
authorizer unless it says `authorization_type = "NONE"`. Those routes, any HTTP API
built without the gate as its default authorizer, and any Lambda function URL open
to anyone have to be listed in `ALLOWED` below with the check that protects them,
so adding one is a reviewed decision rather than a one-word diff. The guard also
holds both environments to the gate's identity mode, since leaving it moves every
`require_identity_jwt` route off the gate.

Run from the repository root: `python3 .github/scripts/check_ungated_routes.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TERRAFORM = Path(__file__).resolve().parents[2] / "terraform"

ALLOWED: dict[str, str] = {
    "GET /api/v1/runs/{run_id}/bundle": "run token bound to the run (require_run_token, runs/router.py)",
    "POST /api/v1/runs/{run_id}/artifact-uploads": "run token bound to the run (require_run_token, runs/router.py)",
    "POST /api/v1/runs/{run_id}/heartbeat": "run token bound to the run (require_run_token, runs/router.py)",
    "POST /api/v1/runs/{run_id}/credentials": "run token bound to the run (require_run_token, runs/router.py)",
    "POST /api/v1/runs/{run_id}/phase-result": "run token bound to the run (require_run_token, runs/router.py)",
    "POST /api/v1/runs/{run_id}/runner-token": "signed STS identity of the run's ECS task (runs/runner_tokens.py)",
    "POST /api/v1/github/webhooks": "GitHub HMAC signature, refused without a secret (common/github/webhooks.py)",
    "GET /v1/modules/{namespace}/{name}/{provider}/versions": "registry:read bearer (registry/protocol_router.py)",
    "GET /v1/modules/{namespace}/{name}/{provider}/{version}/download": "registry:read bearer (registry/protocol_router.py)",
    "GET /v1/providers/{namespace}/{type}/versions": "registry:read bearer (registry/protocol_router.py)",
    "GET /v1/providers/{namespace}/{type}/{version}/download/{os}/{arch}": "registry:read bearer (registry/protocol_router.py)",
    "POST /v1/oauth/token": "single-use code with PKCE and expiry (workspaces/terraform_login.py)",
    "GET /api/v2/ping": "public, answers the API version only (workspaces/tfe_router.py)",
    "GET /api/v2/organizations/{organization}/entitlement-set": "scoped wpk_ key (workspaces/tfe_router.py)",
    "GET /api/v2/organizations/{organization}/workspaces": "scoped wpk_ key (workspaces/tfe_router.py)",
    "POST /api/v2/organizations/{organization}/workspaces": "any credential, always 422 (workspaces/tfe_router.py)",
    "GET /api/v2/organizations/{organization}/workspaces/{workspace_name}": "scoped wpk_ key (workspaces/tfe_router.py)",
    "GET /api/v2/workspaces/{workspace_id}": "scoped wpk_ key (workspaces/tfe_router.py)",
    "GET /api/v2/workspaces/{workspace_id}/all-vars": "scoped wpk_ key (workspaces/tfe_router.py)",
    "POST /api/v2/workspaces/{workspace_id}/configuration-versions": "scoped wpk_ key (workspaces/tfe_router.py)",
    "GET /api/v2/configuration-versions/{config_version_id}": "scoped wpk_ key (workspaces/tfe_router.py)",
    "POST /api/v2/workspaces/{workspace_id}/actions/lock": "scoped wpk_ key (workspaces/tfe_router.py)",
    "POST /api/v2/workspaces/{workspace_id}/actions/unlock": "scoped wpk_ key (workspaces/tfe_router.py)",
    "POST /api/v2/workspaces/{workspace_id}/actions/force-unlock": "scoped wpk_ key (workspaces/tfe_router.py)",
    "GET /api/v2/workspaces/{workspace_id}/current-state-version": "scoped wpk_ key (workspaces/tfe_state_router.py)",
    "GET /api/v2/workspaces/{workspace_id}/current-state-version-outputs": "scoped wpk_ key (tfe_state_router.py)",
    "POST /api/v2/workspaces/{workspace_id}/state-versions": "scoped wpk_ key (workspaces/tfe_state_router.py)",
    "GET /api/v2/state-versions/{state_version_id}": "scoped wpk_ key (workspaces/tfe_state_router.py)",
    "GET /api/v2/state-versions/{state_version_id}/download": "scoped wpk_ key (workspaces/tfe_state_router.py)",
    "GET /api/v2/state-version-outputs/{output_id}": "scoped wpk_ key (workspaces/tfe_state_router.py)",
    "POST /api/v2/runs": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/runs/{run_id}": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/runs/{run_id}/run-events": "scoped wpk_ key (runs/tfe_router.py)",
    "POST /api/v2/runs/{run_id}/actions/apply": "scoped wpk_ key (runs/tfe_router.py)",
    "POST /api/v2/runs/{run_id}/actions/discard": "scoped wpk_ key (runs/tfe_router.py)",
    "POST /api/v2/runs/{run_id}/actions/cancel": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/workspaces/{workspace_id}/runs": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/plans/{plan_id}": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/plans/{plan_id}/logs/{token}": "HMAC path token bound to the phase, 12h expiry (runs/tfe_runs.py)",
    "GET /api/v2/applies/{apply_id}": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/applies/{apply_id}/logs/{token}": "HMAC path token bound to the phase, 12h expiry (runs/tfe_runs.py)",
    "GET /api/v2/organizations/{organization}/runs/queue": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/v2/organizations/{organization}/capacity": "scoped wpk_ key (runs/tfe_router.py)",
    "GET /api/auth/.well-known/jwks.json": "public signing keys (webbpulse.identity)",
    "GET /api/auth/.well-known/openid-configuration": "public discovery document (webbpulse.identity)",
    "GET /api/auth/oauth/providers": "public provider names (webbpulse.identity)",
    "GET /.well-known/openid-configuration": "public run OIDC discovery (oidc_issuer/handler.py)",
    "GET /.well-known/jwks.json": "public run OIDC signing keys (oidc_issuer/handler.py)",
    "aws_lambda_function_url.e2e_notification_receiver": "staging only, stateless signature echo (e2e_notification_receiver)",
    "module.oidc_api": "the run OIDC issuer, public by design for cloud workload identity federation",
}

ROUTE_KEY = re.compile(r'"((?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|ANY) /[^"]*)"\s*=\s*\{')
NONE_TYPE = re.compile(r'authorization_type\s*=\s*"NONE"')
FUNCTION_URL = re.compile(r'resource\s+"aws_lambda_function_url"\s+"([^"]+)"\s*\{')
MODULE = re.compile(r'module\s+"([^"]+)"\s*\{')
RAW_ROUTE = re.compile(r'resource\s+"aws_apigatewayv2_route"\s+"([^"]+)"')
GATE_AUTHORIZER = re.compile(r"authorizer_id\s*=\s*[^\n]*module\.access_gate")
REQUIRED_TFVARS = {"identity_jwt_mode": '"gate"', "access_gate": "true"}


def _block(text: str, start: int) -> str:
    """The text between the brace at `start` and its match."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    raise ValueError(f"unbalanced braces from offset {start}")


def ungated(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Every ungated entry point found in `files`, and every problem the parse cannot explain."""
    found: list[str] = []
    problems: list[str] = []
    for name, text in sorted(files.items()):
        explained = 0
        for match in ROUTE_KEY.finditer(text):
            body = _block(text, match.end() - 1)
            if NONE_TYPE.search(body):
                found.append(match.group(1))
                explained += 1
        for match in FUNCTION_URL.finditer(text):
            if NONE_TYPE.search(_block(text, match.end() - 1)):
                found.append(f"aws_lambda_function_url.{match.group(1)}")
                explained += 1
        for match in MODULE.finditer(text):
            body = _block(text, match.end() - 1)
            if "modules/http-api" not in body:
                continue
            if not GATE_AUTHORIZER.search(body):
                found.append(f"module.{match.group(1)}")
        for match in RAW_ROUTE.finditer(text):
            problems.append(f"{name}: aws_apigatewayv2_route.{match.group(1)} bypasses the http-api module; declare it there")
        total = len(NONE_TYPE.findall(text))
        if total != explained:
            problems.append(
                f'{name}: {total - explained} authorization_type = "NONE" outside a literal route key or function URL'
            )
    return found, problems


def tfvars_problems() -> list[str]:
    """Each environment that leaves the gate's identity mode or turns the gate off."""
    problems: list[str] = []
    for path in sorted((TERRAFORM / "env").glob("*.tfvars")):
        text = path.read_text()
        for key, value in REQUIRED_TFVARS.items():
            if not re.search(rf"^\s*{key}\s*=\s*{re.escape(value)}\s*$", text, re.MULTILINE):
                problems.append(f"{path.name}: {key} must stay {value}, or the gate no longer covers identity routes")
    return problems


def main() -> int:
    """Print every unlisted or stale entry and exit non-zero when there is one."""
    files = {path.name: path.read_text() for path in sorted(TERRAFORM.glob("*.tf"))}
    found, problems = ungated(files)
    problems += tfvars_problems()
    duplicates = sorted({key for key in found if found.count(key) > 1})
    problems += [f"{key} is declared ungated more than once" for key in duplicates]
    problems += [f"{key} skips the access gate but is not in ALLOWED" for key in sorted(set(found) - set(ALLOWED))]
    problems += [f"{key} is in ALLOWED but no longer skips the gate; remove it" for key in sorted(set(ALLOWED) - set(found))]
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(f"Every ungated entry point needs an entry in {Path(__file__).name} naming the check that protects it.")
        return 1
    print(f"{len(found)} ungated entry points, all allowlisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
