from __future__ import annotations

import pathlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest.mock import patch

import main


class MigrationBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary_directory.name)
        self.database_path = self.root / "legacy.db"
        self.backup_directory = self.root / "backups"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_existing_database_is_backed_up_before_pending_migrations(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as db:
            db.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
            db.execute("INSERT INTO legacy_marker(value) VALUES ('before-migration')")
            db.commit()

        with patch.object(main, "DB_PATH", self.database_path), patch.object(
            main, "MIGRATION_BACKUP_DIR", self.backup_directory
        ), patch.object(main, "CREATE_MIGRATION_BACKUPS", True):
            main.initialise_db()

        backups = list(self.backup_directory.glob("*.db"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as backup:
            marker = backup.execute("SELECT value FROM legacy_marker").fetchone()[0]
            migration_table = backup.execute(
                """
                SELECT COUNT(*) FROM sqlite_master
                WHERE type = 'table' AND name = 'schema_migrations'
                """
            ).fetchone()[0]
            integrity = backup.execute("PRAGMA quick_check").fetchone()[0]
        self.assertEqual(marker, "before-migration")
        self.assertEqual(migration_table, 0)
        self.assertEqual(integrity, "ok")

        with closing(sqlite3.connect(self.database_path)) as upgraded:
            self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 34)
            self.assertEqual(upgraded.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_fresh_database_does_not_create_unnecessary_backup(self) -> None:
        with patch.object(main, "DB_PATH", self.database_path), patch.object(
            main, "MIGRATION_BACKUP_DIR", self.backup_directory
        ), patch.object(main, "CREATE_MIGRATION_BACKUPS", True):
            main.initialise_db()

        self.assertFalse(self.backup_directory.exists())

    def test_current_database_does_not_create_backup_on_normal_restart(self) -> None:
        with patch.object(main, "DB_PATH", self.database_path), patch.object(
            main, "MIGRATION_BACKUP_DIR", self.backup_directory
        ), patch.object(main, "CREATE_MIGRATION_BACKUPS", True):
            main.initialise_db()
            main.initialise_db()

        self.assertFalse(self.backup_directory.exists())

    def test_periodic_backup_is_integrity_checked_and_contains_current_data(self) -> None:
        periodic_directory = self.root / "periodic"
        with patch.object(main, "DB_PATH", self.database_path), patch.object(
            main, "MIGRATION_BACKUP_DIR", self.backup_directory
        ), patch.object(main, "PERIODIC_BACKUP_DIR", periodic_directory):
            main.initialise_db()
            with main.connect_db() as db:
                db.execute("CREATE TABLE backup_marker(value TEXT NOT NULL)")
                db.execute("INSERT INTO backup_marker(value) VALUES ('current-data')")

            backup_path = main.create_periodic_database_backup()

        self.assertTrue(backup_path.is_file())
        self.assertFalse(list(periodic_directory.glob("*.tmp")))
        with closing(sqlite3.connect(backup_path)) as backup:
            self.assertEqual(
                backup.execute("SELECT value FROM backup_marker").fetchone()[0],
                "current-data",
            )
            self.assertEqual(backup.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_periodic_retention_preserves_pre_migration_backups(self) -> None:
        periodic_directory = self.root / "periodic"
        with patch.object(main, "DB_PATH", self.database_path), patch.object(
            main, "MIGRATION_BACKUP_DIR", self.backup_directory
        ), patch.object(main, "PERIODIC_BACKUP_DIR", periodic_directory), patch.object(
            main, "PERIODIC_BACKUP_RETENTION_COUNT", 2
        ):
            main.initialise_db()
            migration_copy = periodic_directory / "legacy-pre-v6-example.db"
            periodic_directory.mkdir(parents=True, exist_ok=True)
            migration_copy.write_bytes(b"migration-copy")
            for _ in range(3):
                main.create_periodic_database_backup()

            periodic_copies = main.periodic_backup_paths()

        self.assertEqual(len(periodic_copies), 2)
        self.assertTrue(migration_copy.is_file())

    def test_recent_periodic_backup_prevents_redundant_restart_copy(self) -> None:
        periodic_directory = self.root / "periodic"
        with patch.object(main, "DB_PATH", self.database_path), patch.object(
            main, "MIGRATION_BACKUP_DIR", self.backup_directory
        ), patch.object(main, "PERIODIC_BACKUP_DIR", periodic_directory), patch.object(
            main, "PERIODIC_BACKUP_INTERVAL_SECONDS", 3_600
        ):
            main.initialise_db()
            backup_path = main.create_periodic_database_backup()
            backup_time = backup_path.stat().st_mtime
            delay = main.seconds_until_periodic_backup(now=backup_time + 100)

        self.assertAlmostEqual(delay, 3_500, delta=1)


if __name__ == "__main__":
    unittest.main()
