"""Where the identity module's own tables live in this environment.

The identity module names its ten tables, and the `api-keys` table, from one
prefix. Two callers need it, `auth.api_key_store` and the identity glue, and this
is the single place that decides it so the two cannot disagree.

It sits in `common` rather than in the identity domain so the runs image can read
the `api-keys` table without importing any identity glue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ..composition.settings import Settings


def identity_table_prefix(settings: "Settings") -> str:
    """The prefix the identity module's tables carry in this environment.

    `IDENTITY_TABLE_PREFIX` first, because the stack slugs production to `prod`
    while `ENVIRONMENT` is the word `production`: deriving the prefix there would
    name tables that do not exist. The derived form is the fallback, for the local
    stack and the suite, where the environment and the slug do agree.
    """
    configured = settings.IDENTITY_TABLE_PREFIX.strip()
    return configured or f"webbpulse-terraform-{settings.ENVIRONMENT}"
