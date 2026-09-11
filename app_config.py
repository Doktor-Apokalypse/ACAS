"""Environment-backed runtime configuration for the application."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(RuntimeError):
    """Raised when an environment setting cannot produce a safe runtime value."""


def parse_integer_setting(
    name: str,
    value: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer; received {value!r}") from exc
    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum}; received {parsed}")
    if maximum is not None and parsed > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum}; received {parsed}")
    return parsed


def parse_float_setting(
    name: str,
    value: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number; received {value!r}") from exc
    if not math.isfinite(parsed):
        raise ConfigurationError(f"{name} must be finite; received {value!r}")
    if minimum is not None and parsed < minimum:
        raise ConfigurationError(f"{name} must be at least {minimum:g}; received {parsed:g}")
    if maximum is not None and parsed > maximum:
        raise ConfigurationError(f"{name} must be at most {maximum:g}; received {parsed:g}")
    return parsed


def parse_boolean_setting(name: str, value: str) -> bool:
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(
        f"{name} must be one of true/false, yes/no, on/off, or 1/0; received {value!r}"
    )


def env_integer(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    return parse_integer_setting(
        name,
        os.getenv(name, str(default)),
        minimum=minimum,
        maximum=maximum,
    )


def env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    return parse_float_setting(
        name,
        os.getenv(name, str(default)),
        minimum=minimum,
        maximum=maximum,
    )


def env_boolean(name: str, default: bool) -> bool:
    return parse_boolean_setting(name, os.getenv(name, str(default)))


HOST = os.getenv("HOST", "127.0.0.1").strip()
PORT = env_integer("PORT", 8000, minimum=1, maximum=65535)
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "deepseek-coder-v2:16B").strip()
MAX_HISTORY_MESSAGES = env_integer("MAX_HISTORY_MESSAGES", 50, minimum=1)
CHAT_HISTORY_PAGE_SIZE = env_integer(
    "CHAT_HISTORY_PAGE_SIZE", 20, minimum=1, maximum=100
)
CHAT_LIST_PAGE_SIZE = env_integer("CHAT_LIST_PAGE_SIZE", 50, minimum=1, maximum=200)
ADMIN_USER_PAGE_SIZE = env_integer("ADMIN_USER_PAGE_SIZE", 50, minimum=5, maximum=100)
MAX_MESSAGE_CHARS = env_integer("MAX_MESSAGE_CHARS", 1_500_000, minimum=1)
OLLAMA_CONTEXT_SIZE = env_integer("OLLAMA_CONTEXT_SIZE", 32_768, minimum=1)
OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT = env_boolean("OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT", True)
OLLAMA_ANALYSIS_CONTEXT_MIN = env_integer("OLLAMA_ANALYSIS_CONTEXT_MIN", 8_192, minimum=1)
OLLAMA_ANALYSIS_CONTEXT_MAX = env_integer("OLLAMA_ANALYSIS_CONTEXT_MAX", 65_536, minimum=1)
DIRECT_MESSAGE_CHARS = env_integer("DIRECT_MESSAGE_CHARS", 20_000, minimum=1)
LARGE_CHUNK_CHARS = env_integer("LARGE_CHUNK_CHARS", 20_000, minimum=2)
LARGE_CHUNK_OVERLAP_CHARS = env_integer(
    "LARGE_CHUNK_OVERLAP_CHARS", 800, minimum=0
)
LARGE_CHUNK_SUMMARY_TOKENS = env_integer(
    "LARGE_CHUNK_SUMMARY_TOKENS", 650, minimum=1
)
CONSOLIDATION_CHUNK_CHARS = env_integer(
    "CONSOLIDATION_CHUNK_CHARS", 24_000, minimum=1
)
CONSOLIDATION_MAX_CHARS = env_integer(
    "CONSOLIDATION_MAX_CHARS", 70_000, minimum=1
)
LARGE_REQUEST_CONTEXT_CHARS = env_integer(
    "LARGE_REQUEST_CONTEXT_CHARS", 1_500, minimum=1
)
LARGE_VERIFICATION_TOKENS = env_integer(
    "LARGE_VERIFICATION_TOKENS", 4_096, minimum=1
)
EVIDENCE_REPAIR_SOURCE_CHARS = env_integer(
    "EVIDENCE_REPAIR_SOURCE_CHARS", 24_000, minimum=1
)
SOURCE_INVENTORY_MAX_CHARS = env_integer(
    "SOURCE_INVENTORY_MAX_CHARS", 16_000, minimum=1
)
FUNCTION_ANALYSIS_MAX_SOURCE_CHARS = env_integer(
    "FUNCTION_ANALYSIS_MAX_SOURCE_CHARS", 40_000, minimum=100
)
FUNCTION_ANALYSIS_CHUNK_CHARS = env_integer(
    "FUNCTION_ANALYSIS_CHUNK_CHARS", 20_000, minimum=100
)
FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS = env_integer(
    "FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS", 8_192, minimum=128
)
FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT = env_boolean("FUNCTION_ANALYSIS_ADAPTIVE_OUTPUT", True)
FUNCTION_ANALYSIS_CONTEXT_CHARS = env_integer(
    "FUNCTION_ANALYSIS_CONTEXT_CHARS", 20_000, minimum=0, maximum=40_000
)
FUNCTION_ANALYSIS_BATCH_SIZE = env_integer(
    "FUNCTION_ANALYSIS_BATCH_SIZE", 8, minimum=1, maximum=8
)
FUNCTION_ANALYSIS_BATCH_MAX_CHARS = env_integer(
    "FUNCTION_ANALYSIS_BATCH_MAX_CHARS", 32_000, minimum=1_000, maximum=120_000
)
FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS = env_integer(
    "FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS", 16_384, minimum=256, maximum=32_768
)
FUNCTION_ANALYSIS_CACHE_RETENTION_DAYS = env_integer(
    "FUNCTION_ANALYSIS_CACHE_RETENTION_DAYS", 90, minimum=1, maximum=3_650
)
FUNCTION_ANALYSIS_CACHE_MAX_ROWS_PER_USER = env_integer(
    "FUNCTION_ANALYSIS_CACHE_MAX_ROWS_PER_USER", 5_000, minimum=1, maximum=100_000
)
HISTORY_CONTEXT_MESSAGE_CHARS = env_integer(
    "HISTORY_CONTEXT_MESSAGE_CHARS", 60_000, minimum=1
)
MODEL_INPUT_CHAR_BUDGET = env_integer(
    "MODEL_INPUT_CHAR_BUDGET", 90_000, minimum=1
)
OLLAMA_MAX_OUTPUT_TOKENS = env_integer(
    "OLLAMA_MAX_OUTPUT_TOKENS", 4_096, minimum=1
)
OLLAMA_TEMPERATURE = env_float("OLLAMA_TEMPERATURE", 0.2, minimum=0, maximum=2)
OLLAMA_REPEAT_PENALTY = env_float(
    "OLLAMA_REPEAT_PENALTY", 1.15, minimum=0, maximum=2
)
OLLAMA_SOCKET_TIMEOUT = env_float("OLLAMA_SOCKET_TIMEOUT", 1_800, minimum=0.1)
FUNCTION_ANALYSIS_REQUEST_TIMEOUT = env_float(
    "FUNCTION_ANALYSIS_REQUEST_TIMEOUT", 300, minimum=1, maximum=3_600
)
READINESS_OLLAMA_TIMEOUT_SECONDS = env_float(
    "READINESS_OLLAMA_TIMEOUT_SECONDS", 2, minimum=0.1
)
READINESS_CACHE_SECONDS = env_float("READINESS_CACHE_SECONDS", 5, minimum=0)
JOB_QUEUE_CAPACITY = env_integer("JOB_QUEUE_CAPACITY", 20, minimum=1)
MAX_ACTIVE_JOBS_PER_USER = env_integer(
    "MAX_ACTIVE_JOBS_PER_USER", 3, minimum=1
)
MAX_PENDING_INPUT_CHARS_PER_USER = env_integer(
    "MAX_PENDING_INPUT_CHARS_PER_USER", 3_000_000, minimum=1
)
PROJECT_UPLOAD_MAX_ARCHIVE_BYTES = env_integer(
    "PROJECT_UPLOAD_MAX_ARCHIVE_BYTES", 25_000_000, minimum=1
)
PROJECT_UPLOAD_MAX_EXPANDED_BYTES = env_integer(
    "PROJECT_UPLOAD_MAX_EXPANDED_BYTES", 100_000_000, minimum=1
)
PROJECT_UPLOAD_MAX_FILE_BYTES = env_integer(
    "PROJECT_UPLOAD_MAX_FILE_BYTES", 5_000_000, minimum=1
)
PROJECT_UPLOAD_MAX_FILES = env_integer(
    "PROJECT_UPLOAD_MAX_FILES", 2_000, minimum=1, maximum=100_000
)
PROJECT_UPLOAD_MAX_COMPRESSION_RATIO = env_float(
    "PROJECT_UPLOAD_MAX_COMPRESSION_RATIO", 100, minimum=1
)
OLLAMA_GPT_OSS_REASONING = os.getenv("OLLAMA_GPT_OSS_REASONING", "low").strip().lower()
DB_PATH = Path(os.getenv("CHAT_DB_PATH", str(Path(__file__).with_name("chat_memory.db"))))
CREATE_MIGRATION_BACKUPS = env_boolean("CREATE_MIGRATION_BACKUPS", True)
MIGRATION_BACKUP_DIR = Path(
    os.getenv(
        "MIGRATION_BACKUP_DIR",
        str(DB_PATH.with_name(f"{DB_PATH.stem}_backups")),
    )
)
CREATE_PERIODIC_BACKUPS = env_boolean("CREATE_PERIODIC_BACKUPS", True)
PERIODIC_BACKUP_DIR = Path(
    os.getenv("PERIODIC_BACKUP_DIR", str(MIGRATION_BACKUP_DIR))
)
PERIODIC_BACKUP_INTERVAL_SECONDS = env_integer(
    "PERIODIC_BACKUP_INTERVAL_SECONDS", 86_400, minimum=60
)
PERIODIC_BACKUP_RETENTION_COUNT = env_integer(
    "PERIODIC_BACKUP_RETENTION_COUNT", 7, minimum=1, maximum=365
)
SQLITE_BUSY_TIMEOUT_MS = env_integer("SQLITE_BUSY_TIMEOUT_MS", 10_000, minimum=0)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
TRUSTED_HOSTS_CONFIG = os.getenv("TRUSTED_HOSTS", "")
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = env_integer("SMTP_PORT", 587, minimum=1, maximum=65535)
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USERNAME).strip()
SMTP_USE_TLS = env_boolean("SMTP_USE_TLS", True)
SMTP_USE_SSL = env_boolean("SMTP_USE_SSL", False)
NTFY_SERVER_URL = os.getenv("NTFY_SERVER_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_ACCESS_TOKEN = os.getenv("NTFY_ACCESS_TOKEN", "").strip()
NTFY_TIMEOUT_SECONDS = env_float(
    "NTFY_TIMEOUT_SECONDS", 5, minimum=0.1, maximum=30
)
NTFY_DEDUP_SECONDS = env_float(
    "NTFY_DEDUP_SECONDS", 60, minimum=0, maximum=3_600
)
NTFY_QUEUE_CAPACITY = env_integer(
    "NTFY_QUEUE_CAPACITY", 100, minimum=1, maximum=1_000
)
VERIFICATION_MINUTES = env_integer("VERIFICATION_MINUTES", 30, minimum=1)
REGISTRATION_RESEND_SECONDS = env_integer(
    "REGISTRATION_RESEND_SECONDS", 60, minimum=1
)
REGISTRATION_IP_WINDOW_SECONDS = env_integer(
    "REGISTRATION_IP_WINDOW_SECONDS", 3_600, minimum=1
)
REGISTRATION_MAX_REQUESTS_PER_IP = env_integer(
    "REGISTRATION_MAX_REQUESTS_PER_IP", 10, minimum=1
)
AUTH_EMAIL_RESPONSE_FLOOR_SECONDS = env_float(
    "AUTH_EMAIL_RESPONSE_FLOOR_SECONDS", 0.25, minimum=0.05, maximum=5
)
PASSWORD_RESET_MINUTES = env_integer("PASSWORD_RESET_MINUTES", 30, minimum=1)
PASSWORD_RESET_RESEND_SECONDS = env_integer(
    "PASSWORD_RESET_RESEND_SECONDS", 60, minimum=1
)
PASSWORD_RESET_IP_WINDOW_SECONDS = env_integer(
    "PASSWORD_RESET_IP_WINDOW_SECONDS", 3_600, minimum=1
)
PASSWORD_RESET_MAX_REQUESTS_PER_IP = env_integer(
    "PASSWORD_RESET_MAX_REQUESTS_PER_IP", 10, minimum=1
)
LOGIN_ATTEMPT_COOLDOWN_SECONDS = env_integer(
    "LOGIN_ATTEMPT_COOLDOWN_SECONDS", 5, minimum=0
)
LOGIN_MAX_FAILED_ATTEMPTS = env_integer(
    "LOGIN_MAX_FAILED_ATTEMPTS", 3, minimum=1
)
LOGIN_LOCKOUT_SECONDS = env_integer("LOGIN_LOCKOUT_SECONDS", 300, minimum=1)
SESSION_DAYS = env_integer("SESSION_DAYS", 7, minimum=1)
MAX_SESSIONS_PER_USER = env_integer(
    "MAX_SESSIONS_PER_USER", 10, minimum=1, maximum=100
)
ADMIN_AUDIT_RETENTION_DAYS = env_integer(
    "ADMIN_AUDIT_RETENTION_DAYS", 365, minimum=1, maximum=3_650
)
TERMINAL_JOB_PAYLOAD_RETENTION_DAYS = env_integer(
    "TERMINAL_JOB_PAYLOAD_RETENTION_DAYS", 7, minimum=1, maximum=3_650
)
SECURITY_CLEANUP_INTERVAL_SECONDS = env_integer(
    "SECURITY_CLEANUP_INTERVAL_SECONDS", 3_600, minimum=60
)
SESSION_COOKIE = "chat_session"
OWNER_USERNAME = os.getenv("OWNER_USERNAME", "admin").strip()
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "admin@example.com").strip().lower()
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    "You are a helpful assistant. Use the conversation history to remember context. "
    "Write conversational answers using readable Markdown headings, paragraphs, and lists when "
    "they improve clarity. Always put code samples in Markdown fenced code blocks and include "
    "the language name. When analyzing supplied code, ground technical claims in visible evidence "
    "and use exact identifiers instead of inventing implementation details. Do not wrap an answer "
    "in JSON unless the user explicitly requests JSON.",
)


def validate_application_configuration() -> None:
    """Validate relationships and text settings that cannot be checked while parsing."""
    errors: list[str] = []
    if not HOST:
        errors.append("HOST cannot be empty")
    parsed_ollama_url = urlsplit(OLLAMA_URL)
    if (
        parsed_ollama_url.scheme not in {"http", "https"}
        or not parsed_ollama_url.hostname
        or parsed_ollama_url.username
        or parsed_ollama_url.password
        or parsed_ollama_url.path not in {"", "/"}
        or parsed_ollama_url.query
        or parsed_ollama_url.fragment
    ):
        errors.append("OLLAMA_URL must be a complete HTTP(S) origin without credentials or a path")
    if not OLLAMA_MODEL:
        errors.append("OLLAMA_MODEL cannot be empty")
    if DIRECT_MESSAGE_CHARS > MAX_MESSAGE_CHARS:
        errors.append("DIRECT_MESSAGE_CHARS cannot exceed MAX_MESSAGE_CHARS")
    if OLLAMA_ANALYSIS_CONTEXT_MIN > OLLAMA_ANALYSIS_CONTEXT_MAX:
        errors.append("OLLAMA_ANALYSIS_CONTEXT_MIN cannot exceed OLLAMA_ANALYSIS_CONTEXT_MAX")
    if OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT and OLLAMA_ANALYSIS_CONTEXT_MAX <= max(
        FUNCTION_ANALYSIS_MAX_OUTPUT_TOKENS, FUNCTION_ANALYSIS_BATCH_MAX_OUTPUT_TOKENS
    ) + 2_048:
        errors.append("OLLAMA_ANALYSIS_CONTEXT_MAX must leave space for input beyond the output allowance and 2048-token margin")
    if LARGE_CHUNK_OVERLAP_CHARS >= LARGE_CHUNK_CHARS // 2:
        errors.append("LARGE_CHUNK_OVERLAP_CHARS must be less than half LARGE_CHUNK_CHARS")
    if CONSOLIDATION_CHUNK_CHARS > CONSOLIDATION_MAX_CHARS:
        errors.append("CONSOLIDATION_CHUNK_CHARS cannot exceed CONSOLIDATION_MAX_CHARS")
    if FUNCTION_ANALYSIS_CHUNK_CHARS > FUNCTION_ANALYSIS_MAX_SOURCE_CHARS:
        errors.append(
            "FUNCTION_ANALYSIS_CHUNK_CHARS cannot exceed "
            "FUNCTION_ANALYSIS_MAX_SOURCE_CHARS"
        )
    if PROJECT_UPLOAD_MAX_FILE_BYTES > PROJECT_UPLOAD_MAX_EXPANDED_BYTES:
        errors.append(
            "PROJECT_UPLOAD_MAX_FILE_BYTES cannot exceed PROJECT_UPLOAD_MAX_EXPANDED_BYTES"
        )
    if SMTP_USE_TLS and SMTP_USE_SSL:
        errors.append("SMTP_USE_TLS and SMTP_USE_SSL cannot both be enabled")
    parsed_ntfy_url = urlsplit(NTFY_SERVER_URL)
    if NTFY_TOPIC and (
        parsed_ntfy_url.scheme not in {"http", "https"}
        or not parsed_ntfy_url.hostname
        or parsed_ntfy_url.username
        or parsed_ntfy_url.password
        or parsed_ntfy_url.path not in {"", "/"}
        or parsed_ntfy_url.query
        or parsed_ntfy_url.fragment
    ):
        errors.append(
            "NTFY_SERVER_URL must be a complete HTTP(S) origin without credentials or a path"
        )
    if NTFY_TOPIC and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", NTFY_TOPIC):
        errors.append(
            "NTFY_TOPIC must be 1-64 characters using letters, numbers, _ or -"
        )
    if "\r" in NTFY_ACCESS_TOKEN or "\n" in NTFY_ACCESS_TOKEN:
        errors.append("NTFY_ACCESS_TOKEN cannot contain line breaks")
    if OLLAMA_GPT_OSS_REASONING not in {"low", "medium", "high"}:
        errors.append("OLLAMA_GPT_OSS_REASONING must be low, medium, or high")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,30}", OWNER_USERNAME):
        errors.append(
            "OWNER_USERNAME must be 3-30 characters using letters, numbers, _ or -"
        )
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", OWNER_EMAIL):
        errors.append("OWNER_EMAIL must be a valid email address")
    if not SYSTEM_PROMPT.strip():
        errors.append("SYSTEM_PROMPT cannot be empty")
    if errors:
        raise ConfigurationError("Invalid application configuration:\n- " + "\n- ".join(errors))
