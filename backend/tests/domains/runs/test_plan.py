"""The plan route, its summarisation, its counts and its redaction."""

import json
from typing import Any

import boto3

from app.domains.runs import service as runs_service
from app.domains.runs.schemas.run import RunPlan
from tests.conftest import ARTIFACTS_BUCKET, REGION

BASE = "/api/v1/runs"


def resource_change(
    address: str,
    actions: list[str],
    **overrides: Any,
) -> dict[str, Any]:
    """One `resource_changes` entry as `terraform show -json` writes it."""
    change: dict[str, Any] = {"actions": actions, "before": None, "after": {}}
    change.update(overrides.pop("change", {}))
    entry: dict[str, Any] = {
        "address": address,
        "mode": "managed",
        "type": "aws_s3_bucket",
        "name": address.split(".")[-1],
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": change,
    }
    entry.update(overrides)
    return entry


def upload_plan(run_id: str, document: dict[str, Any]) -> None:
    """Put `document` where the runner would upload this run's plan JSON."""
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=ARTIFACTS_BUCKET,
        Key=runs_service.plan_json_key(run_id),
        Body=json.dumps(document).encode(),
        ContentType="application/json",
    )


def test_the_counts_follow_terraform_s_summary_line():
    """Create adds, update changes, delete destroys, read and no-op count for nothing."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change("aws_s3_bucket.a", ["create"]),
                resource_change("aws_s3_bucket.b", ["create"]),
                resource_change("aws_s3_bucket.c", ["update"]),
                resource_change("aws_s3_bucket.d", ["delete"]),
                resource_change("data.aws_ami.e", ["read"], mode="data"),
                resource_change("aws_s3_bucket.f", ["no-op"]),
            ]
        },
    )
    assert summary["changes"] == {"add": 2, "change": 1, "destroy": 1}
    assert summary["has_changes"] is True


def test_a_replacement_counts_on_both_sides_in_either_order():
    """Terraform writes a replacement either way round, and both add and destroy."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change("aws_s3_bucket.a", ["delete", "create"]),
                resource_change("aws_s3_bucket.b", ["create", "delete"]),
            ]
        },
    )
    assert summary["changes"] == {"add": 2, "change": 0, "destroy": 2}
    assert [entry["action"] for entry in summary["resource_changes"]] == ["replace", "replace"]


def test_an_empty_plan_has_no_changes():
    """A plan that found nothing reports `has_changes` false."""
    summary = runs_service.summarise_plan("run-x", {"resource_changes": []})
    assert summary["changes"] == {"add": 0, "change": 0, "destroy": 0}
    assert summary["has_changes"] is False


def test_resource_changes_keep_terraform_s_order():
    """The viewer renders the list in the order the plan wrote it."""
    addresses = [f"aws_s3_bucket.b{index}" for index in range(5)]
    summary = runs_service.summarise_plan(
        "run-x",
        {"resource_changes": [resource_change(address, ["create"]) for address in addresses]},
    )
    assert [entry["address"] for entry in summary["resource_changes"]] == addresses


def test_a_small_plan_keeps_its_no_ops():
    """Under the cap every entry is returned, so an unchanged resource still renders."""
    summary = runs_service.summarise_plan(
        "run-x",
        {"resource_changes": [resource_change(f"aws_s3_bucket.b{index}", ["no-op"]) for index in range(10)]},
    )
    assert len(summary["resource_changes"]) == 10


def test_a_large_plan_drops_its_no_ops():
    """Above the cap the unchanged entries go, so the payload stays bounded."""
    entries = [resource_change(f"aws_s3_bucket.b{index}", ["no-op"]) for index in range(runs_service.NO_OP_KEPT_BELOW)]
    entries.append(resource_change("aws_s3_bucket.changed", ["create"]))
    summary = runs_service.summarise_plan("run-x", {"resource_changes": entries})
    assert [entry["address"] for entry in summary["resource_changes"]] == ["aws_s3_bucket.changed"]
    assert summary["changes"] == {"add": 1, "change": 0, "destroy": 0}


