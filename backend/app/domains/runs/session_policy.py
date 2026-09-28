"""The session policies the control plane attaches when it vends a run phase's credentials.

A session policy only ever narrows a role. The plan phase's workspace session is
the AWS managed `ReadOnlyAccess`, passed as a session policy ARN rather than
written inline, because IAM rejects a wildcard in an action's service portion:
`*:Get*` is malformed and only the bare `*` may stand for every service. A plan
therefore reads as widely as the role allows and writes nothing, so a provider
bug or a malicious module cannot mutate an account during what the caller was
told is a read-only operation. The apply phase's session is not narrowed at all:
its boundary is the workspace's run role.

`ReadOnlyAccess` holds no `sts:AssumeRole`, so a plan whose providers assume a
role in another account would fail. A workspace may therefore name exact reader
roles in `plan_assume_role_arns`, and a plan's session then also carries an
inline document allowing `sts:AssumeRole` on those ARNs alone. Session policies
passed together are one boundary, so the session may do what either allows and
the role permits. A writer role never belongs on the list: plans select readers,
applies the writers.

State is not the run role's business. The S3 backend gets its own credentials,
from the control plane's state role narrowed by `state_policy` to one workspace's
prefix, so a run can reach its own state and nothing else in the bucket, and a
plan can take the lock but not rewrite the state.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from .schemas.run import Phase

PLAN_SESSION_POLICY_ARNS: Final = ("arn:aws:iam::aws:policy/ReadOnlyAccess",)
"""The managed policies a plan's workspace session is limited to.

`ReadOnlyAccess` is AWS's own enumeration of every non mutating action across
every service, which is the thing an inline document cannot express.
"""

SESSION_POLICY_PLAINTEXT_LIMIT: Final = 2048
"""The characters STS accepts across the inline document and managed ARNs of one session."""


@dataclass(frozen=True)
class SessionPolicy:
    """One phase's session policy, in both forms `AssumeRole` accepts.

    Attributes:
        document: The inline policy, passed as `Policy`, or None for none.
        policy_arns: The managed policies, passed as `PolicyArns`.
    """

    document: dict[str, Any] | None
    policy_arns: tuple[str, ...]


def plan_assume_document(role_arns: Sequence[str]) -> dict[str, Any] | None:
    """The inline statement letting a plan assume exactly `role_arns`, or None when there are none."""
    arns = list(dict.fromkeys(arn for arn in role_arns if arn))
    if not arns:
        return None
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PlanAssumeReaderRoles",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": arns,
            }
        ],
    }


def encode(document: dict[str, Any]) -> str:
    """A policy document as compact JSON, the form sent to STS and counted against its limit."""
    return json.dumps(document, separators=(",", ":"))


def plaintext_size(policy: SessionPolicy) -> int:
    """The characters a session policy spends of `SESSION_POLICY_PLAINTEXT_LIMIT`."""
    inline = len(encode(policy.document)) if policy.document is not None else 0
    return inline + sum(len(arn) for arn in policy.policy_arns)


def for_phase(phase: Phase, plan_assume_role_arns: Sequence[str] = ()) -> SessionPolicy:
    """The workspace session policy for one phase.

    A plan is `ReadOnlyAccess`, plus `sts:AssumeRole` on the workspace's named
    reader roles when it has any. An apply is the role itself, and the list does
    not apply to it.
    """
    if phase == "plan":
        return SessionPolicy(
            document=plan_assume_document(plan_assume_role_arns),
            policy_arns=PLAN_SESSION_POLICY_ARNS,
        )
    return SessionPolicy(document=None, policy_arns=())


def state_prefix(workspace_id: str) -> str:
    """The key prefix every object of one workspace's state lives under."""
    return f"workspaces/{workspace_id}/"


LOCK_FILE_SUFFIX: Final = ".tflock"
"""The suffix of the S3 backend's native lock object, the only one a plan writes."""


def state_policy(state_bucket: str, workspace_id: str, kms_key_arn: str, phase: Phase) -> dict[str, Any]:
    """The session policy that narrows the state role to one workspace and phase.

    Objects are reachable only under the workspace's own prefix, which holds the
    state, its lock and, since the runner points `workspace_key_prefix` there
    too, any CLI workspace states. Listing is allowed only for that prefix, so
    another workspace's keys cannot even be named. A plan persists no state, so
    it may read the state but write and delete only lock objects; an apply may
    write the state itself. The key grant is the state key alone.

    Args:
        state_bucket: The state bucket.
        workspace_id: The workspace the run belongs to.
        kms_key_arn: The state bucket's KMS key.
        phase: The phase the credentials are for.
    """
    prefix = state_prefix(workspace_id)
    objects = f"arn:aws:s3:::{state_bucket}/{prefix}*"
    if phase == "apply":
        writes: list[dict[str, Any]] = [
            {
                "Sid": "WorkspaceStateWrites",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": [objects],
            }
        ]
    else:
        writes = [
            {
                "Sid": "WorkspaceStateLocks",
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:DeleteObject"],
                "Resource": [f"{objects}{LOCK_FILE_SUFFIX}"],
            }
        ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "WorkspaceStateReads",
                "Effect": "Allow",
                "Action": ["s3:GetObject"],
                "Resource": [objects],
            },
            *writes,
            {
                "Sid": "ListWorkspaceState",
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{state_bucket}"],
                "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*"]}},
            },
            {
                "Sid": "StateEncryption",
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"],
                "Resource": [kms_key_arn],
            },
        ],
    }


__all__ = [
    "LOCK_FILE_SUFFIX",
    "PLAN_SESSION_POLICY_ARNS",
    "SESSION_POLICY_PLAINTEXT_LIMIT",
    "SessionPolicy",
    "encode",
    "for_phase",
    "plaintext_size",
    "plan_assume_document",
    "state_policy",
    "state_prefix",
]
