"""Ordered, transactional SQLite schema migrations."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def migration_001_core_schema(db: sqlite3.Connection) -> None:
    """Create the core account, authentication, chat, message, and job schema."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            context_content TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    message_columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
    if "context_content" not in message_columns:
        db.execute("ALTER TABLE messages ADD COLUMN context_content TEXT")
    db.execute(
        "CREATE INDEX IF NOT EXISTS messages_session_id ON messages(session_id, id)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            email_verified_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            is_admin INTEGER NOT NULL DEFAULT 0,
            is_owner INTEGER NOT NULL DEFAULT 0,
            is_banned INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    for column in ("is_admin", "is_owner", "is_banned"):
        if column not in user_columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS registration_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL COLLATE NOCASE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            requested_at INTEGER NOT NULL DEFAULT 0,
            used_at TEXT
        )
        """
    )
    registration_token_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(registration_tokens)")
    }
    if "requested_at" not in registration_token_columns:
        db.execute(
            "ALTER TABLE registration_tokens "
            "ADD COLUMN requested_at INTEGER NOT NULL DEFAULT 0"
        )
    db.execute(
        "CREATE INDEX IF NOT EXISTS registration_tokens_email_requested "
        "ON registration_tokens(email, requested_at DESC)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS registration_rate_limits (
            scope_hash TEXT PRIMARY KEY,
            window_started_at INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS login_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute("CREATE INDEX IF NOT EXISTS login_sessions_token ON login_sessions(token_hash)")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS login_throttles (
            scope_hash TEXT PRIMARY KEY,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            last_attempt_at INTEGER NOT NULL DEFAULT 0,
            locked_until INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            requested_at INTEGER NOT NULL,
            used_at TEXT
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS password_reset_user "
        "ON password_reset_tokens(user_id, requested_at DESC)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_histories (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL DEFAULT 'New chat',
            title_is_custom INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS chat_histories_user_updated "
        "ON chat_histories(user_id, updated_at DESC)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            chat_id TEXT NOT NULL,
            message TEXT,
            status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed')),
            reply TEXT,
            title TEXT,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            cancel_reason TEXT,
            progress_stage TEXT,
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'chat',
            job_kind TEXT NOT NULL DEFAULT 'chat',
            user_message_id INTEGER,
            reply_message_id INTEGER,
            started_at INTEGER NOT NULL DEFAULT 0,
            progress_log TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    job_columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_jobs)")}
    if "cancel_requested" not in job_columns:
        db.execute(
            "ALTER TABLE chat_jobs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
        )
    if "cancel_reason" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN cancel_reason TEXT")
    if "progress_stage" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN progress_stage TEXT")
    if "progress_current" not in job_columns:
        db.execute(
            "ALTER TABLE chat_jobs ADD COLUMN progress_current INTEGER NOT NULL DEFAULT 0"
        )
    if "progress_total" not in job_columns:
        db.execute(
            "ALTER TABLE chat_jobs ADD COLUMN progress_total INTEGER NOT NULL DEFAULT 0"
        )
    if "mode" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN mode TEXT NOT NULL DEFAULT 'chat'")
    if "job_kind" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'chat'")
    if "user_message_id" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN user_message_id INTEGER")
    if "reply_message_id" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN reply_message_id INTEGER")
    if "started_at" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN started_at INTEGER NOT NULL DEFAULT 0")
    if "progress_log" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN progress_log TEXT NOT NULL DEFAULT '[]'")
    db.execute("CREATE INDEX IF NOT EXISTS chat_jobs_user ON chat_jobs(user_id, id)")
def migration_002_active_job_constraint(db: sqlite3.Connection) -> None:
    """Prevent more than one queued or processing job in the same user chat."""
    db.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS chat_jobs_one_active_per_chat
        ON chat_jobs(user_id, chat_id)
        WHERE status IN ('queued', 'processing')
        """
    )


def migration_003_security_cleanup_indexes(db: sqlite3.Connection) -> None:
    """Keep periodic expiry cleanup bounded as authentication tables grow."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS registration_tokens_expires "
        "ON registration_tokens(expires_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS login_sessions_expires "
        "ON login_sessions(expires_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS password_reset_tokens_expires "
        "ON password_reset_tokens(expires_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS login_throttles_updated "
        "ON login_throttles(updated_at)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS registration_rate_limits_updated "
        "ON registration_rate_limits(updated_at)"
    )


def migration_004_general_auth_request_limits(db: sqlite3.Connection) -> None:
    """Replace the registration limiter with reusable authentication request limits."""
    db.execute("ALTER TABLE registration_rate_limits RENAME TO auth_request_rate_limits")
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(auth_request_rate_limits)")
    }
    if "action" not in columns:
        db.execute(
            "ALTER TABLE auth_request_rate_limits "
            "ADD COLUMN action TEXT NOT NULL DEFAULT 'registration'"
        )
    db.execute("DROP INDEX IF EXISTS registration_rate_limits_updated")
    db.execute(
        "CREATE INDEX IF NOT EXISTS auth_request_rate_limits_updated "
        "ON auth_request_rate_limits(updated_at)"
    )


def migration_005_session_user_index(db: sqlite3.Connection) -> None:
    """Index each user's sessions in newest-first order for bounded management."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS login_sessions_user_newest "
        "ON login_sessions(user_id, id DESC)"
    )


def migration_006_admin_audit_events(db: sqlite3.Connection) -> None:
    """Persist a durable record of privileged account changes."""
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN ('make_admin', 'remove_admin', 'ban', 'unban')
            ),
            request_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_007_terminal_job_maintenance_index(db: sqlite3.Connection) -> None:
    """Index completed and failed jobs for bounded retention maintenance."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS chat_jobs_terminal_updated "
        "ON chat_jobs(status, updated_at) "
        "WHERE status IN ('completed', 'failed')"
    )


def migration_008_chat_list_order_index(db: sqlite3.Connection) -> None:
    """Index chat histories in stable newest-first display order."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS chat_histories_user_order "
        "ON chat_histories(user_id, updated_at DESC, created_at DESC, id DESC)"
    )


def migration_009_admin_session_audit_actions(db: sqlite3.Connection) -> None:
    """Replace the audit constraint for session revocation and account unlocking."""
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock'
                )
            ),
            request_id TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_010_admin_job_controls(db: sqlite3.Connection) -> None:
    """Replace the audit constraint and add durable administrator job cancellation."""
    job_columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_jobs)")}
    if "cancel_reason" not in job_columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN cancel_reason TEXT")
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_011_admin_backup_audit_action(db: sqlite3.Connection) -> None:
    """Allow owner-triggered database backups to be recorded in the audit trail."""
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_012_admin_maintenance_audit_action(db: sqlite3.Connection) -> None:
    """Allow owner-triggered retention maintenance to be audited."""
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup',
                    'run_maintenance'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_013_registration_control(db: sqlite3.Connection) -> None:
    """Persist the owner-controlled registration state and its audit action."""
    db.execute(
        """
        CREATE TABLE application_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            updated_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO application_settings(key, value, updated_at)
        VALUES ('registration_enabled', '1', 0)
        """
    )
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup',
                    'run_maintenance', 'set_registration'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_014_ai_work_control(db: sqlite3.Connection) -> None:
    """Persist the owner-controlled AI-work state and its audit action."""
    db.execute(
        """
        INSERT INTO application_settings(key, value, updated_at)
        VALUES ('ai_work_enabled', '1', 0)
        """
    )
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup',
                    'run_maintenance', 'set_registration', 'set_ai_work'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_015_announcement_control(db: sqlite3.Connection) -> None:
    """Allow site-announcement publication and clearing to be audited."""
    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup',
                    'run_maintenance', 'set_registration', 'set_ai_work',
                    'publish_announcement', 'clear_announcement'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_016_advanced_admin_controls(db: sqlite3.Connection) -> None:
    """Add account lifecycle, limits, and remaining audited administration tools."""
    user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
    additions = {
        "last_login_at": "INTEGER",
        "ban_reason": "TEXT",
        "banned_until": "INTEGER",
        "storage_limit_bytes": "INTEGER",
        "active_job_limit": "INTEGER",
        "pending_input_char_limit": "INTEGER",
        "is_anonymized": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in additions.items():
        if name not in user_columns:
            db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
    db.execute(
        "CREATE INDEX IF NOT EXISTS users_ban_expiry "
        "ON users(banned_until) WHERE is_banned = 1 AND banned_until IS NOT NULL"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS registration_tokens_pending "
        "ON registration_tokens(requested_at DESC, id DESC) WHERE used_at IS NULL"
    )
    db.execute(
        "INSERT OR IGNORE INTO application_settings(key, value, updated_at) "
        "VALUES ('last_integrity_check', '', 0)"
    )

    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup',
                    'run_maintenance', 'set_registration', 'set_ai_work',
                    'publish_announcement', 'clear_announcement',
                    'send_password_reset', 'delete_account', 'anonymize_account',
                    'set_user_limits', 'revoke_registration', 'run_integrity_check'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


def migration_017_project_uploads(db: sqlite3.Connection) -> None:
    """Store safely expanded project uploads and their file inventory."""
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS chat_histories_id_user "
        "ON chat_histories(id, user_id)"
    )
    db.execute(
        """
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            chat_id TEXT NOT NULL,
            name TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('zip', 'folder')),
            status TEXT NOT NULL DEFAULT 'uploaded' CHECK(
                status IN ('uploaded', 'indexing', 'indexed', 'analyzing', 'completed', 'failed')
            ),
            file_count INTEGER NOT NULL CHECK(file_count >= 0),
            skipped_file_count INTEGER NOT NULL DEFAULT 0 CHECK(skipped_file_count >= 0),
            total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(chat_id, user_id)
                REFERENCES chat_histories(id, user_id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        "CREATE INDEX projects_chat_newest ON projects(chat_id, user_id, created_at DESC)"
    )
    db.execute(
        """
        CREATE TABLE project_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            content BLOB NOT NULL,
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            is_binary INTEGER NOT NULL DEFAULT 0 CHECK(is_binary IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_id, path COLLATE NOCASE)
        )
        """
    )
    db.execute(
        "CREATE INDEX project_files_project_path ON project_files(project_id, path)"
    )


def migration_018_project_file_inventory(db: sqlite3.Connection) -> None:
    """Add deterministic language and file-classification metadata."""
    project_additions = {
        "primary_language": "TEXT",
        "languages_json": "TEXT NOT NULL DEFAULT '[]'",
        "inventory_status": (
            "TEXT NOT NULL DEFAULT 'pending' "
            "CHECK(inventory_status IN ('pending', 'completed', 'failed'))"
        ),
        "inventory_error": "TEXT",
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, definition in project_additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")

    file_additions = {
        "file_kind": "TEXT NOT NULL DEFAULT 'unknown'",
        "language": "TEXT",
        "language_confidence": "REAL NOT NULL DEFAULT 0",
        "detection_method": "TEXT NOT NULL DEFAULT 'pending'",
        "encoding": "TEXT",
        "line_count": "INTEGER",
        "is_generated": "INTEGER NOT NULL DEFAULT 0 CHECK(is_generated IN (0, 1))",
        "is_entrypoint_candidate": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(is_entrypoint_candidate IN (0, 1))"
        ),
        "analysis_eligible": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(analysis_eligible IN (0, 1))"
        ),
    }
    file_columns = {row["name"] for row in db.execute("PRAGMA table_info(project_files)")}
    for name, definition in file_additions.items():
        if name not in file_columns:
            db.execute(f"ALTER TABLE project_files ADD COLUMN {name} {definition}")
    db.execute(
        "CREATE INDEX project_files_language ON project_files(project_id, language, path)"
    )
    db.execute(
        "CREATE INDEX project_files_analysis_eligible "
        "ON project_files(project_id, analysis_eligible, path)"
    )
    db.execute(
        """
        CREATE TABLE project_languages (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            file_count INTEGER NOT NULL CHECK(file_count >= 0),
            source_bytes INTEGER NOT NULL CHECK(source_bytes >= 0),
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0, 1)),
            PRIMARY KEY(project_id, language)
        )
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX project_languages_one_primary "
        "ON project_languages(project_id) WHERE is_primary = 1"
    )


def migration_019_tree_sitter_adapters(db: sqlite3.Connection) -> None:
    """Store Tree-sitter adapter coverage and bounded parse diagnostics."""
    project_additions = {
        "parser_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(parser_status IN "
            "('pending', 'unsupported', 'partial', 'completed', 'failed'))"
        ),
        "parser_supported_file_count": "INTEGER NOT NULL DEFAULT 0 CHECK(parser_supported_file_count >= 0)",
        "parser_parsed_file_count": "INTEGER NOT NULL DEFAULT 0 CHECK(parser_parsed_file_count >= 0)",
        "parser_syntax_error_file_count": "INTEGER NOT NULL DEFAULT 0 CHECK(parser_syntax_error_file_count >= 0)",
        "parser_failed_file_count": "INTEGER NOT NULL DEFAULT 0 CHECK(parser_failed_file_count >= 0)",
        "parser_error": "TEXT",
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, definition in project_additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")

    file_additions = {
        "parser_adapter": "TEXT",
        "parser_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(parser_status IN "
            "('pending', 'not_applicable', 'unsupported', 'parsed', 'syntax_error', 'failed'))"
        ),
        "grammar_name": "TEXT",
        "grammar_version": "TEXT",
        "parser_source_sha256": "TEXT",
        "parser_error_count": "INTEGER NOT NULL DEFAULT 0 CHECK(parser_error_count >= 0)",
        "parser_missing_count": "INTEGER NOT NULL DEFAULT 0 CHECK(parser_missing_count >= 0)",
        "parser_diagnostics_json": "TEXT NOT NULL DEFAULT '[]'",
        "parser_diagnostics_truncated": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(parser_diagnostics_truncated IN (0, 1))"
        ),
        "parser_error": "TEXT",
        "parser_updated_at": "TEXT",
    }
    file_columns = {row["name"] for row in db.execute("PRAGMA table_info(project_files)")}
    for name, definition in file_additions.items():
        if name not in file_columns:
            db.execute(f"ALTER TABLE project_files ADD COLUMN {name} {definition}")
    db.execute(
        "CREATE INDEX project_files_parser_status "
        "ON project_files(project_id, parser_status, path)"
    )


def migration_020_project_structure_index(db: sqlite3.Connection) -> None:
    """Persist definition, dependency, and call ranges from Tree-sitter queries."""
    project_additions = {
        "structure_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(structure_status IN "
            "('pending', 'unsupported', 'partial', 'completed', 'failed'))"
        ),
        "indexed_file_count": "INTEGER NOT NULL DEFAULT 0 CHECK(indexed_file_count >= 0)",
        "definition_count": "INTEGER NOT NULL DEFAULT 0 CHECK(definition_count >= 0)",
        "dependency_count": "INTEGER NOT NULL DEFAULT 0 CHECK(dependency_count >= 0)",
        "resolved_dependency_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(resolved_dependency_count >= 0)"
        ),
        "ambiguous_dependency_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(ambiguous_dependency_count >= 0)"
        ),
        "call_count": "INTEGER NOT NULL DEFAULT 0 CHECK(call_count >= 0)",
        "structure_error": "TEXT",
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, definition in project_additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")

    file_additions = {
        "structure_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(structure_status IN "
            "('pending', 'not_applicable', 'unsupported', 'indexed', 'failed'))"
        ),
        "definition_count": "INTEGER NOT NULL DEFAULT 0 CHECK(definition_count >= 0)",
        "dependency_count": "INTEGER NOT NULL DEFAULT 0 CHECK(dependency_count >= 0)",
        "call_count": "INTEGER NOT NULL DEFAULT 0 CHECK(call_count >= 0)",
        "structure_error": "TEXT",
        "structure_updated_at": "TEXT",
    }
    file_columns = {row["name"] for row in db.execute("PRAGMA table_info(project_files)")}
    for name, definition in file_additions.items():
        if name not in file_columns:
            db.execute(f"ALTER TABLE project_files ADD COLUMN {name} {definition}")

    db.execute(
        """
        CREATE TABLE project_symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
            parent_symbol_id INTEGER REFERENCES project_symbols(id) ON DELETE SET NULL,
            symbol_kind TEXT NOT NULL CHECK(symbol_kind IN ('function', 'method', 'class', 'struct')),
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            nesting_depth INTEGER NOT NULL CHECK(nesting_depth >= 0),
            start_line INTEGER NOT NULL CHECK(start_line >= 1),
            start_column INTEGER NOT NULL CHECK(start_column >= 1),
            end_line INTEGER NOT NULL CHECK(end_line >= start_line),
            end_column INTEGER NOT NULL CHECK(end_column >= 1),
            start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
            end_byte INTEGER NOT NULL CHECK(end_byte >= start_byte),
            body_start_line INTEGER,
            body_end_line INTEGER,
            body_start_byte INTEGER,
            body_end_byte INTEGER,
            source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(file_id, symbol_kind, start_byte, end_byte, name)
        )
        """
    )
    db.execute(
        "CREATE INDEX project_symbols_file_range "
        "ON project_symbols(project_id, file_id, start_byte, end_byte)"
    )
    db.execute(
        "CREATE INDEX project_symbols_qualified_name "
        "ON project_symbols(project_id, qualified_name, file_id)"
    )
    db.execute(
        """
        CREATE TABLE project_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
            dependency_kind TEXT NOT NULL CHECK(dependency_kind IN ('import', 'import_from', 'include')),
            module_name TEXT NOT NULL,
            imported_names_json TEXT NOT NULL DEFAULT '[]',
            is_relative INTEGER NOT NULL DEFAULT 0 CHECK(is_relative IN (0, 1)),
            resolved_file_id INTEGER REFERENCES project_files(id) ON DELETE SET NULL,
            resolution_status TEXT NOT NULL DEFAULT 'unresolved' CHECK(
                resolution_status IN ('unresolved', 'internal', 'external', 'ambiguous')
            ),
            start_line INTEGER NOT NULL CHECK(start_line >= 1),
            start_column INTEGER NOT NULL CHECK(start_column >= 1),
            end_line INTEGER NOT NULL CHECK(end_line >= start_line),
            end_column INTEGER NOT NULL CHECK(end_column >= 1),
            start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
            end_byte INTEGER NOT NULL CHECK(end_byte >= start_byte),
            source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        "CREATE INDEX project_dependencies_module "
        "ON project_dependencies(project_id, module_name, file_id)"
    )
    db.execute(
        "CREATE INDEX project_dependencies_resolved_file "
        "ON project_dependencies(project_id, resolved_file_id, file_id)"
    )
    db.execute(
        """
        CREATE TABLE project_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
            caller_symbol_id INTEGER REFERENCES project_symbols(id) ON DELETE SET NULL,
            callee TEXT NOT NULL,
            start_line INTEGER NOT NULL CHECK(start_line >= 1),
            start_column INTEGER NOT NULL CHECK(start_column >= 1),
            end_line INTEGER NOT NULL CHECK(end_line >= start_line),
            end_column INTEGER NOT NULL CHECK(end_column >= 1),
            start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
            end_byte INTEGER NOT NULL CHECK(end_byte >= start_byte),
            source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        "CREATE INDEX project_calls_callee "
        "ON project_calls(project_id, callee, file_id)"
    )


def migration_021_function_analysis_contract(db: sqlite3.Connection) -> None:
    """Store validated, versioned Ollama results for individual indexed functions."""
    project_additions = {
        "function_analysis_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(function_analysis_status IN "
            "('pending', 'running', 'partial', 'completed', 'failed', 'cancelled'))"
        ),
        "function_analysis_total_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(function_analysis_total_count >= 0)"
        ),
        "function_analysis_completed_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(function_analysis_completed_count >= 0)"
        ),
        "function_analysis_failed_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(function_analysis_failed_count >= 0)"
        ),
        "function_analysis_skipped_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(function_analysis_skipped_count >= 0)"
        ),
        "function_analysis_error": "TEXT",
        "function_analysis_updated_at": "TEXT",
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, definition in project_additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")

    symbol_additions = {
        "analysis_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(analysis_status IN "
            "('pending', 'processing', 'completed', 'failed', 'stale', 'skipped', "
            "'not_applicable'))"
        ),
        "analysis_attempt_count": "INTEGER NOT NULL DEFAULT 0 CHECK(analysis_attempt_count >= 0)",
        "analysis_error": "TEXT",
        "analyzed_at": "TEXT",
    }
    symbol_columns = {row["name"] for row in db.execute("PRAGMA table_info(project_symbols)")}
    for name, definition in symbol_additions.items():
        if name not in symbol_columns:
            db.execute(f"ALTER TABLE project_symbols ADD COLUMN {name} {definition}")
    db.execute(
        "UPDATE project_symbols SET analysis_status = 'not_applicable' "
        "WHERE symbol_kind NOT IN ('function', 'method')"
    )
    db.execute(
        "CREATE INDEX project_symbols_analysis_status "
        "ON project_symbols(project_id, analysis_status, file_id, start_byte)"
    )

    db.execute(
        """
        CREATE TABLE project_symbol_analyses (
            symbol_id INTEGER PRIMARY KEY REFERENCES project_symbols(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            file_id INTEGER NOT NULL REFERENCES project_files(id) ON DELETE CASCADE,
            contract_version TEXT NOT NULL,
            model_name TEXT NOT NULL,
            source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
            response_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            syntax_valid INTEGER NOT NULL CHECK(syntax_valid IN (0, 1)),
            may_return_value INTEGER NOT NULL CHECK(may_return_value IN (0, 1)),
            return_nullable INTEGER NOT NULL CHECK(return_nullable IN (0, 1)),
            return_description TEXT NOT NULL,
            raised_errors_json TEXT NOT NULL DEFAULT '[]',
            side_effects_json TEXT NOT NULL DEFAULT '[]',
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        "CREATE INDEX project_symbol_analyses_project "
        "ON project_symbol_analyses(project_id, file_id, symbol_id)"
    )
    db.execute(
        """
        CREATE TABLE project_symbol_parameters (
            symbol_id INTEGER NOT NULL REFERENCES project_symbol_analyses(symbol_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            name TEXT NOT NULL,
            parameter_kind TEXT NOT NULL,
            required INTEGER NOT NULL CHECK(required IN (0, 1)),
            accepted_types_json TEXT NOT NULL DEFAULT '[]',
            default_description TEXT,
            description TEXT NOT NULL,
            PRIMARY KEY(symbol_id, ordinal),
            UNIQUE(symbol_id, name)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE project_symbol_return_types (
            symbol_id INTEGER NOT NULL REFERENCES project_symbol_analyses(symbol_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            type_name TEXT NOT NULL,
            description TEXT NOT NULL,
            PRIMARY KEY(symbol_id, ordinal)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE project_symbol_issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id INTEGER NOT NULL REFERENCES project_symbol_analyses(symbol_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            severity TEXT NOT NULL CHECK(severity IN ('error', 'warning', 'info', 'unsafe')),
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            provenance TEXT NOT NULL DEFAULT 'model'
                CHECK(provenance IN ('model', 'deterministic', 'cache', 'fallback')),
            UNIQUE(symbol_id, ordinal)
        )
        """
    )


def migration_022_project_analysis_jobs(db: sqlite3.Connection) -> None:
    """Extend the shared Ollama queue with durable project-analysis progress."""
    additions = {
        "project_id": "TEXT REFERENCES projects(id) ON DELETE SET NULL",
        "project_retry_failed": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(project_retry_failed IN (0, 1))"
        ),
        "input_char_count": "INTEGER NOT NULL DEFAULT 0 CHECK(input_char_count >= 0)",
        "progress_file_path": "TEXT",
        "progress_symbol_name": "TEXT",
        "progress_file_current": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(progress_file_current >= 0)"
        ),
        "progress_file_total": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(progress_file_total >= 0)"
        ),
        "progress_function_current": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(progress_function_current >= 0)"
        ),
        "progress_function_total": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(progress_function_total >= 0)"
        ),
    }
    columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_jobs)")}
    for name, definition in additions.items():
        if name not in columns:
            db.execute(f"ALTER TABLE chat_jobs ADD COLUMN {name} {definition}")
    db.execute(
        """
        UPDATE chat_jobs
        SET input_char_count = length(COALESCE(message, ''))
        WHERE input_char_count = 0 AND message IS NOT NULL
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX chat_jobs_one_active_per_project "
        "ON chat_jobs(project_id) "
        "WHERE project_id IS NOT NULL AND status IN ('queued', 'processing')"
    )
    db.execute(
        "CREATE INDEX chat_jobs_project_newest "
        "ON chat_jobs(project_id, created_at DESC) WHERE project_id IS NOT NULL"
    )


def migration_023_call_compatibility(db: sqlite3.Connection) -> None:
    """Persist call arguments, resolved targets, and contract compatibility findings."""
    project_additions = {
        "call_compatibility_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(call_compatibility_status IN "
            "('pending', 'completed', 'partial', 'failed', 'unavailable'))"
        ),
        "call_compatibility_checked_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(call_compatibility_checked_count >= 0)"
        ),
        "call_compatibility_incompatible_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(call_compatibility_incompatible_count >= 0)"
        ),
        "call_compatibility_unknown_count": (
            "INTEGER NOT NULL DEFAULT 0 CHECK(call_compatibility_unknown_count >= 0)"
        ),
        "call_compatibility_error": "TEXT",
        "call_compatibility_updated_at": "TEXT",
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, definition in project_additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {definition}")

    call_additions = {
        "resolved_symbol_id": "INTEGER REFERENCES project_symbols(id) ON DELETE SET NULL",
        "resolution_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(resolution_status IN "
            "('pending', 'internal', 'unresolved', 'ambiguous', 'external'))"
        ),
        "usage_kind": (
            "TEXT NOT NULL DEFAULT 'unknown' CHECK(usage_kind IN "
            "('statement', 'value', 'return', 'assignment', 'argument', 'condition', 'unknown'))"
        ),
        "expected_return_types_json": "TEXT NOT NULL DEFAULT '[]'",
        "detail_status": (
            "TEXT NOT NULL DEFAULT 'pending' CHECK(detail_status IN ('pending', 'complete'))"
        ),
    }
    call_columns = {row["name"] for row in db.execute("PRAGMA table_info(project_calls)")}
    for name, definition in call_additions.items():
        if name not in call_columns:
            db.execute(f"ALTER TABLE project_calls ADD COLUMN {name} {definition}")
    db.execute(
        "CREATE INDEX project_calls_resolved_symbol "
        "ON project_calls(project_id, resolved_symbol_id, resolution_status)"
    )

    db.execute(
        """
        CREATE TABLE project_call_arguments (
            call_id INTEGER NOT NULL REFERENCES project_calls(id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            keyword_name TEXT,
            expression_kind TEXT NOT NULL,
            inferred_types_json TEXT NOT NULL DEFAULT '[]',
            start_line INTEGER NOT NULL CHECK(start_line >= 1),
            start_column INTEGER NOT NULL CHECK(start_column >= 1),
            end_line INTEGER NOT NULL CHECK(end_line >= start_line),
            end_column INTEGER NOT NULL CHECK(end_column >= 1),
            start_byte INTEGER NOT NULL CHECK(start_byte >= 0),
            end_byte INTEGER NOT NULL CHECK(end_byte >= start_byte),
            PRIMARY KEY(call_id, ordinal)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE project_call_compatibility (
            call_id INTEGER PRIMARY KEY REFERENCES project_calls(id) ON DELETE CASCADE,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            caller_symbol_id INTEGER REFERENCES project_symbols(id) ON DELETE SET NULL,
            callee_symbol_id INTEGER REFERENCES project_symbols(id) ON DELETE SET NULL,
            status TEXT NOT NULL CHECK(status IN ('compatible', 'incompatible', 'unknown')),
            argument_status TEXT NOT NULL CHECK(argument_status IN ('compatible', 'incompatible', 'unknown')),
            return_status TEXT NOT NULL CHECK(return_status IN ('compatible', 'incompatible', 'unknown')),
            caller_analysis_sha256 TEXT,
            callee_analysis_sha256 TEXT,
            checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        "CREATE INDEX project_call_compatibility_project "
        "ON project_call_compatibility(project_id, status, call_id)"
    )
    db.execute(
        """
        CREATE TABLE project_call_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER NOT NULL REFERENCES project_call_compatibility(call_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            severity TEXT NOT NULL CHECK(severity IN ('error', 'warning', 'info')),
            finding_kind TEXT NOT NULL,
            message TEXT NOT NULL,
            argument_ordinal INTEGER,
            expected_types_json TEXT NOT NULL DEFAULT '[]',
            actual_types_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(call_id, ordinal)
        )
        """
    )


def migration_024_function_analysis_cache(db: sqlite3.Connection) -> None:
    """Cache validated function contracts without sharing results between users."""
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    if "function_analysis_cache_hit_count" not in project_columns:
        db.execute(
            "ALTER TABLE projects ADD COLUMN function_analysis_cache_hit_count "
            "INTEGER NOT NULL DEFAULT 0 CHECK(function_analysis_cache_hit_count >= 0)"
        )
    db.execute(
        """
        CREATE TABLE function_analysis_cache (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            language TEXT NOT NULL,
            function_sha256 TEXT NOT NULL CHECK(length(function_sha256) = 64),
            contract_version TEXT NOT NULL,
            model_name TEXT NOT NULL,
            line_basis TEXT NOT NULL DEFAULT 'function_relative_v1'
                CHECK(line_basis = 'function_relative_v1'),
            response_json TEXT NOT NULL,
            use_count INTEGER NOT NULL DEFAULT 0 CHECK(use_count >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT,
            PRIMARY KEY(
                user_id, language, function_sha256, contract_version, model_name
            )
        )
        """
    )
    db.execute(
        "CREATE INDEX function_analysis_cache_recent "
        "ON function_analysis_cache(user_id, last_used_at DESC, updated_at DESC)"
    )


def migration_025_oversized_function_chunking(db: sqlite3.Connection) -> None:
    """Revert former hard-limit skips so oversized functions can use chunk analysis."""
    db.execute(
        """
        UPDATE project_symbols
        SET analysis_status = 'pending', analysis_error = NULL, analyzed_at = NULL
        WHERE symbol_kind IN ('function', 'method') AND analysis_status = 'skipped'
        """
    )
    db.execute(
        """
        UPDATE projects
        SET function_analysis_status = CASE
                WHEN function_analysis_completed_count = function_analysis_total_count
                    THEN 'completed'
                WHEN function_analysis_completed_count > 0
                     OR function_analysis_failed_count > 0
                    THEN 'partial'
                ELSE 'pending'
            END,
            function_analysis_skipped_count = 0,
            function_analysis_updated_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE function_analysis_skipped_count > 0
        """
    )


def migration_026_issue_provenance(db: sqlite3.Connection) -> None:
    """Record whether function-analysis issues came from model, static checks, cache, or fallback."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(project_symbol_issues)")}
    if "provenance" not in columns:
        db.execute(
            """
            ALTER TABLE project_symbol_issues
            ADD COLUMN provenance TEXT NOT NULL DEFAULT 'model'
                CHECK(provenance IN ('model', 'deterministic', 'cache', 'fallback'))
            """
        )


def migration_027_issue_unsafe_severity(db: sqlite3.Connection) -> None:
    """Allow assumption-based hazards to be persisted as first-class unsafe issues."""
    db.execute(
        """
        CREATE TABLE project_symbol_issues_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_id INTEGER NOT NULL REFERENCES project_symbol_analyses(symbol_id) ON DELETE CASCADE,
            ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
            severity TEXT NOT NULL CHECK(severity IN ('error', 'warning', 'info', 'unsafe')),
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            start_line INTEGER,
            end_line INTEGER,
            provenance TEXT NOT NULL DEFAULT 'model'
                CHECK(provenance IN ('model', 'deterministic', 'cache', 'fallback')),
            UNIQUE(symbol_id, ordinal)
        )
        """
    )
    db.execute(
        """
        INSERT INTO project_symbol_issues_new(
            id, symbol_id, ordinal, severity, category, title, description,
            start_line, end_line, provenance
        )
        SELECT id, symbol_id, ordinal, severity, category, title, description,
               start_line, end_line, provenance
        FROM project_symbol_issues
        """
    )
    db.execute("DROP TABLE project_symbol_issues")
    db.execute("ALTER TABLE project_symbol_issues_new RENAME TO project_symbol_issues")


def migration_028_issue_verification_details(db: sqlite3.Connection) -> None:
    """Persist source proof and separate actionable defects from advisories."""
    columns = {
        str(row["name"])
        for row in db.execute("PRAGMA table_info(project_symbol_issues)")
    }
    additions = (
        (
            "proof",
            "TEXT CHECK(proof IS NULL OR proof = 'source-v1')",
        ),
        ("evidence", "TEXT"),
        ("failure_type", "TEXT"),
        ("trigger", "TEXT"),
        (
            "report_tier",
            "TEXT NOT NULL DEFAULT 'defect' CHECK(report_tier IN ('defect', 'advisory'))",
        ),
    )
    for name, declaration in additions:
        if name not in columns:
            db.execute(
                f"ALTER TABLE project_symbol_issues ADD COLUMN {name} {declaration}"
            )
    db.execute(
        """
        UPDATE project_symbol_issues
        SET report_tier = CASE
            WHEN category = 'maintainability' OR severity = 'info' THEN 'advisory'
            ELSE 'defect'
        END
        """
    )


def migration_029_active_job_elapsed_time(db: sqlite3.Connection) -> None:
    """Track paused time so job timers report active analysis time."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_jobs)")}
    if "paused_at" not in columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN paused_at INTEGER")
    if "paused_seconds" not in columns:
        db.execute(
            "ALTER TABLE chat_jobs ADD COLUMN paused_seconds "
            "INTEGER NOT NULL DEFAULT 0 CHECK(paused_seconds >= 0)"
        )


def migration_030_analysis_signal_metrics(db: sqlite3.Connection) -> None:
    """Separate out-of-scope calls and persist model, batch, and deterministic analysis counts."""
    project_additions = {
        "call_compatibility_not_checked_count": (
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(call_compatibility_not_checked_count >= 0)"
        ),
        "function_analysis_model_request_count": (
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(function_analysis_model_request_count >= 0)"
        ),
        "function_analysis_batch_request_count": (
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(function_analysis_batch_request_count >= 0)"
        ),
        "function_analysis_deterministic_count": (
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(function_analysis_deterministic_count >= 0)"
        ),
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, declaration in project_additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {declaration}")

    compatibility_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(project_call_compatibility)")
    }
    if "scope_status" not in compatibility_columns:
        db.execute(
            """
            ALTER TABLE project_call_compatibility
            ADD COLUMN scope_status TEXT NOT NULL DEFAULT 'in_scope'
                CHECK(scope_status IN ('in_scope', 'out_of_scope'))
            """
        )
    db.execute(
        """
        UPDATE project_call_compatibility
        SET scope_status = 'out_of_scope'
        WHERE call_id IN (
                SELECT id
                FROM project_calls
                WHERE resolution_status = 'external'
            )
           OR call_id IN (
                SELECT call_id
                FROM project_call_findings
                WHERE finding_kind IN (
                    'builtin_call', 'external_call', 'dynamic_call',
                    'dynamic_method_call'
                )
            )
        """
    )
    db.execute(
        """
        UPDATE projects
        SET call_compatibility_not_checked_count = (
                SELECT COUNT(*)
                FROM project_call_compatibility AS compatibility
                WHERE compatibility.project_id = projects.id
                  AND compatibility.scope_status = 'out_of_scope'
            ),
            call_compatibility_unknown_count = (
                SELECT COUNT(*)
                FROM project_call_compatibility AS compatibility
                WHERE compatibility.project_id = projects.id
                  AND compatibility.scope_status = 'in_scope'
                  AND compatibility.status = 'unknown'
            )
        """
    )
    db.execute(
        """
        UPDATE projects
        SET call_compatibility_status = CASE
                WHEN call_compatibility_unknown_count > 0 THEN 'partial'
                ELSE 'completed'
            END
        WHERE call_compatibility_checked_count > 0
          AND call_compatibility_status IN ('completed', 'partial')
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS project_call_compatibility_scope "
        "ON project_call_compatibility(project_id, scope_status, status, call_id)"
    )


