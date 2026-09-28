"""Request signing for the venues' authenticated APIs (used here for their WebSocket
market-data feeds, which both venues gate behind an API key).

- Kalshi: sign ``timestamp_ms + METHOD + path`` with the account's private key
  (Ed25519, or RSA-PSS/SHA-256) and send KALSHI-ACCESS-* headers.
- Polymarket US: sign the same string with the Ed25519 key whose base64 secret the
  developer portal shows once, and send X-PM-* headers.
- Novig: sign a six-line NOVIG-V3 string (method, path, canonical query and the
  body's SHA-256) with an Ed25519 or P-256 key and send Novig-* headers
  (https://docs.novig.com/api/signing).
"""

import base64
import hashlib
import time
from pathlib import Path
from urllib.parse import quote, unquote_to_bytes

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa


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


def novig_query(raw: str) -> str:
    """The canonical query: each name and value %-decoded (a bare "+" stays "+") and
    re-encoded keeping only ``A-Z a-z 0-9 - . _ ~``, sorted by name then value."""
    pairs = []
    for part in raw.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append(tuple(quote(unquote_to_bytes(x), safe="-._~") for x in (name, value)))
    return "&".join(f"{n}={v}" for n, v in sorted(pairs))


def novig_string(ts: str, method: str, path: str, query: str, body: bytes) -> str:
    return "\n".join(("NOVIG-V3", ts, method.upper(), path, novig_query(query), hashlib.sha256(body).hexdigest()))


class NovigSigner:
    """A Novig API key: its id (a UUID, sent as Novig-Key-Id) and private key."""

    def __init__(self, key_id: str, private_key_pem: bytes):
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(self.key, (ed25519.Ed25519PrivateKey, ec.EllipticCurvePrivateKey)):
            raise ValueError("Novig private key must be Ed25519 or P-256")

    @classmethod
    def from_file(cls, key_id: str, path: str) -> "NovigSigner":
        return cls(key_id, Path(path).expanduser().read_bytes())

    def sign(self, text: str) -> str:
        msg = text.encode()
        if isinstance(self.key, ed25519.Ed25519PrivateKey):
            sig = self.key.sign(msg)
        else:
            sig = self.key.sign(msg, ec.ECDSA(hashes.SHA256()))  # DER, as Novig expects
        return base64.b64encode(sig).decode()

    def headers(self, method: str, target: str, body: bytes = b"") -> dict[str, str]:
        """``target`` is the path with its query, exactly as sent; ``body`` the exact bytes."""
        ts = _now_ms()
        path, _, query = target.partition("?")
        return {"Novig-Key-Id": self.key_id, "Novig-Timestamp": ts,
                "Novig-Signature": self.sign(novig_string(ts, method, path, query, body))}
