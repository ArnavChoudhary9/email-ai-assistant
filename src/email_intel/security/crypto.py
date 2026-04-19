"""Symmetric encryption for at-rest secrets (account passwords).

Fernet = AES128-CBC + HMAC-SHA256, authenticated. Keys are 32 random bytes,
urlsafe-base64 encoded. Generate one with `generate_key()` or
`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
and put it in .env as APP_ENCRYPTION_KEY.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class InvalidEncryptionKey(ValueError):
    """Raised when APP_ENCRYPTION_KEY is missing, malformed, or doesn't match stored data."""


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


class FernetCipher:
    def __init__(self, key: str) -> None:
        if not key:
            raise InvalidEncryptionKey(
                "APP_ENCRYPTION_KEY is empty. Generate one with "
                "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
                "and set it in .env."
            )
        try:
            self._fernet = Fernet(key.encode("ascii"))
        except (ValueError, TypeError) as e:
            raise InvalidEncryptionKey(f"APP_ENCRYPTION_KEY is not a valid Fernet key: {e}") from e

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            raise InvalidEncryptionKey(
                "Failed to decrypt — APP_ENCRYPTION_KEY may have changed since this row was written."
            ) from e
