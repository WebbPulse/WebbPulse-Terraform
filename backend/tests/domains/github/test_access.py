"""Every GitHub route is admin only."""

from __future__ import annotations

import pytest

from app.common.core.auth import ADMIN, ALL_SCOPES

ROUTES = [
    ("get", "/api/v1/github/app"),
    ("post", "/api/v1/github/app/manifest"),
    ("post", "/api/v1/github/app/conversions"),
    ("post", "/api/v1/github/install-state"),
    ("post", "/api/v1/github/installations"),
    ("get", "/api/v1/github/installations"),
    ("post", "/api/v1/github/installations/1/refresh"),
    ("delete", "/api/v1/github/installations/1"),
    ("get", "/api/v1/github/installations/1/repositories"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_an_anonymous_caller_is_refused(client, method, path):
    """No credential, no route."""
    assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_every_other_scope_is_not_enough(scoped_client, method, path):
    """A key holding every scope but `admin` is refused."""
    with scoped_client(*[scope for scope in ALL_SCOPES if scope != ADMIN]) as caller:
        assert getattr(caller, method)(path).status_code == 403


def test_admin_is_not_a_read_scope():
    """A non-admin session gets the read scopes only, so it never carries `admin`."""
    from app.common.identity.identity_hooks import READ_SCOPES

    assert ADMIN not in READ_SCOPES
    assert ADMIN in ALL_SCOPES
