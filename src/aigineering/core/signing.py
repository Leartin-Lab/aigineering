"""Compatibility exports for protocol-owned signing primitives."""

from aigineering.protocol.signing import (
    DeterministicSigner,
    DeterministicVerifier,
    Ed25519Signer,
    Ed25519Verifier,
    Signer,
    Verifier,
    create_signer,
    create_verifier,
    generate_keypair,
)

__all__ = (
    "DeterministicSigner",
    "DeterministicVerifier",
    "Ed25519Signer",
    "Ed25519Verifier",
    "Signer",
    "Verifier",
    "create_signer",
    "create_verifier",
    "generate_keypair",
)
