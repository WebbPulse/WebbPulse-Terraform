"""The staging e2e notification receiver: checks a generic webhook delivery and echoes the verdict.

The e2e suite points a generic notification configuration at this function's URL with the
signing token repeated as the `token` query parameter. The answer names the trigger, the run
and whether the HMAC-SHA512 signature matched, and the control plane keeps the start of it as
the delivery's `response_excerpt`, which is how the suite reads the outcome. Nothing is stored
and nothing is logged, so the token never leaves the request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

SIGNATURE_HEADER = "x-tfe-notification-signature"


def _body(event: dict[str, Any]) -> bytes:
    """The raw request body, decoded when the function URL base64 encoded it."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return str(raw).encode("utf-8")


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Answer one delivery with whether its signature matched and what it carried."""
    del context
    body = _body(event)
    token = str((event.get("queryStringParameters") or {}).get("token") or "")
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    signature = headers.get(SIGNATURE_HEADER, "")
    expected = hmac.new(token.encode("utf-8"), body, hashlib.sha512).hexdigest() if token else ""
    valid = bool(token and signature) and hmac.compare_digest(signature, expected)
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        payload = {}
    notifications = payload.get("notifications") if isinstance(payload, dict) else None
    first = notifications[0] if isinstance(notifications, list) and notifications else {}
    answer = {
        "received": True,
        "signature_valid": valid,
        "trigger": first.get("trigger") if isinstance(first, dict) else None,
        "run_status": first.get("run_status") if isinstance(first, dict) else None,
        "run_id": payload.get("run_id") if isinstance(payload, dict) else None,
        "payload_version": payload.get("payload_version") if isinstance(payload, dict) else None,
    }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(answer, separators=(",", ":")),
    }
