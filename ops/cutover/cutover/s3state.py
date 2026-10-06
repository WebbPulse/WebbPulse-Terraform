"""The plane's S3 state objects, written byte for byte so lineage and serial survive the move."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

import boto3
from botocore.exceptions import ClientError

from cutover.config import Environment

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class StoreError(Exception):
    """An S3 or KMS call that refused or failed."""


class StateStore(Protocol):
    """The S3 operations the move and the rollback need."""

    def kms_key_arn(self) -> str:
        """The ARN behind the environment's state KMS alias."""
        ...

    def exists(self, key: str) -> bool:
        """Whether an object exists at `key`."""
        ...

    def put_new(self, key: str, body: bytes, kms_key_arn: str) -> None:
        """Create `key` with exactly `body`, refusing if anything is already there."""
        ...

    def get(self, key: str) -> bytes:
        """The current body at `key`."""
        ...


def _code(error: ClientError) -> str:
    """The AWS error code of a client error."""
    details: Any = error.response.get("Error", {})
    return str(details.get("Code", ""))


class S3StateStore:
    """`StateStore` over boto3 with the environment's AdministratorAccess profile."""

    def __init__(self, environment: Environment, session: Any | None = None) -> None:
        """Open a session on the environment's profile unless one is given."""
        self.environment = environment
        self._session = session or boto3.Session(profile_name=environment.aws_profile, region_name=environment.region)
        self._s3: S3Client = self._session.client("s3")

    def kms_key_arn(self) -> str:
        """Resolve the alias, so the object is encrypted under the same key the bucket and runner use."""
        try:
            described = self._session.client("kms").describe_key(KeyId=self.environment.kms_alias)
        except ClientError as error:
            raise StoreError(f"cannot resolve {self.environment.kms_alias}: {_code(error)}") from None
        return str(described["KeyMetadata"]["Arn"])

    def exists(self, key: str) -> bool:
        """HEAD the object; a 404 is absence and anything else is an error."""
        try:
            self._s3.head_object(Bucket=self.environment.state_bucket, Key=key)
        except ClientError as error:
            if _code(error) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise StoreError(f"cannot read s3://{self.environment.state_bucket}/{key}: {_code(error)}") from None
        return True

    def put_new(self, key: str, body: bytes, kms_key_arn: str) -> None:
        """Conditional create with SSE-KMS; a concurrent writer makes this fail rather than be overwritten."""
        try:
            self._s3.put_object(
                Bucket=self.environment.state_bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=kms_key_arn,
                IfNoneMatch="*",
                ChecksumAlgorithm="SHA256",
            )
        except ClientError as error:
            raise StoreError(f"cannot write s3://{self.environment.state_bucket}/{key}: {_code(error)}") from None

    def get(self, key: str) -> bytes:
        """GET the object body."""
        try:
            response = self._s3.get_object(Bucket=self.environment.state_bucket, Key=key)
        except ClientError as error:
            raise StoreError(f"cannot read s3://{self.environment.state_bucket}/{key}: {_code(error)}") from None
        return response["Body"].read()
