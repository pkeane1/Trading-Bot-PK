"""RSA-PSS request signing for Kalshi API authentication."""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiAuth:
    """Signs requests using RSA-PSS per Kalshi's API specification.

    Produces three headers on each request:
    - KALSHI-ACCESS-KEY: The API key ID
    - KALSHI-ACCESS-TIMESTAMP: Unix timestamp in milliseconds
    - KALSHI-ACCESS-SIGNATURE: RSA-PSS signature of "{timestamp}{METHOD}{path}"
    """

    def __init__(self, api_key: str, private_key_path: str) -> None:
        self.api_key = api_key
        self._private_key = self._load_private_key(private_key_path)

    @staticmethod
    def _load_private_key(path: str) -> rsa.RSAPrivateKey:
        pem_data = Path(path).read_bytes()
        key = serialization.load_pem_private_key(pem_data, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError(f"Expected RSA private key, got {type(key).__name__}")
        return key

    def sign_request(self, method: str, path: str) -> dict[str, str]:
        """Generate authentication headers for a request.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: Request path without query string (e.g. /trade-api/v2/markets)

        Returns:
            Dict with the three auth headers.
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}"
        signature = self._private_key.sign(
            message.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }
