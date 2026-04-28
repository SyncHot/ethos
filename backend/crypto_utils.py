"""
Symmetric encryption helpers for secrets stored on disk (SSH passwords, DDNS tokens, etc.).

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from the machine's
unique ID (/etc/machine-id) plus a per-installation salt stored in data/.keyfile.
The encryption is NOT meant to survive a disk move to another machine — that's
intentional: if someone copies data/ to a different machine, secrets cannot be
decrypted.

Folder passwords are one-way hashes and use PBKDF2 instead.
"""

import base64
import hashlib
import hmac
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken

# ── key derivation ──────────────────────────────────────────────

_DATA_DIR = os.environ.get('ETHOS_DATA', os.path.join(os.path.dirname(__file__), '..', 'data'))
_KEYFILE = os.path.join(_DATA_DIR, '.keyfile')
_fernet: Fernet | None = None


def _get_machine_seed() -> bytes:
    """Read /etc/machine-id (Linux) as the machine-specific seed."""
    try:
        with open('/etc/machine-id', 'r') as f:
            return f.read().strip().encode()
    except FileNotFoundError:
        # Fallback: use hostname + boot-id
        import socket
        return socket.gethostname().encode()


def _get_or_create_salt() -> bytes:
    """Return 32-byte salt from .keyfile, creating it on first run."""
    os.makedirs(os.path.dirname(_KEYFILE), exist_ok=True)
    if os.path.isfile(_KEYFILE):
        with open(_KEYFILE, 'rb') as f:
            salt = f.read()
            if len(salt) >= 32:
                return salt[:32]
    salt = secrets.token_bytes(32)
    fd = os.open(_KEYFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, salt)
    finally:
        os.close(fd)
    return salt


def _derive_fernet_key() -> bytes:
    """Derive a 32-byte Fernet key from machine-id + installation salt."""
    seed = _get_machine_seed()
    salt = _get_or_create_salt()
    dk = hashlib.pbkdf2_hmac('sha256', seed, salt, iterations=200_000, dklen=32)
    return base64.urlsafe_b64encode(dk)


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_derive_fernet_key())
    return _fernet


# ── public API: symmetric encrypt / decrypt ─────────────────────

def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret string → returns 'enc:...' prefixed ciphertext."""
    if not plaintext:
        return plaintext
    ct = _get_fernet().encrypt(plaintext.encode())
    return 'enc:' + ct.decode()


def decrypt_secret(value: str) -> str:
    """Decrypt an 'enc:...' value back to plaintext.  If value is NOT
    prefixed with 'enc:', assume legacy plaintext and return as-is
    (allows transparent migration)."""
    if not value or not value.startswith('enc:'):
        return value  # legacy plain text — will be encrypted on next save
    try:
        pt = _get_fernet().decrypt(value[4:].encode())
        return pt.decode()
    except (InvalidToken, Exception):
        return ''  # corrupted → return empty (force re-entry)


# ── public API: salted password hashing (folder passwords) ──────

_PBKDF2_ITERATIONS = 260_000


def hash_folder_password(password: str) -> str:
    """Hash a password with a random salt → 'pbkdf2:<salt_hex>:<hash_hex>'."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, _PBKDF2_ITERATIONS, dklen=32)
    return f"pbkdf2:{salt.hex()}:{dk.hex()}"


def verify_folder_password(password: str, stored: str) -> bool:
    """Verify password against stored hash.  Supports both new 'pbkdf2:...'
    format and legacy unsalted SHA-256 hex strings."""
    if stored.startswith('pbkdf2:'):
        parts = stored.split(':')
        if len(parts) != 3:
            return False
        salt = bytes.fromhex(parts[1])
        expected = parts[2]
        dk = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, _PBKDF2_ITERATIONS, dklen=32)
        return hmac.compare_digest(dk.hex(), expected)
    else:
        # Legacy unsalted SHA-256
        return hmac.compare_digest(hashlib.sha256(password.encode()).hexdigest(), stored)
