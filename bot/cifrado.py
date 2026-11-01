"""Cifrado AES-GCM + PBKDF2, compatible con la web (mismo formato)."""
from __future__ import annotations

import base64
import hashlib
import json
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ITER = 210_000

def _key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITER, 32)


def encrypt(obj, password: str) -> str:
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode(), None)
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "salt": b(salt), "iv": b(iv), "ct": b(ct)})


def decrypt(text: str, password: str):
    d = json.loads(text)
    u = base64.b64decode
    return json.loads(AESGCM(_key(password, u(d["salt"]))).decrypt(u(d["iv"]), u(d["ct"]), None))


def encrypt_bytes(raw: bytes, password: str) -> bytes:
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    return b"MV1" + salt + iv + AESGCM(_key(password, salt)).encrypt(iv, raw, None)


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    assert blob[:3] == b"MV1"
    salt, iv = blob[3:19], blob[19:31]
    return AESGCM(_key(password, salt)).decrypt(iv, blob[31:], None)