def test_the_fields_are_carried_across():
    """Every contract field is taken from the plan, with the documented defaults."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "terraform_version": "1.13.3",
            "resource_changes": [
                resource_change(
                    "module.net.aws_s3_bucket.a",
                    ["update"],
                    module_address="module.net",
                    action_reason="replace_because_tainted",
                    change={
                        "actions": ["update"],
                        "before": {"tags": {"env": "staging"}},
                        "after": {"tags": {"env": "prod"}},
                        "after_unknown": {"arn": True},
                        "replace_paths": [["name"]],
                    },
                )
            ],
        },
    )
    assert summary["terraform_version"] == "1.13.3"
    entry = summary["resource_changes"][0]
    assert entry["module_address"] == "module.net"
    assert entry["mode"] == "managed"
    assert entry["provider_name"] == "registry.terraform.io/hashicorp/aws"
    assert entry["action_reason"] == "replace_because_tainted"
    assert entry["before"] == {"tags": {"env": "staging"}}
    assert entry["after"] == {"tags": {"env": "prod"}}
    assert entry["after_unknown"] == {"arn": True}
    assert entry["replace_paths"] == [["name"]]


def test_absent_optional_fields_default():
    """A plan omitting the optional fields still answers the contract's shape."""
    entry = runs_service.summarise_plan(
        "run-x",
        {"resource_changes": [{"address": "aws_s3_bucket.a", "type": "aws_s3_bucket", "name": "a", "change": {}}]},
    )["resource_changes"][0]
    assert entry["module_address"] == ""
    assert entry["provider_name"] == ""
    assert entry["action_reason"] == ""
    assert entry["replace_paths"] == []
    assert entry["action"] == "no-op"


def test_a_sensitive_attribute_is_redacted():
    """A value the plan marked sensitive never reaches the response."""
    entry = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change(
                    "aws_db_instance.a",
                    ["update"],
                    change={
                        "actions": ["update"],
                        "before": {"password": "old-secret", "name": "db"},
                        "after": {"password": "new-secret", "name": "db"},
                        "before_sensitive": {"password": True},
                        "after_sensitive": {"password": True},
                    },
                )
            ]
        },
    )["resource_changes"][0]
    assert entry["before"] == {"password": runs_service.REDACTED, "name": "db"}
    assert entry["after"] == {"password": runs_service.REDACTED, "name": "db"}


def test_redaction_reaches_nested_dicts_and_lists():
    """A secret nested under a block or a list element is redacted too."""
    entry = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change(
                    "aws_ecs_task_definition.a",
                    ["create"],
                    change={
                        "actions": ["create"],
                        "before": None,
                        "after": {
                            "container": {"env": {"token": "secret", "region": "us-west-2"}},
                            "secrets": ["one", "two"],
                        },
                        "after_sensitive": {
                            "container": {"env": {"token": True}},
                            "secrets": [True, False],
                        },
                    },
                )
            ]
        },
    )["resource_changes"][0]
    assert entry["after"] == {
        "container": {"env": {"token": runs_service.REDACTED, "region": "us-west-2"}},
        "secrets": [runs_service.REDACTED, "two"],
    }


def test_a_whole_branch_marked_sensitive_is_redacted():
    """`True` against a whole object redacts the object, not just its leaves."""
    entry = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change(
                    "aws_secretsmanager_secret_version.a",
                    ["create"],
                    change={
                        "actions": ["create"],
                        "before": None,
                        "after": {"secret_string": {"user": "a", "password": "b"}},
                        "after_sensitive": {"secret_string": True},
                    },
                )
            ]
        },
    )["resource_changes"][0]
    assert entry["after"] == {"secret_string": runs_service.REDACTED}


def test_a_non_object_before_survives():
    """A scalar or list on either side is carried through rather than rejected.

    The plan format does not promise an object there, and a run view that fails
    on an unusual plan is worse than one that renders whatever was written.
    """
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change(
                    "aws_s3_bucket.a",
                    ["update"],
                    change={
                        "actions": ["update"],
                        "before": "a bare string",
                        "after": ["one", "two"],
                    },
                )
            ]
        },
    )
    entry = summary["resource_changes"][0]
    assert entry["before"] == "a bare string"
    assert entry["after"] == ["one", "two"]
    assert RunPlan.model_validate(summary).resource_changes[0].before == "a bare string"


