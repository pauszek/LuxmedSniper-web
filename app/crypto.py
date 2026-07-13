"""Secret handling.

The Luxmed password is stored in SQLite only as a Fernet ciphertext. The
Fernet key is derived from a user-supplied master key via scrypt and lives
exclusively in RAM — after a container restart the app is "locked" until
the master key is provided again (GUI /unlock or an optional key file
bind-mounted from the Proxmox host).
"""
import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app import db

_KEY_CHECK_PLAINTEXT = b"luxmed-sniper-key-check"

# scrypt parameters (n=2^15 keeps unlock < 1 s on a small LXC);
# maxmem must exceed 128*n*r = 32 MiB, OpenSSL's default cap is exactly that
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def _derive_fernet_key(master_key: str, salt: bytes) -> bytes:
    raw = hashlib.scrypt(
        master_key.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=32,
    )
    return base64.urlsafe_b64encode(raw)


class KeyStoreError(Exception):
    pass


class KeyStore:
    """Holds the derived Fernet key in memory; never persists it."""

    def __init__(self):
        self._fernet: Fernet | None = None

    @property
    def is_initialized(self) -> bool:
        return db.get_setting("kdf_salt") is not None

    @property
    def is_unlocked(self) -> bool:
        return self._fernet is not None

    def initialize(self, master_key: str) -> None:
        """First-run setup: create salt + verification token, then unlock."""
        if self.is_initialized:
            raise KeyStoreError("Master key is already initialized")
        salt = secrets.token_bytes(16)
        fernet = Fernet(_derive_fernet_key(master_key, salt))
        db.set_setting("kdf_salt", base64.b64encode(salt).decode())
        db.set_setting("key_check", fernet.encrypt(_KEY_CHECK_PLAINTEXT).decode())
        self._fernet = fernet

    def unlock(self, master_key: str) -> None:
        salt_b64 = db.get_setting("kdf_salt")
        key_check = db.get_setting("key_check")
        if salt_b64 is None or key_check is None:
            raise KeyStoreError("Master key has not been initialized yet")
        fernet = Fernet(_derive_fernet_key(master_key, base64.b64decode(salt_b64)))
        try:
            fernet.decrypt(key_check.encode())
        except InvalidToken:
            raise KeyStoreError("Wrong master key") from None
        self._fernet = fernet

    def lock(self) -> None:
        self._fernet = None

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise KeyStoreError("Locked — unlock with the master key first")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode()

    def decrypt(self, ciphertext: str) -> str:
        if self._fernet is None:
            raise KeyStoreError("Locked — unlock with the master key first")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode("utf-8")
        except InvalidToken as e:
            raise KeyStoreError("Ciphertext does not match the current key") from e


# --- GUI password hashing (scrypt, salt$hash) ---

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM, dklen=32,
    )
    return f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_b64, digest_b64 = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=base64.b64decode(salt_b64),
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, maxmem=_SCRYPT_MAXMEM, dklen=32,
    )
    return secrets.compare_digest(digest, base64.b64decode(digest_b64))
