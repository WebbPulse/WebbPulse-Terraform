"""Encryption for sensitive workspace variable values.

A terraform variable value is a symmetric secret the runner has to receive in the
clear, so it cannot be hashed. The shared package's `SecretMasterKeyCipher` seals
it under a per-value key derived with HKDF-SHA256 from the app secret's
`variables_master_key`, which is the same envelope pattern the TOTP seeds use and
keeps KMS off the read path.

The encryption context binds a ciphertext to one workspace and one variable key,
so a sealed value moved to another row or another key fails to authenticate.
"""

from __future__ import annotations

import base64
from typing import Any, Final

from webbpulse.identity.crypto import (
    MASTER_KEY_BYTES,
    EnvelopeDecryptionFailed,
    SealedSecret,
    SecretMasterKeyCipher,
)

from ..composition.settings import Settings, get_settings

VARIABLE_PURPOSE: Final = "terraform-variable"
"""The `purpose` half of the encryption context, so a sealed variable value can
never be replayed as a TOTP seed and the reverse."""

CIPHERTEXT_FIELDS: Final = ("secret_ciphertext", "secret_nonce", "wrapped_data_key", "secret_scheme")
"""The stored attributes a sealed value occupies. None is a secret on its own."""


class MasterKeyUnavailable(Exception):
    """No usable `variables_master_key` for this environment.

    Raised rather than falling back to a plaintext write, because a sensitive
    variable stored in the clear is indistinguishable from one that was never
    sensitive and nothing later notices.
    """


def _master_key(settings: Settings) -> bytes:
    """The decoded HKDF master key, or a named failure."""
    configured = settings.variables_master_key()
    if not configured:
        raise MasterKeyUnavailable(
            "No variables_master_key is configured. Set VARIABLES_MASTER_KEY or add the "
            "entry to the APP_SECRET_ID secret; a sensitive variable is never stored in "
            "the clear."
        )
    try:
        raw = base64.b64decode(configured.encode("ascii"), validate=True)
    except Exception as error:
        raise MasterKeyUnavailable(f"variables_master_key is not valid base64: {error}") from error
    if len(raw) != MASTER_KEY_BYTES:
        raise MasterKeyUnavailable(f"variables_master_key decodes to {len(raw)} bytes, expected {MASTER_KEY_BYTES}.")
    return raw


def cipher(settings: Settings | None = None) -> SecretMasterKeyCipher:
    """The cipher sealing this environment's sensitive variable values."""
    resolved = settings or get_settings()
    return SecretMasterKeyCipher(_master_key(resolved))


def _context_subject(workspace_id: str, key: str) -> str:
    """The `user_id` half of the encryption context, here a workspace and a key.

    The shared cipher names the field `user_id` because its first caller sealed
    per user. What it means is "the one row this ciphertext belongs to", and for
    a variable that is the workspace and the variable key together.
    """
    return f"{workspace_id}#{key}"


def seal(value: str, *, workspace_id: str, key: str, settings: Settings | None = None) -> dict[str, str]:
    """Seal one sensitive value into the attributes its row stores."""
    sealed = cipher(settings).seal(
        value.encode("utf-8"),
        user_id=_context_subject(workspace_id, key),
        purpose=VARIABLE_PURPOSE,
    )
    return sealed.as_item()


def open_sealed(item: Any, *, workspace_id: str, key: str, settings: Settings | None = None) -> str:
    """Open one sealed value from its stored row.

    Raises:
        EnvelopeDecryptionFailed: When the row carries no usable ciphertext or the
            ciphertext does not authenticate against this workspace and key.
    """
    sealed = SealedSecret.from_item(item)
    if sealed is None:
        raise EnvelopeDecryptionFailed("the stored variable carries no usable ciphertext")
    plaintext = cipher(settings).open(
        sealed,
        user_id=_context_subject(workspace_id, key),
        purpose=VARIABLE_PURPOSE,
    )
    return plaintext.decode("utf-8")


__all__ = [
    "CIPHERTEXT_FIELDS",
    "VARIABLE_PURPOSE",
    "EnvelopeDecryptionFailed",
    "MasterKeyUnavailable",
    "cipher",
    "open_sealed",
    "seal",
]
