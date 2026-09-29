"""Attempt, from inside a real plan, everything a hostile configuration would to fake its run report.

Run by the `external` data source, so it sees exactly what a provider or a hostile
module sees. It tries to read the runner's environment out of every process, which
holds the run id, the run token and the task credential path, then to reach the task
credential endpoint and post a forged `phase-result` claiming no changes. Isolation
should make each step fail: the engine runs as a user that cannot read the runner's
process or reach the credential endpoint and holds no task credentials, so it learns
neither the run id nor a token and cannot post anything.

It never prints a secret. It reports only which steps were reachable as booleans, and
it always exits 0 with a real result so the plan itself produces one `terraform_data`
add. The proof is the run's real outcome: one add, not the forged no change report.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

STOLEN_KEYS = (
    "RUN_ID",
    "TASK_TOKEN",
    "API_BASE_URL",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


def _read_runner_environment() -> dict[str, str]:
    """Whatever another process's environment exposes of the run id, token and credential path.

    The engine user cannot read the runner's `/proc/<pid>/environ`, so this finds
    nothing. The values are never returned, only used to attempt the next step.
    """
    found: dict[str, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "environ").read_bytes()
        except OSError:
            continue
        for item in raw.split(b"\0"):
            name, _, value = item.partition(b"=")
            key = name.decode("latin-1")
            if key in STOLEN_KEYS:
                found[key] = value.decode("latin-1")
    return found


def _reach_credential_endpoint(environ: dict[str, str]) -> bool:
    """Whether the ECS task credential endpoint hands the engine the task role credentials."""
    relative = environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "")
    full = environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "")
    url = full or (f"http://169.254.170.2{relative}" if relative else "")
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            document = json.load(response)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return bool(document.get("AccessKeyId"))


def _post_forged_result(api_base_url: str, environ: dict[str, str]) -> bool:
    """Whether a forged no change `phase-result` posted with the stolen run id and token is accepted."""
    run_id = environ.get("RUN_ID", "")
    token = environ.get("TASK_TOKEN", "")
    if not run_id or not token:
        return False
    body = json.dumps(
        {"run_id": run_id, "phase": "plan", "exit_code": 0, "changes": {"add": 0, "change": 0, "destroy": 0}}
    ).encode()
    request = urllib.request.Request(
        f"{api_base_url}/api/v1/runs/{run_id}/phase-result",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return 200 <= response.status < 300
    except urllib.error.HTTPError as error:
        return 200 <= error.code < 300
    except (urllib.error.URLError, OSError):
        return False


def forge(query: dict[str, str]) -> dict[str, str]:
    """Try each forgery step and report only whether it was reachable."""
    environ = _read_runner_environment()
    reached_credentials = _reach_credential_endpoint(environ)
    posted_forged = _post_forged_result(query["api_base_url"].rstrip("/"), environ)
    return {
        "attempted": "yes",
        "read_runner_environment": json.dumps(bool(environ)),
        "reached_credential_endpoint": json.dumps(reached_credentials),
        "posted_forged_result": json.dumps(posted_forged),
    }


def main() -> int:
    """Read the query, attempt the forgery and print the result the data source expects."""
    try:
        result = forge(json.load(sys.stdin))
    except (KeyError, ValueError) as error:
        print(json.dumps({"attempted": "error", "reason": type(error).__name__}))
        return 0
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
