from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import main
from app_config import current_ollama_model, use_ollama_model
from tests.helpers import DatabaseTestCase


MODEL_A = "Qwen2.5-Coder-7B-Instruct-Hybrid"
MODEL_B = (
    "DeepSeek-R1-Distill-Qwen-7B-awq-g128-int4-asym-bf16-onnx-ryzen-strix"
)


def lemonade_state(*, loaded: str = MODEL_A) -> dict[str, object]:
    return {
        "service": "lemonade",
        "loaded_model": loaded,
        "models": [
            {"name": MODEL_A, "recipe": "ryzenai-llm", "context_length": 65536},
            {"name": MODEL_B, "recipe": "ryzenai-llm", "context_length": 65536},
        ],
    }


class ModelSelectionTests(DatabaseTestCase):
    def privileged_user(self, suffix: str, *, admin: bool = False) -> int:
        user_id = self.create_user(suffix)
        if admin:
            with main.connect_db() as db:
                db.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user_id,))
        return user_id

    def test_environment_model_is_the_initial_fallback(self) -> None:
        self.assertEqual(main.selected_ollama_model(), main.OLLAMA_MODEL)

    def test_model_catalog_is_visible_but_only_admin_can_change_it(self) -> None:
        member_id = self.privileged_user("member")
        admin_id = self.privileged_user("admin", admin=True)
        with patch.object(main, "model_service_state", return_value=lemonade_state()):
            member = main.available_models(self.authenticated_request(member_id))
            admin = main.available_models(self.authenticated_request(admin_id))

        self.assertFalse(member["can_change"])
        self.assertTrue(admin["can_change"])
        self.assertEqual(admin["loaded_model"], MODEL_A)
        self.assertEqual([model["name"] for model in admin["models"]], [MODEL_A, MODEL_B])

    def test_admin_loads_and_persists_selected_model_with_audit_event(self) -> None:
        admin_id = self.privileged_user("admin", admin=True)
        request = self.authenticated_request(
            admin_id, method="PATCH", path="/api/admin/settings/model"
        )
        request.state.request_id = "select-model"
        with patch.object(
            main, "model_service_state", return_value=lemonade_state()
        ), patch.object(main, "load_lemonade_model") as load_model:
            result = main.set_admin_model(
                main.AdminModelSetting(model_name=MODEL_B), request
            )

        load_model.assert_called_once_with(MODEL_B)
        self.assertTrue(result["changed"])
        self.assertEqual(main.selected_ollama_model(), MODEL_B)
        with main.connect_db() as db:
            setting = db.execute(
                "SELECT value, updated_by_user_id FROM application_settings "
                "WHERE key = 'selected_model'"
            ).fetchone()
            event = db.execute(
                "SELECT action, request_id, details FROM admin_audit_events"
            ).fetchone()
        self.assertEqual(tuple(setting), (MODEL_B, admin_id))
        self.assertEqual((event["action"], event["request_id"]), ("set_model", "select-model"))
        self.assertEqual(json.loads(event["details"])["model_name"], MODEL_B)

    def test_model_change_is_rejected_while_a_job_is_active(self) -> None:
        admin_id = self.privileged_user("admin", admin=True)
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('chat', ?, 'Chat')",
                (admin_id,),
            )
            db.execute(
                "INSERT INTO chat_jobs(id, user_id, chat_id, status, model_name) "
                "VALUES ('job', ?, 'chat', 'queued', ?)",
                (admin_id, MODEL_A),
            )
        with patch.object(
            main, "model_service_state", return_value=lemonade_state()
        ), patch.object(main, "load_lemonade_model") as load_model:
            with self.assertRaises(HTTPException) as error:
                main.set_admin_model(
                    main.AdminModelSetting(model_name=MODEL_B),
                    self.authenticated_request(admin_id, method="PATCH"),
                )
        self.assertEqual(error.exception.status_code, 409)
        load_model.assert_not_called()

    def test_non_admin_cannot_change_the_model(self) -> None:
        member_id = self.privileged_user("member")
        with self.assertRaises(HTTPException) as error:
            main.set_admin_model(
                main.AdminModelSetting(model_name=MODEL_B),
                self.authenticated_request(member_id, method="PATCH"),
            )
        self.assertEqual(error.exception.status_code, 403)

    def test_new_chat_job_captures_selected_model(self) -> None:
        user_id = self.privileged_user("member")
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('chat', ?, 'Chat')",
                (user_id,),
            )
            db.execute(
                "UPDATE application_settings SET value = ? WHERE key = 'selected_model'",
                (MODEL_B,),
            )
        with patch.object(main, "enqueue_ollama_job"):
            main.chat(
                main.ChatRequest(chat_id="chat", message="hello"),
                self.authenticated_request(user_id, method="POST", path="/api/chat"),
            )
        with main.connect_db() as db:
            stored = db.execute("SELECT model_name FROM chat_jobs").fetchone()[0]
        self.assertEqual(stored, MODEL_B)

    def test_worker_model_context_is_scoped(self) -> None:
        self.assertEqual(current_ollama_model("fallback"), "fallback")
        with use_ollama_model(MODEL_B):
            self.assertEqual(current_ollama_model("fallback"), MODEL_B)
        self.assertEqual(current_ollama_model("fallback"), "fallback")

    def test_worker_uses_the_model_captured_on_the_job(self) -> None:
        user_id = self.privileged_user("worker")
        with main.connect_db() as db:
            db.execute(
                "INSERT INTO chat_histories(id, user_id, title) VALUES ('chat', ?, 'Chat')",
                (user_id,),
            )
            db.execute(
                """
                INSERT INTO chat_jobs(id, user_id, chat_id, status, model_name)
                VALUES ('job', ?, 'chat', 'queued', ?)
                """,
                (user_id, MODEL_B),
            )
        observed: list[str] = []

        def inspect_context(_job_id: str, _cancel_event: threading.Event) -> None:
            observed.append(current_ollama_model("fallback"))

        with patch.object(main, "process_active_chat_job", side_effect=inspect_context):
            main.process_chat_job("job", threading.Event())

        self.assertEqual(observed, [MODEL_B])
        self.assertEqual(current_ollama_model("fallback"), "fallback")

    def test_admin_header_has_dropdown_and_member_header_has_badge(self) -> None:
        admin_id = self.privileged_user("admin", admin=True)
        member_id = self.privileged_user("member")
        admin_request = self.authenticated_request(admin_id)
        admin_request.state.csp_nonce = "model-admin-nonce"
        member_request = self.authenticated_request(member_id)
        member_request.state.csp_nonce = "model-member-nonce"
        admin_page = main.home(admin_request).body.decode("utf-8")
        member_page = main.home(member_request).body.decode("utf-8")
        self.assertIn('id="model-selector"', admin_page)
        self.assertIn("/api/admin/settings/model", admin_page)
        self.assertNotIn('id="model-selector"', member_page)
        self.assertIn("LLM: <strong>", member_page)


if __name__ == "__main__":
    unittest.main()
