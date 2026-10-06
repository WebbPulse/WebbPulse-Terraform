"""Plan, and only if the plane cannot, apply the control plane's own Terraform locally.

The plane runs `WebbPulse-Terraform-staging` and `WebbPulse-Terraform` itself, so a
bad apply can take down the thing that would apply the fix. This script runs the
same configuration from a checkout against the same S3 state object the runner
uses, with the AdministratorAccess profiles and the exact engine the state was
written by. It needs nothing from the plane's API or registry: variables are read
from the plane's DynamoDB table, platform-modules come from GitHub at the newest
v2 tag, and the Route 53 providers run as the management account's administrator
because the workspace's Route 53 roles trust only the plane.

Usage, from the repository root:

    python3 ops/break-glass/break_glass.py plan staging --engine-bin /path/to/terraform
    python3 ops/break-glass/break_glass.py apply production --engine-bin /path/to/terraform

`plan` runs with `-lock=false` and writes nothing anywhere. `apply` saves a plan,
prints its summary and applies that plan only after `yes` is typed. Nothing here
prints state, variable values or plan values.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

STATE_PROFILE = "WebbPulse-Terraform-Production/AdministratorAccess"
STATE_REGION = "us-west-2"
STATE_BUCKET = "webbpulse-terraform-prod-state"
STATE_KMS_ALIAS = "alias/webbpulse-terraform-prod-state"
DNS_PROFILE = "WebbPulse-Management/AdministratorAccess"
VARIABLES_TABLE = "webbpulse-terraform-prod-variables"
MODULES_REPO = "https://github.com/WebbPulse/terraform-aws-platform-modules.git"
REGISTRY_SOURCE = re.compile(
    r'source(\s*)= "terraform\.webbpulse\.com/WebbPulse/platform-modules/aws//(?P<path>[^"]+)"\n(?P<indent>\s*)version\s*= "~> 2\.(?P<minor>\d+)"\n'
)
VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class Target:
    """One of the plane's own workspaces and the profile its providers run as."""

    workspace_id: str
    provider_profile: str
    dns_aliases: tuple[str, ...]


TARGETS = {
    "staging": Target("ws-01M3KGVRSDRW9741ADKER2B7YG", "WebbPulse-Terraform-Staging/AdministratorAccess", ("parent_dns",)),
    "production": Target(
        "ws-01M3KGVX29VCFSR9NSPKB0ST6Q", "WebbPulse-Terraform-Production/AdministratorAccess", ("dns", "parent_dns")
    ),
}


def aws(*args: str) -> str:
    """Run the AWS CLI as the state account's administrator and return stdout."""
    env = dict(os.environ, AWS_PROFILE=STATE_PROFILE, AWS_REGION=STATE_REGION)
    return subprocess.run(["aws", *args], env=env, check=True, capture_output=True, text=True).stdout


def state_engine_version(workspace_id: str) -> str:
    """The `terraform_version` recorded in the workspace's current state object."""
    raw = subprocess.run(
        ["aws", "s3", "cp", f"s3://{STATE_BUCKET}/workspaces/{workspace_id}/terraform.tfstate", "-"],
        env=dict(os.environ, AWS_PROFILE=STATE_PROFILE, AWS_REGION=STATE_REGION),
        check=True,
        capture_output=True,
    ).stdout
    version = json.loads(raw).get("terraform_version", "")
    del raw
    return str(version)


def engine_version(engine: str) -> str:
    """The version the local engine binary reports."""
    out = subprocess.run([engine, "version", "-json"], check=True, capture_output=True, text=True).stdout
    return str(json.loads(out)["terraform_version"])


def newest_v2_tag() -> tuple[str, int]:
    """The newest `v2.x.y` tag of platform-modules on GitHub and its minor version."""
    out = subprocess.run(["git", "ls-remote", "--tags", MODULES_REPO], check=True, capture_output=True, text=True).stdout
    tags = []
    for line in out.splitlines():
        match = re.search(r"refs/tags/v2\.(\d+)\.(\d+)$", line)
        if match:
            tags.append((int(match.group(1)), int(match.group(2))))
    if not tags:
        sys.exit("no v2 tag on platform-modules")
    minor, patch = max(tags)
    return f"v2.{minor}.{patch}", minor


def rewrite_module_sources(workdir: Path) -> str:
    """Point every platform-modules source at GitHub so init never needs the plane's registry."""
    tag, newest_minor = newest_v2_tag()
    for path in workdir.glob("*.tf"):
        text = path.read_text()

        def to_git(match: re.Match[str]) -> str:
            if int(match.group("minor")) > newest_minor:
                sys.exit(f"{path.name} needs a platform-modules minor newer than {tag}")
            return f'source{match.group(1)}= "git::{MODULES_REPO}//{match.group("path")}?ref={tag}"\n'

        path.write_text(REGISTRY_SOURCE.sub(to_git, text))
    leftover = [p.name for p in workdir.glob("*.tf") if "terraform.webbpulse.com/" in p.read_text()]
    if leftover:
        sys.exit(f"registry sources left after the rewrite: {', '.join(leftover)}")
    return tag


def write_backend(workdir: Path, workspace_id: str) -> None:
    """The runner's S3 backend block, plus the state account's profile, as an override file."""
    kms_arn = aws("kms", "describe-key", "--key-id", STATE_KMS_ALIAS, "--query", "KeyMetadata.Arn", "--output", "text").strip()
    (workdir / "zz_break_glass_override.tf").write_text(
        "terraform {\n"
        '  backend "s3" {\n'
        f'    bucket               = "{STATE_BUCKET}"\n'
        f'    key                  = "workspaces/{workspace_id}/terraform.tfstate"\n'
        f'    region               = "{STATE_REGION}"\n'
        f'    kms_key_id           = "{kms_arn}"\n'
        f'    workspace_key_prefix = "workspaces/{workspace_id}/env"\n'
        f'    profile              = "{STATE_PROFILE}"\n'
        "    encrypt              = true\n"
        "    use_lockfile         = true\n"
        "  }\n"
        "}\n"
    )


def write_dns_providers(workdir: Path, aliases: tuple[str, ...]) -> None:
    """Run the Route 53 aliases as the zone account's administrator instead of the workspace's roles.

    The Route 53 reader and writer roles trust only the workspace run role, which
    trusts only the plane's run-credentials role, so no local principal can reach
    them. The parent zones live in the management account, where the
    AdministratorAccess profile reads and writes Route 53 directly.
    """
    blocks = "".join(
        f'provider "aws" {{\n'
        f'  alias   = "{alias}"\n'
        f'  profile = "{DNS_PROFILE}"\n\n'
        f'  dynamic "assume_role" {{\n'
        f"    for_each = []\n\n"
        f"    content {{\n"
        f'      role_arn = ""\n'
        f"    }}\n"
        f"  }}\n"
        f"}}\n\n"
        for alias in aliases
    )
    (workdir / "zz_break_glass_providers_override.tf").write_text(blocks)


