"""The derived MAC key survives the move onto `webbpulse.security.derive_key`.

Before the move `variable_cipher.derive_key` called `cryptography`'s HKDF directly,
with SHA-256, a 32 byte length, no salt and `webbpulse-terraform/<purpose>` as the
info. Every signed `tfe.v2` log URL in flight was minted under that key, so the
shared helper must reproduce it byte for byte. These tests rebuild the old
construction inline from `cryptography` primitives under a fixed master key and
hold the new derivation, and a token signed under the old key, against it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Final

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from webbpulse.security import derive_key

from app.common.composition.settings import Settings
from app.common.core import variable_cipher
from app.domains.runs import tfe_runs

FIXED_MASTER: Final = bytes(range(32))
"""A fixed 32 byte master key, so the vectors below never depend on the environment."""

RFC5869_A3_IKM: Final = bytes([0x0B] * 22)
RFC5869_A3_OKM: Final = bytes.fromhex(
    "8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8"
)
"""RFC 5869 appendix A.3: SHA-256 with an empty salt and empty info, 42 bytes."""


def old_derive_key(master: bytes, purpose: str) -> bytes:
    """The derivation exactly as it stood before the move onto the shared helper."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=f"webbpulse-terraform/{purpose}".encode(),
    ).derive(master)


@pytest.fixture
def fixed_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings whose variables master key is `FIXED_MASTER`."""
    monkeypatch.setenv("VARIABLES_MASTER_KEY", base64.b64encode(FIXED_MASTER).decode("ascii"))
    return Settings()


def test_the_shared_helper_matches_rfc_5869_with_an_empty_salt() -> None:
    """An empty salt is the RFC's zero-filled one, which is what `salt=None` meant before."""
    assert derive_key(RFC5869_A3_IKM, "", length=42) == RFC5869_A3_OKM
    assert HKDF(algorithm=hashes.SHA256(), length=42, salt=None, info=b"").derive(RFC5869_A3_IKM) == RFC5869_A3_OKM


@pytest.mark.parametrize("purpose", [tfe_runs.LOG_KEY_PURPOSE, "another-purpose", ""])
def test_the_derived_key_is_byte_for_byte_the_old_one(fixed_settings: Settings, purpose: str) -> None:
    """The new derivation returns exactly the old key for every purpose."""
    assert variable_cipher.derive_key(purpose, fixed_settings) == old_derive_key(FIXED_MASTER, purpose)


def test_a_log_token_signed_under_the_old_key_still_verifies(fixed_settings: Settings) -> None:
    """A token minted before the move reads its log after it, and only its own."""
    issued = 1_800_000_000
    expires = issued + tfe_runs.LOG_URL_TTL_SECONDS
    old_key = old_derive_key(FIXED_MASTER, tfe_runs.LOG_KEY_PURPOSE)
    digest = hmac.new(old_key, f"plan-X:{expires}".encode(), hashlib.sha256).digest()
    token = f"{expires}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"

    assert tfe_runs.verify_log_token("plan-X", token, fixed_settings, now=issued + 60)
    assert not tfe_runs.verify_log_token("plan-Y", token, fixed_settings, now=issued + 60)
    assert tfe_runs.log_token("plan-X", fixed_settings, now=issued) == token
