from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from cryptography.fernet import Fernet


@dataclass(frozen=True, slots=True)
class FernetBillingKeyCipher:
    secret: str

    def encrypt(self, plaintext: str) -> str:
        token = self._fernet().encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        plaintext = self._fernet().decrypt(ciphertext.encode("ascii"))
        return plaintext.decode("utf-8")

    def _fernet(self) -> Fernet:
        key = base64.urlsafe_b64encode(
            hashlib.sha256(self.secret.encode("utf-8")).digest()
        )
        return Fernet(key)
