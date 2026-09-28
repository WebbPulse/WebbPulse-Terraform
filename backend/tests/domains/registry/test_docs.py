"""A published version's documentation: extracted from its tarball and served for the module page."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import boto3

from app.domains.registry import docs, service
from tests.domains.registry.conftest import github_tarball

VERSION_URL = "/api/v1/registry/modules/WebbPulse/example/aws/versions"

MAIN = """
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

variable "name" {
  type        = string
  description = "The bucket name."
}

variable "tags" {
  type    = map(string)
  default = { team = "platform" }
}

variable "secret" {
  type      = string
  default   = "x"
  sensitive = true
  description = <<-EOT
    A value never shown.
  EOT
}

resource "aws_s3_bucket" "this" {
  bucket = var.name
}

output "arn" {
  value       = aws_s3_bucket.this.arn
  description = "The bucket ARN."
}
"""

CHILD = """
variable "size" {
  type    = number
  default = 3
}

output "count" {
  value = var.size
}
"""


def _pack(tmp_path: Path, files: dict[str, str]) -> Path:
    """A module tarball with `files` at its root, as the publisher stores it."""
    target = tmp_path / "module.tar.gz"
    with tarfile.open(target, "w:gz") as archive:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return target


def test_extract_reads_inputs_outputs_providers_and_resources(tmp_path):
    """The root module's interface, required inputs first."""
    extracted = docs.extract(_pack(tmp_path, {"main.tf": MAIN, "README.md": "# Example\n"}))

    assert extracted["schema"] == docs.DOCS_SCHEMA
    assert extracted["readme"] == "# Example\n"
    assert [item["name"] for item in extracted["inputs"]] == ["name", "secret", "tags"]
    name, secret, tags = extracted["inputs"]
    assert name == {
        "name": "name",
        "type": "string",
        "description": "The bucket name.",
        "default": None,
        "required": True,
        "sensitive": False,
    }
    assert secret["sensitive"] is True
    assert secret["description"] == "A value never shown."
    assert tags["type"] == "map(string)"
    assert tags["default"] == '{\n  team = "platform"\n}'
    assert extracted["outputs"] == [{"name": "arn", "description": "The bucket ARN.", "sensitive": False}]
    assert extracted["providers"] == [{"name": "aws", "source": "hashicorp/aws", "version": ">= 5.0"}]
    assert extracted["resources"] == [{"type": "aws_s3_bucket", "name": "this"}]
    assert extracted["parse_errors"] == []


def test_extract_reads_submodules_and_skips_deeper_directories(tmp_path):
    """`modules/<name>` is a submodule; examples and nested directories are not documented."""
    extracted = docs.extract(
        _pack(
            tmp_path,
            {
                "main.tf": MAIN,
                "modules/child/main.tf": CHILD,
                "modules/child/README.md": "child",
                "examples/basic/main.tf": CHILD,
                "modules/child/deep/main.tf": CHILD,
            },
        )
    )

    assert [module["name"] for module in extracted["submodules"]] == ["child"]
    child = extracted["submodules"][0]
    assert child["path"] == "modules/child"
    assert child["readme"] == "child"
    assert child["inputs"][0]["default"] == "3"
    assert child["outputs"][0]["name"] == "count"


def test_a_file_that_does_not_parse_is_named_not_fatal(tmp_path):
    """The rest of the module is still documented."""
    extracted = docs.extract(_pack(tmp_path, {"main.tf": MAIN, "broken.tf": "variable {{{"}))

    assert extracted["parse_errors"] == ["broken.tf"]
    assert extracted["outputs"][0]["name"] == "arn"


def test_an_oversized_file_is_skipped(tmp_path, monkeypatch):
    """The bounds keep a hostile module from costing more than a page view is worth."""
    monkeypatch.setattr(docs, "MAX_TF_FILE_BYTES", 10)

    assert docs.extract(_pack(tmp_path, {"main.tf": MAIN}))["parse_errors"] == ["main.tf"]


