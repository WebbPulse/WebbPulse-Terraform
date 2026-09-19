"""Create or update a verified admin user with a password credential.

Registration is disabled in every deployed environment (`IDENTITY_REGISTRATION_ENABLED`
is `false`), so there is no HTTP route that creates the first account. This writes it
through the same package stores the identity routes read, so the row and the credential
are byte identical to ones a registration would have produced.

Re-runnable: an existing user is updated rather than recreated, and the password
credential is overwritten, so running it twice is not an error and rotating a password is
the same command again.

The password comes from `CONTROL_PLANE_USER_PASSWORD` rather than an argument, so it never
reaches a shell history or a process listing, and nothing secret is printed.

`--environment` must be given and must match `ENVIRONMENT`. The flag exists so that
pointing a shell at staging and running the production command writes nothing: an admin
account is a credential, and the account it lands in has to be stated rather than
inherited from whatever the environment happened to hold.

Usage (from backend/):
    AWS_PROFILE=... ENVIRONMENT=staging \\
      WORKSPACES_TABLE=... USERS_TABLE=... \\
      CONTROL_PLANE_USER_PASSWORD='...' \\
      uv run python scripts/create_user.py --environment staging someone@webbpulse.com
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Final, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASSWORD_VARIABLE: Final = "CONTROL_PLANE_USER_PASSWORD"
"""The environment variable carrying the password. Never an argument."""

ALLOWED_ENVIRONMENTS: Final = ("staging", "production")
"""The environments this script will write to. `local` and `test` are excluded
deliberately: the suite and the local stack seed their own rows."""


def _credential_store(settings: Any) -> Any:
    """The package credential store over this environment's identity tables.

    The prefix is the identity module's, the same expression the glue and
    `auth.api_key_store` use, because all three reach tables that module named.
    """
    from webbpulse.dynamodb import Repository
    from webbpulse.identity import CREDENTIALS_TABLE, DynamoCredentialStore

    from app.common.db.identity_tables import identity_table_prefix

    return DynamoCredentialStore(
        Repository(
            CREDENTIALS_TABLE,
            prefix=identity_table_prefix(settings),
            region_name=settings.AWS_REGION_NAME or None,
            endpoint_url=settings.dynamodb_endpoint_url,
        )
    )


def create_user(email: str, password: str, settings: Any) -> str:
    """Create or update the user and its password credential, returning the user id.

    `email_verified` is set because `may_authenticate` refuses an unverified address,
    and `is_admin` because the account this script exists to create is the first one,
    which has to be able to administer the control plane.

    The secret goes through `check_password` then `hash_password`, the same pair the
    package's register flow uses, so a password the sign-in route would later reject is
    refused now rather than written and discovered at the first login.
    """
    from webbpulse.identity.flows import PASSWORD_CREDENTIAL_TYPE
    from webbpulse.identity.passwords import check_password
    from webbpulse.identity.storage import CredentialRecord, now_iso
    from webbpulse.security import hash_password

    from app.common.db.users import User, UserRepository

    users = UserRepository(settings=settings)
    existing = users.get_by_email(email)
    if existing is None:
        user = users.create(User(email=email, email_verified=True, is_admin=True))
    else:
        user = users.update(existing.id, email_verified=True, is_admin=True, disabled=False)

    secret = hash_password(check_password(password))
    stamp = now_iso()
    _credential_store(settings).put(
        CredentialRecord(
            user_id=user.id,
            credential_type=PASSWORD_CREDENTIAL_TYPE,
            secret=secret,
            created_at=stamp,
            updated_at=stamp,
        )
    )
    return user.id


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """The parsed arguments: the address, and the environment it must match."""
    parser = argparse.ArgumentParser(description="Create or update a verified admin user with a password credential.")
    parser.add_argument("email", help="The address the account signs in with.")
    parser.add_argument(
        "--environment",
        required=True,
        choices=ALLOWED_ENVIRONMENTS,
        help="The environment being written to. Must match ENVIRONMENT.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Create the user and return 0, or print why nothing was written and return 1."""
    arguments = _parse(argv)

    from app.common.config import get_settings

    settings = get_settings()
    resolved = settings.ENVIRONMENT.strip().lower()
    if resolved != arguments.environment:
        print(
            f"--environment is {arguments.environment!r} but ENVIRONMENT is {resolved!r}. "
            "Nothing was written. Point the shell at the intended account and say which one it is.",
            file=sys.stderr,
        )
        return 1

    password = os.environ.get(PASSWORD_VARIABLE, "")
    if not password:
        print(f"{PASSWORD_VARIABLE} must be set. Nothing was written.", file=sys.stderr)
        return 1

    email = arguments.email.strip()
    if not email:
        print("The address is empty. Nothing was written.", file=sys.stderr)
        return 1

    try:
        user_id = create_user(email, password, settings)
    except Exception as error:  # noqa: BLE001
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"{email} is a verified admin in {resolved} with id {user_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
