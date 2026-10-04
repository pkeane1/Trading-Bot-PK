"""Generate Kalshi auth headers for manual API testing.

Usage:
    python generate_headers.py GET /trade-api/v2/portfolio/balance
"""

import base64
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import os

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()

api_key = os.environ["KALSHI_API_KEY"]
key_path = os.environ["KALSHI_PRIVATE_KEY_PATH"]

# Load private key
private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)

# Get method and path from args
method = sys.argv[1].upper() if len(sys.argv) > 1 else "GET"
path = sys.argv[2] if len(sys.argv) > 2 else "/trade-api/v2/portfolio/balance"

# Generate timestamp (milliseconds)
timestamp_ms = str(int(time.time() * 1000))

# Sign: "{timestamp}{METHOD}{path}"
message = f"{timestamp_ms}{method}{path}"
signature = private_key.sign(
    message.encode(),
    padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.MAX_LENGTH,
    ),
    hashes.SHA256(),
)
signature_b64 = base64.b64encode(signature).decode()

print()
print("=== Kalshi Auth Headers ===")
print(f"KALSHI-ACCESS-KEY:       {api_key}")
print(f"KALSHI-ACCESS-TIMESTAMP: {timestamp_ms}")
print(f"KALSHI-ACCESS-SIGNATURE: {signature_b64}")
print()
print("=== curl command (demo) ===")
print(f'''curl --request {method} \\
  --url https://demo-api.kalshi.co{path} \\
  --header 'KALSHI-ACCESS-KEY: {api_key}' \\
  --header 'KALSHI-ACCESS-TIMESTAMP: {timestamp_ms}' \\
  --header 'KALSHI-ACCESS-SIGNATURE: {signature_b64}' ''')
print()
print("NOTE: These headers expire quickly — run this script again if the request fails with 401.")
