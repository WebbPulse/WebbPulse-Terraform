"""The `users` table: the account row the identity package's hooks read and write.

Credentials, passkeys, OAuth links and second factors are not here. They belong to
`webbpulse.identity`'s own tables, which the identity module provisions. This table
holds only what the control plane itself needs to know about a person.

The physical name comes from `USERS_TABLE` the way the four product tables do, so
nothing here derives a name the Terraform stack did not choose.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Final, Mapping

from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, Field, field_validator
from webbpulse.dynamodb import Repository

from ..composition.settings import Settings, get_settings
from .tables import USERS, local_table_name

EMAIL_INDEX: Final = "email_lower-index"
"""The GSI resolving a lowercased address to its user, which is how sign-in looks one up."""


def utc_now() -> datetime:
    """The current UTC time, as a timezone-aware datetime."""
    return datetime.now(timezone.utc)


def new_user_id() -> str:
    """A fresh user id, which becomes the `sub` claim of every token minted for them."""
    return str(uuid.uuid4())


def validate_email(value: str) -> str:
    """The address with surrounding space stripped, rejecting one that is not an address.

    Deliberately looser than `EmailStr`: it checks the shape rather than the
    deliverability of the domain, so a reserved test domain such as `.invalid` or
    `.test` round trips. The local e2e stack signs in as one, and `EmailStr` rejects
    those outright, which would fail every read of the row rather than the write that
    created it.

    This is the only check an address gets. No request body carries one, so every
    address arrives through the identity package's hooks or a seeding script, and
    widening this widens what those may store.
    """
    address = value.strip()
    local, separator, domain = address.partition("@")
    if not separator or not local or "@" in domain:
        raise ValueError("value is not a valid email address")
    if "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise ValueError("value is not a valid email address")
    if any(character.isspace() for character in address):
        raise ValueError("value is not a valid email address")
    return address


class User(BaseModel):
    """One person's control plane account."""

    id: str = Field(default_factory=new_user_id)
    email: str
    display_name: str = ""
    email_verified: bool = False
    disabled: bool = False
    is_admin: bool = False
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str) -> str:
        """Normalise and shape check the stored address."""
        return validate_email(value)

    @property
    def email_lower(self) -> str:
        """The address lowercased, which is what the lookup index stores."""
        return str(self.email).strip().lower()


class UserRepository:
    """Reads and writes `users` rows through the shared package repository.

    Constructing it makes no AWS call: the package repository builds its client on
    first use.
    """

    def __init__(self, repository: Repository | None = None, settings: Settings | None = None) -> None:
        """Take an injected package repository, or build this table's own."""
        self._repository = repository if repository is not None else _build_repository(settings)

    def get(self, user_id: str) -> User | None:
        """The user with this id, or `None`."""
        if not user_id:
            return None
        item = self._repository.get({"id": user_id})
        return _as_user(item) if item is not None else None

    def get_by_email(self, email: str) -> User | None:
        """The user holding this address, or `None`.

        Queries `email_lower-index`, so casing and surrounding space never matter.
        """
        normalized = email.strip().lower()
        if not normalized:
            return None
        page = self._repository.query(Key("email_lower").eq(normalized), index_name=EMAIL_INDEX, limit=1)
        if not page.items:
            return None
        return _as_user(page.items[0])

    def create(self, user: User) -> User:
        """Write a new user row, keyed by id and indexed by lowercased address."""
        self._repository.put(_as_item(user))
        return user

    def update(self, user_id: str, **attributes: Any) -> User:
        """Apply `attributes` to one user row and return the stored result.

        Every attribute name is aliased, because DynamoDB reserves ordinary words
        such as `name` and `status` and rejects an expression using them directly.
        Setting `email` rewrites the indexed `email_lower` alongside it, so the
        lookup index can never disagree with the address on the row.

        Raises `KeyError` when no row has this id, rather than creating one: an
        update naming a user who is not there is a bug in the caller. The check is a
        read rather than a condition expression, because DynamoDB refuses a
        condition on a key attribute in `UpdateItem`.
        """
        stored = self.get(user_id)
        if stored is None:
            raise KeyError(user_id)

        values = dict(attributes)
        if "email" in values:
            values["email_lower"] = str(values["email"]).strip().lower()
        if not values:
            return stored

        names = {f"#n{index}": key for index, key in enumerate(values)}
        expression_values = {f":v{index}": value for index, value in enumerate(values.values())}
        assignments = ", ".join(f"#n{index} = :v{index}" for index in range(len(values)))

        item = self._repository.update(
            {"id": user_id},
            update_expression=f"SET {assignments}",
            expression_values=expression_values,
            expression_names=names,
            return_values="ALL_NEW",
        )
        if item is None:
            raise KeyError(user_id)
        return _as_user(item)

    def delete(self, user_id: str) -> bool:
        """Hard-delete this user row, returning whether one was there."""
        if self.get(user_id) is None:
            return False
        self._repository.delete({"id": user_id})
        return True


def _build_repository(settings: Settings | None = None) -> Repository:
    """The package repository for the `users` table in this environment.

    Built the way `repositories.py` builds the product tables: the stack names the
    table whole, so the physical name is passed as the logical one with an empty
    prefix and `logical_name` is restored for the callers that read it.
    """
    resolved = settings or get_settings()
    physical = resolved.USERS_TABLE or local_table_name(USERS, resolved.ENVIRONMENT)
    repository = Repository(
        physical,
        prefix="",
        region_name=resolved.AWS_REGION_NAME or None,
        endpoint_url=resolved.dynamodb_endpoint_url,
    )
    repository.logical_name = USERS
    return repository


def _as_item(user: User) -> dict[str, Any]:
    """A user as the stored item, carrying the index's lowercased address."""
    item = user.model_dump(mode="json")
    item["email_lower"] = user.email_lower
    return item


def _as_user(item: Mapping[str, Any]) -> User:
    """One stored item as a `User`."""
    return User.model_validate(dict(item))
