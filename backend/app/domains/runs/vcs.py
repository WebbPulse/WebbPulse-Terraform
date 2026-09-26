"""Issuing an upload URL to a GitHub Actions workflow run.

The workflow authenticates with the OIDC token GitHub mints for the job, and that
token is the only source of truth about where the upload came from. The
repository, its id, the event, the branch, the pull request number and the commit
are read from its verified claims. What the body says about a pull request's head
and base is recorded, marked unverified, and never used to decide anything.

The ingest record is written before any URL exists, so the consumer that picks up
the uploaded object reads everything it trusts from the record, keyed by the
upload id in the object key, and nothing from the object itself.

The upload id is derived from the repository id, the commit, the workflow run and
its attempt. The workflow retries the request on a 5xx or a dropped connection,
and a retry lands on the same id, the same record and the same key with a freshly
signed URL, so a retried request never produces a second upload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Final, Mapping, Optional

from boto3.dynamodb.conditions import Attr
from webbpulse.dynamodb import ConditionFailed, now_iso

from ...common.composition.settings import Settings, get_settings
from ...common.db import repositories
from ...common.workspaces import vcs as workspace_vcs

_log = logging.getLogger(__name__)

GITHUB_ISSUER: Final = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URI: Final = f"{GITHUB_ISSUER}/.well-known/jwks"

UPLOAD_ID_PREFIX: Final = "up-"
UPLOAD_ID_PATTERN: Final = r"up-[0-9A-HJKMNP-TV-Z]{26}"
INGEST_PREFIX: Final = "ingest/"
INGEST_CONTENT_TYPE: Final = "application/gzip"
UPLOAD_URL_TTL: Final = 900
RECORD_TTL: Final = timedelta(days=3)
"""How long an ingest record outlives its request, matching the bucket's lifecycle
on `ingest/`."""

PUSH_EVENT: Final = "push"
PULL_REQUEST_EVENT: Final = "pull_request"

_BRANCH_REF = re.compile(r"^refs/heads/(?P<branch>.+)$")
_PULL_REF = re.compile(r"^refs/pull/(?P<number>[1-9][0-9]{0,9})/merge$")

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

TRUSTED_CLAIMS: Final = (
    "repository",
    "repository_id",
    "repository_owner",
    "event_name",
    "ref",
    "sha",
    "actor",
    "run_id",
    "run_attempt",
)
"""The claims an upload is attributed by. Every one is required."""


class InvalidUploadToken(Exception):
    """The bearer is missing, fails verification, or lacks a trusted claim."""


class UnsupportedEvent(Exception):
    """The token is for an event or a ref this bridge does not start runs from."""


class RepositoryNotBound(Exception):
    """No workspace is bound to the token's repository."""


def crockford(digest: bytes, length: int = 26) -> str:
    """The first `length` Crockford base32 characters of `digest`, ULID alphabet."""
    number = int.from_bytes(digest, "big")
    bits = len(digest) * 8
    return "".join(_CROCKFORD[(number >> (bits - 5 * (index + 1))) & 31] for index in range(length))


def upload_id_for(claims: Mapping[str, Any]) -> str:
    """The upload id one workflow run attempt's upload of one commit gets.

    Deterministic in the repository id, the commit, the run and the attempt, so a
    retried request reaches the same record and the same key.
    """
    seed = ":".join(str(claims[name]) for name in ("repository_id", "sha", "run_id", "run_attempt"))
    return UPLOAD_ID_PREFIX + crockford(hashlib.sha256(seed.encode()).digest())


def ingest_key(upload_id: str) -> str:
    """The artifacts bucket key an upload's tarball is PUT to."""
    return f"{INGEST_PREFIX}{upload_id}.tar.gz"


@lru_cache(maxsize=4)
def _verifier(audience: str) -> Any:
    """One JWKS verifier per audience per process, so GitHub's keys are cached."""
    from webbpulse.identity.verifier import JwksVerifier

    return JwksVerifier(issuer=GITHUB_ISSUER, audience=audience, jwks_uri=GITHUB_JWKS_URI)


def verify_token(token: str, *, settings: Settings | None = None) -> dict[str, Any]:
    """The verified claims of a GitHub Actions OIDC token.

    Checks the signature against GitHub's JWKS, the issuer, the configured
    audience, and `exp` and `nbf` with the verifier's leeway.

    Raises:
        InvalidUploadToken: No token, a token that fails verification, or one
            missing a trusted claim.
    """
    from webbpulse.identity.service import InvalidToken

    resolved = settings or get_settings()
    if not token:
        raise InvalidUploadToken("no bearer token")
    try:
        claims = _verifier(resolved.VCS_OIDC_AUDIENCE).verify(token, expected_type=None)
    except InvalidToken as error:
        raise InvalidUploadToken(str(error)) from error
    missing = [name for name in TRUSTED_CLAIMS if not str(claims.get(name, "") or "").strip()]
    if missing:
        raise InvalidUploadToken(f"the token lacks {', '.join(missing)}")
    return dict(claims)


