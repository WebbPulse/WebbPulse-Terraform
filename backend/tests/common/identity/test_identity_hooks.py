"""The hooks the identity package asks before it mints anything.

`may_authenticate` is the gate: it decides whether a row that exists is allowed to
become a session. It returns `None` to permit and raises to refuse, so the cases
that must refuse are the ones worth pinning.
"""

from __future__ import annotations

import pytest
from webbpulse.identity import AuthenticationRefused
from webbpulse.identity.claims import coerce_claims
from webbpulse.identity.scopes import SCOPES_KEY

from app.common.core.auth import ALL_SCOPES, RUNNER_SCOPE
from app.common.db.users import User, UserRepository
from app.common.identity.identity_hooks import (
    ADMIN_ROLE,
    READ_SCOPES,
    ControlPlaneIdentityHooks,
)

EMAIL = "Someone@Example.COM"


@pytest.fixture
def hooks() -> ControlPlaneIdentityHooks:
    """Hooks over the mocked users table."""
    return ControlPlaneIdentityHooks(UserRepository())


def _store(hooks: ControlPlaneIdentityHooks, **overrides: object) -> User:
    """Write one user with the given overrides and return it."""
    fields: dict[str, object] = {"id": "user-1", "email": EMAIL, "email_verified": True}
    fields.update(overrides)
    repository = hooks.user_repository()
    assert isinstance(repository, UserRepository)
    return repository.create(User(**fields))  # type: ignore[arg-type]


def test_a_verified_enabled_user_may_authenticate(hooks: ControlPlaneIdentityHooks) -> None:
    """The permitting case returns None rather than raising."""
    _store(hooks)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    assert hooks.may_authenticate(user) is None


def test_a_disabled_user_is_refused(hooks: ControlPlaneIdentityHooks) -> None:
    """A disabled account cannot sign in however good its password is."""
    _store(hooks, disabled=True)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    with pytest.raises(AuthenticationRefused) as refusal:
        hooks.may_authenticate(user)
    assert refusal.value.error_code == "ACCOUNT_DISABLED"


def test_an_unverified_user_is_refused(hooks: ControlPlaneIdentityHooks) -> None:
    """An unverified address cannot sign in.

    This deployment sends no verification email, so the only way a row becomes
    verified is `scripts/create_user.py`, which sets it deliberately.
    """
    _store(hooks, email_verified=False)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    with pytest.raises(AuthenticationRefused) as refusal:
        hooks.may_authenticate(user)
    assert refusal.value.error_code == "EMAIL_NOT_VERIFIED"


def test_the_refusals_do_not_distinguish_themselves(hooks: ControlPlaneIdentityHooks) -> None:
    """Both refusals carry one message, so neither enumerates an address."""
    _store(hooks, disabled=True, email_verified=False)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    with pytest.raises(AuthenticationRefused) as refusal:
        hooks.may_authenticate(user)
    assert "may not sign in" in str(refusal.value)


def test_an_admin_carries_the_admin_role(hooks: ControlPlaneIdentityHooks) -> None:
    """`is_admin` becomes the `roles` claim consumers test membership against."""
    _store(hooks, is_admin=True, display_name="Someone")
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    claims = hooks.claims_for(user)
    assert claims["roles"] == [ADMIN_ROLE]
    assert claims["display_name"] == "Someone"


def test_a_plain_user_carries_an_empty_roles_list(hooks: ControlPlaneIdentityHooks) -> None:
    """`roles` is always a list, so a consumer's check is one shape."""
    _store(hooks)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    assert hooks.claims_for(user)["roles"] == []


def test_a_user_is_found_by_address_case_insensitively(hooks: ControlPlaneIdentityHooks) -> None:
    """Sign-in looks the address up lowercased, so the stored case cannot matter."""
    _store(hooks)
    assert hooks.load_user_by_email("someone@example.com") is not None
    assert hooks.load_user_by_email("nobody@example.com") is None


def test_marking_an_unknown_user_verified_is_an_error(hooks: ControlPlaneIdentityHooks) -> None:
    """A spent verification link naming no row has to fail visibly."""
    with pytest.raises(ValueError):
        hooks.mark_email_verified("user-missing")


def test_an_admin_token_carries_every_scope(hooks: ControlPlaneIdentityHooks) -> None:
    """An admin holds the whole contract, so no guarded route refuses them."""
    _store(hooks, is_admin=True)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    assert hooks.claims_for(user)["scope"].split() == list(ALL_SCOPES)


def test_a_non_admin_token_carries_the_read_scopes_only(hooks: ControlPlaneIdentityHooks) -> None:
    """A plain account sees the control plane and changes nothing in it."""
    _store(hooks)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    granted = hooks.claims_for(user)["scope"].split()
    assert granted == [scope for scope in ALL_SCOPES if scope.endswith(":read")]
    assert not [scope for scope in granted if not scope.endswith(":read")]


def test_the_runner_scope_reaches_no_person(hooks: ControlPlaneIdentityHooks) -> None:
    """The run token's scope is never granted to a human, admin or not."""
    _store(hooks, is_admin=True)
    user = hooks.load_user_by_id("user-1")
    assert user is not None
    assert RUNNER_SCOPE not in hooks.claims_for(user)["scope"].split()


def test_the_claims_round_trip_into_the_scopes_list(hooks: ControlPlaneIdentityHooks) -> None:
    """`coerce_claims` splits the claim into the `scopes` list `require_scopes` reads."""
    _store(hooks, is_admin=True)
    admin = hooks.load_user_by_id("user-1")
    assert admin is not None
    assert coerce_claims(hooks.claims_for(admin))[SCOPES_KEY] == list(ALL_SCOPES)

    repository = hooks.user_repository()
    assert isinstance(repository, UserRepository)
    repository.create(User(id="user-2", email="plain@example.com", email_verified=True))
    plain = hooks.load_user_by_id("user-2")
    assert plain is not None
    assert coerce_claims(hooks.claims_for(plain))[SCOPES_KEY] == list(READ_SCOPES)


def test_delete_user_removes_the_row_and_reports_it(hooks: ControlPlaneIdentityHooks) -> None:
    """The ephemeral e2e user route's hook: the row goes, and the answer is `True`."""
    _store(hooks)
    assert hooks.delete_user("user-1") is True
    assert hooks.load_user_by_id("user-1") is None


def test_delete_user_answers_false_for_an_unknown_id(hooks: ControlPlaneIdentityHooks) -> None:
    """A delete of a row that is not there reports it rather than raising."""
    assert hooks.delete_user("user-missing") is False


def test_delete_user_releases_the_address(hooks: ControlPlaneIdentityHooks) -> None:
    """The address is free afterwards, so an e2e run can reuse it on the next pass."""
    _store(hooks)
    hooks.delete_user("user-1")
    assert hooks.load_user_by_email(EMAIL.lower()) is None
