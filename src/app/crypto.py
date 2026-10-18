"""Token-at-rest encryption (Fernet).

The OAuth access/refresh tokens in email_connections are long-lived credentials
for a customer's mailbox, so they're encrypted before they reach the database and
decrypted only inside db.py's connection getters. Decryption is *tolerant*:
pre-existing plaintext rows (no 'v1:' prefix) are returned unchanged and re-encrypt
themselves on the next token write — so there is no migration step.

Generate a key once:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and set it as TOKEN_ENCRYPTION_KEY (Render + local .env).
"""
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_PREFIX = "v1:"          # version tag — lets a future MultiFernet rotation coexist
_fernet: Fernet | None = None


def _f() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(settings.token_encryption_key.encode())
    return _fernet


def encrypt(plaintext: str | None) -> str | None:
    """None -> None (refresh_token is nullable). Returns 'v1:<fernet-token>'."""
    if plaintext is None:
        return None
    return _PREFIX + _f().encrypt(plaintext.encode()).decode()


def decrypt(stored: str | None) -> str | None:
    """Decrypt a stored token.

    Legacy plaintext (no `v1:` prefix) is returned unchanged, so the one-time
    cutover from plaintext to encrypted needs no migration. But a value that *is*
    `v1:`-prefixed and fails to decrypt is a real fault — almost always a
    TOKEN_ENCRYPTION_KEY that doesn't match the key the token was encrypted with —
    so fail loudly instead of silently handing back ciphertext (which downstream
    treats as a token and only fails much later with an opaque provider error)."""
    if stored is None:
        return None
    if not stored.startswith(_PREFIX):
        return stored
    try:
        return _f().decrypt(stored[len(_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Failed to decrypt a v1:-prefixed token — TOKEN_ENCRYPTION_KEY almost "
            "certainly does not match the key used to encrypt it. Check this "
            "environment's key."
        ) from e
