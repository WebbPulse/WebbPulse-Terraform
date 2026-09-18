"""Turning DynamoDB's conditional check failure into the library's own exception.

`webbpulse.dynamodb` defines `ConditionFailed` and `webbpulse.http` renders it as
a 409, but `Repository.put` and `Repository.update` do not raise it: botocore's
`ClientError` reaches the caller instead. Catching `ConditionFailed` around a
conditional write therefore catches nothing, which turns a lost optimistic write
into a 500 rather than the conflict it is.

`condition_failed` is the guard every conditional write in this project wraps
itself in until the library raises the exception itself, at which point the body
of this module becomes a re-raise.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from webbpulse.dynamodb import ConditionFailed

CONDITIONAL_CHECK_FAILED = "ConditionalCheckFailedException"
"""DynamoDB's error code for a condition that did not hold."""


def is_conditional_check_failure(error: Any) -> bool:
    """Whether a botocore error is a rejected condition rather than a real fault."""
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return False
    error_block = response.get("Error", {})
    if not isinstance(error_block, Mapping):
        return False
    return bool(error_block.get("Code") == CONDITIONAL_CHECK_FAILED)


@contextmanager
def condition_failed(table: str, *, condition: str = "", key: Mapping[str, Any] | None = None) -> Iterator[None]:
    """Re-raise a rejected conditional write as `ConditionFailed`.

    Every other `ClientError` passes through untouched: a throttle and a missing
    table are faults, and swallowing them into a conflict would hide them.

    Args:
        table: The table the write targeted, for the exception's message.
        condition: The condition expression, for the exception's message.
        key: The key the condition guarded, for the exception's message.
    """
    from botocore.exceptions import ClientError

    try:
        yield
    except ClientError as error:
        if is_conditional_check_failure(error):
            raise ConditionFailed(table, condition, key) from error
        raise


__all__ = ["CONDITIONAL_CHECK_FAILED", "condition_failed", "is_conditional_check_failure"]
