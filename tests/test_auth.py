"""Tests for RSA-PSS request signing."""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.api.auth import KalshiAuth


@pytest.fixture
def rsa_key_pair():
    """Generate a temporary RSA key pair for testing."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key


@pytest.fixture
def key_file(rsa_key_pair, tmp_path):
    """Write the private key to a temp PEM file."""
    pem = rsa_key_pair.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "test_key.pem"
    key_path.write_bytes(pem)
    return str(key_path)


class TestKalshiAuth:
    def test_sign_request_returns_three_headers(self, key_file):
        auth = KalshiAuth(api_key="test-key-123", private_key_path=key_file)
        headers = auth.sign_request("GET", "/trade-api/v2/markets")

        assert "KALSHI-ACCESS-KEY" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "KALSHI-ACCESS-SIGNATURE" in headers

    def test_sign_request_key_matches(self, key_file):
        auth = KalshiAuth(api_key="my-api-key", private_key_path=key_file)
        headers = auth.sign_request("GET", "/trade-api/v2/markets")

        assert headers["KALSHI-ACCESS-KEY"] == "my-api-key"

    def test_sign_request_timestamp_is_numeric(self, key_file):
        auth = KalshiAuth(api_key="test-key", private_key_path=key_file)
        headers = auth.sign_request("GET", "/trade-api/v2/portfolio/balance")

        assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
        assert len(headers["KALSHI-ACCESS-TIMESTAMP"]) == 13  # milliseconds

    def test_signature_is_valid_base64(self, key_file):
        auth = KalshiAuth(api_key="test-key", private_key_path=key_file)
        headers = auth.sign_request("POST", "/trade-api/v2/portfolio/orders")

        # Should not raise
        decoded = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
        assert len(decoded) > 0

    def test_signature_verifies(self, rsa_key_pair, key_file):
        auth = KalshiAuth(api_key="test-key", private_key_path=key_file)
        headers = auth.sign_request("GET", "/trade-api/v2/markets")

        # Verify the signature using the public key
        timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]
        message = f"{timestamp}GET/trade-api/v2/markets"
        signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

        public_key = rsa_key_pair.public_key()
        # Should not raise
        public_key.verify(
            signature,
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )

    def test_different_methods_produce_different_signatures(self, key_file):
        auth = KalshiAuth(api_key="test-key", private_key_path=key_file)
        h1 = auth.sign_request("GET", "/trade-api/v2/markets")
        h2 = auth.sign_request("POST", "/trade-api/v2/markets")

        # Signatures should differ (different message content)
        assert h1["KALSHI-ACCESS-SIGNATURE"] != h2["KALSHI-ACCESS-SIGNATURE"]
