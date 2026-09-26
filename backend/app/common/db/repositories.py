"""The `webbpulse.dynamodb.Repository` instances every domain reads through.

Each is built per call rather than held, so a test that moves the table names or
the DynamoDB endpoint underneath the settings is read rather than a stale one.
The physical names come from the environment, so nothing here derives a name the
Terraform stack did not choose.
"""

from __future__ import annotations

from webbpulse.dynamodb import Repository

from ..composition.settings import Settings, get_settings
from .tables import CONFIG_VERSIONS, RUNS, USERS, VARIABLES, VCS_UPLOADS, WORKSPACES, local_table_name


def _repository(logical_name: str, physical_name: str, settings: Settings) -> Repository:
    """A repository over one table, named by the stack rather than by a prefix.

    `Repository` builds its physical name as `<prefix>-<logical>`, and this
    project's names come from the environment whole. The physical name is passed
    as the logical one with an empty prefix, so the resolved name is exactly what
    Terraform chose, and `logical_name` is restored afterwards for the callers
    that read it.
    """
    repository = Repository(
        physical_name,
        prefix="",
        region_name=settings.AWS_REGION_NAME or None,
        endpoint_url=settings.dynamodb_endpoint_url,
    )
    repository.logical_name = logical_name
    return repository


def _name(configured: str, logical_name: str, settings: Settings) -> str:
    """The configured physical name, or the local convention when none is set."""
    return configured or local_table_name(logical_name, settings.ENVIRONMENT)


def workspaces(settings: Settings | None = None) -> Repository:
    """The workspaces table."""
    resolved = settings or get_settings()
    return _repository(WORKSPACES, _name(resolved.WORKSPACES_TABLE, WORKSPACES, resolved), resolved)


def runs(settings: Settings | None = None) -> Repository:
    """The runs table."""
    resolved = settings or get_settings()
    return _repository(RUNS, _name(resolved.RUNS_TABLE, RUNS, resolved), resolved)


def variables(settings: Settings | None = None) -> Repository:
    """The variables table."""
    resolved = settings or get_settings()
    return _repository(VARIABLES, _name(resolved.VARIABLES_TABLE, VARIABLES, resolved), resolved)


def config_versions(settings: Settings | None = None) -> Repository:
    """The config versions table."""
    resolved = settings or get_settings()
    return _repository(
        CONFIG_VERSIONS,
        _name(resolved.CONFIG_VERSIONS_TABLE, CONFIG_VERSIONS, resolved),
        resolved,
    )


def users(settings: Settings | None = None) -> Repository:
    """The users table the identity hooks read and write."""
    resolved = settings or get_settings()
    return _repository(USERS, _name(resolved.USERS_TABLE, USERS, resolved), resolved)


def vcs_uploads(settings: Settings | None = None) -> Repository:
    """The ingest records `POST /vcs/uploads` writes and the ingest consumer reads."""
    resolved = settings or get_settings()
    return _repository(VCS_UPLOADS, _name(resolved.VCS_UPLOADS_TABLE, VCS_UPLOADS, resolved), resolved)


def physical_names(settings: Settings | None = None) -> dict[str, str]:
    """Every logical table paired with the physical name this environment uses."""
    resolved = settings or get_settings()
    return {
        WORKSPACES: _name(resolved.WORKSPACES_TABLE, WORKSPACES, resolved),
        RUNS: _name(resolved.RUNS_TABLE, RUNS, resolved),
        VARIABLES: _name(resolved.VARIABLES_TABLE, VARIABLES, resolved),
        CONFIG_VERSIONS: _name(resolved.CONFIG_VERSIONS_TABLE, CONFIG_VERSIONS, resolved),
        USERS: _name(resolved.USERS_TABLE, USERS, resolved),
        VCS_UPLOADS: _name(resolved.VCS_UPLOADS_TABLE, VCS_UPLOADS, resolved),
    }
