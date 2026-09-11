from __future__ import annotations

from datetime import datetime
from pathlib import Path

import main
from changelog import (
    TIMESTAMP_FORMAT,
    build_changelog_entries,
    render_changelog_markdown,
)
from migrations import MIGRATIONS
from tests.helpers import DatabaseTestCase


class ChangelogTests(DatabaseTestCase):
    def test_static_changelog_is_complete_generated_and_newest_first(self) -> None:
        entries = build_changelog_entries()
        migration_entries = [
            entry for entry in entries if entry.migration_version is not None
        ]

        self.assertEqual(
            {entry.migration_version for entry in migration_entries},
            {version for version, _name, _function in MIGRATIONS},
        )
        timestamps = [
            datetime.strptime(entry.recorded_at_utc, TIMESTAMP_FORMAT)
            for entry in entries
        ]
        self.assertEqual(timestamps, sorted(timestamps, reverse=True))
        self.assertEqual(
            next(
                entry.action
                for entry in migration_entries
                if entry.migration_version == 25
            ),
            "Reverted",
        )
        expected = render_changelog_markdown(entries)
        actual = Path(main.__file__).with_name("changelog.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(actual, expected)
        self.assertTrue(actual.startswith("# Apokalypse Coder Bot — Changelog\n\n"))
        self.assertEqual(actual.count("\n## "), len(entries))

    def test_database_entries_include_current_installation_status(self) -> None:
        with main.connect_db() as db:
            entries = build_changelog_entries(db)

        migration_entries = [
            entry for entry in entries if entry.migration_version is not None
        ]
        self.assertTrue(migration_entries)
        self.assertTrue(all(entry.installed for entry in migration_entries))
        self.assertTrue(all(entry.applied_at_utc for entry in migration_entries))

    def test_authenticated_page_and_download_show_generated_changelog(self) -> None:
        user_id = self.create_user("changelog")
        request = self.authenticated_request(user_id, path="/changelog")
        request.state.csp_nonce = "test-changelog-nonce"

        page = main.changelog_page(request)
        document = page.body.decode("utf-8")
        self.assertIn('href="/api/changelog.md">Download Markdown</a>', document)

        self.assertIn("Migration-driven application changelog", document)
        self.assertIn("Higher-signal call checks and measured analysis work", document)
        self.assertIn("Migration 30: Analysis Signal Metrics", document)
        self.assertIn("Migration 31: Analysis Accuracy And Coverage", document)
        self.assertIn("Source-proven findings, broader local call coverage", document)
        self.assertIn("Migration 29: Active Job Elapsed Time", document)
        self.assertIn("Installed on this database", document)
        self.assertLess(
            document.index("Migration-driven application changelog"),
            document.index("Migration 29: Active Job Elapsed Time"),
        )

        download = main.download_changelog(
            self.authenticated_request(user_id, path="/api/changelog.md")
        )
        text = download.body.decode("utf-8")
        self.assertIn("# Apokalypse Coder Bot — Changelog", text)
        self.assertIn("Installed on this database:", text)
        self.assertEqual(
            download.headers["content-disposition"],
            'attachment; filename="changelog.md"',
        )
        self.assertEqual(download.headers["content-type"], "text/markdown; charset=utf-8")

    def test_chat_header_links_to_changelog(self) -> None:
        user_id = self.create_user("changelog-link")
        request = self.authenticated_request(user_id)
        request.state.csp_nonce = "test-home-nonce"
        response = main.home(request)
        self.assertIn('href="/changelog">Changelog</a>', response.body.decode("utf-8"))

    def test_chat_header_shows_escaped_configured_model(self) -> None:
        user_id = self.create_user("model-badge")
        request = self.authenticated_request(user_id)
        request.state.csp_nonce = "test-home-nonce"
        original_model = main.OLLAMA_MODEL
        main.OLLAMA_MODEL = "deepseek<coder>&v2"
        try:
            document = main.home(request).body.decode("utf-8")
        finally:
            main.OLLAMA_MODEL = original_model

        self.assertIn("LLM: <strong>deepseek&lt;coder&gt;&amp;v2</strong>", document)
        self.assertNotIn("deepseek<coder>&v2", document)
