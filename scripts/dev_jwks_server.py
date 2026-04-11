"""Local JWKS server for development.

Generates a persistent RSA keypair on first run (stored in .dev-keys/).
Serves the public key as a JWKS document on http://localhost:8080/.well-known/jwks.json
so the gateway can verify tokens minted by dev_token.py.

Usage:
    python scripts/dev_jwks_server.py

Leave it running in a separate terminal while using the gateway.
"""

import base64
import http.server
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# ── Key persistence ──────────────────────────────────────────────────────────

KEYS_DIR = os.path.join(os.path.dirname(__file__), "..", ".dev-keys")
PRIVATE_KEY_PATH = os.path.join(KEYS_DIR, "private.pem")
PUBLIC_KEY_PATH = os.path.join(KEYS_DIR, "public.pem")
KID = "dev-key-1"


def _ensure_keys():
    os.makedirs(KEYS_DIR, exist_ok=True)
    if not os.path.exists(PRIVATE_KEY_PATH):
        print("Generating new RSA keypair...")
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with open(PRIVATE_KEY_PATH, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        with open(PUBLIC_KEY_PATH, "wb") as f:
            f.write(private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ))
        print(f"Keys written to {KEYS_DIR}/")
    else:
        print(f"Using existing keys from {KEYS_DIR}/")


def _b64url(n: int) -> str:
    """Encode a big integer as base64url (no padding), as required by JWK."""
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


def _build_jwks() -> dict:
    with open(PUBLIC_KEY_PATH, "rb") as f:
        public_key = serialization.load_pem_public_key(f.read())
    pub_numbers = public_key.public_numbers()  # type: ignore[attr-defined]
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KID,
                "n": _b64url(pub_numbers.n),
                "e": _b64url(pub_numbers.e),
            }
        ]
    }


# ── HTTP server ───────────────────────────────────────────────────────────────

JWKS_PATH = "/.well-known/jwks.json"


class JWKSHandler(http.server.BaseHTTPRequestHandler):
    jwks_payload: bytes = b""

    def do_GET(self):
        if self.path == JWKS_PATH:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.jwks_payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        # Suppress the default per-request stdout noise
        pass


if __name__ == "__main__":
    _ensure_keys()
    jwks = _build_jwks()
    JWKSHandler.jwks_payload = json.dumps(jwks).encode()

    host, port = "localhost", 8080
    server = http.server.HTTPServer((host, port), JWKSHandler)
    print(f"JWKS server listening on http://{host}:{port}{JWKS_PATH}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
