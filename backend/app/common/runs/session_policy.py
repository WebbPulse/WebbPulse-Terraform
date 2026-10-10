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

A plan also reads secret values, which `ReadOnlyAccess` withholds: it lacks
`secretsmanager:GetSecretValue` and `kms:Decrypt`. Refreshing a secret version
or a SecureString parameter, and any ephemeral secret read, needs both, so a plan
session carries an inline grant for them. `kms:Decrypt` is limited by
`kms:ViaService` to Secrets Manager and SSM, so the plan cannot decrypt arbitrary
ciphertext directly. SSM reads need no statement: `ReadOnlyAccess` already holds
`ssm:Get*`. The secret statements and the reader role statement share one inline
document.

Which secrets the grant reaches depends on the run. A workspace may name secret
ARN patterns in `plan_secret_arns`; when it does, every plan of it reads those
secrets and no others. When it names none, a confirmable plan reads any secret
the run role can, because its apply holds the role's full rights anyway, and a
speculative plan, a `plan_only` run or a pull request plan, reads none.
State is not the run role's business. The S3 backend gets its own credentials,
from the control plane's state role narrowed by `state_policy` to one workspace's
prefix, so a run can reach its own state and nothing else in the bucket, and a
plan can take the lock but not rewrite the state.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

Phase = Literal["plan", "apply"]
"""A run phase, the same values as the runs domain's own `Phase`."""

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


ANY_SECRET_ARN: Final = "arn:aws:secretsmanager:*:*:secret:*"
"""The secrets a confirmable plan of a workspace with no `plan_secret_arns` may read."""


def plan_secret_statements(secret_arns: Sequence[str] | None) -> tuple[dict[str, Any], ...]:
    """The statements letting a plan read secret values: `secret_arns`, any secret for None, nothing when empty."""
    if secret_arns is None:
        resource: str | list[str] = ANY_SECRET_ARN
    else:
        arns = list(dict.fromkeys(arn for arn in secret_arns if arn))
        if not arns:
            return ()
        resource = arns
    return (
        {
            "Sid": "PlanReadSecretValues",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": resource,
        },
        {
            "Sid": "PlanDecryptViaSecretsAndSsm",
            "Effect": "Allow",
            "Action": "kms:Decrypt",
            "Resource": "*",
            "Condition": {"StringLike": {"kms:ViaService": ["secretsmanager.*.amazonaws.com", "ssm.*.amazonaws.com"]}},
        },
    )


def plan_secret_arns_for(secret_arns: Sequence[str], *, speculative: bool) -> list[str] | None:
    """The secrets one plan may read: the workspace's list, else none when speculative and any otherwise."""
    named = [arn for arn in secret_arns if arn]
    if named:
        return named
    return [] if speculative else None


def plan_assume_statement(role_arns: Sequence[str]) -> dict[str, Any] | None:
    """The statement letting a plan assume exactly `role_arns`, or None when there are none."""
    arns = list(dict.fromkeys(arn for arn in role_arns if arn))
    if not arns:
        return None
    return {
        "Sid": "PlanAssumeReaderRoles",
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": arns,
    }


def plan_document(role_arns: Sequence[str] = (), secret_arns: Sequence[str] | None = None) -> dict[str, Any] | None:
    """The inline document of a plan session, or None when it grants nothing.

    It holds the secret reads `plan_secret_statements` gives `secret_arns`, plus
    `sts:AssumeRole` on `role_arns` when any.
    """
    statements = [dict(statement) for statement in plan_secret_statements(secret_arns)]
    assume = plan_assume_statement(role_arns)
    if assume is not None:
        statements.append(assume)
    if not statements:
        return None
    return {"Version": "2012-10-17", "Statement": statements}


def encode(document: dict[str, Any]) -> str:
    """A policy document as compact JSON, the form sent to STS and counted against its limit."""
    return json.dumps(document, separators=(",", ":"))


def plaintext_size(policy: SessionPolicy) -> int:
    """The characters a session policy spends of `SESSION_POLICY_PLAINTEXT_LIMIT`."""
    inline = len(encode(policy.document)) if policy.document is not None else 0
    return inline + sum(len(arn) for arn in policy.policy_arns)


def for_phase(
    phase: Phase, plan_assume_role_arns: Sequence[str] = (), *, plan_secret_arns: Sequence[str] | None = None
) -> SessionPolicy:
    """The workspace session policy for one phase.

    A plan is `ReadOnlyAccess` plus an inline document allowing reads of the
    secrets `plan_secret_arns` names (any for None, none when empty) and, when
    the workspace names any, `sts:AssumeRole` on its reader roles. An apply is
    the role itself, and neither list applies to it.
    """
    if phase == "plan":
        return SessionPolicy(
            document=plan_document(plan_assume_role_arns, plan_secret_arns),
            policy_arns=PLAN_SESSION_POLICY_ARNS,
        )
    return SessionPolicy(document=None, policy_arns=())


def plan_policy_fits(plan_assume_role_arns: Sequence[str], plan_secret_arns: Sequence[str]) -> bool:
    """Whether every plan session of a workspace with these lists stays within `SESSION_POLICY_PLAINTEXT_LIMIT`."""
    candidates = [list(plan_secret_arns)] if any(plan_secret_arns) else [None, []]
    return all(
        plaintext_size(for_phase("plan", plan_assume_role_arns, plan_secret_arns=secrets))
        <= SESSION_POLICY_PLAINTEXT_LIMIT
        for secrets in candidates
    )


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
    "ANY_SECRET_ARN",
    "LOCK_FILE_SUFFIX",
    "PLAN_SESSION_POLICY_ARNS",
    "SESSION_POLICY_PLAINTEXT_LIMIT",
    "SessionPolicy",
    "encode",
    "for_phase",
    "plaintext_size",
    "plan_assume_statement",
    "plan_document",
    "plan_policy_fits",
    "plan_secret_arns_for",
    "plan_secret_statements",
    "state_policy",
    "state_prefix",
]
