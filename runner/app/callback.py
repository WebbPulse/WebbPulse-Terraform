"""Reporting the phase outcome back to the waiting Step Functions execution."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from app.models import Changes

if TYPE_CHECKING:
    from mypy_boto3_stepfunctions.client import SFNClient
else:
    SFNClient = object

MAX_ERROR_LENGTH = 256
MAX_CAUSE_LENGTH = 32768


def send_success(client: SFNClient, task_token: str, exit_code: int, changes: Changes) -> None:
    """Hand the state machine the phase's exit code and change counts."""
    payload = {"exit_code": exit_code, "changes": changes.model_dump()}
    client.send_task_success(taskToken=task_token, output=json.dumps(payload))


def send_failure(client: SFNClient, task_token: str, error: str, cause: str) -> None:
    """Hand the state machine a short error name and a redacted cause."""
    try:
        client.send_task_failure(
            taskToken=task_token,
            error=error[:MAX_ERROR_LENGTH],
            cause=cause[:MAX_CAUSE_LENGTH],
        )
    except Exception as failure:
        print(f"send_task_failure failed: {type(failure).__name__}", file=sys.stderr, flush=True)
