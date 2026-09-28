"""Passkeys through the real pages, with a Chromium virtual authenticator standing in for a device.

A user of this module's own signs in with its password, adds a passkey on the security page,
signs out, signs in with only the passkey, then signs out and does it again. The authenticator
is a CDP virtual one holding a discoverable credential with user verification, so the sign-in
button's ceremony completes with no email typed, the way a person with a passkey would use it.

The cases share one browser context and one authenticator, so they run in order on one worker
and a later case reads the credential an earlier one created. The context records no trace,
because the password is typed into it and nothing here should keep it.

Nothing here prints a password, a credential, a challenge or a token.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest

AVAILABILITY_PATH = "/api/auth/passkeys/availability"
SECURITY_PATH = "/settings/security"
PASSKEY_NAME = "E2E virtual key"
ADD_BUTTON = "button:has-text('Add a passkey')"
NAME_INPUT = "input[aria-label='New passkey name']"
CONTINUE_BUTTON = "button[type=submit]:has-text('Continue')"
ADDED_NOTICE = f"[role=status]:has-text('Passkey \"{PASSKEY_NAME}\" added.')"
PASSKEY_ROWS = "ul[aria-label='Passkeys'] [data-testid='passkey-row']"
PASSKEY_BUTTON = "button:has-text('Sign in with passkey')"
LOGIN_OPTIONS_PATH = "/api/auth/login/passkey/options"

pytestmark = [pytest.mark.xdist_group("passkey-sign-in")]

writes = pytest.mark.e2e_writes


@dataclass
class PasskeyUser:
    """A throwaway user of this module's own, and whether it holds a passkey yet."""

    email: str
    password: str
    registered: bool = False


@dataclass
class PasskeyBrowser:
    """The page the cases drive and the CDP session holding its virtual authenticator."""

    page: Any
    cdp: Any
    authenticator_id: str


@pytest.fixture(scope="module")
def passkey_user(
    anon: Any,
    e2e_env: Any,
    admin_mint_token: str,
    ephemeral_user_attributes: dict[str, Any],
) -> Iterator[PasskeyUser]:
    """A user created for this module and deleted after it, which purges its passkeys too."""
    if e2e_env.read_only or not admin_mint_token:
        pytest.skip("adding a passkey needs a writable environment and an admin token to create a user")

    from webbpulse.e2e.ephemeral import create_ephemeral_user, describe_delete_failure

    user = create_ephemeral_user(
        anon,
        run_id=f"{e2e_env.run_id}-passkey-sign-in",
        admin_token=admin_mint_token,
        attributes=dict(ephemeral_user_attributes),
    )
    if user is None:
        pytest.skip("this deployment does not offer the ephemeral user route")
    try:
        yield PasskeyUser(email=user.credentials.email, password=user.credentials.password)
    finally:
        failure = describe_delete_failure(anon, user, admin_token=admin_mint_token)
        if failure:
            import warnings

            warnings.warn(f"the passkey sign-in user was not deleted: {failure}", stacklevel=1)


@pytest.fixture(scope="module")
def passkey_browser(browser: Any, e2e_env: Any, gate_cookies: Any) -> Iterator[PasskeyBrowser]:
    """One untraced context with a virtual platform authenticator attached to its page."""
    if e2e_env.browser_name != "chromium":
        pytest.skip("the virtual authenticator is a Chromium DevTools feature")

    context = browser.new_context(base_url=e2e_env.web_base_url, ignore_https_errors=False)
    if gate_cookies is not None:
        context.add_cookies(gate_cookies.as_playwright_cookies())
    try:
        page = context.new_page()
        cdp = context.new_cdp_session(page)
        cdp.send("WebAuthn.enable", {"enableUI": False})
        added = cdp.send(
            "WebAuthn.addVirtualAuthenticator",
            {
                "options": {
                    "protocol": "ctap2",
                    "transport": "internal",
                    "hasResidentKey": True,
                    "hasUserVerification": True,
                    "isUserVerified": True,
                    "automaticPresenceSimulation": True,
                }
            },
        )
        yield PasskeyBrowser(page=page, cdp=cdp, authenticator_id=str(added["authenticatorId"]))
    finally:
        context.close()


def _credential_count(browser: PasskeyBrowser) -> int:
    """How many credentials the virtual authenticator holds, never their contents."""
    listed = browser.cdp.send("WebAuthn.getCredentials", {"authenticatorId": browser.authenticator_id})
    return len(listed.get("credentials") or [])


def _presence(browser: PasskeyBrowser, *, present: bool) -> None:
    """Whether the authenticator answers ceremonies by itself, as a person touching it would."""
    browser.cdp.send(
        "WebAuthn.setAutomaticPresenceSimulation",
        {"authenticatorId": browser.authenticator_id, "enabled": present},
    )