def migration_031_analysis_accuracy_and_coverage(db: sqlite3.Connection) -> None:
    """Record adaptive batch fallbacks and treat completed local-call passes as complete."""
    additions = {
        "function_analysis_batch_fallback_count": (
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK(function_analysis_batch_fallback_count >= 0)"
        ),
        "function_analysis_batch_error": "TEXT",
    }
    project_columns = {row["name"] for row in db.execute("PRAGMA table_info(projects)")}
    for name, declaration in additions.items():
        if name not in project_columns:
            db.execute(f"ALTER TABLE projects ADD COLUMN {name} {declaration}")
    db.execute(
        """
        UPDATE projects
        SET call_compatibility_status = 'completed'
        WHERE call_compatibility_checked_count > 0
          AND call_compatibility_status = 'partial'
          AND call_compatibility_error IS NULL
        """
    )


def migration_032_editable_project_tree(db: sqlite3.Connection) -> None:
    """Track upload origins and an explicitly selected project entry point."""
    db.execute("ALTER TABLE projects ADD COLUMN main_file_path TEXT")
    db.execute("""
        CREATE TABLE project_upload_batches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('zip', 'folder', 'files')),
            name TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    db.execute("CREATE INDEX project_upload_batches_project ON project_upload_batches(project_id, id)")
    db.execute("ALTER TABLE project_files ADD COLUMN upload_batch_id INTEGER REFERENCES project_upload_batches(id) ON DELETE CASCADE")
    db.execute("CREATE INDEX project_files_upload_batch ON project_files(upload_batch_id)")
    for project in db.execute("SELECT id, name, source_kind FROM projects").fetchall():
        batch_id = db.execute(
            "INSERT INTO project_upload_batches(project_id, source_kind, name) VALUES (?, ?, ?)",
            (project["id"], project["source_kind"], project["name"]),
        ).lastrowid
        db.execute("UPDATE project_files SET upload_batch_id = ? WHERE project_id = ?", (batch_id, project["id"]))


def migration_033_function_output_budgets(db: sqlite3.Connection) -> None:
    """Store explainable output estimates and bounded model-usage observations."""
    db.execute("""CREATE TABLE function_analysis_budgets (
        symbol_id INTEGER PRIMARY KEY REFERENCES project_symbols(id) ON DELETE CASCADE,
        budget_json TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("""CREATE TABLE function_analysis_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        symbol_id INTEGER NOT NULL REFERENCES project_symbols(id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL, model TEXT NOT NULL, language TEXT NOT NULL, budget_version TEXT NOT NULL,
        complexity_bucket INTEGER NOT NULL, dependency_bucket INTEGER NOT NULL,
        output_limit INTEGER NOT NULL, generated_tokens INTEGER, prompt_tokens INTEGER,
        reasoning_characters INTEGER NOT NULL DEFAULT 0, answer_characters INTEGER NOT NULL DEFAULT 0,
        truncated INTEGER NOT NULL DEFAULT 0, outcome TEXT NOT NULL, request_kind TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    db.execute("CREATE INDEX function_usage_history ON function_analysis_usage(user_id,model,language,budget_version,id)")
    db.execute("CREATE INDEX function_usage_project ON function_analysis_usage(project_id,symbol_id)")


def migration_034_function_tree_descriptions(db: sqlite3.Connection) -> None:
    """Target one indexed function when a tree description is requested."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_jobs)")}
    if "project_symbol_id" not in columns:
        db.execute(
            "ALTER TABLE chat_jobs ADD COLUMN project_symbol_id INTEGER "
            "REFERENCES project_symbols(id) ON DELETE SET NULL"
        )
    db.execute(
        "CREATE INDEX chat_jobs_project_symbol "
        "ON chat_jobs(project_symbol_id) WHERE project_symbol_id IS NOT NULL"
    )


def migration_035_runtime_model_selection(db: sqlite3.Connection) -> None:
    """Persist the selected model, capture it on jobs, and audit model changes."""
    db.execute(
        "INSERT OR IGNORE INTO application_settings(key, value, updated_at) "
        "VALUES ('selected_model', '', 0)"
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(chat_jobs)")}
    if "model_name" not in columns:
        db.execute("ALTER TABLE chat_jobs ADD COLUMN model_name TEXT NOT NULL DEFAULT ''")

    db.execute("ALTER TABLE admin_audit_events RENAME TO admin_audit_events_old")
    db.execute(
        """
        CREATE TABLE admin_audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            actor_username TEXT NOT NULL,
            target_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            target_username TEXT NOT NULL,
            action TEXT NOT NULL CHECK(
                action IN (
                    'make_admin', 'remove_admin', 'ban', 'unban',
                    'revoke_sessions', 'unlock', 'cancel_job', 'create_backup',
                    'run_maintenance', 'set_registration', 'set_ai_work',
                    'publish_announcement', 'clear_announcement',
                    'send_password_reset', 'delete_account', 'anonymize_account',
                    'set_user_limits', 'revoke_registration', 'run_integrity_check',
                    'set_model'
                )
            ),
            request_id TEXT NOT NULL,
            details TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        """
        INSERT INTO admin_audit_events(
            id, actor_user_id, actor_username, target_user_id, target_username,
            action, request_id, details, created_at
        )
        SELECT id, actor_user_id, actor_username, target_user_id, target_username,
               action, request_id, details, created_at
        FROM admin_audit_events_old
        """
    )
    db.execute("DROP TABLE admin_audit_events_old")
    db.execute(
        "CREATE INDEX admin_audit_events_newest "
        "ON admin_audit_events(created_at DESC, id DESC)"
    )


MIGRATIONS: tuple[Migration, ...] = (
    (1, "core_schema", migration_001_core_schema),
    (2, "one_active_job_per_chat", migration_002_active_job_constraint),
    (3, "security_cleanup_indexes", migration_003_security_cleanup_indexes),
    (4, "general_auth_request_limits", migration_004_general_auth_request_limits),
    (5, "session_user_index", migration_005_session_user_index),
    (6, "admin_audit_events", migration_006_admin_audit_events),
    (7, "terminal_job_maintenance_index", migration_007_terminal_job_maintenance_index),
    (8, "chat_list_order_index", migration_008_chat_list_order_index),
    (9, "admin_session_audit_actions", migration_009_admin_session_audit_actions),
    (10, "admin_job_controls", migration_010_admin_job_controls),
    (11, "admin_backup_audit_action", migration_011_admin_backup_audit_action),
    (12, "admin_maintenance_audit_action", migration_012_admin_maintenance_audit_action),
    (13, "registration_control", migration_013_registration_control),
    (14, "ai_work_control", migration_014_ai_work_control),
    (15, "announcement_control", migration_015_announcement_control),
    (16, "advanced_admin_controls", migration_016_advanced_admin_controls),
    (17, "project_uploads", migration_017_project_uploads),
    (18, "project_file_inventory", migration_018_project_file_inventory),
    (19, "tree_sitter_adapters", migration_019_tree_sitter_adapters),
    (20, "project_structure_index", migration_020_project_structure_index),
    (21, "function_analysis_contract", migration_021_function_analysis_contract),
    (22, "project_analysis_jobs", migration_022_project_analysis_jobs),
    (23, "call_compatibility", migration_023_call_compatibility),
    (24, "function_analysis_cache", migration_024_function_analysis_cache),
    (25, "oversized_function_chunking", migration_025_oversized_function_chunking),
    (26, "issue_provenance", migration_026_issue_provenance),
    (27, "issue_unsafe_severity", migration_027_issue_unsafe_severity),
    (28, "issue_verification_details", migration_028_issue_verification_details),
    (29, "active_job_elapsed_time", migration_029_active_job_elapsed_time),
    (30, "analysis_signal_metrics", migration_030_analysis_signal_metrics),
    (31, "analysis_accuracy_and_coverage", migration_031_analysis_accuracy_and_coverage),
    (32, "editable_project_tree", migration_032_editable_project_tree),
    (33, "function_output_budgets", migration_033_function_output_budgets),
    (34, "function_tree_descriptions", migration_034_function_tree_descriptions),
    (35, "runtime_model_selection", migration_035_runtime_model_selection),
)

# Earliest reliable UTC chronology available in this checkout. Versions 1-28
# use their first recorded application time in the development database;
# versions 29-31 and 34-35 use their implementation completion times. These are history markers,
# not public release dates.
MIGRATION_RECORDED_AT_UTC: dict[int, str] = {
    35: "2026-09-15 16:24:23 UTC",
    34: "2026-09-10 12:00:00 UTC",
    33: "2026-09-09 00:04:23 UTC",
    32: "2026-09-07 18:30:09 UTC",
    1: "2026-08-05 17:33:49 UTC",
    2: "2026-08-05 17:33:49 UTC",
    3: "2026-08-05 21:37:27 UTC",
    4: "2026-08-05 21:37:27 UTC",
    5: "2026-08-05 21:37:27 UTC",
    6: "2026-08-05 21:37:27 UTC",
    7: "2026-08-05 21:37:27 UTC",
    8: "2026-08-05 21:37:27 UTC",
    9: "2026-08-05 21:37:27 UTC",
    10: "2026-08-05 21:37:27 UTC",
    11: "2026-08-05 21:37:27 UTC",
    12: "2026-08-05 21:37:27 UTC",
    13: "2026-08-05 21:37:27 UTC",
    14: "2026-08-05 21:37:27 UTC",
    15: "2026-08-05 21:37:27 UTC",
    16: "2026-08-05 21:37:27 UTC",
    17: "2026-08-11 20:09:21 UTC",
    18: "2026-08-11 20:09:21 UTC",
    19: "2026-08-11 20:09:21 UTC",
    20: "2026-08-11 20:09:21 UTC",
    21: "2026-08-11 20:09:21 UTC",
    22: "2026-08-11 20:09:21 UTC",
    23: "2026-08-11 20:09:21 UTC",
    24: "2026-08-11 20:09:21 UTC",
    25: "2026-08-11 20:09:21 UTC",
    26: "2026-08-15 15:55:57 UTC",
    27: "2026-08-23 15:11:27 UTC",
    28: "2026-08-24 17:54:18 UTC",
    29: "2026-08-24 18:06:18 UTC",
    30: "2026-08-24 20:00:28 UTC",
    31: "2026-08-24 22:18:46 UTC",
}

MIGRATION_CHANGE_ACTIONS: dict[int, str] = {
    35: "Added",
    34: "Added",
    33: "Added",
    32: "Added",
    1: "Added",
    2: "Changed",
    3: "Changed",
    4: "Replaced",
    5: "Changed",
    6: "Added",
    7: "Changed",
    8: "Changed",
    9: "Replaced",
    10: "Replaced",
    11: "Replaced",
    12: "Replaced",
    13: "Added",
    14: "Added",
    15: "Replaced",
    16: "Replaced",
    17: "Added",
    18: "Added",
    19: "Added",
    20: "Added",
    21: "Added",
    22: "Added",
    23: "Added",
    24: "Added",
    25: "Reverted",
    26: "Added",
    27: "Replaced",
    28: "Changed",
    29: "Changed",
    30: "Changed",
    31: "Changed",
}


def pending_migration_versions(db: sqlite3.Connection) -> list[int]:
    table_exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if table_exists is None:
        return [version for version, _name, _migration in MIGRATIONS]
    applied = {
        int(row["version"])
        for row in db.execute("SELECT version FROM schema_migrations")
    }
    return [
        version
        for version, _name, _migration in MIGRATIONS
        if version not in applied
    ]


def apply_migrations(
    db: sqlite3.Connection,
    *,
    target_version: int | None = None,
) -> list[int]:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    applied = {
        int(row["version"]): str(row["name"])
        for row in db.execute("SELECT version, name FROM schema_migrations")
    }
    known = {version: name for version, name, _ in MIGRATIONS}
    unknown_versions = sorted(set(applied) - set(known))
    if unknown_versions:
        raise RuntimeError(
            "Database schema is newer than this application; unknown migration version(s): "
            + ", ".join(str(version) for version in unknown_versions)
        )
    renamed_versions = [
        version for version, name in applied.items() if known.get(version) != name
    ]
    if renamed_versions:
        raise RuntimeError(
            "Recorded migration name mismatch for version(s): "
            + ", ".join(str(version) for version in sorted(renamed_versions))
        )
    newly_applied: list[int] = []
    for version, name, migration in MIGRATIONS:
        if target_version is not None and version > target_version:
            break
        if version in applied:
            continue
        migration(db)
        db.execute(
            "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
            (version, name),
        )
        newly_applied.append(version)
    latest = max((*applied, *newly_applied), default=0)
    db.execute(f"PRAGMA user_version = {latest}")
    return newly_applied
