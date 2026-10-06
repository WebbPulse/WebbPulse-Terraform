"""The users table stream purge, as the workspaces function serves it.

Deleting a users row is the whole of an ephemeral e2e user's teardown. Its identity
rows go only when the users table's stream reaches the purge route, so these pin that
the function the stream is mapped to serves that route and that a REMOVE record empties
the deleted user's rows while leaving everyone else's alone.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from webbpulse.dynamodb import Repository
from webbpulse.events import events_path
from webbpulse.identity import CREDENTIALS_TABLE, DynamoCredentialStore
from webbpulse.identity.api_keys import API_KEYS_TABLE, DynamoApiKeyStore, mint
from webbpulse.identity.storage import CredentialRecord

from app.common.composition import settings as settings_module
from app.common.composition.wiring import build_domain_app
from tests.conftest import REGION, TABLE_PREFIX

DELETED = "user-deleted"

KEPT = "user-kept"


@pytest.fixture
def credentials(identity_environment: None) -> DynamoCredentialStore:
    """The credentials store over the mocked identity table the purge empties."""
    del identity_environment
    return DynamoCredentialStore(Repository(CREDENTIALS_TABLE, prefix=TABLE_PREFIX, region_name=REGION))


@pytest.fixture
def api_keys(identity_environment: None) -> DynamoApiKeyStore:
    """The api-keys store over the mocked identity table the purge also empties."""
    del identity_environment
    return DynamoApiKeyStore(Repository(API_KEYS_TABLE, prefix=TABLE_PREFIX, region_name=REGION))


@pytest.fixture
def workspaces_client(identity_environment: None) -> Iterator[TestClient]:
    """A client for the workspaces function alone, which the users stream is mapped to."""
    del identity_environment
    app = build_domain_app("workspaces", settings=settings_module.get_settings())
    with TestClient(app) as client:
        yield client


def _store(credentials: DynamoCredentialStore, user_id: str) -> None:
    """Write one password credential row for a user."""
    credentials.put(CredentialRecord(user_id=user_id, credential_type="password", secret="hash"))


def _remove(user_id: str) -> dict[str, Any]:
    """A KEYS_ONLY stream record for a hard delete of one users row."""
    return {
        "eventID": f"event-{user_id}",
        "eventName": "REMOVE",
        "eventSource": "aws:dynamodb",
        "dynamodb": {"Keys": {"id": {"S": user_id}}},
    }


def test_a_users_row_delete_purges_that_users_identity_rows(
    workspaces_client: TestClient, credentials: DynamoCredentialStore
) -> None:
    """A REMOVE record empties the deleted user's credentials and reports no failures."""
    _store(credentials, DELETED)
    _store(credentials, KEPT)

    response = workspaces_client.post(events_path(), json={"Records": [_remove(DELETED)]})

    assert response.status_code == 200
    assert response.json().get("batchItemFailures") == []
    assert credentials.get(DELETED, "password") is None
    assert credentials.get(KEPT, "password") is not None


def test_a_users_row_delete_purges_that_users_api_keys(
    workspaces_client: TestClient, api_keys: DynamoApiKeyStore
) -> None:
    """A REMOVE record deletes the deleted user's API keys and leaves other subjects' keys."""
    deleted = mint(user_id=DELETED, tenant_id="tenant", scopes=("workspaces:read",), store=api_keys)
    kept = mint(user_id=KEPT, tenant_id="tenant", scopes=("workspaces:read",), store=api_keys)

    response = workspaces_client.post(events_path(), json={"Records": [_remove(DELETED)]})

    assert response.status_code == 200
    assert response.json().get("batchItemFailures") == []
    assert api_keys.get(deleted.record.key_hash) is None
    assert api_keys.get(kept.record.key_hash) is not None