def event_from_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """The event, branch and pull request number the verified claims describe.

    A push is only `event_name == "push"` to a branch ref. A pull request's number
    comes from its `refs/pull/<n>/merge` ref.

    Raises:
        UnsupportedEvent: Any other event, or a ref of the wrong shape, a tag push
            included.
    """
    event = str(claims["event_name"])
    ref = str(claims["ref"])
    if event == PUSH_EVENT:
        match = _BRANCH_REF.match(ref)
        if match is None:
            raise UnsupportedEvent(f"a push to {ref} is not a push to a branch")
        return {"event": PUSH_EVENT, "branch": match.group("branch"), "pr_number": None}
    if event == PULL_REQUEST_EVENT:
        match = _PULL_REF.match(ref)
        if match is None:
            raise UnsupportedEvent(f"a pull request ref of {ref} is not refs/pull/<n>/merge")
        return {"event": PULL_REQUEST_EVENT, "branch": None, "pr_number": int(match.group("number"))}
    raise UnsupportedEvent(f"the {event} event does not start runs")


def claims_digest(claims: Mapping[str, Any]) -> str:
    """A sha256 over the trusted claims, canonical JSON, to tie a record to its token."""
    canonical = json.dumps({name: str(claims[name]) for name in TRUSTED_CLAIMS}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _record(
    upload_id: str,
    claims: Mapping[str, Any],
    event: Mapping[str, Any],
    *,
    head_sha: Optional[str],
    base_sha: Optional[str],
    size_bytes: int,
) -> dict[str, Any]:
    """The ingest record one upload writes. Everything but the two unverified shas
    and the size comes from the verified claims."""
    now = datetime.now(timezone.utc)
    item: dict[str, Any] = {
        "upload_id": upload_id,
        "key": ingest_key(upload_id),
        "repo": str(claims["repository"]),
        "repository_id": str(claims["repository_id"]),
        "repository_owner": str(claims["repository_owner"]),
        "event": str(event["event"]),
        "ref": str(claims["ref"]),
        "sha": str(claims["sha"]),
        "actor": str(claims["actor"]),
        "workflow_run_id": str(claims["run_id"]),
        "workflow_run_attempt": str(claims["run_attempt"]),
        "claims_digest": claims_digest(claims),
        "size_bytes": int(size_bytes),
        "created_at": now_iso(),
        "created_at_ms": int(now.timestamp() * 1000),
        "expires_at": int((now + RECORD_TTL).timestamp()),
    }
    if event.get("branch"):
        item["branch"] = str(event["branch"])
    if event.get("pr_number") is not None:
        item["pr_number"] = int(event["pr_number"])
        if head_sha:
            item["head_sha"] = head_sha
        if base_sha:
            item["base_sha"] = base_sha
    return item


def issue_upload(
    token: str,
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Verify the workflow's token, record the upload and sign its PUT.

    Idempotent on the repository id, the commit, the workflow run and its attempt:
    a repeat returns the same `upload_id` and a new URL for the same key, and
    writes no second record. A repeat declaring a different size moves the
    record's size to it, since the URL signs the new one.

    Raises:
        InvalidUploadToken: The token did not verify.
        UnsupportedEvent: The token is for neither a branch push nor a pull request.
        RepositoryNotBound: No workspace is bound to the repository. Checked before
            anything is written and before a URL exists.
    """
    from webbpulse.storage import presigned_put

    resolved = settings or get_settings()
    claims = verify_token(token, settings=resolved)
    event = event_from_claims(claims)

    repository = str(claims["repository"])
    repository_id = str(claims["repository_id"])
    bound = workspace_vcs.bound_workspaces(repository, repository_id, settings=resolved)
    if not bound:
        raise RepositoryNotBound(repository)
    for workspace in bound:
        if workspace.get("vcs_repository_id") is None:
            workspace_vcs.record_repository_id(str(workspace["workspace_id"]), repository_id, settings=resolved)

    upload_id = upload_id_for(claims)
    size_bytes = int(payload["size_bytes"])
    uploads = repositories.vcs_uploads(resolved)
    item = _record(
        upload_id,
        claims,
        event,
        head_sha=payload.get("sha"),
        base_sha=payload.get("base_sha"),
        size_bytes=size_bytes,
    )
    try:
        uploads.put(item, condition=Attr("upload_id").not_exists())
        reused = False
    except ConditionFailed:
        reused = True
        existing = uploads.get({"upload_id": upload_id}, consistent=True) or {}
        if int(existing.get("size_bytes", size_bytes)) != size_bytes:
            uploads.update(
                {"upload_id": upload_id},
                update_expression="SET size_bytes = :size",
                expression_values={":size": size_bytes},
            )

    upload = presigned_put(
        resolved.ARTIFACTS_BUCKET,
        ingest_key(upload_id),
        INGEST_CONTENT_TYPE,
        size_bytes,
        UPLOAD_URL_TTL,
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.s3_endpoint_url,
    )
    _log.info(
        "Issued a VCS upload URL.",
        extra={
            "event": "runs.vcs.upload_issued",
            "upload_id": upload_id,
            "repo": repository,
            "vcs_event": event["event"],
            "reused": reused,
            "workspaces": len(bound),
        },
    )
    return {
        "upload_id": upload_id,
        "upload_url": upload.url,
        "headers": dict(upload.headers),
        "expires_in": UPLOAD_URL_TTL,
    }


__all__ = [
    "GITHUB_ISSUER",
    "GITHUB_JWKS_URI",
    "INGEST_CONTENT_TYPE",
    "INGEST_PREFIX",
    "RECORD_TTL",
    "TRUSTED_CLAIMS",
    "UPLOAD_ID_PATTERN",
    "UPLOAD_URL_TTL",
    "InvalidUploadToken",
    "RepositoryNotBound",
    "UnsupportedEvent",
    "claims_digest",
    "crockford",
    "event_from_claims",
    "ingest_key",
    "issue_upload",
    "upload_id_for",
    "verify_token",
]
