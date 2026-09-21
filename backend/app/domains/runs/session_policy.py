"""The session policies the runner attaches when it assumes a workspace's run role.

A session policy only ever narrows a role, so the apply phase's is the widest
thing that changes nothing: it allows everything the role already allows and
relies on the role itself for the boundary. The plan phase's is narrower than
that, and deliberately: a plan reads the world and writes nothing outside the
state and artifact buckets, so a provider bug or a malicious module in someone's
configuration cannot mutate an account during what the caller was told is a
read-only operation.

The plan's "read everything" half is the AWS managed policy `ReadOnlyAccess`,
passed to `AssumeRole` as a session policy ARN rather than written inline,
because IAM rejects a wildcard in an action's service portion: `*:Get*` is
malformed and only the bare `*` may stand for every service. A session's
permissions are the intersection of the role with the union of its session
policies, so pairing the managed policy with the inline state, artifact and
encryption statements below reads as widely as the role allows while writing
only this run's own objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from .schemas.run import Phase

PLAN_SESSION_POLICY_ARNS: Final = ("arn:aws:iam::aws:policy/ReadOnlyAccess",)
"""The managed policies a plan's session unions with its inline document.

`ReadOnlyAccess` is AWS's own enumeration of every non mutating action across
every service, which is the thing the inline document cannot express.
"""


@dataclass(frozen=True)
class SessionPolicy:
    """One phase's session policy, in both forms `AssumeRole` accepts.

    Attributes:
        document: The inline policy, passed as `Policy`.
        policy_arns: The managed policies, passed as `PolicyArns`.
    """

    document: dict[str, Any]
    policy_arns: tuple[str, ...]


def plan_policy(state_bucket: str, state_key: str, artifacts_bucket: str, run_id: str) -> dict[str, Any]:
    """The inline half of a plan phase's session policy.

    Reads come from the managed `ReadOnlyAccess` policy the session unions this
    with. Writes are allowed only against this workspace's state object, its
    lock and this run's artifact keys, because `terraform plan` does write: it
    takes the S3 lock, refreshes state and uploads the plan files.

    Args:
        state_bucket: The state bucket.
        state_key: This workspace's state object key.
        artifacts_bucket: The artifacts bucket.
        run_id: This run, which scopes the artifact keys.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "StateAndLock",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                "Resource": [
                    f"arn:aws:s3:::{state_bucket}/{state_key}",
                    f"arn:aws:s3:::{state_bucket}/{state_key}.tflock",
                ],
            },
            {
                "Sid": "PlanArtifacts",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject"],
                "Resource": [f"arn:aws:s3:::{artifacts_bucket}/runs/{run_id}/*"],
            },
            {
                "Sid": "StateEncryption",
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                "Resource": "*",
            },
        ],
    }


def apply_policy() -> dict[str, Any]:
    """The session policy for an apply phase, which narrows nothing.

    An apply's boundary is the workspace's run role, not the session: a policy
    that tried to enumerate what an arbitrary configuration is allowed to create
    would break every provider it failed to anticipate.
    """
    return {
        "Version": "2012-10-17",
        "Statement": [{"Sid": "InheritRole", "Effect": "Allow", "Action": "*", "Resource": "*"}],
    }


def for_phase(
    phase: Phase,
    *,
    state_bucket: str,
    state_key: str,
    artifacts_bucket: str,
    run_id: str,
) -> SessionPolicy:
    """The session policy for one phase of one run, inline document and ARNs."""
    if phase == "plan":
        return SessionPolicy(
            document=plan_policy(state_bucket, state_key, artifacts_bucket, run_id),
            policy_arns=PLAN_SESSION_POLICY_ARNS,
        )
    return SessionPolicy(document=apply_policy(), policy_arns=())


__all__ = ["PLAN_SESSION_POLICY_ARNS", "SessionPolicy", "apply_policy", "for_phase", "plan_policy"]
