"""Verifying a provider release's detached OpenPGP signature against the registry's public key.

GoReleaser signs a provider's `SHA256SUMS` with `gpg --detach-sign`, which writes one
version 4 signature packet over the file's bytes. Terraform checks that signature
itself at install time against the key the download endpoint serves; the registry
checks it once at ingest too, so a release signed by any other key is refused
before it is published rather than failing every `terraform init` afterwards.

Only what that needs is implemented: ASCII armor, the old and new packet headers,
version 4 RSA public keys and subkeys, and version 4 RSA binary document
signatures over SHA-256, SHA-384 or SHA-512. Anything else is refused.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Final, Iterator

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

PUBLIC_KEY_TAG: Final = 6
PUBLIC_SUBKEY_TAG: Final = 14
SIGNATURE_TAG: Final = 2
RSA_ALGORITHMS: Final = frozenset({1, 3})
BINARY_DOCUMENT: Final = 0x00
ISSUER_SUBPACKET: Final = 16
ISSUER_FINGERPRINT_SUBPACKET: Final = 33

HASHES: Final[dict[int, type[hashes.HashAlgorithm]]] = {
    8: hashes.SHA256,
    9: hashes.SHA384,
    10: hashes.SHA512,
}


class SignatureInvalid(Exception):
    """The signature is malformed, made by another key, or does not match the data."""


@dataclass(frozen=True, slots=True)
class RsaKey:
    """One RSA public key or subkey from a key block."""

    key_id: str
    fingerprint: str
    public_key: rsa.RSAPublicKey


def dearmor(armored: str) -> bytes:
    """The binary packets inside an ASCII armored block.

    Raises:
        SignatureInvalid: The text is not armor.
    """
    lines = [line.strip() for line in armored.strip().splitlines()]
    try:
        start = next(index for index, line in enumerate(lines) if line.startswith("-----BEGIN PGP"))
        end = next(index for index, line in enumerate(lines) if line.startswith("-----END PGP"))
    except StopIteration as error:
        raise SignatureInvalid("the key is not ASCII armored") from error
    body = lines[start + 1 : end]
    while body and body[0]:
        body = body[1:]
    encoded = "".join(line for line in body if line and not line.startswith("="))
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise SignatureInvalid("the armor is not base64") from error


def _packets(data: bytes) -> Iterator[tuple[int, bytes, bytes]]:
    """Each packet as `(tag, header, body)`.

    Raises:
        SignatureInvalid: A header is malformed or a body is truncated.
    """
    offset = 0
    while offset < len(data):
        first = data[offset]
        if not first & 0x80:
            raise SignatureInvalid("a packet header is malformed")
        if first & 0x40:
            tag = first & 0x3F
            octet = data[offset + 1]
            if octet < 192:
                length, size = octet, 2
            elif octet < 224:
                length, size = ((octet - 192) << 8) + data[offset + 2] + 192, 3
            elif octet == 255:
                length, size = int.from_bytes(data[offset + 2 : offset + 6], "big"), 6
            else:
                raise SignatureInvalid("partial body lengths are not supported")
        else:
            tag = (first >> 2) & 0x0F
            kind = first & 0x03
            if kind == 3:
                raise SignatureInvalid("indeterminate packet lengths are not supported")
            width = 1 << kind
            length, size = int.from_bytes(data[offset + 1 : offset + 1 + width], "big"), 1 + width
        body = data[offset + size : offset + size + length]
        if len(body) != length:
            raise SignatureInvalid("a packet is truncated")
        yield tag, data[offset : offset + size], body
        offset += size + length


def _mpi(data: bytes, offset: int) -> tuple[int, int]:
    """One multiprecision integer at `offset`, and the offset past it."""
    bits = int.from_bytes(data[offset : offset + 2], "big")
    width = (bits + 7) // 8
    value = data[offset + 2 : offset + 2 + width]
    if len(value) != width:
        raise SignatureInvalid("an integer is truncated")
    return int.from_bytes(value, "big"), offset + 2 + width


def _rsa_key(body: bytes) -> RsaKey | None:
    """The key a version 4 public key or subkey packet carries, or `None` when it is not RSA."""
    if len(body) < 6 or body[0] != 4 or body[5] not in RSA_ALGORITHMS:
        return None
    modulus, offset = _mpi(body, 6)
    exponent, offset = _mpi(body, offset)
    material = body[:offset]
    fingerprint = hashlib.sha1(b"\x99" + len(material).to_bytes(2, "big") + material, usedforsecurity=False)
    digest = fingerprint.hexdigest().upper()
    return RsaKey(
        key_id=digest[-16:],
        fingerprint=digest,
        public_key=rsa.RSAPublicNumbers(exponent, modulus).public_key(),
    )


def public_keys(armored: str) -> list[RsaKey]:
    """Every RSA key and subkey in an armored public key block.

    Raises:
        SignatureInvalid: The block holds no RSA key.
    """
    keys = [
        key
        for tag, _, body in _packets(dearmor(armored))
        if tag in (PUBLIC_KEY_TAG, PUBLIC_SUBKEY_TAG)
        for key in [_rsa_key(body)]
        if key is not None
    ]
    if not keys:
        raise SignatureInvalid("the key block holds no RSA key")
    return keys


def _subpackets(data: bytes) -> Iterator[tuple[int, bytes]]:
    """Each signature subpacket as `(type, value)`."""
    offset = 0
    while offset < len(data):
        octet = data[offset]
        if octet < 192:
            length, size = octet, 1
        elif octet < 255:
            length, size = ((octet - 192) << 8) + data[offset + 1] + 192, 2
        else:
            length, size = int.from_bytes(data[offset + 1 : offset + 5], "big"), 5
        if length < 1:
            raise SignatureInvalid("a subpacket is empty")
        value = data[offset + size : offset + size + length]
        yield value[0] & 0x7F, value[1:]
        offset += size + length


def _issuers(hashed: bytes, unhashed: bytes) -> set[str]:
    """The key ids and fingerprints a signature names as its issuer, upper case hex."""
    found: set[str] = set()
    for kind, value in [*_subpackets(hashed), *_subpackets(unhashed)]:
        if kind == ISSUER_SUBPACKET:
            found.add(value.hex().upper())
        elif kind == ISSUER_FINGERPRINT_SUBPACKET and value:
            found.add(value[1:].hex().upper())
    return found


def verify_detached(data: bytes, signature: bytes, armored_key: str) -> str:
    """Check a binary detached signature over `data`, returning the signing key id.

    Raises:
        SignatureInvalid: The signature is malformed, names no key in the block, or
            does not verify.
    """
    packets = [(tag, body) for tag, _, body in _packets(signature) if tag == SIGNATURE_TAG]
    if len(packets) != 1:
        raise SignatureInvalid("the signature file holds no single signature")
    body = packets[0][1]
    if len(body) < 6 or body[0] != 4:
        raise SignatureInvalid("only version 4 signatures are supported")
    signature_type, algorithm, hash_algorithm = body[1], body[2], body[3]
    if signature_type != BINARY_DOCUMENT or algorithm not in RSA_ALGORITHMS:
        raise SignatureInvalid("only RSA signatures of a binary document are supported")
    hash_type = HASHES.get(hash_algorithm)
    if hash_type is None:
        raise SignatureInvalid("the signature's hash algorithm is not supported")
    hashed_length = int.from_bytes(body[4:6], "big")
    hashed_end = 6 + hashed_length
    unhashed_length = int.from_bytes(body[hashed_end : hashed_end + 2], "big")
    unhashed_end = hashed_end + 2 + unhashed_length
    hashed, unhashed = body[6:hashed_end], body[hashed_end + 2 : unhashed_end]
    value, _ = _mpi(body, unhashed_end + 2)
    issuers = _issuers(hashed, unhashed)
    candidates = [key for key in public_keys(armored_key) if key.key_id in issuers or key.fingerprint in issuers]
    if not candidates:
        raise SignatureInvalid("the signature was made by a key the registry does not hold")
    trailer = body[:hashed_end]
    signed = data + trailer + b"\x04\xff" + len(trailer).to_bytes(4, "big")
    for key in candidates:
        width = (key.public_key.key_size + 7) // 8
        try:
            key.public_key.verify(value.to_bytes(width, "big"), signed, padding.PKCS1v15(), hash_type())
        except (InvalidSignature, OverflowError):
            continue
        return key.key_id
    raise SignatureInvalid("the signature does not match the data")


__all__ = ["RsaKey", "SignatureInvalid", "dearmor", "public_keys", "verify_detached"]