def test_publishing_stores_the_documentation(module, publish, github, settings):
    """The docs land beside the tarball when the tag publishes."""
    github.archive = github_tarball({"main.tf": MAIN, "README.md": "hello"})
    publish("1.2.3")

    body = boto3.client("s3", region_name="us-west-2").get_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=service.docs_key("WebbPulse", "example", "aws", "1.2.3")
    )["Body"]
    stored = json.loads(body.read())
    assert stored["readme"] == "hello"
    assert stored["outputs"][0]["name"] == "arn"


def test_a_failed_extraction_still_publishes(module, publish, github, settings, monkeypatch):
    """The documentation is best effort, never the reason a version fails."""

    def explode(*_args, **_kwargs):
        """Fail as a parser bug would."""
        raise RuntimeError("parser bug")

    monkeypatch.setattr(docs, "extract", explode)

    assert publish("1.2.3") == {"WebbPulse/example/aws": "published"}


def test_the_version_route_serves_the_documentation(auth_client, module, publish, github):
    """The module page's read: the version and its interface."""
    github.archive = github_tarball({"main.tf": MAIN, "modules/child/main.tf": CHILD})
    publish("1.2.3")

    response = auth_client.get(f"{VERSION_URL}/1.2.3")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "WebbPulse/example/aws"
    assert body["vcs_repo"] == "WebbPulse/terraform-aws-example"
    assert body["version"]["status"] == "published"
    assert [item["name"] for item in body["docs"]["inputs"]] == ["name", "secret", "tags"]
    assert body["docs"]["submodules"][0]["name"] == "child"


def test_a_version_published_before_docs_is_extracted_on_first_view(auth_client, module, publish, github, settings):
    """A missing document is extracted from the stored tarball and kept."""
    github.archive = github_tarball({"main.tf": MAIN})
    publish("1.2.3")
    key = service.docs_key("WebbPulse", "example", "aws", "1.2.3")
    s3 = boto3.client("s3", region_name="us-west-2")
    s3.delete_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key)

    response = auth_client.get(f"{VERSION_URL}/1.2.3")

    assert response.json()["docs"]["outputs"][0]["name"] == "arn"
    assert s3.head_object(Bucket=settings.ARTIFACTS_BUCKET, Key=key)


def test_an_old_shape_is_extracted_again(auth_client, module, publish, github, settings):
    """A document of an older schema is replaced rather than served."""
    github.archive = github_tarball({"main.tf": MAIN})
    publish("1.2.3")
    key = service.docs_key("WebbPulse", "example", "aws", "1.2.3")
    boto3.client("s3", region_name="us-west-2").put_object(
        Bucket=settings.ARTIFACTS_BUCKET, Key=key, Body=json.dumps({"schema": 0}).encode()
    )

    assert auth_client.get(f"{VERSION_URL}/1.2.3").json()["docs"]["outputs"][0]["name"] == "arn"


def test_a_failed_version_has_no_documentation(auth_client, module, publish, github):
    """The page shows the failure instead."""
    github.archive = github_tarball({"README.md": "no terraform"})
    publish("1.2.3")

    body = auth_client.get(f"{VERSION_URL}/1.2.3").json()

    assert body["version"]["status"] == "failed"
    assert body["docs"] is None


def test_an_unknown_version_is_a_404(auth_client, module):
    """Nothing sits at the version."""
    response = auth_client.get(f"{VERSION_URL}/9.9.9")

    assert response.status_code == 404
    assert response.json()["error_code"] == "REGISTRY_NOT_FOUND"


def test_a_version_that_is_not_semver_is_refused(auth_client, module):
    """The path is validated before anything is read."""
    assert auth_client.get(f"{VERSION_URL}/latest").status_code == 422


def test_deleting_the_module_removes_its_documentation(auth_client, module, publish, github, settings):
    """The docs sit under the module's prefix, so the delete takes them."""
    github.archive = github_tarball({"main.tf": MAIN})
    publish("1.2.3")

    assert auth_client.delete("/api/v1/registry/modules/WebbPulse/example/aws").status_code == 204

    listed = boto3.client("s3", region_name="us-west-2").list_objects_v2(
        Bucket=settings.ARTIFACTS_BUCKET, Prefix=service.module_prefix("WebbPulse", "example", "aws")
    )
    assert listed.get("KeyCount", 0) == 0
