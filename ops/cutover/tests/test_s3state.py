"""The S3 store's request shapes, checked with botocore's stubber and no network."""

from __future__ import annotations

import io
from typing import Any

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from cutover.config import ENVIRONMENTS
from cutover.s3state import S3StateStore, StoreError

from .fakes import KMS_ARN, make_state

ENV = ENVIRONMENTS["staging"]
KEY = "workspaces/ws-01K7Z3Y9V2B4N6M8P0Q2R4S6T8/terraform.tfstate"


class StubSession:
    """A session handing out stubbed clients."""

    def __init__(self) -> None:
        """Build static credential clients and their stubbers."""
        session = boto3.Session(aws_access_key_id="x", aws_secret_access_key="y", region_name="us-west-2")
        self.clients: dict[str, Any] = {"s3": session.client("s3"), "kms": session.client("kms")}
        self.stubs = {name: Stubber(client) for name, client in self.clients.items()}
        for stub in self.stubs.values():
            stub.activate()

    def client(self, name: str) -> Any:
        """The stubbed client."""
        return self.clients[name]


@pytest.fixture
def session() -> StubSession:
    """A fresh stub session."""
    return StubSession()


def test_put_new_is_conditional_and_kms_encrypted(session: StubSession) -> None:
    """The write carries the exact body, SSE-KMS with the key ARN and If-None-Match."""
    body = make_state()
    session.stubs["s3"].add_response(
        "put_object",
        {},
        {
            "Bucket": ENV.state_bucket,
            "Key": KEY,
            "Body": body,
            "ContentType": "application/json",
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": KMS_ARN,
            "IfNoneMatch": "*",
            "ChecksumAlgorithm": "SHA256",
        },
    )
    S3StateStore(ENV, session).put_new(KEY, body, KMS_ARN)
    session.stubs["s3"].assert_no_pending_responses()


def test_put_new_failure_names_only_the_code(session: StubSession) -> None:
    """A precondition failure surfaces as the code."""
    session.stubs["s3"].add_client_error("put_object", "PreconditionFailed", http_status_code=412)
    with pytest.raises(StoreError, match="PreconditionFailed"):
        S3StateStore(ENV, session).put_new(KEY, b"{}", KMS_ARN)


def test_exists(session: StubSession) -> None:
    """404 is absence, 200 presence and 403 an error."""
    stub = session.stubs["s3"]
    stub.add_client_error("head_object", "404", http_status_code=404)
    stub.add_response("head_object", {}, {"Bucket": ENV.state_bucket, "Key": KEY})
    stub.add_client_error("head_object", "403", http_status_code=403)
    store = S3StateStore(ENV, session)
    assert store.exists(KEY) is False
    assert store.exists(KEY) is True
    with pytest.raises(StoreError, match="403"):
        store.exists(KEY)


def test_get_and_kms(session: StubSession) -> None:
    """The body is returned and the alias resolves to its ARN."""
    body = make_state()
    session.stubs["s3"].add_response(
        "get_object", {"Body": StreamingBody(io.BytesIO(body), len(body))}, {"Bucket": ENV.state_bucket, "Key": KEY}
    )
    session.stubs["kms"].add_response(
        "describe_key", {"KeyMetadata": {"KeyId": "k", "Arn": KMS_ARN}}, {"KeyId": ENV.kms_alias}
    )
    store = S3StateStore(ENV, session)
    assert store.get(KEY) == body
    assert store.kms_key_arn() == KMS_ARN


def test_kms_failure(session: StubSession) -> None:
    """An unresolvable alias is a store error naming the alias."""
    session.stubs["kms"].add_client_error("describe_key", "NotFoundException", expected_params={"KeyId": ANY})
    with pytest.raises(StoreError, match="NotFoundException"):
        S3StateStore(ENV, session).kms_key_arn()
