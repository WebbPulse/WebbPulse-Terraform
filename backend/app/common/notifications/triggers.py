"""The HCP Terraform notification triggers and the run transitions that fire them."""

from __future__ import annotations

from typing import Final, Literal, Mapping

Trigger = Literal[
    "run:created",
    "run:planning",
    "run:needs_attention",
    "run:applying",
    "run:completed",
    "run:errored",
]
"""A trigger a configuration can subscribe to, spelled as HCP Terraform spells it."""

RUN_CREATED: Final = "run:created"
RUN_PLANNING: Final = "run:planning"
RUN_NEEDS_ATTENTION: Final = "run:needs_attention"
RUN_APPLYING: Final = "run:applying"
RUN_COMPLETED: Final = "run:completed"
RUN_ERRORED: Final = "run:errored"

VERIFICATION: Final = "verification"
"""The trigger a test delivery carries, as HCP's verify action sends it."""

ALL_TRIGGERS: Final[tuple[str, ...]] = (
    RUN_CREATED,
    RUN_PLANNING,
    RUN_NEEDS_ATTENTION,
    RUN_APPLYING,
    RUN_COMPLETED,
    RUN_ERRORED,
)
"""Every subscribable trigger, in the order a run reaches them."""

STATUS_TRIGGERS: Final[Mapping[str, str]] = {
    "planning": RUN_PLANNING,
    "awaiting_confirmation": RUN_NEEDS_ATTENTION,
    "applying": RUN_APPLYING,
    "applied": RUN_COMPLETED,
    "planned_and_finished": RUN_COMPLETED,
    "discarded": RUN_COMPLETED,
    "errored": RUN_ERRORED,
    "cancelled": RUN_ERRORED,
}
"""The trigger a run fires on entering each status. A new run fires `run:created`.

A discarded run fires `run:completed`, since it ended on a path nobody can act on any
more and without a fault, and a cancelled one fires `run:errored`, as on HCP."""

NOTIFYING_STATUSES: Final = frozenset(STATUS_TRIGGERS)
"""The statuses a transition into fires a notification. Terraform's stream filter lists them too."""

TITLES: Final[Mapping[str, str]] = {
    "pending": "Run Created",
    "planning": "Run Planning",
    "awaiting_confirmation": "Run Needs Attention",
    "applying": "Run Applying",
    "applied": "Run Applied",
    "planned_and_finished": "Run Planned and Finished",
    "discarded": "Run Discarded",
    "errored": "Run Errored",
    "cancelled": "Run Canceled",
}
"""The headline each notification carries, worded as HCP words them."""


def title_for(trigger: str, status: str) -> str:
    """The headline for one notification: `Run Created` for a new run, else the status's own."""
    if trigger == RUN_CREATED:
        return TITLES["pending"]
    return TITLES.get(status, f"Run {status.replace('_', ' ').title()}")


def trigger_for(event_name: str, old_status: str | None, new_status: str) -> str | None:
    """The trigger one runs table write fires, or `None` when it fires none.

    An insert is a created run. A modify fires only when the status moved into a
    notifying status, so a write that leaves the status alone fires nothing twice.
    """
    if event_name == "INSERT":
        return RUN_CREATED
    if event_name != "MODIFY" or old_status == new_status:
        return None
    return STATUS_TRIGGERS.get(new_status)


__all__ = [
    "ALL_TRIGGERS",
    "NOTIFYING_STATUSES",
    "RUN_APPLYING",
    "RUN_COMPLETED",
    "RUN_CREATED",
    "RUN_ERRORED",
    "RUN_NEEDS_ATTENTION",
    "RUN_PLANNING",
    "STATUS_TRIGGERS",
    "TITLES",
    "VERIFICATION",
    "Trigger",
    "title_for",
    "trigger_for",
]