def test_a_non_object_marked_sensitive_is_still_redacted():
    """Redaction reaches a scalar and a list, not only an object's keys."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [
                resource_change(
                    "aws_s3_bucket.a",
                    ["update"],
                    change={
                        "actions": ["update"],
                        "before": "hunter2",
                        "before_sensitive": True,
                        "after": ["hunter2", "public"],
                        "after_sensitive": [True, False],
                    },
                )
            ]
        },
    )
    entry = summary["resource_changes"][0]
    assert entry["before"] == runs_service.REDACTED
    assert entry["after"] == [runs_service.REDACTED, "public"]
    assert "hunter2" not in json.dumps(summary)


def test_output_changes_are_summarised():
    """Each root output becomes one entry carrying its flattened action."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [],
            "output_changes": {
                "url": {"actions": ["create"], "before": None, "after": "https://example.com"},
                "stable": {"actions": ["no-op"], "before": "same", "after": "same"},
            },
        },
    )
    by_name = {entry["name"]: entry for entry in summary["output_changes"]}
    assert by_name["url"]["action"] == "create"
    assert by_name["url"]["after"] == "https://example.com"
    assert by_name["stable"]["action"] == "no-op"


def test_an_output_change_alone_is_a_change():
    """A plan with no resource changes but a changed output still has changes."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [],
            "output_changes": {"url": {"actions": ["update"], "before": "a", "after": "b"}},
        },
    )
    assert summary["changes"] == {"add": 0, "change": 0, "destroy": 0}
    assert summary["has_changes"] is True


def test_an_unchanged_output_alone_is_not_a_change():
    """A no-op output does not make an otherwise empty plan look like work."""
    summary = runs_service.summarise_plan(
        "run-x",
        {
            "resource_changes": [],
            "output_changes": {"url": {"actions": ["no-op"], "before": "a", "after": "a"}},
        },
    )
    assert summary["has_changes"] is False


def test_a_sensitive_output_is_redacted():
    """A sensitive output carries the redaction string on both sides."""
    entry = runs_service.summarise_plan(
        "run-x",
        {
            "output_changes": {
                "token": {
                    "actions": ["update"],
                    "before": "old",
                    "after": "new",
                    "before_sensitive": True,
                    "after_sensitive": True,
                }
            }
        },
    )["output_changes"][0]
    assert entry["sensitive"] is True
    assert entry["before"] == runs_service.REDACTED
    assert entry["after"] == runs_service.REDACTED


def test_an_unknown_output_carries_its_flag():
    """`after_unknown` reaches the viewer so it can render a known-after-apply output."""
    entry = runs_service.summarise_plan(
        "run-x",
        {"output_changes": {"arn": {"actions": ["create"], "after_unknown": True}}},
    )["output_changes"][0]
    assert entry["after_unknown"] is True


def test_the_route_returns_the_plan(auth_client, created_run):
    """An uploaded plan comes back summarised, and the raw document does not."""
    run_id = created_run["run_id"]
    upload_plan(
        run_id,
        {
            "format_version": "1.2",
            "terraform_version": "1.13.3",
            "resource_changes": [
                resource_change("aws_s3_bucket.a", ["create"]),
                resource_change("aws_s3_bucket.b", ["delete", "create"]),
            ],
            "output_changes": {"url": {"actions": ["create"], "after": "https://example.com"}},
            "prior_state": {"values": {"root_module": {}}},
        },
    )

    response = auth_client.get(f"{BASE}/{run_id}/plan")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == run_id
    assert body["terraform_version"] == "1.13.3"
    assert body["changes"] == {"add": 2, "change": 0, "destroy": 1}
    assert [entry["address"] for entry in body["resource_changes"]] == [
        "aws_s3_bucket.a",
        "aws_s3_bucket.b",
    ]
    assert body["has_changes"] is True
    assert "prior_state" not in body
    assert "format_version" not in body


def test_the_route_redacts_sensitive_values(auth_client, created_run):
    """No sealed value crosses the wire, which is the point of summarising server side."""
    run_id = created_run["run_id"]
    upload_plan(
        run_id,
        {
            "resource_changes": [
                resource_change(
                    "aws_db_instance.a",
                    ["create"],
                    change={
                        "actions": ["create"],
                        "before": None,
                        "after": {"password": "hunter2"},
                        "after_sensitive": {"password": True},
                    },
                )
            ]
        },
    )

    response = auth_client.get(f"{BASE}/{run_id}/plan")
    assert response.status_code == 200, response.text
    assert "hunter2" not in response.text
    assert response.json()["resource_changes"][0]["after"] == {"password": runs_service.REDACTED}


def test_a_run_without_a_plan_is_404(auth_client, created_run):
    """A run polled before its plan uploaded says so rather than erroring."""
    response = auth_client.get(f"{BASE}/{created_run['run_id']}/plan")
    assert response.status_code == 404
    assert response.json()["message"] == "That run has no plan yet."


def test_an_absent_run_is_404(auth_client):
    """A plan for a run that is not there is the run's own 404."""
    response = auth_client.get(f"{BASE}/run-01JBQ0000000000000000000AA/plan")
    assert response.status_code == 404
    assert response.json()["message"] == "No such run."


