"""The runner's signed proof of which task it is, traded with the runs domain for its run token.

The run token used to arrive as a task override, which put it in the Step
Functions execution input and history. Instead the runner signs an STS
`GetCallerIdentity` request with its task role and binds the run id into a
signed header. The runs domain replays it to STS, which names the task, and
only a running task started for that run gets the token.
"""

from __future__ import annotations

from typing import Any

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

RUN_ID_HEADER = "x-webbpulse-run-id"
"""Mirrors the runs domain's header; signed so the proof names exactly one run."""

STS_BODY = "Action=GetCallerIdentity&Version=2011-06-15"
"""The request body the runs domain replays, so it is the body that is signed."""

CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
"""The content type STS expects for a query protocol POST."""


class IdentityError(RuntimeError):
    """The task has no credentials to sign with."""


def signed_identity_headers(credentials: Any, region: str, run_id: str) -> dict[str, str]:
    """The headers of a SigV4 signed regional STS `GetCallerIdentity` naming `run_id`.

    Nothing is sent to STS from here: the runs domain replays these headers
    with the same body, which is what makes them a proof it can check.
    """
    if credentials is None:
        raise IdentityError("the task has no AWS credentials")
    request = AWSRequest(
        method="POST",
        url=f"https://sts.{region}.amazonaws.com/",
        data=STS_BODY,
        headers={"Content-Type": CONTENT_TYPE, RUN_ID_HEADER: run_id},
    )
    SigV4Auth(credentials.get_frozen_credentials(), "sts", region).add_auth(request)
    return {name: str(value) for name, value in request.headers.items()}


def session_signer(session: Any, region: str) -> Any:
    """A signer bound to a boto3 session's credentials, resolved at call time."""

    def sign(run_id: str) -> dict[str, str]:
        """Sign an identity proof for `run_id` with the session's current credentials."""
        return signed_identity_headers(session.get_credentials(), region, run_id)

    return sign
