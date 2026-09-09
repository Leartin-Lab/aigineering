"""Cryptographic signer and verifier interfaces (ADR-007)."""

from __future__ import annotations

import base64
import hashlib
from abc import ABC, abstractmethod


class Signer(ABC):
    @abstractmethod
    def sign(self, data: bytes) -> str: ...

    @property
    @abstractmethod
    def signer_id(self) -> str: ...

    @property
    @abstractmethod
    def kind(self) -> str: ...


class Verifier(ABC):
    @abstractmethod
    def verify(self, data: bytes, signature: str, signer_id: str) -> bool: ...


class DeterministicSigner(Signer):
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
    def verify(self, data: bytes, signature: str, signer_id: str) -> bool:
        expected = hashlib.sha256(data).hexdigest()
        return signature == f"asig_{expected}"


try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


class Ed25519Signer(Signer):
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

    @classmethod
    def from_private_key_hex(cls, private_key_hex: str) -> Ed25519Signer:
        if not _CRYPTO_AVAILABLE:
            raise ImportError("Ed25519Signer requires the 'cryptography' package.")
        try:
            private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
                bytes.fromhex(private_key_hex)
            )
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid Ed25519 private key encoding") from exc
        return cls(private_key)

    @property
    def private_key_hex(self) -> str:
        raw = self._private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        return raw.hex()

    @property
    def signer_id(self) -> str:
        return self._public_key_hex

    def sign(self, data: bytes) -> str:
        sig = self._private_key.sign(data)
        encoded = base64.b64encode(sig).decode("ascii")
        return f"ed25519:{encoded}"


class Ed25519Verifier(Verifier):
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
    if kind == "deterministic":
        return DeterministicSigner(**kwargs)
    if kind == "ed25519":
        return Ed25519Signer(**kwargs)
    raise ValueError(f"Unknown signer kind: {kind!r}")


def create_verifier(kind: str) -> Verifier:
    if kind in {"deterministic", DeterministicSigner.kind}:
        return DeterministicVerifier()
    if kind == "ed25519":
        return Ed25519Verifier()
    raise ValueError(f"Unknown verifier kind: {kind!r}")


def generate_keypair() -> tuple[Ed25519Signer, str]:
    signer = Ed25519Signer()
    return signer, signer.signer_id