def test_the_plan_needs_a_read_scope(client, created_run):
    """An unauthenticated caller cannot read a run's plan."""
    assert client.get(f"{BASE}/{created_run['run_id']}/plan").status_code == 401


def test_summarise_outputs_redacts_sensitive_values():
    """A sensitive output keeps its name but never its value, whatever the upload held."""
    outputs = runs_service.summarise_outputs(
        {
            "url": {"sensitive": False, "type": "string", "value": "https://example.com"},
            "token": {"sensitive": True, "type": "string", "value": "leaked-if-shown"},
            "bad": "not an object",
        }
    )
    assert outputs == [
        {"name": "token", "value": runs_service.REDACTED, "sensitive": True},
        {"name": "url", "value": "https://example.com", "sensitive": False},
    ]
    assert runs_service.summarise_outputs([]) == []


def test_an_unapplied_plan_has_no_applied_outputs(auth_client, created_run):
    """Before an apply there is nothing to report, so the field is null."""
    run_id = created_run["run_id"]
    upload_plan(run_id, {"format_version": "1.2", "resource_changes": []})
    assert auth_client.get(f"{BASE}/{run_id}/plan").json()["applied_outputs"] is None


def test_an_applied_run_returns_its_outputs(auth_client, awaiting_confirmation):
    """Once applied, the plan carries the outputs the apply left, sensitive ones redacted."""
    run_id = awaiting_confirmation["run_id"]
    upload_plan(run_id, {"format_version": "1.2", "resource_changes": []})
    auth_client.post(f"{BASE}/{run_id}/confirm")
    runs_service.record_phase_result(
        run_id, {"phase": "apply", "exit_code": 0, "changes": {"add": 2, "change": 1, "destroy": 0}}
    )
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=ARTIFACTS_BUCKET,
        Key=runs_service.outputs_key(run_id),
        Body=json.dumps(
            {
                "pet_name": {"sensitive": False, "type": "string", "value": "lucky-horse"},
                "secret": {"sensitive": True, "type": "string", "value": "never-shown"},
            }
        ).encode(),
    )

    body = auth_client.get(f"{BASE}/{run_id}/plan").json()
    assert body["applied_outputs"] == [
        {"name": "pet_name", "value": "lucky-horse", "sensitive": False},
        {"name": "secret", "value": runs_service.REDACTED, "sensitive": True},
    ]
    assert "never-shown" not in json.dumps(body)


def test_an_applied_run_without_outputs_reports_null(auth_client, awaiting_confirmation):
    """An apply whose outputs upload failed still serves its plan."""
    run_id = awaiting_confirmation["run_id"]
    upload_plan(run_id, {"format_version": "1.2", "resource_changes": []})
    auth_client.post(f"{BASE}/{run_id}/confirm")
    runs_service.record_phase_result(run_id, {"phase": "apply", "exit_code": 0, "changes": {}})
    body = auth_client.get(f"{BASE}/{run_id}/plan")
    assert body.status_code == 200
    assert body.json()["applied_outputs"] is None
