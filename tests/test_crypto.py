from __future__ import annotations

import pytest

from email_intel.security import FernetCipher, InvalidEncryptionKey, generate_key


def test_roundtrip():
    key = generate_key()
    cipher = FernetCipher(key)
    ct = cipher.encrypt("hunter2")
    assert ct != "hunter2"
    assert cipher.decrypt(ct) == "hunter2"


def test_wrong_key_fails():
    k1, k2 = generate_key(), generate_key()
    ct = FernetCipher(k1).encrypt("secret")
    with pytest.raises(InvalidEncryptionKey):
        FernetCipher(k2).decrypt(ct)


def test_empty_key_rejected():
    with pytest.raises(InvalidEncryptionKey):
        FernetCipher("")


def test_malformed_key_rejected():
    with pytest.raises(InvalidEncryptionKey):
        FernetCipher("not-a-valid-fernet-key")