def write_variables(workdir: Path, workspace_id: str) -> dict[str, str]:
    """Write the workspace's variables the way the runner does and return its env variables."""
    items = json.loads(
        aws(
            "dynamodb",
            "query",
            "--table-name",
            VARIABLES_TABLE,
            "--key-condition-expression",
            "workspace_id = :w",
            "--expression-attribute-values",
            json.dumps({":w": {"S": workspace_id}}),
            "--output",
            "json",
        )
    )["Items"]
    literals: dict[str, str] = {}
    hcl: dict[str, str] = {}
    env: dict[str, str] = {}
    for item in items:
        key = item["key"]["S"]
        if item.get("sensitive", {}).get("BOOL", False):
            sys.exit(f"{key} is sensitive; this script only handles workspaces without sensitive variables")
        value = item.get("value", {}).get("S", "")
        if item.get("category", {}).get("S", "terraform") == "env":
            env[key] = value
        elif item.get("hcl", {}).get("BOOL", False):
            if not VARIABLE_NAME.fullmatch(key):
                sys.exit(f"variable name is not a valid HCL identifier: {key}")
            hcl[key] = value
        else:
            literals[key] = value
    literal_file = workdir / "zz_webbpulse.auto.tfvars.json"
    literal_file.write_text(json.dumps(literals, sort_keys=True))
    literal_file.chmod(0o600)
    hcl_file = workdir / "zz_webbpulse.auto.tfvars"
    hcl_file.write_text("".join(f"{key} = (\n{hcl[key]}\n)\n" for key in sorted(hcl)))
    hcl_file.chmod(0o600)
    print(f"variables: {len(literals)} literal, {len(hcl)} hcl, env {', '.join(sorted(env)) or 'none'}")
    return env


def run(engine: str, workdir: Path, env: dict[str, str], *args: str) -> int:
    """Run the engine in `workdir` and return its exit code."""
    return subprocess.run([engine, *args], cwd=workdir, env=env).returncode


def main() -> None:
    """Parse arguments, stage a private copy of `terraform/` and plan or apply it."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["plan", "apply"])
    parser.add_argument("environment", choices=sorted(TARGETS))
    parser.add_argument("--engine-bin", default="terraform", help="a terraform binary at exactly the state's version")
    parser.add_argument("--keep", action="store_true", help="keep the private working copy for inspection")
    args = parser.parse_args()
    target = TARGETS[args.environment]
    engine = shutil.which(args.engine_bin) or sys.exit(f"no engine at {args.engine_bin}")

    wanted = state_engine_version(target.workspace_id)
    found = engine_version(engine)
    if wanted != found:
        sys.exit(f"state was written by {wanted}, engine is {found}; use the exact version")
    print(f"workspace {target.workspace_id}, engine {found}")

    source = Path(__file__).resolve().parents[2] / "terraform"
    workdir = Path(tempfile.mkdtemp(prefix="break-glass-"))
    workdir.chmod(0o700)
    try:
        shutil.copytree(source, workdir, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
        print(f"platform-modules from GitHub at {rewrite_module_sources(workdir)}")
        write_backend(workdir, target.workspace_id)
        write_dns_providers(workdir, target.dns_aliases)
        extra = write_variables(workdir, target.workspace_id)
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("TF_", "AWS_ACCESS", "AWS_SECRET", "AWS_SESSION"))
        }
        env.update(extra)
        env.update(
            AWS_PROFILE=target.provider_profile,
            AWS_REGION=STATE_REGION,
            TF_IN_AUTOMATION="1",
            TF_VAR_webbpulse_run_phase=args.action,
        )
        if run(engine, workdir, env, "init", "-input=false", "-no-color") != 0:
            sys.exit("init failed")
        if args.action == "plan":
            code = run(engine, workdir, env, "plan", "-input=false", "-lock=false", "-detailed-exitcode", "-no-color")
            print(f"plan exit {code}: {'no changes' if code == 0 else 'changes' if code == 2 else 'error'}")
            sys.exit(0 if code in (0, 2) else code)
        code = run(engine, workdir, env, "plan", "-input=false", "-out=break-glass.tfplan", "-detailed-exitcode", "-no-color")
        if code == 0:
            print("no changes; nothing to apply")
            return
        if code != 2:
            sys.exit(f"plan failed with exit {code}")
        if input("Apply this plan? Type yes: ").strip() != "yes":
            sys.exit("not applied")
        sys.exit(run(engine, workdir, env, "apply", "-input=false", "-no-color", "break-glass.tfplan"))
    finally:
        if args.keep:
            print(f"kept {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
