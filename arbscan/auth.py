"""Request signing for the venues' authenticated APIs (used here for their WebSocket
market-data feeds, which both venues gate behind an API key).

- Kalshi: sign ``timestamp_ms + METHOD + path`` with the account's private key
  (Ed25519, or RSA-PSS/SHA-256) and send KALSHI-ACCESS-* headers.
- Polymarket US: sign the same string with the Ed25519 key whose base64 secret the
  developer portal shows once, and send X-PM-* headers.
"""

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


def _now_ms() -> str:
    return str(int(time.time() * 1000))


class KalshiSigner:
    def __init__(self, key_id: str, private_key_pem: bytes):
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(self.key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)):
            raise ValueError("Kalshi private key must be Ed25519 or RSA")

    @classmethod
    def from_file(cls, key_id: str, path: str) -> "KalshiSigner":
        return cls(key_id, Path(path).expanduser().read_bytes())

    def sign(self, text: str) -> str:
        msg = text.encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                 salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = _now_ms()
        return {"KALSHI-ACCESS-KEY": self.key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": self.sign(ts + method + path.split("?")[0])}


class PMSigner:
    def __init__(self, key_id: str, secret_b64: str):
        self.key_id = key_id
        self.key = ed25519.Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret_b64)[:32])

    def sign(self, text: str) -> str:
        return base64.b64encode(self.key.sign(text.encode())).decode()

    def headers(self, method: str, path: str) -> dict[str, str]:
        ts = _now_ms()
        return {"X-PM-Access-Key": self.key_id, "X-PM-Timestamp": ts,
                "X-PM-Signature": self.sign(ts + method + path.split("?")[0])}
