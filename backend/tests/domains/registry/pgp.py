"""Building OpenPGP keys and detached signatures in process, so the suite needs no gpg binary.

Only the packets the registry reads are produced: a version 4 RSA public key and a
version 4 binary document signature over SHA-256 naming its issuer.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CREATED = (1_700_000_000).to_bytes(4, "big")


def _mpi(value: int) -> bytes:
    """One multiprecision integer."""
    width = (value.bit_length() + 7) // 8
    return value.bit_length().to_bytes(2, "big") + value.to_bytes(width, "big")


def _packet(tag: int, body: bytes) -> bytes:
    """One new format packet with a five octet length."""
    return bytes([0xC0 | tag, 0xFF]) + len(body).to_bytes(4, "big") + body


@dataclass
class SigningKey:
    """An RSA key with its OpenPGP public key packet and id."""

    private: rsa.RSAPrivateKey
    body: bytes
    key_id: str

    @classmethod
    def generate(cls) -> "SigningKey":
        """A fresh 2048 bit key."""
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private.public_key().public_numbers()
        body = b"\x04" + CREATED + b"\x01" + _mpi(numbers.n) + _mpi(numbers.e)
        fingerprint = hashlib.sha1(b"\x99" + len(body).to_bytes(2, "big") + body, usedforsecurity=False)
        return cls(private=private, body=body, key_id=fingerprint.hexdigest().upper()[-16:])

    def armored(self) -> str:
        """The public key block as ASCII armor."""
        encoded = base64.b64encode(_packet(6, self.body)).decode()
        lines = [encoded[index : index + 64] for index in range(0, len(encoded), 64)]
        return "\n".join(["-----BEGIN PGP PUBLIC KEY BLOCK-----", "", *lines, "-----END PGP PUBLIC KEY BLOCK-----", ""])

    def sign(self, data: bytes) -> bytes:
        """A binary detached signature over `data`."""
        hashed = bytes([5, 2]) + CREATED
        trailer = b"\x04\x00\x01\x08" + len(hashed).to_bytes(2, "big") + hashed
        unhashed = bytes([9, 16]) + bytes.fromhex(self.key_id)
        signed = data + trailer + b"\x04\xff" + len(trailer).to_bytes(4, "big")
        value = self.private.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        left = hashlib.sha256(signed).digest()[:2]
        body = trailer + len(unhashed).to_bytes(2, "big") + unhashed + left + _mpi(int.from_bytes(value, "big"))
        return _packet(2, body)
