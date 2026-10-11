"""The device grant store, with each new grant recorded in the audit trail.

`wp-tf login` ends in the package's device grant service writing one grant. That
write is the moment a CLI gains a session, so this store records it as
`device_grant.opened`, naming the person, the client and the scopes and never the
refresh hash. Every other call is the package's own store unchanged.
"""

from __future__ import annotations

from webbpulse.audit import AuditActor, AuditTarget
from webbpulse.identity import DeviceGrantRecord, DynamoDeviceGrantStore

from .. import audit


class AuditedDeviceGrantStore(DynamoDeviceGrantStore):
    """`DynamoDeviceGrantStore` that records a grant's opening once it is written."""

    def put(self, record: DeviceGrantRecord) -> None:
        """Write the grant, then record it best effort."""
        super().put(record)
        audit.record(
            audit.DEVICE_GRANT_OPENED,
            request=None,
            claims=None,
            actor=AuditActor(id=record.user_id, kind="user", source="cli"),
            target=AuditTarget(type=audit.DEVICE_GRANT, id=record.grant_id, label=record.client_id),
            payload={"client_id": record.client_id, "scopes": list(record.scopes)},
        )


__all__ = ["AuditedDeviceGrantStore"]
