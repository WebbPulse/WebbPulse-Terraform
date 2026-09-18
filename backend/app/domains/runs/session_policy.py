"""The session policies the runner attaches when it assumes a workspace's run role.

A session policy only ever narrows a role, so the apply phase's is the widest
thing that changes nothing: it allows everything the role already allows and
relies on the role itself for the boundary. The plan phase's is narrower than
that, and deliberately: a plan reads the world and writes nothing outside the
state and artifact buckets, so a provider bug or a malicious module in someone's
configuration cannot mutate an account during what the caller was told is a
read-only operation.

The read-only allowance is expressed as verb prefixes rather than an enumeration
of services, because an enumeration silently fails closed for a provider nobody
listed and silently fails open for a mutating call that happens to start with
`Get`. Neither is ideal, and the prefix form is the one whose failure mode is a
plan that errors rather than a plan that writes.
"""

from __future__ import annotations

from typing import Any, Final

from .schemas.run import Phase

READ_ONLY_ACTION_PREFIXES: Final = (
    "Describe*",
    "Get*",
    "List*",
    "Lookup*",
    "Search*",
    "BatchGet*",
    "Scan",
    "Query",
    "Head*",
    "Select*",
    "Check*",
    "Validate*",
    "Simulate*",
    "Preview*",
    "Estimate*",
    "Generate*Report",
    "Retrieve*",
    "View*",
    "Read*",
    "Test*",
    "Query*",
)
"""Verb prefixes a plan is allowed to call, across every service.

Wildcarded as `*:<prefix>` rather than per service. A few of these are mutating
on some service somewhere, which is why the state-write statement below is an
explicit allow rather than something inherited from a prefix.
"""

_READ_ONLY_ACTIONS: Final = tuple(f"*:{prefix}" for prefix in READ_ONLY_ACTION_PREFIXES)


def plan_policy(state_bucket: str, state_key: str, artifacts_bucket: str, run_id: str) -> dict[str, Any]:
    """The read-only session policy for a plan phase.

    Reads are allowed everywhere by verb prefix. Writes are allowed only against
    this workspace's state object, its lock and this run's artifact keys, because
    `terraform plan` does write: it takes the S3 lock, refreshes state and
    uploads the plan files.

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
                "Sid": "ReadEverything",
                "Effect": "Allow",
                "Action": list(_READ_ONLY_ACTIONS),
                "Resource": "*",
            },
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
) -> dict[str, Any]:
    """The session policy for one phase of one run."""
    if phase == "plan":
        return plan_policy(state_bucket, state_key, artifacts_bucket, run_id)
    return apply_policy()


__all__ = ["READ_ONLY_ACTION_PREFIXES", "apply_policy", "for_phase", "plan_policy"]
