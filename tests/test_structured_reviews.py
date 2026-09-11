import io
import json
import unittest
from unittest.mock import patch

import analysis_engine as engine
from ollama_budget import ContextBudgetExceeded
from tests.test_analysis import _StreamingResponse
from tests.test_function_analysis import valid_result


TARGET = dict(language="python", file_path="sample.py", symbol_kind="function",
              qualified_name="target", start_line=1, end_line=2,
              source="def target(value):\n    return value", analysis_context="# Caller context")


class StructuredReviewTests(unittest.TestCase):
    def test_backend_startup_failure_stops_without_json_or_transport_retry(self):
        detail = "timed out waiting for llama-server to start - "
        failures = (
            engine.urllib.error.HTTPError(
                "http://127.0.0.1:11434/api/chat", 500, "Internal Server Error", {},
                io.BytesIO(json.dumps({"error": detail}).encode()),
            ),
            _StreamingResponse([json.dumps({"error": detail}).encode()]),
        )
        for failure in failures:
            with self.subTest(transport=type(failure).__name__):
                def respond(*_args, **_kwargs):
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                with patch.object(engine.urllib.request, "urlopen", side_effect=respond) as call:
                    with self.assertRaisesRegex(engine.OllamaUnavailableError, "timed out waiting"):
                        engine.request_function_analysis(**TARGET)
                self.assertEqual(call.call_count, 1)

    def test_unreachable_backend_and_idle_timeout_are_project_failures(self):
        for failure in (engine.urllib.error.URLError("connection refused"), TimeoutError("idle")):
            with self.subTest(error=type(failure).__name__):
                with patch.object(engine.urllib.request, "urlopen", side_effect=failure) as call:
                    with self.assertRaises(engine.OllamaUnavailableError):
                        engine.request_function_analysis(**TARGET)
                self.assertEqual(call.call_count, 1)

    def test_schema_rejection_remains_a_request_error(self):
        failure = _StreamingResponse([b'{"error":"invalid JSON schema for format"}'])
        with patch.object(engine.urllib.request, "urlopen", return_value=failure):
            with self.assertRaises(RuntimeError) as caught:
                engine.ask_ollama([{"role": "user", "content": "Review this function"}])
        self.assertNotIsInstance(caught.exception, engine.OllamaUnavailableError)

    def test_decoder_schema_avoids_large_repetitions_and_preserves_structural_constraints(self):
        for full in (engine.function_analysis_schema(), engine.function_analysis_batch_schema()):
            original = json.dumps(full, sort_keys=True)
            decoder = engine.ollama_decoding_schema(full)
            def compare(before, after):
                if isinstance(before, dict):
                    removed = {"maxLength"} if before.get("type") == "string" else {"maxItems"} if before.get("type") == "array" else set()
                    self.assertEqual(set(after), set(before) - removed)
                    for key in after:
                        compare(before[key], after[key])
                elif isinstance(before, list):
                    self.assertEqual(len(before), len(after))
                    for a, b in zip(before, after):
                        compare(a, b)
                else:
                    self.assertEqual(before, after)
            compare(full, decoder)
            self.assertEqual(json.dumps(full, sort_keys=True), original)
        oversized = valid_result().model_dump(); oversized["summary"] = "x" * 2001
        with self.assertRaises(ValueError):
            engine.normalize_function_analysis_payload(json.dumps(oversized))

    def test_transport_sends_decoder_schema_and_keeps_full_limits_in_prompt(self):
        requests = []
        def capture(request, **kwargs):
            requests.append(json.loads(request.data))
            return _StreamingResponse([b'{"message":{"content":"{}"},"done":true}\n'])
        schema = engine.function_analysis_schema()
        with patch.object(engine.urllib.request, "urlopen", side_effect=capture):
            engine.ask_ollama([{"role": "user", "content": "Review source"}], response_format=schema, adaptive_context=True)
        self.assertEqual(requests[0]["format"], engine.ollama_decoding_schema(schema))
        self.assertNotIn("maxLength", requests[0]["format"]["properties"]["summary"])
        self.assertIn('"maxLength":2000', requests[0]["messages"][0]["content"])

    def test_lemonade_hybrid_uses_native_openai_stream(self):
        requests = []

        def capture(request, **kwargs):
            requests.append((request.full_url, json.loads(request.data)))
            return _StreamingResponse([
                b": ping\n",
                b'data: {"choices":[{"delta":{"content":"{\\"ok\\":"},"finish_reason":null}]}\n',
                b'data: {"choices":[{"delta":{"content":"true}"},"finish_reason":"stop"}]}\n',
                b'data: {"choices":[{"delta":{},"finish_reason":null}],"usage":{"prompt_tokens":12,"completion_tokens":5}}\n',
                b"data: [DONE]\n",
            ])

        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        with patch.object(engine.urllib.request, "urlopen", side_effect=capture), patch.object(
            engine, "OLLAMA_MODEL", "Qwen2.5-Coder-7B-Instruct-Hybrid"
        ):
            result = engine.ask_ollama(
                [{"role": "user", "content": "Review source"}],
                response_format=schema,
                adaptive_context=True,
            )

        self.assertEqual(json.loads(result), {"ok": True})
        self.assertEqual(requests[0][0], "http://127.0.0.1:11434/v1/chat/completions")
        body = requests[0][1]
        self.assertNotIn("format", body)
        self.assertNotIn("options", body)
        self.assertEqual(body["max_completion_tokens"], engine.OLLAMA_MAX_OUTPUT_TOKENS)
        self.assertIn('"required":["ok"]', body["messages"][0]["content"])

    def test_lemonade_hybrid_uses_compact_semantic_instruction(self):
        requests = []

        def capture(request, **kwargs):
            requests.append(json.loads(request.data))
            return _StreamingResponse([
                b'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n',
                b"data: [DONE]\n",
            ])

        schema = {
            "type": "object",
            "properties": {
                key: {"type": "array"}
                for key in (
                    "behavior_claims", "parameter_inferences", "escaping_errors",
                    "side_effects", "issues",
                )
            },
        }
        with patch.object(engine.urllib.request, "urlopen", side_effect=capture), patch.object(
            engine, "OLLAMA_MODEL", "Qwen2.5-Coder-7B-Instruct-Hybrid"
        ):
            engine.ask_ollama(
                [{"role": "user", "content": "Prompt with compact output shape"}],
                response_format=schema,
                adaptive_context=True,
            )

        system = requests[0]["messages"][0]["content"]
        self.assertIn("compact semantic output shape", system)
        self.assertNotIn("OUTPUT JSON SCHEMA", system)

    def test_wire_schemas_require_arrays_and_nested_fields_without_changing_storage_model(self):
        for schema in (engine.function_analysis_schema(), engine.function_analysis_batch_schema()):
            contracts = [schema, *schema["$defs"].values()]
            for contract in contracts:
                if contract.get("type") == "object":
                    self.assertEqual(set(contract["required"]), set(contract["properties"]))
                    self.assertFalse(contract["additionalProperties"])
            self.assertIn("title", schema["$defs"]["FunctionIssue"]["properties"])
            analysis = schema["$defs"].get("FunctionAnalysisResult", schema)
            self.assertNotIn("review_status", analysis["properties"])
            self.assertEqual(analysis["properties"]["confidence"]["maximum"], 1)
        self.assertNotIn("parameters", engine.FunctionAnalysisResult.model_json_schema()["required"])

    def test_nested_summary_keeps_outer_contract_and_confidence_and_rejects_conflicts(self):
        payload = valid_result().model_dump()
        payload["summary"] = {"behavior": payload["summary"]}
        result = engine.normalize_function_analysis_payload(json.dumps(payload))
        self.assertEqual(result.confidence, .92)
        self.assertEqual(result.returns.possible_types[0].type, "int")
        payload["summary"]["confidence"] = .1
        with self.assertRaisesRegex(engine.FunctionAnalysisResponseError, "conflicting"):
            engine.normalize_function_analysis_payload(json.dumps(payload))

    def test_batch_does_not_fill_missing_parameter_review_from_model_defaults(self):
        payload = valid_result().model_dump()
        del payload["parameters"]
        with self.assertRaisesRegex(engine.FunctionAnalysisResponseError, "parameters"):
            engine.normalize_function_analysis_batch_payload(
                json.dumps({"results": [{"request_id": "one", "analysis": payload}]}),
                expected_request_ids=["one"],
            )

    def test_invalid_review_retries_once_with_full_source_and_specific_error(self):
        invalid = valid_result().model_dump(); invalid["confidence"] = 95
        with patch.object(engine, "ask_ollama", side_effect=[json.dumps(invalid), valid_result().model_dump_json()]) as ask:
            result = engine.request_function_analysis(**TARGET)
        self.assertEqual(result.confidence, .92)
        self.assertEqual(ask.call_count, 2)
        first, second = ask.call_args_list
        self.assertEqual(first.args[0][0], second.args[0][0])
        self.assertIn("confidence must be a number", second.args[0][-1]["content"])
        self.assertEqual(first.kwargs["num_predict"], second.kwargs["num_predict"])

    def test_complete_review_uses_one_request(self):
        with patch.object(engine, "ask_ollama", return_value=valid_result().model_dump_json()) as ask:
            engine.request_function_analysis(**TARGET)
        ask.assert_called_once()

    def test_incomplete_parameter_review_is_retried_and_never_promoted_after_second_failure(self):
        payload = valid_result().model_dump(); payload["parameters"] = [None]
        with patch.object(engine, "ask_ollama", return_value=json.dumps(payload)) as ask:
            result = engine.request_function_analysis(**TARGET)
        self.assertEqual(ask.call_count, 2)
        self.assertEqual(result.review_status, "partial")
        self.assertLessEqual(result.confidence, .5)

    def test_cancellation_and_context_budget_errors_do_not_trigger_repair_requests(self):
        cancelled = False
        def response(*args, **kwargs):
            nonlocal cancelled
            cancelled = True
            return "{}"
        with patch.object(engine, "ask_ollama", side_effect=response) as ask:
            with self.assertRaises(engine.AnalysisCancelled):
                engine.request_function_analysis(**TARGET, cancel_check=lambda: cancelled)
        ask.assert_called_once()
        with patch.object(engine, "ask_ollama", side_effect=ContextBudgetExceeded("too large")) as ask:
            with self.assertRaises(ContextBudgetExceeded):
                engine.request_function_analysis(**TARGET)
        ask.assert_called_once()

    def test_truncated_review_retries_with_larger_output_allowance(self):
        with patch.object(engine, "ask_ollama", side_effect=[engine.StructuredOutputTruncated("num_predict exhausted"), valid_result().model_dump_json()]) as ask:
            engine.request_function_analysis(**TARGET)
        self.assertGreater(ask.call_args_list[1].kwargs["num_predict"], ask.call_args_list[0].kwargs["num_predict"])

    def test_chunk_retry_keeps_the_same_fragment_and_boundaries(self):
        target = {key: value for key, value in TARGET.items() if key not in ("start_line", "end_line")}
        with patch.object(engine, "ask_ollama", side_effect=["{}", valid_result().model_dump_json()]) as ask:
            engine.request_function_chunk_analysis(**target, function_start_line=1, function_end_line=100,
                chunk_start_line=1, chunk_end_line=2, chunk_index=1, chunk_total=2)
        self.assertEqual(ask.call_args_list[0].args[0][0], ask.call_args_list[1].args[0][0])

    def test_transport_grounds_schema_keeps_json_retry_and_avoids_assistant_prefill(self):
        requests = []
        def response(request, **kwargs):
            requests.append(json.loads(request.data))
            if len(requests) == 1:
                return _StreamingResponse([b'{"message":{"content":"partial"},"done":false}\n'])
            return _StreamingResponse([b'{"message":{"content":"{\\"ok\\":true}"},"done":true,"done_reason":"stop"}\n'])
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
        with patch.object(engine.urllib.request, "urlopen", side_effect=response), patch.object(engine, "OLLAMA_MODEL", "gpt-oss:20b"):
            self.assertEqual(json.loads(engine.ask_ollama([{"role": "user", "content": "Review source"}], response_format=schema, adaptive_context=True)), {"ok": True})
        for request in requests:
            self.assertEqual(request["format"], schema)
            self.assertNotIn("assistant", [message["role"] for message in request["messages"]])
            system = request["messages"][0]["content"]
            self.assertIn('"required":["ok"]', system)
            self.assertNotIn("ordinary final-answer", system)
            self.assertEqual(request["options"]["temperature"], 0)
            self.assertEqual(request["options"]["repeat_penalty"], 1)

    def test_done_length_rejects_even_syntactically_valid_json(self):
        with patch.object(engine.urllib.request, "urlopen", return_value=_StreamingResponse([
            b'{"message":{"content":"{}"},"done":true,"done_reason":"length","eval_count":4096}\n'
        ])):
            with self.assertRaisesRegex(engine.StructuredOutputTruncated, "generated tokens=4096"):
                engine.ask_ollama([{"role": "user", "content": "Review source"}], response_format="json")
