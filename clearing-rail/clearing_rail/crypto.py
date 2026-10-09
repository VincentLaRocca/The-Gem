"""Signature interface, key registry, and canonical encodings.

The scheme is pluggable through the :class:`Verifier` protocol. An Ed25519
implementation is provided when the optional ``cryptography`` package is
installed; the core never imports it otherwise.
"""
from __future__ import annotations

import hashlib
import struct
from decimal import Decimal
from typing import Dict, Iterable, Protocol

from .types import Hop, ValidationError

CYCLE_TAG = b"clearing-rail/cycle/v1"
HOP_TAG = b"clearing-rail/hop-sig/v1"
BLOCK_TAG = b"clearing-rail/block/v1"


class Verifier(Protocol):
    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool: ...


class KeyRegistry:
    """entity id -> public key. Key issuance/rotation/revocation is an interface (see INTERFACES.md)."""

    def __init__(self):
        self._keys: Dict[str, bytes] = {}

    def register(self, entity_id: str, public_key: bytes) -> None:
        if entity_id in self._keys:
            raise ValidationError(f"key already registered for {entity_id}")
        self._keys[entity_id] = bytes(public_key)

    def get(self, entity_id: str):
        return self._keys.get(entity_id)


class Ed25519Verifier:
    def __init__(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # optional dep
        from cryptography.exceptions import InvalidSignature
        self._pk = Ed25519PublicKey
        self._invalid = InvalidSignature

    def verify(self, public_key: bytes, message: bytes, signature: bytes) -> bool:
        try:
            self._pk.from_public_bytes(public_key).verify(signature, message)
            return True
        except (self._invalid, ValueError, TypeError):
            return False


def canonical_amount(amount: Decimal) -> str:
    """'10', '10.0', '1E+1' all encode as '10'."""
    s = format(Decimal(amount).normalize(), "f")
    return "0" if s in ("-0", "0") else s


def _field(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _s(x: str) -> bytes:
    return _field(x.encode("utf-8"))


def cycle_hash(candidate_id: str, solver_id: str, hops: Iterable[Hop]) -> bytes:
    hops = tuple(hops)
    parts = [CYCLE_TAG, _s(candidate_id), _s(solver_id), struct.pack(">I", len(hops))]
    for h in hops:
        parts += [_s(h.debtor), _s(h.creditor), _s(canonical_amount(h.amount))]
    return hashlib.sha256(b"".join(parts)).digest()


def hop_message(chash: bytes, index: int, hop: Hop) -> bytes:
    """What hop ``index``'s debtor signs: the whole cycle hash plus its own leg."""
    return b"".join([HOP_TAG, _field(chash), struct.pack(">I", index),
                     _s(hop.debtor), _s(hop.creditor), _s(canonical_amount(hop.amount))])


def block_message(chash: bytes) -> bytes:
    return BLOCK_TAG + _field(chash)
