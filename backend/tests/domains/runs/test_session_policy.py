"""Coverage for the session policies a run phase's vended credentials carry."""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.domains.runs import session_policy

ACTION_PATTERN = re.compile(r"^[a-z0-9-]+:[A-Za-z0-9*?]+$")
"""What IAM accepts for an action other than the bare `*`.

The service portion takes no wildcard, which is the rule that made `*:Get*`
a MalformedPolicyDocumentException and broke every plan.
"""

BUCKET = "webbpulse-terraform-test-state"
WORKSPACE_ID = "ws-1"
KMS_KEY = "arn:aws:kms:us-west-2:870550636948:key/state"


def statements(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A policy's statements keyed by Sid."""
    return {statement["Sid"]: statement for statement in document["Statement"]}


def actions(document: dict[str, Any]) -> list[str]:
    """Every action named anywhere in a policy document, flattened."""
    collected: list[str] = []
    for statement in document["Statement"]:
        action = statement["Action"]
        collected.extend([action] if isinstance(action, str) else action)
    return collected


SECRET_SIDS = ["PlanReadSecretValues", "PlanDecryptViaSecretsAndSsm"]

READ_ONLY_ACTION = re.compile(r"^(secretsmanager:GetSecretValue|kms:Decrypt|sts:AssumeRole)$")
"""The only actions a plan's inline document may name: reads, plus assuming a listed reader."""


def test_the_plan_phase_is_read_only_access_plus_secret_reads() -> None:
    """A plan's workspace session is ReadOnlyAccess and an inline document holding the secret reads alone."""
    policy = session_policy.for_phase("plan")
    assert policy.policy_arns == ("arn:aws:iam::aws:policy/ReadOnlyAccess",)
    assert policy.document is not None
    assert list(statements(policy.document)) == SECRET_SIDS


def test_the_plan_may_read_any_secret_value() -> None:
    """GetSecretValue covers every secret the run role reaches, and nothing else in Secrets Manager."""
    statement = statements(session_policy.plan_document())["PlanReadSecretValues"]
    assert statement == {
        "Sid": "PlanReadSecretValues",
        "Effect": "Allow",
        "Action": "secretsmanager:GetSecretValue",
        "Resource": "arn:aws:secretsmanager:*:*:secret:*",
    }


def test_the_plan_decrypts_only_through_secrets_manager_and_ssm() -> None:
    """kms:Decrypt carries a ViaService condition, so the plan cannot call KMS on ciphertext directly."""
    statement = statements(session_policy.plan_document())["PlanDecryptViaSecretsAndSsm"]
    assert statement["Action"] == "kms:Decrypt"
    assert statement["Condition"] == {
        "StringLike": {"kms:ViaService": ["secretsmanager.*.amazonaws.com", "ssm.*.amazonaws.com"]}
    }


def test_the_plan_document_names_no_write_action() -> None:
    """Every action in the plan's inline document is a read or a reader assume, and each is a valid IAM action."""
    document = session_policy.plan_document([READER])
    for statement in document["Statement"]:
        assert statement["Effect"] == "Allow"
        assert "NotAction" not in statement and "NotResource" not in statement
    for action in actions(document):
        assert ACTION_PATTERN.match(action), action
        assert READ_ONLY_ACTION.match(action), action


def test_the_secret_statements_are_not_shared_mutable_state() -> None:
    """Editing one returned document leaves the next plan's untouched."""
    first = session_policy.plan_document()
    first["Statement"][0]["Resource"] = "*"
    assert session_policy.plan_document()["Statement"][0]["Resource"] == "arn:aws:secretsmanager:*:*:secret:*"


def test_the_apply_phase_narrows_nothing() -> None:
    """An apply is bounded by the run role alone."""
    policy = session_policy.for_phase("apply")
    assert policy.policy_arns == ()
    assert policy.document is None


@pytest.mark.parametrize("phase", ["plan", "apply"])
def test_every_state_action_is_a_valid_iam_action(phase: str) -> None:
    """Each action names a real service prefix with no wildcard."""
    for action in actions(session_policy.state_policy(BUCKET, WORKSPACE_ID, KMS_KEY, phase)):  # type: ignore[arg-type]
        assert ACTION_PATTERN.match(action), action


@pytest.mark.parametrize("malformed", ["*:Get*", "*:Describe*", "*:List*"])
def test_the_validity_check_rejects_the_shape_that_broke_staging(malformed: str) -> None:
    """The guard itself fails on the exact actions STS rejected."""
    assert not ACTION_PATTERN.match(malformed)


@pytest.mark.parametrize("phase", ["plan", "apply"])
def test_state_objects_are_scoped_to_the_workspace_prefix(phase: str) -> None:
    """Every object resource sits under `workspaces/<id>/`, and listing is limited to that prefix."""
    document = session_policy.state_policy(BUCKET, WORKSPACE_ID, KMS_KEY, phase)  # type: ignore[arg-type]
    by_sid = statements(document)
    prefix = f"arn:aws:s3:::{BUCKET}/workspaces/{WORKSPACE_ID}/"
    for sid, statement in by_sid.items():
        if sid in ("ListWorkspaceState", "StateEncryption"):
            continue
        for resource in statement["Resource"]:
            assert resource.startswith(prefix), resource
    listing = by_sid["ListWorkspaceState"]
    assert listing["Resource"] == [f"arn:aws:s3:::{BUCKET}"]
    assert listing["Condition"] == {"StringLike": {"s3:prefix": [f"workspaces/{WORKSPACE_ID}/*"]}}
    assert by_sid["StateEncryption"]["Resource"] == [KMS_KEY]
    assert not any("*" == action or action.endswith(":*") for action in actions(document))


def test_a_plan_may_write_only_lock_objects() -> None:
    """A plan reads state and takes the lock, but cannot rewrite the state itself."""
    by_sid = statements(session_policy.state_policy(BUCKET, WORKSPACE_ID, KMS_KEY, "plan"))
    assert "WorkspaceStateWrites" not in by_sid
    assert by_sid["WorkspaceStateLocks"]["Resource"] == [f"arn:aws:s3:::{BUCKET}/workspaces/{WORKSPACE_ID}/*.tflock"]


def test_an_apply_may_write_the_workspace_state() -> None:
    """An apply writes and deletes objects under its own prefix."""
    by_sid = statements(session_policy.state_policy(BUCKET, WORKSPACE_ID, KMS_KEY, "apply"))
    assert by_sid["WorkspaceStateWrites"]["Resource"] == [f"arn:aws:s3:::{BUCKET}/workspaces/{WORKSPACE_ID}/*"]
    assert set(by_sid["WorkspaceStateWrites"]["Action"]) == {"s3:PutObject", "s3:DeleteObject"}


def test_another_workspace_prefix_is_not_a_prefix_match() -> None:
    """`ws-1` does not reach `ws-10`, because the prefix ends in a slash."""
    document = session_policy.state_policy(BUCKET, WORKSPACE_ID, KMS_KEY, "apply")
    for statement in document["Statement"]:
        for resource in statement["Resource"]:
            assert "workspaces/ws-1*" not in resource


READER = "arn:aws:iam::488386929690:role/WebbPulse-Terraform-Route53-Reader"
OTHER_READER = "arn:aws:iam::123456789012:role/Other-Reader"


def test_a_plan_with_reader_roles_may_assume_exactly_those() -> None:
    """The plan keeps ReadOnlyAccess and the secret reads, and gains `sts:AssumeRole` on the named ARNs."""
    policy = session_policy.for_phase("plan", [READER, OTHER_READER, READER])
    assert policy.policy_arns == ("arn:aws:iam::aws:policy/ReadOnlyAccess",)
    assert policy.document is not None
    by_sid = statements(policy.document)
    assert list(by_sid) == [*SECRET_SIDS, "PlanAssumeReaderRoles"]
    assert by_sid["PlanAssumeReaderRoles"] == {
        "Sid": "PlanAssumeReaderRoles",
        "Effect": "Allow",
        "Action": "sts:AssumeRole",
        "Resource": [READER, OTHER_READER],
    }


def test_an_empty_reader_list_adds_no_assume_statement() -> None:
    """No reader roles means the secret reads alone."""
    for arns in ([], [""]):
        document = session_policy.for_phase("plan", arns).document
        assert document is not None
        assert list(statements(document)) == SECRET_SIDS


def test_the_apply_phase_ignores_reader_roles() -> None:
    """An apply is bounded by the run role alone, list or not."""
    policy = session_policy.for_phase("apply", [READER])
    assert policy.policy_arns == ()
    assert policy.document is None


def test_the_largest_allowed_reader_list_fits_the_session_policy_limit() -> None:
    """Ten ARNs of the longest accepted length, the secret reads and ReadOnlyAccess stay inside the STS limit."""
    from app.domains.workspaces.schemas.workspace import (
        PLAN_ASSUME_ROLE_ARN_MAX_LENGTH,
        PLAN_ASSUME_ROLE_ARNS_MAX,
        validate_plan_assume_role_arns,
    )

    head = "arn:aws:iam::123456789012:role/"
    arns = [
        f"{head}{index:02d}{'r' * (PLAN_ASSUME_ROLE_ARN_MAX_LENGTH - len(head) - 2)}"
        for index in range(PLAN_ASSUME_ROLE_ARNS_MAX)
    ]
    assert all(len(arn) == PLAN_ASSUME_ROLE_ARN_MAX_LENGTH for arn in arns)
    assert validate_plan_assume_role_arns(arns) == arns
    size = session_policy.plaintext_size(session_policy.for_phase("plan", arns))
    assert size <= session_policy.SESSION_POLICY_PLAINTEXT_LIMIT, size


def test_ten_realistic_reader_roles_fit_the_session_policy_limit() -> None:
    """Ten pathed reader roles with 64 character names, the IAM name maximum, fit beside the secret reads."""
    from app.domains.workspaces.schemas.workspace import PLAN_ASSUME_ROLE_ARNS_MAX, validate_plan_assume_role_arns

    arns = [
        f"arn:aws:iam::{index:012d}:role/terraform/readers/{f'WebbPulse-Platform-Route53-Reader-{index:02d}':R<64}"
        for index in range(PLAN_ASSUME_ROLE_ARNS_MAX)
    ]
    assert validate_plan_assume_role_arns(arns) == arns
    size = session_policy.plaintext_size(session_policy.for_phase("plan", arns))
    assert size <= session_policy.SESSION_POLICY_PLAINTEXT_LIMIT, size


def test_a_reader_arn_past_the_cap_is_refused() -> None:
    """One character over the cap fails validation, so no stored list can overflow the STS limit."""
    from app.domains.workspaces.schemas.workspace import (
        PLAN_ASSUME_ROLE_ARN_MAX_LENGTH,
        validate_plan_assume_role_arns,
    )

    head = "arn:aws:iam::123456789012:role/"
    with pytest.raises(ValueError):
        validate_plan_assume_role_arns([head + "r" * (PLAN_ASSUME_ROLE_ARN_MAX_LENGTH - len(head) + 1)])
