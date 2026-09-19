"""`scripts/create_user.py`, which creates the first account registration cannot.

The properties worth holding are that it is re-runnable, that the row it writes is
one `may_authenticate` permits, and that the environment guard refuses rather than
writing into the wrong account.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from app.common.composition import settings as settings_module
from app.common.db.users import UserRepository

EMAIL = "someone@webbpulse.com"

PASSWORD = "a-sufficiently-long-password"

SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "create_user.py"


@pytest.fixture
def script() -> Any:
    """The script loaded as a module, so `main` is called rather than subprocessed."""
    spec = importlib.util.spec_from_file_location("create_user_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


@pytest.fixture
def staging_environment(identity_tables: str) -> Iterator[None]:
    """`ENVIRONMENT=staging` with the password set, which is what the script needs.

    The identity module's tables are created a second time under the staging prefix,
    because the script derives that prefix from `ENVIRONMENT` exactly as the deployed
    function does and the suite's own tables carry the `test` one. `USERS_TABLE` is
    an explicit variable rather than a prefix, so the users row keeps landing in the
    table the rest of the suite reads.
    """
    del identity_tables
    import boto3
    from webbpulse.identity import TABLES

    client = boto3.client("dynamodb", region_name="us-west-2")
    existing = set(client.list_tables()["TableNames"])
    for spec in TABLES:
        request = spec.create_table_request("webbpulse-terraform-staging")
        if request["TableName"] not in existing:
            client.create_table(**request)

    previous_environment = os.environ.get("ENVIRONMENT")
    previous_password = os.environ.get("CONTROL_PLANE_USER_PASSWORD")
    os.environ["ENVIRONMENT"] = "staging"
    os.environ["CONTROL_PLANE_USER_PASSWORD"] = PASSWORD
    settings_module.reset_settings_cache()
    yield
    if previous_environment is None:
        os.environ.pop("ENVIRONMENT", None)
    else:
        os.environ["ENVIRONMENT"] = previous_environment
    if previous_password is None:
        os.environ.pop("CONTROL_PLANE_USER_PASSWORD", None)
    else:
        os.environ["CONTROL_PLANE_USER_PASSWORD"] = previous_password
    settings_module.reset_settings_cache()


def test_it_creates_a_verified_admin(script: Any, staging_environment: None) -> None:
    """The row it writes is one `may_authenticate` permits and `claims_for` calls admin."""
    del staging_environment
    assert script.main(["--environment", "staging", EMAIL]) == 0

    stored = UserRepository().get_by_email(EMAIL)
    assert stored is not None
    assert stored.email_verified is True
    assert stored.is_admin is True
    assert stored.disabled is False


def test_it_writes_a_password_credential(script: Any, staging_environment: None) -> None:
    """A user row without a credential cannot sign in, so the credential is the point.

    The store is rebuilt here from the package constants and the glue's own prefix,
    rather than read off the script, so the assertion is that the credential landed
    where the identity routes will look for it. The secret is checked through the
    package's own verifier at the package's current cost, which is what makes the
    script's hashing and the sign-in route's hashing the same hashing.
    """
    del staging_environment
    assert script.main(["--environment", "staging", EMAIL]) == 0

    from webbpulse.dynamodb import Repository
    from webbpulse.identity import CREDENTIALS_TABLE, DynamoCredentialStore
    from webbpulse.identity.flows import PASSWORD_CREDENTIAL_TYPE
    from webbpulse.security import needs_rehash, verify_password

    from app.common.db.identity_tables import identity_table_prefix

    settings = settings_module.get_settings()
    stored = UserRepository().get_by_email(EMAIL)
    assert stored is not None

    store = DynamoCredentialStore(
        Repository(
            CREDENTIALS_TABLE,
            prefix=identity_table_prefix(settings),
            region_name=settings.AWS_REGION_NAME or None,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
    )
    record = store.get(stored.id, PASSWORD_CREDENTIAL_TYPE)
    assert record is not None
    assert verify_password(PASSWORD, record.secret) is True
    assert needs_rehash(record.secret) is False


def test_it_is_re_runnable(script: Any, staging_environment: None) -> None:
    """A second run updates the same row rather than creating a second account.

    Rotating a password is this same command again, so a duplicate row here would
    mean two accounts on one address and a sign-in that picks one arbitrarily.
    """
    del staging_environment
    assert script.main(["--environment", "staging", EMAIL]) == 0
    first = UserRepository().get_by_email(EMAIL)
    assert first is not None

    assert script.main(["--environment", "staging", EMAIL]) == 0
    second = UserRepository().get_by_email(EMAIL)
    assert second is not None
    assert second.id == first.id


def test_a_mismatched_environment_writes_nothing(script: Any, staging_environment: None) -> None:
    """Pointing a staging shell at production writes no row at all."""
    del staging_environment
    assert script.main(["--environment", "production", EMAIL]) == 1
    assert UserRepository().get_by_email(EMAIL) is None


def test_a_missing_password_writes_nothing(script: Any, staging_environment: None) -> None:
    """No password is a refusal rather than an account with an empty credential."""
    del staging_environment
    os.environ.pop("CONTROL_PLANE_USER_PASSWORD", None)
    assert script.main(["--environment", "staging", EMAIL]) == 1
    assert UserRepository().get_by_email(EMAIL) is None


def test_it_refuses_an_environment_it_does_not_know(script: Any, staging_environment: None) -> None:
    """`local` and `test` are not choices, so the suite's own environment is refused."""
    del staging_environment
    with pytest.raises(SystemExit):
        script.main(["--environment", "test", EMAIL])
