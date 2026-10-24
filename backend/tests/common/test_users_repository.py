"""The `users` table: the row the identity package's hooks read and write.

`update` builds a real `UpdateExpression`, so these cover the paths the identity
flows drive: verifying an address, and keeping the lookup index in step with it.
"""

from __future__ import annotations

import pytest

from app.common.db.users import User, UserRepository

EMAIL = "Someone@Example.COM"


@pytest.fixture
def users() -> UserRepository:
    """A users repository against the mocked table."""
    return UserRepository()


def _create(users: UserRepository, email: str = EMAIL) -> User:
    """Store one user and return it."""
    return users.create(User(id="user-1", email=email, display_name="Someone"))


def test_a_user_round_trips(users: UserRepository) -> None:
    """The row written is the row read back."""
    _create(users)
    stored = users.get("user-1")
    assert stored is not None
    assert stored.display_name == "Someone"
    assert stored.email_verified is False
    assert stored.is_admin is False


def test_a_user_is_found_by_address_case_insensitively(users: UserRepository) -> None:
    """Sign-in looks the address up lowercased, so the stored case cannot matter."""
    _create(users)
    assert users.get_by_email("someone@example.com") is not None
    assert users.get_by_email("  SOMEONE@EXAMPLE.COM  ") is not None


def test_an_unknown_address_is_a_miss(users: UserRepository) -> None:
    """A miss is None rather than an error, which is what the hooks expect."""
    assert users.get_by_email("nobody@example.com") is None


def test_an_empty_lookup_is_a_miss(users: UserRepository) -> None:
    """An empty id or address costs no query."""
    assert users.get("") is None
    assert users.get_by_email("   ") is None


def test_marking_an_address_verified_persists(users: UserRepository) -> None:
    """The write behind `mark_email_verified`, which every sign-in depends on."""
    _create(users)
    assert users.update("user-1", email_verified=True).email_verified is True
    stored = users.get("user-1")
    assert stored is not None and stored.email_verified is True


def test_changing_the_address_moves_the_index(users: UserRepository) -> None:
    """The lookup index cannot be left pointing at the old address.

    A stale entry would let a user sign in under an address they no longer hold, so
    the update rewrites `email_lower` alongside `email`.
    """
    _create(users)
    users.update("user-1", email="Other@Example.com")
    assert users.get_by_email("other@example.com") is not None
    assert users.get_by_email("someone@example.com") is None


def test_updating_an_unknown_user_raises(users: UserRepository) -> None:
    """An update naming no row is a caller bug, not a create."""
    with pytest.raises(KeyError):
        users.update("user-missing", email_verified=True)


def test_deleting_reports_whether_a_row_was_there(users: UserRepository) -> None:
    """Delete is idempotent and says which case it hit."""
    _create(users)
    assert users.delete("user-1") is True
    assert users.delete("user-1") is False
    assert users.get("user-1") is None


def test_many_users_are_read_in_one_batch(users: UserRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """A long id list resolves through `get_many`, never a `GetItem` per id."""
    ids = [f"user-{index}" for index in range(150)]
    for user_id in ids:
        users.create(User(id=user_id, email=f"{user_id}@example.test", display_name=user_id))
    monkeypatch.setattr(users._repository, "get", lambda *_args, **_kwargs: pytest.fail("GetItem per id"))

    found = users.get_many([*ids, *ids[:10]])

    assert sorted(found) == sorted(ids)
    assert all(found[user_id].display_name == user_id for user_id in ids)


def test_missing_users_are_left_out_of_a_batch(users: UserRepository) -> None:
    """An id with no row is absent from the result, and blank or empty lists cost nothing."""
    _create(users)
    assert set(users.get_many(["user-1", "user-gone", ""])) == {"user-1"}
    assert users.get_many([]) == {}
    assert users.get_many(["user-gone"]) == {}


RESERVED_DOMAIN_EMAIL = "e2e-local@e2e.invalid"


def test_a_reserved_domain_address_round_trips(users: UserRepository) -> None:
    """A `.invalid` address stores and reads back, which the local e2e stack signs in as."""
    _create(users, email=RESERVED_DOMAIN_EMAIL)
    stored = users.get("user-1")
    assert stored is not None
    assert stored.email == RESERVED_DOMAIN_EMAIL
    assert users.get_by_email("E2E-Local@E2E.Invalid") is not None


@pytest.mark.parametrize(
    "address",
    ["someone", "@example.com", "someone@", "someone@example", "some one@example.com", "a@b@example.com"],
)
def test_a_malformed_address_is_still_refused(address: str) -> None:
    """Relaxing the reserved domain check must not let a non address onto the row."""
    with pytest.raises(ValueError):
        User(id="user-1", email=address)
