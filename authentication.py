"""Authentication input validation and cryptographic primitives."""

import hashlib
import re
import secrets

from fastapi import HTTPException


SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 3
SCRYPT_SALT_BYTES = 16
SCRYPT_DERIVED_KEY_BYTES = 64
SCRYPT_HASH_MAX_MEMORY = 64 * 1024 * 1024
SCRYPT_VERIFY_MAX_MEMORY = 256 * 1024 * 1024
SCRYPT_VERIFY_MAX_WORK = 2**21
MIN_NEW_PASSWORD_LENGTH = 15
MAX_PASSWORD_LENGTH = 128

BLOCKLISTED_PASSWORDS = frozenset(
    {
        "111111111111111",
        "123456789012345",
        "123456789123456",
        "aaaaaaaaaaaaaaa",
        "adminadminadmin",
        "administrator123",
        "apokalypsecodeanalysissystem",
        "changemechangeme",
        "changemepassword",
        "codingai12345678",
        "codingaipassword",
        "correcthorsebatterystaple",
        "deepseek-coder-v2",
        "defaultpassword",
        "iloveyouiloveyou",
        "letmeinletmein123",
        "mypassword12345",
        "ollamadeepseekchat",
        "passphrase12345",
        "password1234567",
        "passwordpassword",
        "passwordpassword1",
        "qwerty123456789",
        "qwertyuiop12345",
        "secretsecret1234",
        "thisisapassword",
        "trustnoone123456",
        "welcome12345678",
        "welcome123welcome",
        "zxcvbnm12345678",
    }
)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    return email


def validate_username(username: str) -> str:
    username = username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,30}", username):
        raise HTTPException(
            status_code=422,
            detail="Username must be 3-30 characters using letters, numbers, _ or -",
        )
    return username


def validate_password(password: str) -> None:
    if len(password) < MIN_NEW_PASSWORD_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Password must be {MIN_NEW_PASSWORD_LENGTH}-{MAX_PASSWORD_LENGTH} "
                "characters. Longer passphrases and spaces are allowed."
            ),
        )
    blocklist_candidate = password.casefold()
    if (
        blocklist_candidate in BLOCKLISTED_PASSWORDS
        or blocklist_candidate.strip() in BLOCKLISTED_PASSWORDS
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "That password is too common or predictable. Choose a different, "
                "long passphrase."
            ),
        )


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=SCRYPT_HASH_MAX_MEMORY,
        dklen=SCRYPT_DERIVED_KEY_BYTES,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${derived.hex()}"


def parse_scrypt_hash(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """Parse and bound stored parameters before allowing them into OpenSSL."""
    if not isinstance(encoded, str) or len(encoded) > 512:
        return None
    try:
        algorithm, n_text, r_text, p_text, salt_hex, expected_hex = encoded.split("$")
        if (
            not 32 <= len(salt_hex) <= 128
            or len(salt_hex) % 2
            or not 64 <= len(expected_hex) <= 256
            or len(expected_hex) % 2
            or not re.fullmatch(r"[0-9a-fA-F]+", salt_hex)
            or not re.fullmatch(r"[0-9a-fA-F]+", expected_hex)
        ):
            return None
        n, r, p = int(n_text), int(r_text), int(p_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (AttributeError, TypeError, ValueError):
        return None
    if algorithm != "scrypt" or n < 2**13 or n > 2**17 or n & (n - 1):
        return None
    if not 1 <= r <= 16 or not 1 <= p <= 16:
        return None
    if not 16 <= len(salt) <= 64 or not 32 <= len(expected) <= 128:
        return None
    estimated_memory = 128 * n * r
    estimated_work = n * r * p
    if (
        estimated_memory > SCRYPT_VERIFY_MAX_MEMORY // 2
        or estimated_work > SCRYPT_VERIFY_MAX_WORK
    ):
        return None
    return n, r, p, salt, expected


def password_matches(password: str, encoded: str) -> bool:
    parsed = parse_scrypt_hash(encoded)
    if parsed is None:
        return False
    n, r, p, salt, expected = parsed
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=SCRYPT_VERIFY_MAX_MEMORY,
            dklen=len(expected),
        )
        return secrets.compare_digest(actual, expected)
    except (MemoryError, ValueError):
        return False


def password_needs_rehash(encoded: str) -> bool:
    parsed = parse_scrypt_hash(encoded)
    if parsed is None:
        return True
    n, r, p, salt, expected = parsed
    return (
        (n, r, p) != (SCRYPT_N, SCRYPT_R, SCRYPT_P)
        or len(salt) != SCRYPT_SALT_BYTES
        or len(expected) != SCRYPT_DERIVED_KEY_BYTES
    )
