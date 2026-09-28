"""Signing in with an authenticator app, through the API and through the real sign-in page.

A user of this module's own enrols and activates a TOTP factor, then signs out. The password
leg must answer with a challenge and nothing a session could be built from: no access token,
no refresh token and no refresh cookie. The browser case then signs in through the deployed
form, which must stop on the second factor step and only reach the app once a code is entered.

The browser case is the one that matters for the page. The API always answered with a
challenge; the guest guard unmounted the form while the password leg was in flight, so the
ticket was lost and the person was shown the empty form again with no code prompt.

Nothing here prints a password, a seed, a code, a ticket or a token.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

IDENTITY_PREFIX = "/api/auth"
LOGIN_PATH = f"{IDENTITY_PREFIX}/login"
SECOND_FACTOR_HEADING = "h2:has-text('Second factor')"
CODE_INPUT = "input[autocomplete='one-time-code']"
VERIFY_BUTTON = "button[type=submit]:has-text('Verify')"
REFRESH_COOKIE_MARKERS = ("refresh", "session")

pytestmark = [pytest.mark.e2e_writes, pytest.mark.xdist_group("totp-sign-in")]


@dataclass
class EnrolledUser:
    """A throwaway user with an active TOTP factor, and the last time step it burned."""

    email: str
    password: str
    seed: str
    last_step: int


def _seed_of(payload: Any) -> str:
    """The base32 seed from an enrolment body, top level or under `data`."""
    if not isinstance(payload, dict):
        return ""
    for holder in (payload, payload.get("data")):
        if isinstance(holder, dict):
            for name in ("secret", "seed"):
                value = holder.get(name)
                if isinstance(value, str) and value:
                    return value
    return ""


def _next_step(totp: Any, last_step: int) -> int:
    """The soonest step after `last_step` the server will accept, waiting for it if needed."""
    target = last_step + 1
    deadline = time.monotonic() + totp.TIME_STEP_SECONDS * (totp.VERIFICATION_WINDOW + 2)
    while target > totp.current_step() + totp.VERIFICATION_WINDOW:
        assert time.monotonic() < deadline, f"time step {target} never came into range"
        time.sleep(1)
    return target


def _next_code(user: EnrolledUser) -> str:
    """A code for the next unburned step, recording that step as used."""
    from webbpulse.identity import totp

    user.last_step = _next_step(totp, user.last_step)
    return str(totp.generate_code(user.seed, step=user.last_step))


def _set_cookie_names(response: Any) -> list[str]:
    """The cookie names a response sets, never their values."""
    return [header.split("=", 1)[0].strip() for header in response.headers.get_list("set-cookie")]


@pytest.fixture(scope="module")
def enrolled_user(
    anon: Any,
    e2e_env: Any,
    admin_mint_token: str,
    ephemeral_user_attributes: dict[str, Any],
) -> Iterator[EnrolledUser]:
    """A user of this module's own, enrolled and activated through the identity routes, then signed out."""
    if e2e_env.read_only or not admin_mint_token:
        pytest.skip("enrolling a factor needs a writable environment and an admin token to create a user")

    from webbpulse.e2e.ephemeral import create_ephemeral_user, describe_delete_failure
    from webbpulse.e2e.identity import login, logout
    from webbpulse.identity import totp

    user = create_ephemeral_user(
        anon,
        run_id=f"{e2e_env.run_id}-totp-sign-in",
        admin_token=admin_mint_token,
        attributes=dict(ephemeral_user_attributes),
    )
    if user is None:
        pytest.skip("this deployment does not offer the ephemeral user route")
    try:
        session = login(anon, user.credentials.email, user.credentials.password)
        enrol = session.client.post(f"{IDENTITY_PREFIX}/totp/enrol", json={})
        assert enrol.status_code == 200, f"enrolment answered {enrol.status_code}"
        seed = _seed_of(enrol.json())
        assert seed, "enrolment answered 200 with no seed"

        step = totp.current_step()
        activate = session.client.post(
            f"{IDENTITY_PREFIX}/totp/activate", json={"code": totp.generate_code(seed, step=step)}
        )
        assert activate.status_code == 200, f"activation answered {activate.status_code}"
        ended = logout(session)
        assert ended.status_code in (200, 204), f"logout answered {ended.status_code}"

        yield EnrolledUser(
            email=user.credentials.email,
            password=user.credentials.password,
            seed=seed,
            last_step=step,
        )
    finally:
        failure = describe_delete_failure(anon, user, admin_token=admin_mint_token)
        if failure:
            import warnings

            warnings.warn(f"the TOTP sign-in user was not deleted: {failure}", stacklevel=1)


def test_the_password_leg_issues_a_challenge_and_no_session(anon: Any, enrolled_user: EnrolledUser) -> None:
    """With a factor active the password alone yields a ticket, and no token or refresh cookie."""
    response = anon.post(LOGIN_PATH, json={"email": enrolled_user.email, "password": enrolled_user.password})
    assert response.status_code == 200, f"the password leg answered {response.status_code}"
    body = response.json()
    assert body.get("mfa_required") is True, "the password leg did not ask for a second factor"
    assert body.get("mfa_ticket"), "the challenge carried no ticket for the second leg"
    assert "totp" in (body.get("factors") or []), f"the challenge offered {body.get('factors')}"

    holders = [body, body.get("data") if isinstance(body.get("data"), dict) else {}]
    token_names = ("access_token", "refresh_token", "id_token", "token")
    leaked = sorted(name for holder in holders for name in token_names if holder.get(name))
    assert not leaked, f"the password leg issued {leaked} before any code was entered"
    cookies = [name for name in _set_cookie_names(response) if any(m in name.lower() for m in REFRESH_COOKIE_MARKERS)]
    assert not cookies, f"the password leg set {cookies} before any code was entered"


def test_the_sign_in_page_asks_for_the_code(
    page: Any,
    login_form: Any,
    e2e_env: Any,
    enrolled_user: EnrolledUser,
    console_errors: Any,
) -> None:
    """The deployed form stops on the second factor step, and a code completes the sign-in."""
    timeout = e2e_env.browser_timeout_ms
    page.goto(login_form.path, wait_until="domcontentloaded")
    page.fill(login_form.email, enrolled_user.email)
    page.fill(login_form.password, enrolled_user.password)
    page.click(login_form.submit)

    page.wait_for_selector(SECOND_FACTOR_HEADING, state="visible", timeout=timeout)
    assert page.locator(login_form.signed_in_marker).count() == 0, "the app opened before a code was entered"
    assert page.locator(CODE_INPUT).is_visible(), "the second factor step shows no code field"

    page.fill(CODE_INPUT, _next_code(enrolled_user))
    page.click(VERIFY_BUTTON)
    page.wait_for_selector(login_form.signed_in_marker, state="visible", timeout=timeout)
    assert not console_errors, f"signing in with a code logged console errors: {console_errors.summary()}"
