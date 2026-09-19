"""The hooks the identity package asks before it mints anything.

`may_authenticate` is the gate: it decides whether a row that exists is allowed to
become a session. It returns `None` to permit and raises to refuse, so the cases
that must refuse are the ones worth pinning.
"""

from __future__ import annotations

import pytest
from webbpulse.identity import AuthenticationRefused

from app.common.db.users import User, UserRepository
from app.domains.identity.identity_hooks import ADMIN_ROLE, ControlPlaneIdentityHooks

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
