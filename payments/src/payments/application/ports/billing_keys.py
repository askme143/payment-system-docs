from __future__ import annotations

from typing import Protocol


class BillingKeyCipher(Protocol):
    def encrypt(self, plaintext: str) -> str:
        raise NotImplementedError

    def decrypt(self, ciphertext: str) -> str:
        raise NotImplementedError
