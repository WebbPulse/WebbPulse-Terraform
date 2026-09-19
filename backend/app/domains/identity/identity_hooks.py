"""The control plane's `IdentityHooks`: how the package reads and writes `users`.

Looks a user up, decides whether they may sign in, supplies this product's claims,
and creates the row a package registration would need. Credentials, passkeys and
OAuth links are the package's own tables, so none of them appear here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from webbpulse.identity import AuthenticationRefused

from app.common.db.users import User, UserRepository

REFUSAL_MESSAGE: Final = "This account may not sign in."
"""One message for every refusal, so a caller cannot tell a disabled account from
an unverified one and use the difference to enumerate addresses."""

ADMIN_ROLE: Final = "admin"
"""The role the `is_admin` flag maps to in the `roles` claim."""


class ControlPlaneIdentityHooks:
    """The control plane's `IdentityHooks`, satisfying the protocol structurally.

    Stateless apart from one repository, so a single instance is shared per process.
    Constructing it makes no AWS call and caches no boto3 object.
    """

    def __init__(self, users: UserRepository | None = None) -> None:
        """Take an injected users repository, or build this product's own."""
        self._users = users if users is not None else UserRepository()

    def load_user_by_id(self, user_id: str) -> Mapping[str, Any] | None:
        """The user whose id is this `sub`, or `None`."""
        user = self._users.get(user_id)
        return _as_mapping(user) if user is not None else None

    def load_user_by_email(self, email: str) -> Mapping[str, Any] | None:
        """The user holding this address, or `None`.

        The address arrives lowercased and stripped, and the repository queries the
        lowercased index, so a miss costs the same single query a hit does.
        """
        user = self._users.get_by_email(email)
        return _as_mapping(user) if user is not None else None

    def may_authenticate(self, user: Mapping[str, Any]) -> None:
        """Permit an enabled, verified account and refuse everything else.

        Returns `None` to permit and raises to refuse, which is the protocol's shape
        and the one where forgetting to return lands on the refusing side.
        """
        if user.get("disabled"):
            raise AuthenticationRefused(REFUSAL_MESSAGE, error_code="ACCOUNT_DISABLED")
        if not user.get("email_verified"):
            raise AuthenticationRefused(REFUSAL_MESSAGE, error_code="EMAIL_NOT_VERIFIED")

    def claims_for(self, user: Mapping[str, Any]) -> Mapping[str, Any]:
        """This product's claims: the roles list and the display name.

        `roles` is always a list so a consumer's check is one shape. Consumers must
        test membership and never index.
        """
        roles: list[str] = [ADMIN_ROLE] if user.get("is_admin") else []
        return {"roles": roles, "display_name": user.get("display_name", "")}

    def create_user(self, *, email: str, attributes: Mapping[str, Any]) -> Mapping[str, Any]:
        """Create a users row for a package registration and return it.

        Registration is disabled in every deployed environment, so nothing calls this
        through an HTTP route today. It is implemented rather than raising because
        the protocol requires it and `scripts/create_user.py` writes through the same
        repository this returns.
        """
        record = dict(attributes)
        record.pop("id", None)
        record.pop("hashed_password", None)
        record["email"] = email
        record["display_name"] = str(record.get("display_name") or _display_name_from(email))
        return _as_mapping(self._users.create(User(**record)))

    def mark_email_verified(self, user_id: str) -> None:
        """Record that this user's address is confirmed, on the `users` row.

        Raises `ValueError` for a missing row rather than passing silently: the link
        is already spent, so a failure has to be visible.
        """
        if self._users.get(user_id) is None:
            raise ValueError(
                f"mark_email_verified found no user with id {user_id!r}. The link was consumed, "
                "so the address is not verified and the user needs a new one."
            )
        self._users.update(user_id, email_verified=True)

    def delete_user(self, user_id: str) -> bool:
        """Hard-delete this product's users row, returning whether one was there.

        Only the users row. The identity rows belong to the identity module's tables,
        which this product does not purge from here.
        """
        return self._users.delete(user_id)

    def on_user_created(self, user: Mapping[str, Any], via: str) -> None:
        """No side effects to run.

        No email is sent because this deployment configures no sender, and a new
        control plane account owns no default rows.
        """
        del user, via

    def has_other_sign_in_method(self, user_id: str) -> bool:
        """Whether this user holds a sign-in method the package cannot see.

        The control plane keeps none: every credential, passkey and OAuth link lives
        in the package's own tables, which `unlink` counts for itself.
        """
        del user_id
        return False

    def user_repository(self) -> object:
        """This product's users repository. Typed `object`, as the protocol has it."""
        return self._users


def _as_mapping(user: User) -> Mapping[str, Any]:
    """A user row as the plain mapping the hooks protocol returns.

    `mode="json"` so the id reaches the package as the string it becomes in the
    `sub` claim.
    """
    return user.model_dump(mode="json")


def _display_name_from(email: str) -> str:
    """A display name derived from the address's local part."""
    return email.partition("@")[0].strip() or "user"
