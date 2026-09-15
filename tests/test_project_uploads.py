from __future__ import annotations

import asyncio
import io
import json
import stat
import unittest
import zipfile
from unittest.mock import patch

from fastapi import HTTPException
from starlette.datastructures import UploadFile

import main
import web_assets
from project_function_analysis import load_function_analysis_task
from project_uploads import (
    ProjectUploadError,
    ProjectUploadTooLarge,
    ProjectBundle,
    project_file,
    UploadLimits,
    ingest_folder,
    ingest_github_repository,
    ingest_files,
    ingest_zip,
    validate_github_repository_url,
)
from tests.helpers import DatabaseTestCase, run_asgi_response


def zip_bytes(entries: dict[str, bytes], *, compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return output.getvalue()


class SafeProjectIngestionTests(unittest.TestCase):
    def test_github_urls_require_direct_credential_free_https_urls(self) -> None:
        self.assertEqual(
            validate_github_repository_url(" https://github.com/acme/demo.git "),
            ("acme", "demo"),
        )
        rejected = (
            "http://github.com/acme/demo",
            "https://user:secret@github.com/acme/demo",
            "https://github.com:8443/acme/demo",
            "https://example.com/acme/demo",
            "https://github.com/acme/demo/tree/main",
            "https://github.com/acme/demo#main",
        )
        for url in rejected:
            with self.subTest(url=url), self.assertRaises(ProjectUploadError):
                validate_github_repository_url(url)

    def test_github_import_downloads_and_safely_ingests_a_source_archive(self) -> None:
        archive = zip_bytes({
            "acme-demo-abc123/README.md": b"# Demo\n",
            "acme-demo-abc123/src/main.py": b"print(1)\n",
            "acme-demo-abc123/node_modules/pkg.js": b"ignored",
        })
        with patch("project_uploads._download_github_archive", return_value=archive) as download:
            bundle = ingest_github_repository("https://github.com/acme/demo", self.limits)

        self.assertEqual(bundle.name, "demo")
        self.assertEqual(bundle.source_kind, "folder")
        self.assertEqual(bundle.skipped_files, 1)
        self.assertEqual(
            [(item.path, item.content) for item in bundle.files],
            [("README.md", b"# Demo\n"), ("src/main.py", b"print(1)\n")],
        )
        download.assert_called_once_with("acme", "demo", self.limits.max_archive_bytes)

    def test_individual_files_are_flat_named_and_checked(self) -> None:
        single = ingest_files([("main.py", io.BytesIO(b"print('hello')\n"))], self.limits)
        self.assertEqual(single.name, "main.py")
        self.assertEqual(single.files[0].path, "main.py")
        multiple = ingest_files([("main.py", io.BytesIO(b"one")), ("helper.js", io.BytesIO(b"two"))], self.limits)
        self.assertEqual(multiple.name, "Uploaded files (2)")
        self.assertEqual(multiple.total_bytes, 6)
        for filename in ("../outside.py", "folder/main.py", "C:\\outside.py", ""):
            with self.subTest(filename=filename), self.assertRaises(ProjectUploadError):
                ingest_files([(filename, io.BytesIO(b"unsafe"))], self.limits)
        with self.assertRaises(ProjectUploadError):
            ingest_files([("main.py", io.BytesIO(b"one")), ("main.py", io.BytesIO(b"two"))], self.limits)
        with self.assertRaises(ProjectUploadError):
            ingest_files([], self.limits)
        with self.assertRaises(ProjectUploadTooLarge):
            ingest_files([("main.py", io.BytesIO(b"123456"))], UploadLimits(10, 5, 5, 1, 100))

    def setUp(self) -> None:
        self.limits = UploadLimits(
            max_archive_bytes=1_000_000,
            max_expanded_bytes=2_000_000,
            max_file_bytes=1_000_000,
            max_files=20,
            max_compression_ratio=100,
        )

    def test_zip_is_expanded_in_memory_with_hashes_and_ignored_directories(self) -> None:
        bundle = ingest_zip(
            io.BytesIO(
                zip_bytes(
                    {
                        "demo/main.py": b"print('safe')\n",
                        "demo/src/tool.py": b"def tool():\n    return 1\n",
                        "demo/node_modules/package/index.js": b"ignored",
                    }
                )
            ),
            "demo.zip",
            self.limits,
        )

        self.assertEqual(bundle.name, "demo")
        self.assertEqual(bundle.source_kind, "zip")
        self.assertEqual(bundle.skipped_files, 1)
        self.assertEqual([item.path for item in bundle.files], ["demo/main.py", "demo/src/tool.py"])
        self.assertEqual(len(bundle.files[0].sha256), 64)
        self.assertFalse(bundle.files[0].is_binary)

    def test_zip_rejects_traversal_links_and_compression_bombs(self) -> None:
        with self.assertRaisesRegex(ProjectUploadError, "relative|components"):
            ingest_zip(
                io.BytesIO(zip_bytes({"../outside.py": b"bad"})),
                "bad.zip",
                self.limits,
            )

        linked = io.BytesIO()
        with zipfile.ZipFile(linked, "w") as archive:
            info = zipfile.ZipInfo("project/link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        with self.assertRaisesRegex(ProjectUploadError, "links and special files"):
            ingest_zip(io.BytesIO(linked.getvalue()), "linked.zip", self.limits)

        bomb_limits = UploadLimits(1_000_000, 2_000_000, 1_000_000, 20, 5)
        with self.assertRaisesRegex(ProjectUploadError, "compression ratio"):
            ingest_zip(
                io.BytesIO(zip_bytes({"project/repeated.txt": b"A" * 100_000})),
                "bomb.zip",
                bomb_limits,
            )

    def test_folder_limits_files_and_total_content(self) -> None:
        small_limits = UploadLimits(100, 5, 5, 1, 100)
        with self.assertRaises(ProjectUploadTooLarge):
            ingest_folder(
                [("project/one.py", io.BytesIO(b"123")), ("project/two.py", io.BytesIO(b"4"))],
                small_limits,
            )
        with self.assertRaises(ProjectUploadTooLarge):
            ingest_folder(
                [("project/one.py", io.BytesIO(b"123456"))],
                small_limits,
            )


class ProjectUploadApiTests(DatabaseTestCase):
    def test_github_repository_import_uses_the_existing_project_pipeline(self) -> None:
        user_id = self.create_user("github-project")
        chat_id = self.create_chat(user_id, "github-project-chat")
        bundle = ProjectBundle(
            "demo",
            "folder",
            (project_file("src/main.py", b"def main():\n    return 1\n"),),
            25,
            3,
        )
        with patch("main.ingest_github_repository", return_value=bundle) as importer:
            result = asyncio.run(main.upload_project(
                request=self.authenticated_request(
                    user_id, method="POST", path="/api/projects"
                ),
                chat_id=chat_id,
                source_kind="github",
                repository_url="https://github.com/acme/demo",
            ))

        importer.assert_called_once_with(
            "https://github.com/acme/demo",
            main.PROJECT_UPLOAD_LIMITS,
        )
        self.assertEqual(result["name"], "demo")
        self.assertEqual(result["file_count"], 1)
        self.assertEqual(result["primary_language"], "python")
        with main.connect_db() as db:
            project = db.execute(
                "SELECT source_kind, skipped_file_count FROM projects WHERE id = ?",
                (result["id"],),
            ).fetchone()
            batch = db.execute(
                "SELECT source_kind, name FROM project_upload_batches WHERE project_id = ?",
                (result["id"],),
            ).fetchone()
        self.assertEqual(tuple(project), ("folder", 3))
        self.assertEqual(tuple(batch), ("folder", "demo (GitHub)"))

    def test_real_multipart_github_request_reaches_upload_endpoint_without_files(self) -> None:
        user_id = self.create_user("multipart-github")
        chat_id = self.create_chat(user_id, "multipart-github-chat")
        request = self.authenticated_request(user_id)
        boundary = "github-import-boundary"
        repository_url = "https://github.com/acme/demo"
        parts = [
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()
            for name, value in (
                ("chat_id", chat_id),
                ("source_kind", "github"),
                ("repository_url", repository_url),
            )
        ]
        parts.append(f"--{boundary}--\r\n".encode())
        bundle = ProjectBundle(
            "demo",
            "folder",
            (project_file("main.py", b"def main():\n    return 1\n"),),
            25,
            0,
        )
        with patch("main.ingest_github_repository", return_value=bundle) as importer:
            status, _, body = run_asgi_response(
                method="POST",
                path="/api/projects",
                host="127.0.0.1:8000",
                origin="http://127.0.0.1:8000",
                body=b"".join(parts),
                content_type=f"multipart/form-data; boundary={boundary}",
                cookie=request.headers["cookie"],
            )
        self.assertEqual(status, 201, body.decode(errors="replace"))
        self.assertEqual(json.loads(body)["name"], "demo")
        importer.assert_called_once_with(repository_url, main.PROJECT_UPLOAD_LIMITS)

    def test_real_multipart_file_selection_is_persisted_and_indexed(self) -> None:
        user_id = self.create_user("multipart-files")
        chat_id = self.create_chat(user_id)
        request = self.authenticated_request(user_id)
        cookie = request.headers["cookie"]
        for selected in (
            [("main.py", b"def main():\n    return 1\n")],
            [("main.py", b"def main():\n    return 1\n"), ("helper.js", b"function helper() { return 2; }\n")],
        ):
            with self.subTest(count=len(selected)):
                boundary = "file-selection-boundary"
                parts = [
                    f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                    for name, value in (("chat_id", chat_id), ("source_kind", "files"))
                ]
                if len(selected) > 1:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="project_name"\r\n\r\n  My analysis engine  \r\n'.encode())
                for filename, content in selected:
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode() + content + b"\r\n")
                parts.append(f"--{boundary}--\r\n".encode())
                status, _, body = run_asgi_response(
                    method="POST", path="/api/projects", host="127.0.0.1:8000", origin="http://127.0.0.1:8000",
                    body=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}", cookie=cookie,
                )
                self.assertEqual(status, 201, body.decode(errors="replace"))
                result = json.loads(body)
                self.assertEqual(result["file_count"], len(selected))
                self.assertEqual(result["name"], "main.py" if len(selected) == 1 else "My analysis engine")
                self.assertEqual(result["function_analysis_total_count"], len(selected))
                with main.connect_db() as db:
                    rows = db.execute("SELECT path, content FROM project_files WHERE project_id = ? ORDER BY id", (result["id"],)).fetchall()
                self.assertEqual([(row["path"], bytes(row["content"])) for row in rows], selected)

    def test_custom_project_name_survives_append_for_every_upload_kind(self) -> None:
        user_id = self.create_user("named-projects")
        chat_id = self.create_chat(user_id)
        request = self.authenticated_request(user_id, method="POST", path="/api/projects")
        for kind in ("files", "folder", "zip"):
            with self.subTest(kind=kind):
                content = b"def main(): return 1\n"
                payload = zip_bytes({"src/main.py": content}) if kind == "zip" else content
                project = asyncio.run(main.upload_project(
                    request=request, chat_id=chat_id, source_kind=kind,
                    files=[UploadFile(file=io.BytesIO(payload), filename="source.zip" if kind == "zip" else "main.py")],
                    relative_paths=json.dumps(["src/main.py"]), project_name="  Engine comparison  ",
                ))
                appended = asyncio.run(main.upload_project(
                    request=request, chat_id=chat_id, source_kind="files", project_id=project["id"],
                    files=[UploadFile(file=io.BytesIO(b"def helper(): return 2"), filename="helper.py")],
                    project_name="Must not rename an existing project",
                ))
                self.assertEqual(appended["name"], "Engine comparison")
                self.assertEqual(appended["file_count"], 2)
                with main.connect_db() as db:
                    self.assertEqual(db.execute("SELECT name FROM projects WHERE id=?", (project["id"],)).fetchone()[0], "Engine comparison")
                    batch_names = [row[0] for row in db.execute("SELECT name FROM project_upload_batches WHERE project_id=?", (project["id"],))]
                self.assertNotIn("Engine comparison", batch_names)

    def test_project_can_be_renamed_only_by_its_owner(self) -> None:
        user_id = self.create_user("rename-project")
        chat_id = self.create_chat(user_id, "rename-project-chat")
        project = asyncio.run(main.upload_project(
            request=self.authenticated_request(user_id, method="POST", path="/api/projects"),
            chat_id=chat_id,
            source_kind="files",
            files=[UploadFile(file=io.BytesIO(b"def main(): return 1\n"), filename="main.py")],
            project_name="Before",
        ))
        renamed = main.rename_project(
            str(project["id"]),
            main.RenameProjectRequest(name="  After / Display name  "),
            self.authenticated_request(
                user_id, method="PATCH", path=f"/api/projects/{project['id']}"
            ),
        )
        self.assertEqual(renamed["project"]["name"], "Display name")
        stranger_id = self.create_user("rename-project-stranger")
        with self.assertRaises(HTTPException) as hidden:
            main.rename_project(
                str(project["id"]),
                main.RenameProjectRequest(name="Stolen"),
                self.authenticated_request(
                    stranger_id, method="PATCH", path=f"/api/projects/{project['id']}"
                ),
            )
        self.assertEqual(hidden.exception.status_code, 404)

    def test_file_selection_rejects_duplicates_and_respects_storage_quota(self) -> None:
        user_id = self.create_user("files-limits")
        chat_id = self.create_chat(user_id)
        request = self.authenticated_request(user_id, method="POST", path="/api/projects")
        uploads = [UploadFile(file=io.BytesIO(b"123"), filename="same.py") for _ in range(2)]
        with self.assertRaises(HTTPException) as rejected:
            asyncio.run(main.upload_project(request=request, chat_id=chat_id, source_kind="files", files=uploads))
        self.assertEqual(rejected.exception.status_code, 422)
        self.assertTrue(all(upload.file.closed for upload in uploads))
        with main.connect_db() as db:
            db.execute("UPDATE users SET storage_limit_bytes = 2 WHERE id = ?", (user_id,))
        with self.assertRaises(HTTPException) as rejected:
            asyncio.run(main.upload_project(request=request, chat_id=chat_id, source_kind="files", files=[UploadFile(file=io.BytesIO(b"123"), filename="main.py")]))
        self.assertEqual(rejected.exception.status_code, 413)
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)

    def create_chat(self, user_id: int, chat_id: str = "project-chat") -> str:
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES (?, ?, 'Project')",
                (chat_id, user_id),
            )
        return chat_id

    def test_folder_upload_is_owned_persisted_listed_and_deletable(self) -> None:
        user_id = self.create_user("project")
        chat_id = self.create_chat(user_id)
        request = self.authenticated_request(user_id, method="POST", path="/api/projects")
        uploads = [
            UploadFile(file=io.BytesIO(b"print('hello')\n"), filename="main.py"),
            UploadFile(file=io.BytesIO(b"def helper():\n    return 1\n"), filename="helper.py"),
        ]
        result = asyncio.run(
            main.upload_project(
                request=request,
                chat_id=chat_id,
                source_kind="folder",
                files=uploads,
                relative_paths=json.dumps(["demo/main.py", "demo/src/helper.py"]),
            )
        )

        self.assertEqual(result["name"], "demo")
        self.assertEqual(result["file_count"], 2)
        self.assertEqual(result["primary_language"], "python")
        self.assertEqual(result["inventory_status"], "completed")
        self.assertEqual(result["parser_status"], "completed")
        self.assertEqual(result["parser_parsed_file_count"], 2)
        self.assertEqual(result["structure_status"], "completed")
        self.assertEqual(result["indexed_file_count"], 2)
        self.assertEqual(result["definition_count"], 1)
        self.assertEqual(result["function_analysis_status"], "pending")
        self.assertEqual(result["function_analysis_total_count"], 1)
        self.assertEqual(result["languages"][0]["language"], "python")
        with main.connect_db() as db:
            rows = db.execute(
                "SELECT path, content, size_bytes, sha256 FROM project_files ORDER BY path"
            ).fetchall()
        self.assertEqual([row["path"] for row in rows], ["demo/main.py", "demo/src/helper.py"])
        self.assertEqual(bytes(rows[0]["content"]), b"print('hello')\n")
        self.assertEqual(rows[0]["size_bytes"], len(rows[0]["content"]))
        self.assertEqual(len(rows[0]["sha256"]), 64)

        loaded = main.load_chat(
            chat_id,
            self.authenticated_request(user_id, path=f"/api/chats/{chat_id}"),
        )
        self.assertEqual([project["id"] for project in loaded["projects"]], [result["id"]])
        inventory = main.get_project_inventory(
            str(result["id"]),
            self.authenticated_request(user_id, path=f"/api/projects/{result['id']}"),
        )
        self.assertEqual(inventory["primary_language"], "python")
        self.assertEqual([item["path"] for item in inventory["files"]], [
            "demo/main.py",
            "demo/src/helper.py",
        ])
        self.assertEqual(inventory["files"][0]["language"], "python")
        self.assertEqual(inventory["files"][0]["detection_method"], "extension")
        self.assertEqual(inventory["files"][0]["is_entrypoint_candidate"], 1)
        self.assertEqual(inventory["files"][0]["parser_status"], "parsed")
        self.assertEqual(inventory["files"][0]["parser_adapter"], "python")
        self.assertEqual(inventory["files"][0]["parser_diagnostics"], [])
        self.assertEqual(inventory["files"][0]["structure_status"], "indexed")
        self.assertEqual(
            {adapter["language"] for adapter in inventory["adapters"]},
            {
                "python", "cpp", "csharp", "javascript", "typescript",
                "rust", "shell", "powershell", "html", "sql",
            },
        )
        with main.connect_db() as db:
            project_id = str(result["id"])
            file_id = int(
                db.execute(
                    "SELECT id FROM project_files WHERE project_id = ? AND path = 'demo/src/helper.py'",
                    (project_id,),
                ).fetchone()[0]
            )
            symbol_id = int(
                db.execute(
                    "SELECT id FROM project_symbols WHERE project_id = ? AND qualified_name = 'helper'",
                    (project_id,),
                ).fetchone()[0]
            )
            task = load_function_analysis_task(db, symbol_id)
            db.execute(
                """
                INSERT INTO project_symbol_analyses(
                    symbol_id, project_id, file_id, contract_version, model_name,
                    source_sha256, response_json, summary, syntax_valid,
                    may_return_value, return_nullable, return_description,
                    raised_errors_json, side_effects_json, confidence
                ) VALUES (?, ?, ?, '1.0', 'test', ?, '{}', 'summary', 1, 1, 0,
                          'returns', '[]', '[]', 0.9)
                """,
                (symbol_id, project_id, file_id, rows[1]["sha256"]),
            )
            db.execute(
                """
                INSERT INTO function_analysis_cache(
                    user_id, language, function_sha256, contract_version, model_name,
                    response_json, last_used_at
                ) VALUES (?, 'python', ?, '1.0', ?, '{}', CURRENT_TIMESTAMP)
                """,
                (user_id, task.function_sha256, main.OLLAMA_MODEL),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, project_id, job_kind, status)
                VALUES ('old-project-job', ?, ?, ?, 'project_analysis', 'completed')
                """,
                (user_id, chat_id, project_id),
            )
            main.JOB_CANCEL_EVENTS["old-project-job"] = main.threading.Event()
            main.JOB_PAUSE_EVENTS["old-project-job"] = main.threading.Event()

        deleted = main.delete_project(
            str(result["id"]),
            self.authenticated_request(
                user_id, method="DELETE", path=f"/api/projects/{result['id']}"
            ),
        )
        self.assertEqual(deleted, {"message": "Cleared demo and 1 cached function result"})
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_files").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbols").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_dependencies").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_calls").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_analyses").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_issues").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_parameters").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_symbol_return_types").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_call_arguments").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_call_compatibility").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_call_findings").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_languages").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM function_analysis_cache").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM chat_jobs").fetchone()[0], 0)
        self.assertNotIn("old-project-job", main.JOB_CANCEL_EVENTS)
        self.assertNotIn("old-project-job", main.JOB_PAUSE_EVENTS)

    def test_real_multipart_zip_request_reaches_upload_endpoint(self) -> None:
        user_id = self.create_user("multipart-project")
        chat_id = self.create_chat(user_id, "multipart-chat")
        token = "multipart-session-token"
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO login_sessions(user_id, token_hash, expires_at) VALUES (?, ?, ?)",
                (user_id, main.token_digest(token), int(main.time.time()) + 300),
            )
        boundary = "apokalypse-test-boundary"
        parts: list[bytes] = []
        for name, value in (
            ("chat_id", chat_id),
            ("source_kind", "zip"),
            ("relative_paths", "[]"),
        ):
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode()
            )
        parts.append(
            (
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="files"; filename="demo.zip"\r\n'
                "Content-Type: application/zip\r\n\r\n"
            ).encode()
            + zip_bytes({"demo/main.py": b"print('multipart')\n"})
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        status, _headers, response_body = run_asgi_response(
            method="POST",
            path="/api/projects",
            host="127.0.0.1:8000",
            origin="http://127.0.0.1:8000",
            body=b"".join(parts),
            content_type=f"multipart/form-data; boundary={boundary}",
            cookie=f"{main.SESSION_COOKIE}={token}",
        )
        self.assertEqual(status, 201, response_body.decode(errors="replace"))
        self.assertEqual(json.loads(response_body)["name"], "demo")
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_files").fetchone()[0], 1)

    def test_upload_obeys_account_storage_limit_and_chat_cascade(self) -> None:
        user_id = self.create_user("quota-project")
        chat_id = self.create_chat(user_id, "quota-chat")
        with main.connect_db() as db:
            db.execute("UPDATE users SET storage_limit_bytes = 3 WHERE id = ?", (user_id,))
        request = self.authenticated_request(user_id, method="POST", path="/api/projects")
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(
                main.upload_project(
                    request=request,
                    chat_id=chat_id,
                    source_kind="folder",
                    files=[UploadFile(file=io.BytesIO(b"1234"), filename="main.py")],
                    relative_paths=json.dumps(["demo/main.py"]),
                )
            )
        self.assertEqual(raised.exception.status_code, 413)

        with main.connect_db() as db:
            db.execute("UPDATE users SET storage_limit_bytes = NULL WHERE id = ?", (user_id,))
        uploaded = asyncio.run(
            main.upload_project(
                request=self.authenticated_request(user_id, method="POST", path="/api/projects"),
                chat_id=chat_id,
                source_kind="folder",
                files=[UploadFile(file=io.BytesIO(b"1234"), filename="main.py")],
                relative_paths=json.dumps(["demo/main.py"]),
            )
        )
        self.assertTrue(uploaded["id"])
        main.delete_chat(
            chat_id,
            self.authenticated_request(user_id, method="DELETE", path=f"/api/chats/{chat_id}"),
        )
        with main.connect_db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM project_files").fetchone()[0], 0)

    def test_composer_contains_files_folder_zip_and_github_attachment_controls(self) -> None:
        self.assertGreaterEqual(web_assets.HTML.count("Apokalypse Code Analysis System"), 2)
        self.assertNotIn('class="sidebar"', web_assets.HTML)
        self.assertNotIn('id="new-chat"', web_assets.HTML)
        self.assertNotIn('id="mode-picker"', web_assets.HTML)
        self.assertNotIn('id="analyse-mode"', web_assets.HTML)
        self.assertIn("function selectedRequestMode(){return 'analyse'}", web_assets.HTML)
        self.assertIn(".app{height:100vh;display:grid;grid-template-columns:minmax(0,1fr)}", web_assets.HTML)
        self.assertIn(".app{grid-template-rows:minmax(0,1fr)}", web_assets.HTML)
        self.assertIn("controls.className='project-chip-actions'", web_assets.HTML)
        self.assertIn("justify-content:flex-end", web_assets.HTML)
        self.assertIn('id="attachment-button"', web_assets.HTML)
        self.assertIn('id="choose-files"', web_assets.HTML)
        self.assertIn('<input id="files-input" type="file" multiple hidden>', web_assets.HTML)
        self.assertIn("uploadProjectFiles('files',filesInput.files)", web_assets.HTML)
        self.assertIn("filesInput.value=''", web_assets.HTML)
        self.assertIn('id="folder-input"', web_assets.HTML)
        self.assertIn("webkitdirectory", web_assets.HTML)
        self.assertIn('id="zip-input"', web_assets.HTML)
        self.assertIn('id="choose-github"', web_assets.HTML)
        self.assertIn('id="github-import-modal"', web_assets.HTML)
        self.assertIn("Git is not required", web_assets.HTML)
        self.assertIn("uploadGitHubRepository(repositoryUrl)", web_assets.HTML)
        self.assertIn("formData.append('source_kind','github')", web_assets.HTML)
        self.assertIn(
            "projectUploading=false;if(currentChatId===requestedChatId)",
            web_assets.HTML,
        )
        self.assertIn("Wait for the current project change to finish.", web_assets.HTML)
        self.assertIn("/api/projects", web_assets.HTML)
        self.assertIn("/analysis-jobs", web_assets.HTML)
        self.assertIn("project-chip-action", web_assets.HTML)
        self.assertIn("progress_function_current", web_assets.HTML)
        self.assertIn('id="project-report-modal"', web_assets.HTML)
        all_results = '<option value="all">All Results</option>'
        actionable = '<option value="problems">Actionable Findings</option>'
        self.assertLess(web_assets.HTML.index(all_results), web_assets.HTML.index(actionable))
        self.assertIn("projectReportFilter.value='all'", web_assets.HTML)
        self.assertIn("/analysis-report", web_assets.HTML)
        self.assertIn("Call compatibility", web_assets.HTML)
        self.assertIn("reused from cache", web_assets.HTML)
        self.assertIn("Reset Analysis", web_assets.HTML)
        self.assertIn("/analysis-cache", web_assets.HTML)
        self.assertIn("Purge Cache", web_assets.HTML)
        self.assertIn("/function-analysis-cache", web_assets.HTML)
        self.assertIn("Clear Project", web_assets.HTML)
        self.assertIn("files, hashes, parse data, and analysis results", web_assets.HTML)
        self.assertNotIn("project-chip-remove", web_assets.HTML)

    def test_composer_orders_job_status_project_status_and_text_entry(self) -> None:
        job_status_index = web_assets.HTML.index('id="job-status"')
        project_status_index = web_assets.HTML.index('id="project-upload-status"')
        text_entry_index = web_assets.HTML.index('id="message"')

        self.assertLess(job_status_index, project_status_index)
        self.assertLess(project_status_index, text_entry_index)
        self.assertIn("item.append(lines,head)", web_assets.HTML)
        self.assertIn("pause-generation", web_assets.HTML)
        self.assertIn("/pause", web_assets.HTML)
        self.assertIn("/resume", web_assets.HTML)
        self.assertIn("grid-template-areas:\"job job\" \"projects projects\" \"status status\" \"meta meta\" \"input send\"", web_assets.HTML)
        self.assertIn("grid-area:send", web_assets.HTML)
        self.assertIn("grid-column:1/-1", web_assets.HTML)
        self.assertIn("height:104px", web_assets.HTML)
        self.assertIn("height:92px", web_assets.HTML)
        self.assertNotIn("@media(min-width:821px) and (min-height:900px)", web_assets.HTML)
        self.assertIn(".chat-main:has(.job-status:not([hidden])) #chat", web_assets.HTML)
        self.assertIn(".chat-main:has(.job-status:not([hidden])) .message-form{margin-top:auto}", web_assets.HTML)
        self.assertNotIn(".chat-main:has(.job-status:not([hidden])) .message-form{flex:1 1 auto", web_assets.HTML)
        self.assertIn(".chat-main:has(.job-status:not([hidden])) .job-status .thinking{max-height:min(52dvh,620px)}", web_assets.HTML)
        self.assertIn(".chat-main:has(.job-status:not([hidden])) .job-status .thinking{max-height:36dvh}", web_assets.HTML)
        self.assertIn("thinking-label", web_assets.HTML)
        self.assertIn("Project results ", web_assets.HTML)
        self.assertIn("Source position ", web_assets.HTML)
        self.assertIn("(dependency ordered)", web_assets.HTML)
        self.assertIn("white-space:nowrap", web_assets.HTML)
        self.assertIn("00:00:00", web_assets.HTML)
        self.assertIn("Math.floor(seconds/3600)", web_assets.HTML)
        self.assertIn("thinking.paused?thinking.pausedElapsed", web_assets.HTML)
        self.assertIn("container.scrollTop=container.scrollHeight", web_assets.HTML)
        self.assertIn("function projectProgressStatus", web_assets.HTML)
        self.assertIn("'File '+Number(data?.progress_file_current||0)+' of '+Number(data?.progress_file_total||0)+' : '", web_assets.HTML)
        self.assertIn("'\\n'+fn", web_assets.HTML)
        self.assertIn("completed-with-errors", web_assets.HTML)
        self.assertIn("completed-with-warnings", web_assets.HTML)
        self.assertIn("hasError?'completed-with-errors':hasWarning?'completed-with-warnings'", web_assets.HTML)
        self.assertIn("max-height:26vh", web_assets.HTML)
        self.assertIn("overflow-y:auto", web_assets.HTML)
        self.assertIn(".message-form{display:grid", web_assets.HTML)
        self.assertIn("@media(max-width:820px),(orientation:portrait)", web_assets.HTML)
        self.assertIn("height:100dvh", web_assets.HTML)
        self.assertIn("grid-template-columns:22px minmax(0,1fr) auto auto auto", web_assets.HTML)
        self.assertIn("max-height:24dvh", web_assets.HTML)


if __name__ == "__main__":
    unittest.main()
