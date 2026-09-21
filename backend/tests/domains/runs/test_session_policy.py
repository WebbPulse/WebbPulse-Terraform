"""Coverage for the per phase session policies handed to `sts:AssumeRole`."""

from __future__ import annotations

import re

import pytest

from app.domains.runs import session_policy

ACTION_PATTERN = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9*?]+$")
"""What IAM accepts for an action other than the bare `*`.

The service portion takes no wildcard, which is the rule that made `*:Get*`
a MalformedPolicyDocumentException and broke every plan.
"""

BUCKET_ARGUMENTS = {
    "state_bucket": "webbpulse-terraform-test-state",
    "state_key": "workspaces/ws-1/terraform.tfstate",
    "artifacts_bucket": "webbpulse-terraform-test-artifacts",
    "run_id": "run-01JTEST",
}


def actions(document: dict[str, object]) -> list[str]:
    """Every action named anywhere in a policy document, flattened."""
    collected: list[str] = []
    for statement in document["Statement"]:  # type: ignore[index]
        action = statement["Action"]
        collected.extend([action] if isinstance(action, str) else action)
    return collected


def test_every_plan_action_is_a_valid_iam_action() -> None:
    """Each action is the bare `*` or a real service prefix with no wildcard.

    This is the check that would have caught the malformed `*:Get*` form before
    STS did, which it did only once a run reached the Plan state in staging.
    """
    for action in actions(session_policy.plan_policy(**BUCKET_ARGUMENTS)):
        assert action == "*" or ACTION_PATTERN.match(action), action


def test_the_plan_document_names_no_wildcard_service() -> None:
    """No action carries a wildcard in its service portion."""
    for action in actions(session_policy.plan_policy(**BUCKET_ARGUMENTS)):
        assert not action.startswith("*:")


def test_the_plan_document_grants_only_state_artifacts_and_encryption() -> None:
    """Reads come from the managed policy, so the inline half stays narrow."""
    document = session_policy.plan_policy(**BUCKET_ARGUMENTS)
    sids = [statement["Sid"] for statement in document["Statement"]]
    assert sids == ["StateAndLock", "PlanArtifacts", "StateEncryption"]


def test_the_plan_phase_unions_the_managed_read_only_policy() -> None:
    """A plan's session policy ARNs are exactly ReadOnlyAccess."""
    policy = session_policy.for_phase(phase="plan", **BUCKET_ARGUMENTS)
    assert policy.policy_arns == ("arn:aws:iam::aws:policy/ReadOnlyAccess",)
    assert policy.document == session_policy.plan_policy(**BUCKET_ARGUMENTS)


def test_the_apply_phase_unions_nothing() -> None:
    """An apply narrows nothing, so it needs no managed policy."""
    policy = session_policy.for_phase(phase="apply", **BUCKET_ARGUMENTS)
    assert policy.policy_arns == ()
    assert policy.document["Statement"][0]["Action"] == "*"


def test_every_apply_action_is_a_valid_iam_action() -> None:
    """The apply document's bare `*` is the one legal way to name every service."""
    for action in actions(session_policy.apply_policy()):
        assert action == "*" or ACTION_PATTERN.match(action), action


@pytest.mark.parametrize("malformed", ["*:Get*", "*:Describe*", "*:List*"])
def test_the_validity_check_rejects_the_shape_that_broke_staging(malformed: str) -> None:
    """The guard itself fails on the exact actions STS rejected."""
    assert not ACTION_PATTERN.match(malformed)


def test_the_plan_document_scopes_writes_to_this_run() -> None:
    """State, lock and artifact resources name this workspace and this run only."""
    document = session_policy.plan_policy(**BUCKET_ARGUMENTS)
    resources: list[str] = []
    for statement in document["Statement"]:
        resource = statement["Resource"]
        resources.extend([resource] if isinstance(resource, str) else resource)
    assert f"arn:aws:s3:::{BUCKET_ARGUMENTS['artifacts_bucket']}/runs/run-01JTEST/*" in resources
    assert any(resource.endswith(".tflock") for resource in resources)