def _sign_out(browser: PasskeyBrowser, login_form: Any, timeout: int) -> None:
    """Sign out through the app chrome and wait for the sign-in form.

    The authenticator stops answering first. The sign-in page arms passkey autofill as it
    mounts, and an authenticator that answers by itself would complete that request before
    the button is ever pressed, so the button case would prove the autofill path instead.
    """
    _presence(browser, present=False)
    browser.page.click(login_form.sign_out)
    browser.page.wait_for_selector(login_form.signed_out_marker, state="visible", timeout=timeout)


def _sign_in_with_passkey(browser: PasskeyBrowser, login_form: Any, timeout: int) -> None:
    """Press the passkey button on the sign-in page and wait for the signed-in chrome.

    The page settles first, so the autofill request it arms on mount is already pending when
    the button is pressed and the options request awaited here is the button's own. The
    authenticator answers only once that ceremony has started, so the sign-in it completes
    is the button's rather than the autofill's.
    """
    page = browser.page
    page.goto(login_form.path, wait_until="domcontentloaded")
    button = page.locator(PASSKEY_BUTTON)
    button.wait_for(state="visible", timeout=timeout)
    page.wait_for_load_state("networkidle", timeout=timeout)
    with page.expect_request(
        lambda request: request.method == "POST" and request.url.endswith(LOGIN_OPTIONS_PATH), timeout=timeout
    ):
        button.click()
    _presence(browser, present=True)
    page.wait_for_selector(login_form.signed_in_marker, state="visible", timeout=timeout)


def test_the_deployment_offers_passwordless_passkeys(anon: Any) -> None:
    """The anonymous availability probe says passkeys are a way in here, production included."""
    response = anon.get(AVAILABILITY_PATH)
    assert response.status_code == 200, f"the availability probe answered {response.status_code}"
    body = response.json()
    assert body.get("enabled") is True, "the deployment does not declare the passkey routes"
    assert body.get("passwordless") is True, "the deployment does not offer passwordless sign-in"


@writes
def test_a_passkey_is_added_on_the_security_page(
    passkey_browser: PasskeyBrowser,
    passkey_user: PasskeyUser,
    login_form: Any,
    e2e_env: Any,
) -> None:
    """Signed in with a password, the security page registers a passkey on the device."""
    timeout = e2e_env.browser_timeout_ms
    page = passkey_browser.page
    page.goto(login_form.path, wait_until="domcontentloaded")
    page.fill(login_form.email, passkey_user.email)
    page.fill(login_form.password, passkey_user.password)
    page.click(login_form.submit)
    page.wait_for_selector(login_form.signed_in_marker, state="visible", timeout=timeout)

    page.goto(SECURITY_PATH, wait_until="domcontentloaded")
    page.click(ADD_BUTTON, timeout=timeout)
    page.fill(NAME_INPUT, PASSKEY_NAME)
    page.click(CONTINUE_BUTTON)

    page.wait_for_selector(ADDED_NOTICE, state="visible", timeout=timeout)
    rows = page.locator(PASSKEY_ROWS).filter(has_text=PASSKEY_NAME)
    assert rows.count() == 1, "the new passkey is not listed on the security page"
    assert _credential_count(passkey_browser) == 1, "the authenticator holds no credential after adding one"
    passkey_user.registered = True


@writes
def test_signing_in_with_the_passkey_alone(
    passkey_browser: PasskeyBrowser,
    passkey_user: PasskeyUser,
    login_form: Any,
    e2e_env: Any,
) -> None:
    """After signing out, the passkey button signs the user in with no email or password typed."""
    if not passkey_user.registered:
        pytest.fail("no passkey was added, so there is nothing to sign in with")
    timeout = e2e_env.browser_timeout_ms
    _sign_out(passkey_browser, login_form, timeout)
    _sign_in_with_passkey(passkey_browser, login_form, timeout)

    passkey_browser.page.goto(SECURITY_PATH, wait_until="domcontentloaded")
    rows = passkey_browser.page.locator(PASSKEY_ROWS).filter(has_text=PASSKEY_NAME)
    rows.first.wait_for(state="visible", timeout=timeout)
    assert "Last used" in rows.first.inner_text(), "signing in did not mark the passkey used"


@writes
def test_signing_out_and_in_again_with_the_passkey(
    passkey_browser: PasskeyBrowser,
    passkey_user: PasskeyUser,
    login_form: Any,
    e2e_env: Any,
) -> None:
    """A second sign-out and passkey sign-in works the same, so the session really ended."""
    if not passkey_user.registered:
        pytest.fail("no passkey was added, so there is nothing to sign in with")
    timeout = e2e_env.browser_timeout_ms
    _sign_out(passkey_browser, login_form, timeout)
    passkey_browser.page.goto("/workspaces", wait_until="domcontentloaded")
    passkey_browser.page.wait_for_url(f"**{login_form.protected_redirect}**", timeout=timeout)
    _sign_in_with_passkey(passkey_browser, login_form, timeout)
