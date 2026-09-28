"""The control plane's OIDC issuer: the discovery document and the JWKS, both anonymous.

Google and Azure workload identity federation fetch these two documents to verify
the identity tokens the runs function signs for a run with KMS. The public halves
of the signing keys come from `kms:GetPublicKey` and are cached per container, so a
cold start is the only KMS call on the path. Every key listed in `SIGNING_KEY_IDS`
is published, which lets a rotation publish the new key before it signs and keep
the old one until tokens it signed have expired.

It runs on the Lambda Python runtime with nothing but the standard library and
boto3, so it deploys as one file.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

CACHE_SECONDS = 300
SIGNING_ALGORITHM = "RSASSA_PKCS1_V1_5_SHA_256"
CLAIMS_SUPPORTED = [
    "sub",
    "aud",
    "exp",
    "iat",
    "nbf",
    "iss",
    "jti",
    "terraform_workspace_id",
    "terraform_workspace_name",
    "terraform_run_id",
    "terraform_run_phase",
]

_cache: dict[str, Any] = {"at": 0.0, "jwks": None}


def b64url(data: bytes) -> str:
    """Base64url without padding, as JOSE encodes binary values."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _read(data: bytes, offset: int, tag: int) -> tuple[bytes, int]:
    """One DER element of the expected tag at `offset`: its content and the offset after it."""
    if data[offset] != tag:
        raise ValueError(f"expected DER tag {tag:#x} at {offset}, found {data[offset]:#x}")
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    return data[offset : offset + length], offset + length


def rsa_public_numbers(der: bytes) -> tuple[bytes, bytes]:
    """The modulus and exponent of a DER SubjectPublicKeyInfo RSA key, as unsigned big endian bytes."""
    spki, _ = _read(der, 0, 0x30)
    _, offset = _read(spki, 0, 0x30)
    bits, _ = _read(spki, offset, 0x03)
    if bits[0] != 0:
        raise ValueError("the public key bit string has unused bits")
    key, _ = _read(bits[1:], 0, 0x30)
    modulus, offset = _read(key, 0, 0x02)
    exponent, _ = _read(key, offset, 0x02)
    return modulus.lstrip(b"\x00"), exponent.lstrip(b"\x00")


def key_id(kms_key_id: str) -> str:
    """The `kid` of a KMS key: its key id, whether given bare or as an ARN."""
    return kms_key_id.rsplit("/", 1)[-1]


def jwk(kms_key_id: str, der: bytes) -> dict[str, str]:
    """One RS256 signing key as a JWK."""
    modulus, exponent = rsa_public_numbers(der)
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": key_id(kms_key_id),
        "n": b64url(modulus),
        "e": b64url(exponent),
    }


def signing_key_ids() -> list[str]:
    """The KMS keys to publish, newest first."""
    return [value.strip() for value in os.environ.get("SIGNING_KEY_IDS", "").split(",") if value.strip()]


def build_jwks(kms: Any, key_ids: list[str]) -> dict[str, Any]:
    """The JWKS for `key_ids`, skipping any key that is not an RS256 signing key."""
    keys = []
    for kms_key_id in key_ids:
        response = kms.get_public_key(KeyId=kms_key_id)
        if response.get("KeyUsage") != "SIGN_VERIFY" or SIGNING_ALGORITHM not in response.get("SigningAlgorithms", []):
            continue
        keys.append(jwk(str(response["KeyId"]), bytes(response["PublicKey"])))
    return {"keys": keys}


def discovery(issuer: str) -> dict[str, Any]:
    """The OpenID Provider metadata, in the shape HCP Terraform's issuer publishes."""
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "response_types_supported": ["id_token"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid"],
        "claims_supported": CLAIMS_SUPPORTED,
    }


def _jwks() -> dict[str, Any]:
    """The JWKS, from the container's cache while it is fresh."""
    if _cache["jwks"] is None or time.monotonic() - float(_cache["at"]) > CACHE_SECONDS:
        import boto3

        _cache["jwks"] = build_jwks(boto3.client("kms"), signing_key_ids())
        _cache["at"] = time.monotonic()
    return _cache["jwks"]


def _response(status: int, body: dict[str, Any], cache: bool) -> dict[str, Any]:
    """An HTTP API payload 2.0 response."""
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "cache-control": f"public, max-age={CACHE_SECONDS}" if cache else "no-store",
        },
        "body": json.dumps(body, separators=(",", ":")),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Serve the discovery document or the JWKS; anything else is a 404."""
    path = str(event.get("rawPath", ""))
    if path == "/.well-known/openid-configuration":
        return _response(200, discovery(os.environ["ISSUER"]), cache=True)
    if path == "/.well-known/jwks.json":
        try:
            return _response(200, _jwks(), cache=True)
        except Exception as error:
            print(json.dumps({"event": "oidc.jwks.failed", "error": type(error).__name__}))
            return _response(503, {"message": "The signing keys are unavailable."}, cache=False)
    return _response(404, {"message": "Not found."}, cache=False)
