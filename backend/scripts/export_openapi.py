"""Export one merged OpenAPI document covering every domain's routes.

The frontend's types are generated from this file, so the document has to describe
the surface as the gateway presents it rather than as any one function sees it.
Each domain is built through `build_domain_app`, the same composition both roots
use, and the resulting `paths` maps are merged. The routers already carry the
gateway's own prefixes, `/api/v1` for the product routes and `/api/auth` for the
identity router, so a merged path is byte for byte the route key in
`terraform/apigateway.tf` and the path the frontend calls.

The environment mirrors `tests/conftest.py` and `tests/domains/identity/conftest.py`:
`ENVIRONMENT` is `test`, table and bucket names are fake, and the identity signer is
the package's in-process one. Nothing here reaches AWS, so the export runs on a
workstation with no credentials and in CI with no role.

Output is deterministic: sorted keys, a stable domain order and a trailing newline,
so regenerating without a backend change produces no diff.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
OUTPUT_PATH = REPO_ROOT / "frontend" / "src" / "api" / "openapi.json"

ENVIRONMENT = "test"
TABLE_PREFIX = f"webbpulse-terraform-{ENVIRONMENT}"

EXPORT_ENVIRONMENT = {
    "TESTING": "1",
    "AWS_DEFAULT_REGION": "us-west-2",
    "AWS_REGION_NAME": "us-west-2",
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "ENVIRONMENT": ENVIRONMENT,
    "LOG_LEVEL": "WARNING",
    "WORKSPACES_TABLE": f"{TABLE_PREFIX}-workspaces",
    "RUNS_TABLE": f"{TABLE_PREFIX}-runs",
    "VARIABLES_TABLE": f"{TABLE_PREFIX}-variables",
    "CONFIG_VERSIONS_TABLE": f"{TABLE_PREFIX}-config-versions",
    "USERS_TABLE": f"{TABLE_PREFIX}-users",
    "STATE_BUCKET": f"{TABLE_PREFIX}-state",
    "ARTIFACTS_BUCKET": f"{TABLE_PREFIX}-artifacts",
    "RUNNER_LOG_GROUP": f"/aws/ecs/{TABLE_PREFIX}-runner",
    "VARIABLES_MASTER_KEY": base64.b64encode(b"k" * 32).decode(),
    "IDENTITY_TABLE_PREFIX": TABLE_PREFIX,
    "IDENTITY_ISSUER": "https://api.example.test/api/auth",
    "IDENTITY_AUDIENCE": "webbpulse-terraform-test-api",
    "IDENTITY_ENVIRONMENT": "local",
    "IDENTITY_SIGNER": "local",
    "IDENTITY_SIGNING_KEY_ARNS": '["local"]',
    "IDENTITY_LOCAL_SIGNER_SEED": "seed-for-the-in-process-signer",
    "IDENTITY_RP_ID": "example.test",
    "IDENTITY_RP_NAME": "WebbPulse Terraform",
    "IDENTITY_REGISTRATION_ENABLED": "false",
    "IDENTITY_TOTP_CIPHER": "secret",
    "IDENTITY_TOTP_MASTER_KEY": base64.b64encode(b"m" * 32).decode(),
}
"""What the suite sets, with the identity signer moved to the in-process one.

`IDENTITY_ISSUER` has to be present or the workspaces domain builds none of the
identity glue and every `/api/auth` path would silently leave the document.
"""

UNSET_VARIABLES = (
    "APP_SECRET_ID",
    "APP_SECRETS_ARN",
    "DYNAMODB_ENDPOINT_URL",
    "S3_ENDPOINT_URL",
)
"""Cleared so an operator's own shell cannot point the export at a real secret or a
real endpoint."""

DOCUMENT_TITLE = "WebbPulse Terraform control plane"
DOCUMENT_DESCRIPTION = "Workspaces, variables, config versions and runs, as the gateway routes them."


def apply_environment() -> None:
    """Put the export environment in place before anything imports the app."""
    os.environ.update(EXPORT_ENVIRONMENT)
    for name in UNSET_VARIABLES:
        os.environ.pop(name, None)


def merge_component(
    merged: dict[str, dict[str, Any]],
    section: str,
    name: str,
    definition: Any,
    domain: str,
) -> None:
    """Add one component, refusing a name two domains define differently.

    `ErrorResponse` and `ValidationErrorDetail` come from the shared package and so
    appear under both domains. They are identical, which makes the merge safe. A
    name that ever stops being identical is a real ambiguity in the contract, so it
    fails here rather than resolving to whichever domain merged last.
    """
    existing = merged.setdefault(section, {}).get(name)
    if existing is not None and existing != definition:
        raise SystemExit(f"{domain} redefines components.{section}.{name} with a different shape")
    merged[section][name] = definition


def build_document() -> dict[str, Any]:
    """Every domain's paths and components on one document, in `DOMAINS` order."""
    from app.common.composition.settings import get_settings
    from app.common.composition.wiring import DOMAINS, build_domain_app
    from app.common.version import VERSION

    settings = get_settings()

    paths: dict[str, Any] = {}
    components: dict[str, dict[str, Any]] = {}
    openapi_version = ""

    for name, domain in DOMAINS.items():
        document = build_domain_app(domain, settings=settings).openapi()
        openapi_version = openapi_version or str(document["openapi"])

        for path, item in document.get("paths", {}).items():
            if path in paths:
                raise SystemExit(f"{name} redefines the path {path}, which another domain already serves")
            paths[path] = item

        for section, entries in document.get("components", {}).items():
            for entry_name, definition in entries.items():
                merge_component(components, section, entry_name, definition, name)

    return {
        "openapi": openapi_version,
        "info": {
            "title": DOCUMENT_TITLE,
            "description": DOCUMENT_DESCRIPTION,
            "version": VERSION,
        },
        "paths": paths,
        "components": components,
    }


def render(document: dict[str, Any]) -> str:
    """The document as deterministic JSON, sorted throughout and newline terminated."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    """Write the merged document, and report where it landed."""
    apply_environment()
    sys.path.insert(0, str(BACKEND_ROOT))

    rendered = render(build_document())
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
