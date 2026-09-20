"""Seed the local stack's e2e user, which no deployed environment ever runs.

`create_user.py` deliberately refuses the `local` environment, because an admin
account is a credential and a deployed one has to be asked for by name. The local
stack still needs a user to sign in as, so it seeds its own here through the same
`create_user` function, guarded the other way: this refuses anything but `local`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from create_user import PASSWORD_VARIABLE, create_user  # noqa: E402

LOCAL_ENVIRONMENT = "local"


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    """The parsed arguments: the address to seed."""
    parser = argparse.ArgumentParser(description="Seed the local stack's verified admin user.")
    parser.add_argument("email", help="The address the local suite signs in with.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Seed the user and return 0, or print why nothing was written and return 1."""
    arguments = _parse(argv)

    from app.common.config import get_settings

    settings = get_settings()
    resolved = settings.ENVIRONMENT.strip().lower()
    if resolved != LOCAL_ENVIRONMENT:
        print(
            f"ENVIRONMENT is {resolved!r}, and this script writes only to {LOCAL_ENVIRONMENT!r}. Nothing was written.",
            file=sys.stderr,
        )
        return 1

    password = os.environ.get(PASSWORD_VARIABLE, "")
    if not password:
        print(f"{PASSWORD_VARIABLE} must be set. Nothing was written.", file=sys.stderr)
        return 1

    user_id = create_user(arguments.email.strip(), password, settings)
    print(f"Seeded the local user {user_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
