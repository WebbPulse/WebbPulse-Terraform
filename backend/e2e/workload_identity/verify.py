"""Verify, inside a real plan, the Google and Azure identity tokens the runner was handed.

Run by the `external` data source, so it sees the engine's environment exactly as a
provider does. It finds each token where the provider would, fetches the issuer's
discovery document and JWKS anonymously, checks the RS256 signature with nothing but
the standard library, and checks the claims. It prints only what it verified and the
key id, never a token, and exits non zero on any failure, which fails the plan.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from typing import Any

SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
CLOCK_SKEW_SECONDS = 300


class VerificationFailed(Exception):
    """A token, its signature, its claims or the provider configuration is wrong."""


def _b64decode(value: str) -> bytes:
    """Base64url with the padding JOSE strips put back."""
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _fetch(url: str) -> dict[str, Any]:
    """One JSON document, fetched with no credentials at all."""
    with urllib.request.urlopen(url, timeout=15) as response:
        return dict(json.load(response))


def _verify_rs256(token: str, jwks: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The claims and kid of `token`, once its PKCS#1 v1.5 SHA-256 signature checks out."""
    parts = token.split(".")
    if len(parts) != 3:
        raise VerificationFailed("the token is not a compact JWS")
    header = json.loads(_b64decode(parts[0]))
    if header.get("alg") != "RS256":
        raise VerificationFailed(f"the token is signed with {header.get('alg')}, not RS256")
    keys = [key for key in jwks.get("keys", []) if key.get("kid") == header.get("kid")]
    if not keys:
        raise VerificationFailed(f"the JWKS does not publish kid {header.get('kid')}")
    modulus = int.from_bytes(_b64decode(keys[0]["n"]), "big")
    exponent = int.from_bytes(_b64decode(keys[0]["e"]), "big")
    length = (modulus.bit_length() + 7) // 8
    signature = _b64decode(parts[2])
    if len(signature) != length:
        raise VerificationFailed("the signature is not the key's length")
    encoded = pow(int.from_bytes(signature, "big"), exponent, modulus).to_bytes(length, "big")
    digest = SHA256_DIGEST_INFO + hashlib.sha256(f"{parts[0]}.{parts[1]}".encode("ascii")).digest()
    expected = b"\x00\x01" + b"\xff" * (length - len(digest) - 3) + b"\x00" + digest
    if not hmac.compare_digest(encoded, expected):
        raise VerificationFailed("the signature does not verify against the published key")
    return json.loads(_b64decode(parts[1])), str(header["kid"])


def _check_claims(claims: dict[str, Any], *, issuer: str, audience: str, workspace_id: str) -> None:
    """The claims HCP Terraform's dynamic credentials carry, for this workspace's plan."""
    now = int(time.time())
    expected = {
        "iss": issuer,
        "aud": audience,
        "sub": f"workspace:{workspace_id}:run_phase:plan",
        "terraform_workspace_id": workspace_id,
        "terraform_run_phase": "plan",
    }
    for name, value in expected.items():
        if claims.get(name) != value:
            raise VerificationFailed(f"claim {name} is {claims.get(name)!r}, expected {value!r}")
    if not str(claims.get("terraform_run_id", "")).startswith("run-"):
        raise VerificationFailed("claim terraform_run_id does not name a run")
    if not claims.get("terraform_workspace_name"):
        raise VerificationFailed("claim terraform_workspace_name is empty")
    if not 0 < int(claims["exp"]) - int(claims["iat"]) <= 3600:
        raise VerificationFailed("the token lives longer than an hour")
    if not int(claims["nbf"]) - CLOCK_SKEW_SECONDS <= now < int(claims["exp"]):
        raise VerificationFailed("the token is not valid now")


def _read(path: str) -> str:
    """A token file's contents."""
    with open(path, encoding="ascii") as handle:
        return handle.read().strip()


def verify(query: dict[str, str], environ: dict[str, str]) -> dict[str, str]:
    """Verify both tokens as the Google and Azure providers would find them."""
    issuer = query["issuer"].rstrip("/")
    workspace_id = query["workspace_id"]
    discovery = _fetch(f"{issuer}/.well-known/openid-configuration")
    if discovery.get("issuer") != issuer:
        raise VerificationFailed(f"discovery names issuer {discovery.get('issuer')!r}")
    jwks = _fetch(str(discovery["jwks_uri"]))

    credential_path = environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if not credential_path:
        raise VerificationFailed("GOOGLE_APPLICATION_CREDENTIALS is not set")
    with open(credential_path, encoding="utf-8") as handle:
        credential = json.load(handle)
    provider = environ.get("TFC_GCP_WORKLOAD_PROVIDER_NAME", "").strip("/")
    gcp_audience = f"//iam.googleapis.com/{provider}"
    if credential.get("type") != "external_account" or credential.get("audience") != gcp_audience:
        raise VerificationFailed("the Google credential is not an external account for the workload provider")
    service_account = environ.get("TFC_GCP_RUN_SERVICE_ACCOUNT_EMAIL", "")
    if service_account not in str(credential.get("service_account_impersonation_url", "")):
        raise VerificationFailed("the Google credential does not impersonate the run service account")
    gcp_claims, gcp_kid = _verify_rs256(_read(credential["credential_source"]["file"]), jwks)
    _check_claims(gcp_claims, issuer=issuer, audience=gcp_audience, workspace_id=workspace_id)

    if environ.get("ARM_USE_OIDC") != "true":
        raise VerificationFailed("ARM_USE_OIDC is not true")
    if environ.get("ARM_CLIENT_ID") != environ.get("TFC_AZURE_RUN_CLIENT_ID"):
        raise VerificationFailed("ARM_CLIENT_ID is not the run client id")
    azure_claims, azure_kid = _verify_rs256(_read(environ["ARM_OIDC_TOKEN_FILE_PATH"]), jwks)
    _check_claims(azure_claims, issuer=issuer, audience="api://AzureADTokenExchange", workspace_id=workspace_id)

    return {"gcp": "verified", "azure": "verified", "kid": gcp_kid if gcp_kid == azure_kid else "mixed"}


def main() -> int:
    """Read the query from stdin, verify, and print the result the data source expects."""
    try:
        result = verify(json.load(sys.stdin), dict(os.environ))
    except (VerificationFailed, KeyError, OSError, ValueError) as error:
        print(f"workload identity verification failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
