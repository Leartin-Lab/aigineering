"""Cryptographic signer and verifier interfaces (ADR-007).

Provides abstract :class:`Signer` / :class:`Verifier` protocols and
concrete implementations:
- :class:`DeterministicSigner` — SHA-256 content-hash seal (existing behaviour)
- :class:`Ed25519Signer` — Ed25519 public-key signatures (new in 0.5)
"""

from __future__ import annotations

import base64
import hashlib
from abc import ABC, abstractmethod


# ---------------------------------------------------------------------------
# Abstract interfaces
# ---------------------------------------------------------------------------


class Signer(ABC):
    """Abstract signer: produces a signature string for arbitrary data."""

    @abstractmethod
    def sign(self, data: bytes) -> str: ...

    @property
    @abstractmethod
    def signer_id(self) -> str:
        """Public identifier for this signer (e.g. public key or name)."""
        ...

    @property
    @abstractmethod
    def kind(self) -> str:
        """Signature kind prefix (e.g. ``asig_`` or ``ed25519:``)."""
        ...


class Verifier(ABC):
    """Abstract verifier: checks a signature against data and signer identity."""

    @abstractmethod
    def verify(self, data: bytes, signature: str, signer_id: str) -> bool: ...


# ---------------------------------------------------------------------------
# Deterministic signer (existing behaviour — not cryptographic)
# ---------------------------------------------------------------------------


class DeterministicSigner(Signer):
    """SHA-256 content-hash seal — NOT a public-key signature.

    This is the current production signer.  It provides auditability
    (deterministic replay) but zero non-repudiation.  ADR-007 reserves
    the ``Asset.signed_by`` / ``Asset.provenance_seal`` fields for
    future cryptographic signers.
    """

    kind = "asig_"

    def __init__(self, signer_id: str = "deterministic") -> None:
        self._signer_id = signer_id

    @property
    def signer_id(self) -> str:
        return self._signer_id

    def sign(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        return f"asig_{digest}"


class DeterministicVerifier(Verifier):
    """Verifier for :class:`DeterministicSigner`."""

    def verify(self, data: bytes, signature: str, signer_id: str) -> bool:
        expected = hashlib.sha256(data).hexdigest()
        return signature == f"asig_{expected}"


# ---------------------------------------------------------------------------
# Ed25519 signer (new cryptographic implementation)
# ---------------------------------------------------------------------------

try:
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


class Ed25519Signer(Signer):
    """Ed25519 public-key signer.

    Uses the ``cryptography`` library.  The ``signer_id`` is the
    hex-encoded public key.
    """

    kind = "ed25519"

    def __init__(self, private_key: ed25519.Ed25519PrivateKey | None = None) -> None:
        if not _CRYPTO_AVAILABLE:
            raise ImportError(
                "Ed25519Signer requires the 'cryptography' package. "
                "Install with: pip install cryptography"
            )
        self._private_key = private_key or ed25519.Ed25519PrivateKey.generate()
        pub_bytes = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self._public_key_hex = pub_bytes.hex()

    @property
    def signer_id(self) -> str:
        return self._public_key_hex

    def sign(self, data: bytes) -> str:
        sig = self._private_key.sign(data)
        encoded = base64.b64encode(sig).decode("ascii")
        return f"ed25519:{encoded}"


class Ed25519Verifier(Verifier):
    """Verifier for Ed25519 signatures."""

    def verify(self, data: bytes, signature: str, signer_id: str) -> bool:
        if not _CRYPTO_AVAILABLE:
            raise ImportError("Ed25519Verifier requires the 'cryptography' package.")
        if not signature.startswith("ed25519:"):
            return False
        try:
            pub_key = ed25519.Ed25519PublicKey.from_public_bytes(
                bytes.fromhex(signer_id)
            )
            raw_sig = base64.b64decode(signature[len("ed25519:") :])
            pub_key.verify(raw_sig, data)
            return True
        except Exception:
            return False


def create_signer(kind: str = "deterministic", **kwargs) -> Signer:
    """Factory: create a signer by kind.

    Args:
        kind: ``"deterministic"`` or ``"ed25519"``.
        **kwargs: Passed to the signer constructor.

    Returns:
        A :class:`Signer` instance.
    """
    if kind == "deterministic":
        return DeterministicSigner(**kwargs)
    if kind == "ed25519":
        return Ed25519Signer(**kwargs)
    raise ValueError(f"Unknown signer kind: {kind!r}")


def create_verifier(kind: str) -> Verifier:
    """Factory: create a verifier by kind."""
    if kind == "deterministic":
        return DeterministicVerifier()
    if kind == "ed25519":
        return Ed25519Verifier()
    raise ValueError(f"Unknown verifier kind: {kind!r}")


def generate_keypair() -> tuple[Ed25519Signer, str]:
    """Generate a new Ed25519 keypair.

    Returns:
        (signer, public_key_hex)
    """
    signer = Ed25519Signer()
    return signer, signer.signer_id
