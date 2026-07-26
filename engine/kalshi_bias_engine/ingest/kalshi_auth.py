"""Kalshi request signing.

Kalshi's trade API authenticates via RSA-PSS-SHA256 signatures over
``{timestamp_ms}{method}{path}`` with three headers:
  KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE, KALSHI-ACCESS-TIMESTAMP.

Verify against current docs before Phase 2: signing envelope, header names,
and PSS parameters are the parts most likely to have shifted.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


@dataclass
class KalshiSigner:
    key_id: str
    private_key_pem: str

    def __post_init__(self) -> None:
        if self.private_key_pem:
            self._key = serialization.load_pem_private_key(
                self.private_key_pem.encode("utf-8"), password=None
            )
            if not isinstance(self._key, rsa.RSAPrivateKey):
                raise TypeError("Kalshi private key must be RSA")
        else:
            self._key = None

    @property
    def enabled(self) -> bool:
        return self._key is not None and bool(self.key_id)

    def headers(self, method: str, path: str) -> dict[str, str]:
        if not self.enabled:
            return {}
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + path).encode("utf-8")
        sig = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }
