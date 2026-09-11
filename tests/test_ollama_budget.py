import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import analysis_engine
from ollama_budget import ContextBudgetExceeded, choose_analysis_context, ollama_context_scope
from tests.test_analysis import _StreamingResponse


def choose(content, **kwargs):
    return choose_analysis_context([{"role": "user", "content": content}], None, 4096,
                                   minimum=8192, maximum=65536, **kwargs)[0]


class OllamaBudgetTests(unittest.TestCase):
    def test_sizes_include_input_and_response_allowance(self):
        self.assertEqual(choose("small"), 8192)
        self.assertEqual(choose("x" * 12000), 32768)
        self.assertEqual(choose("x" * 40000), 65536)

    def test_unicode_schema_and_all_messages_count(self):
        messages = [{"role": "system", "content": "s" * 3000}, {"role": "user", "content": "界" * 3000}]
        size, estimate = choose_analysis_context(messages, {"description": "d" * 3000}, 8192, minimum=8192, maximum=65536)
        self.assertGreater(estimate, 15000)
        self.assertEqual(size, 32768)

    def test_scope_only_grows_and_resets_after_exception(self):
        with self.assertRaises(RuntimeError):
            with ollama_context_scope():
                self.assertEqual(choose("small"), 8192)
                self.assertEqual(choose("x" * 40000), 65536)
                self.assertEqual(choose("small"), 65536)
                raise RuntimeError("cancelled")
        self.assertEqual(choose("small"), 8192)

    def test_scopes_are_isolated_between_workers(self):
        with ollama_context_scope():
            choose("x" * 40000)
            with ThreadPoolExecutor(max_workers=1) as workers:
                self.assertEqual(workers.submit(choose, "small").result(), 8192)

    def test_over_budget_is_explicit_without_sending_a_request(self):
        with patch.object(analysis_engine.urllib.request, "urlopen") as request:
            with self.assertRaises(ContextBudgetExceeded):
                analysis_engine.ask_ollama([{"role": "user", "content": "x" * 100000}], adaptive_context=True)
            request.assert_not_called()

    def test_transport_sends_selected_tier_and_logs_actual_timings(self):
        requests = []
        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            return _StreamingResponse([b'{"message":{"content":"A complete response."},"done":true,"prompt_eval_count":125,"eval_count":6,"load_duration":1000000,"total_duration":5000000}\n'])
        with patch.object(analysis_engine.urllib.request, "urlopen", side_effect=response), \
             patch.object(analysis_engine, "OLLAMA_MODEL", "fixture"), \
             self.assertLogs("uvicorn.error", level="INFO") as logs:
            with ollama_context_scope():
                for content in ("small", "x" * 40000, "small"):
                    analysis_engine.ask_ollama([{"role": "user", "content": content}], num_predict=4096, adaptive_context=True)
        self.assertEqual([request["options"]["num_ctx"] for request in requests], [8192,65536,65536])
        self.assertTrue(any("prompt_tokens=125" in line and "load_ms=1.0" in line for line in logs.output))

    def test_fixed_mode_remains_available(self):
        requests = []
        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            return _StreamingResponse([b'{"message":{"content":"A complete response."},"done":true}\n'])
        with patch.object(analysis_engine.urllib.request, "urlopen", side_effect=response), \
             patch.object(analysis_engine, "OLLAMA_ADAPTIVE_ANALYSIS_CONTEXT", False), \
             patch.object(analysis_engine, "OLLAMA_CONTEXT_SIZE", 65536):
            analysis_engine.ask_ollama([{"role": "user", "content": "small"}], adaptive_context=True)
        self.assertEqual(requests[0]["options"]["num_ctx"], 65536)
