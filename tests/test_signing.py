"""Tests for cryptographic signer/verifier interfaces."""

import pytest

from aigineering.core.signing import (
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


class TestDeterministicSigner:
    def test_sign_is_deterministic(self):
        signer = DeterministicSigner()
        sig1 = signer.sign(b"hello")
        sig2 = signer.sign(b"hello")
        assert sig1 == sig2
        assert sig1.startswith("asig_")

    def test_different_data_produces_different_signature(self):
        signer = DeterministicSigner()
        assert signer.sign(b"a") != signer.sign(b"b")

    def test_verify_round_trip(self):
        signer = DeterministicSigner()
        verifier = DeterministicVerifier()
        data = b"test data"
        sig = signer.sign(data)
        assert verifier.verify(data, sig, signer.signer_id) is True

    def test_verify_rejects_tampered(self):
        signer = DeterministicSigner()
        verifier = DeterministicVerifier()
        sig = signer.sign(b"original")
        assert verifier.verify(b"tampered", sig, signer.signer_id) is False


class TestEd25519Signer:
    def test_sign_and_verify_round_trip(self):
        signer = Ed25519Signer()
        verifier = Ed25519Verifier()
        data = b"test message"
        sig = signer.sign(data)
        assert sig.startswith("ed25519:")
        assert verifier.verify(data, sig, signer.signer_id) is True

    def test_verify_rejects_wrong_key(self):
        signer1 = Ed25519Signer()
        signer2 = Ed25519Signer()
        verifier = Ed25519Verifier()
        sig = signer1.sign(b"data")
        assert verifier.verify(b"data", sig, signer2.signer_id) is False

    def test_verify_rejects_tampered_data(self):
        signer = Ed25519Signer()
        verifier = Ed25519Verifier()
        sig = signer.sign(b"original")
        assert verifier.verify(b"tampered", sig, signer.signer_id) is False

    def test_verify_rejects_wrong_format(self):
        verifier = Ed25519Verifier()
        assert verifier.verify(b"data", "not_ed25519:", "00" * 32) is False

    def test_signer_id_is_hex_pubkey(self):
        signer = Ed25519Signer()
        assert len(signer.signer_id) == 64  # 32 bytes hex
        assert all(c in "0123456789abcdef" for c in signer.signer_id)

    def test_generate_keypair(self):
        signer, pubkey_hex = generate_keypair()
        assert isinstance(signer, Ed25519Signer)
        assert signer.signer_id == pubkey_hex
        assert len(pubkey_hex) == 64

    def test_private_key_round_trip(self):
        original = Ed25519Signer()
        restored = Ed25519Signer.from_private_key_hex(original.private_key_hex)

        assert restored.signer_id == original.signer_id
        signature = restored.sign(b"after restart")
        assert Ed25519Verifier().verify(b"after restart", signature, original.signer_id)


class TestFactory:
    def test_create_deterministic(self):
        s = create_signer("deterministic")
        assert isinstance(s, DeterministicSigner)
        v = create_verifier("deterministic")
        assert isinstance(v, DeterministicVerifier)

    def test_create_ed25519(self):
        s = create_signer("ed25519")
        assert isinstance(s, Ed25519Signer)
        v = create_verifier("ed25519")
        assert isinstance(v, Ed25519Verifier)

    def test_create_unknown_raises(self):
        with pytest.raises(ValueError):
            create_signer("banana")
        with pytest.raises(ValueError):
            create_verifier("banana")


class TestProtocolCompliance:
    def test_deterministic_satisfies_signer_protocol(self):
        assert isinstance(DeterministicSigner(), Signer)

    def test_ed25519_satisfies_signer_protocol(self):
        assert isinstance(Ed25519Signer(), Signer)

    def test_abstract_signer_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Signer()

    def test_abstract_verifier_cannot_instantiate(self):
        with pytest.raises(TypeError):
            Verifier()
