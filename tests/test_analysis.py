import threading
import time
import unittest
import json

import analysis_engine
from analysis_engine import EvidenceFinding, EvidenceReview, VerifiedFeature


class _StreamingResponse:
    def __init__(self, lines):
        self._lines = iter(lines)
        self.closed = False
        self.read_count = 0

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self._lines)
        self.read_count += 1
        return value

    def close(self):
        self.closed = True


class AnalysisEngineTests(unittest.TestCase):
    def setUp(self):
        self.original_urlopen = analysis_engine.urllib.request.urlopen
        self.original_ask_ollama = analysis_engine.ask_ollama

    def tearDown(self):
        analysis_engine.urllib.request.urlopen = self.original_urlopen
        analysis_engine.ask_ollama = self.original_ask_ollama

    def test_ask_ollama_accepts_only_a_complete_stream(self):
        calls = []

        def fake_urlopen(_request, timeout):
            calls.append(timeout)
            return _StreamingResponse(
                [
                    b'{"message":{"content":"hello "},"done":false}\n',
                    b'{"message":{"content":"world"},"done":true}\n',
                ]
            )

        analysis_engine.urllib.request.urlopen = fake_urlopen

        result = analysis_engine.ask_ollama([{"role": "user", "content": "test prompt"}])

        self.assertEqual(result, "hello world")
        self.assertEqual(len(calls), 1)

    def test_ask_ollama_retries_then_rejects_truncated_streams(self):
        calls = []

        def fake_urlopen(_request, timeout):
            calls.append(timeout)
            return _StreamingResponse(
                [b'{"message":{"content":"partial"},"done":false}\n']
            )

        analysis_engine.urllib.request.urlopen = fake_urlopen

        with self.assertRaisesRegex(RuntimeError, "terminal completion event"):
            analysis_engine.ask_ollama(
                [{"role": "user", "content": "test prompt"}]
            )

        self.assertEqual(len(calls), 2)

    def test_ask_ollama_can_be_cancelled_while_connecting(self):
        cancel_event = threading.Event()
        connection_started = threading.Event()
        release_connection = threading.Event()

        def slow_urlopen(_request, timeout):
            connection_started.set()
            release_connection.wait(timeout=2)
            return _StreamingResponse(
                [b'{"message":{"content":"late"},"done":true}\n']
            )

        analysis_engine.urllib.request.urlopen = slow_urlopen

        def cancel_after_connection_starts():
            self.assertTrue(connection_started.wait(timeout=1))
            cancel_event.set()

        canceller = threading.Thread(target=cancel_after_connection_starts)
        canceller.start()
        started = time.monotonic()
        try:
            with self.assertRaises(analysis_engine.AnalysisCancelled):
                analysis_engine.ask_ollama(
                    [{"role": "user", "content": "test prompt"}],
                    cancel_check=cancel_event.is_set,
                )
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            release_connection.set()
            canceller.join(timeout=1)

    def test_function_request_deadline_interrupts_a_stream(self):
        release_stream = threading.Event()

        class BlockingResponse(_StreamingResponse):
            def __next__(self):
                release_stream.wait(timeout=2)
                raise StopIteration

            def close(self):
                self.closed = True
                release_stream.set()

        response = BlockingResponse([])
        analysis_engine.urllib.request.urlopen = lambda _request, timeout: response

        started = time.monotonic()
        with self.assertRaisesRegex(
            analysis_engine.OllamaRequestDeadlineExceeded,
            "request limit",
        ):
            analysis_engine.ask_ollama(
                [{"role": "user", "content": "test prompt"}],
                request_timeout=0.05,
            )

        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(response.closed)

    def test_repetition_detection_does_not_reject_normal_source_code(self):
        normal_source = "\n".join(
            f"def function_{index}(value): return value + {index}"
            for index in range(100)
        )

        self.assertFalse(analysis_engine.response_is_degenerate(normal_source))
        self.assertTrue(analysis_engine.response_is_degenerate("a" * 3000))
        self.assertTrue(analysis_engine.response_is_degenerate("repeat me " * 800))

    def test_ask_ollama_interrupts_repetitive_stream_before_full_output(self):
        responses = []
        line = (json.dumps({
            "message": {"content": "repeat me " * 25},
            "done": False,
        }) + "\n").encode()

        def fake_urlopen(_request, timeout):
            response = _StreamingResponse([line] * 200)
            responses.append(response)
            return response

        analysis_engine.urllib.request.urlopen = fake_urlopen

        with self.assertRaisesRegex(RuntimeError, "repetitive, unusable response twice"):
            analysis_engine.ask_ollama([{"role": "user", "content": "test prompt"}])

        self.assertEqual(len(responses), 2)
        self.assertTrue(all(response.closed for response in responses))
        self.assertTrue(all(response.read_count < 30 for response in responses))

    def test_evidence_validation_keeps_supported_items_and_rejects_bogus_ones(self):
        source = (
            "def add(left, right):\n"
            "    result = left + right\n"
            "    return result\n"
        )
        _inventory, source_facts = analysis_engine.build_verified_source_inventory(source)
        supported = EvidenceFinding(
            priority="Low",
            title="Addition result is returned",
            impact="The helper exposes the calculated value.",
            recommendation="Keep this behavior covered by a unit test.",
            identifier="add",
            evidence="return result",
        )
        bogus = EvidenceFinding(
            priority="High",
            title="Missing authentication",
            impact="Anyone can supposedly access the helper.",
            recommendation="Add authentication.",
            identifier="authenticate_user",
            evidence="allow_anonymous_access()",
        )
        feature = VerifiedFeature(
            title="Addition helper",
            explanation="Adds two supplied values and returns the result.",
            identifier="add",
            evidence="result = left + right",
        )

        findings, features, rejected = analysis_engine.validate_evidence_review(
            EvidenceReview(
                findings=[supported, bogus],
                correct_features=[feature],
            ),
            source,
            source_facts,
        )

        self.assertEqual(findings, [supported])
        self.assertEqual(features, [feature])
        self.assertEqual(rejected, 1)

    def test_verification_repair_binds_evidence_to_its_identifier(self):
        source = (
            "def add(left, right):\n"
            "    result = left + right\n"
            "    return result\n"
        )
        message = (
            "Review this source.\n\n"
            "--- BEGIN SCRIPT ---\n"
            f"{source}"
            "--- END SCRIPT ---"
        )
        calls = []

        def fake_ask_ollama(_messages, **kwargs):
            calls.append(kwargs["response_format"])
            if len(calls) == 1:
                identifier_schema = calls[0]["$defs"]["EvidenceFinding"]["properties"][
                    "identifier"
                ]
                self.assertEqual(identifier_schema["enum"], ["add"])
                return EvidenceReview(
                    findings=[
                        EvidenceFinding(
                            priority="Low",
                            title="Addition behavior should remain covered",
                            identifier="add",
                            evidence="paraphrased evidence that is not source text",
                            impact="A regression could change the returned result.",
                            recommendation="Keep a unit test for this return path.",
                        )
                    ],
                    correct_features=[],
                ).model_dump_json()

            repair_definition = calls[1]["$defs"]["RepairEvidenceFinding"]
            self.assertNotIn("identifier", repair_definition["properties"])
            evidence_key = repair_definition["properties"]["evidence_key"]["enum"][0]
            return json.dumps(
                {
                    "findings": [{"evidence_key": evidence_key}],
                    "correct_features": [],
                }
            )

        analysis_engine.ask_ollama = fake_ask_ollama
        stages = []
        reply, _context = analysis_engine.verify_analysis_notes(
            message,
            "Potential defect in `add`: the returned addition result needs regression coverage.",
            progress_callback=lambda stage, _current, _total: stages.append(stage),
        )

        self.assertEqual(len(calls), 2)
        self.assertIn("repairing_evidence", stages)
        self.assertIn("retained 1 recommendation(s)", reply)
        self.assertIn("`add`", reply)

    def test_repair_hydration_cannot_cross_mix_candidate_fields(self):
        first = EvidenceFinding(
            priority="High",
            title="First candidate",
            identifier="first",
            evidence="invalid first evidence",
            impact="First impact.",
            recommendation="First recommendation.",
        )
        second = EvidenceFinding(
            priority="Low",
            title="Second candidate",
            identifier="second",
            evidence="invalid second evidence",
            impact="Second impact.",
            recommendation="Second recommendation.",
        )
        original = EvidenceReview(findings=[first, second], correct_features=[])
        selection = analysis_engine.EvidenceRepairReview(
            findings=[analysis_engine.RepairEvidenceFinding(evidence_key="E0001")],
            correct_features=[],
        )

        hydrated = analysis_engine.hydrate_evidence_repair(
            selection,
            {"E0001": ("finding", 1, "second", "return second_result")},
            original,
        )

        self.assertEqual(len(hydrated.findings), 1)
        self.assertEqual(hydrated.findings[0].title, "Second candidate")
        self.assertEqual(hydrated.findings[0].impact, "Second impact.")
        self.assertEqual(hydrated.findings[0].identifier, "second")
        self.assertEqual(hydrated.findings[0].evidence, "return second_result")

    def test_bounded_ntfy_empty_input_claim_is_rejected_as_contradictory(self):
        source = (
            "NTFY_MESSAGE_MAX_BYTES = 100\n"
            "def bounded_ntfy_message(message):\n"
            "    encoded = message.encode('utf-8')\n"
            "    if len(encoded) <= NTFY_MESSAGE_MAX_BYTES:\n"
            "        return message\n"
            "    return encoded[:50].decode('utf-8', errors='ignore')\n"
        )
        _inventory, facts = analysis_engine.build_verified_source_inventory(source)
        finding = EvidenceFinding(
            priority="High",
            title="Empty input can fail while slicing",
            identifier="bounded_ntfy_message",
            evidence=(
                "if len(encoded) <= NTFY_MESSAGE_MAX_BYTES:\n"
                "        return message"
            ),
            impact="An empty value reaches slicing and raises UnicodeDecodeError.",
            recommendation="Return early when the encoded value is empty.",
        )

        findings, _features, rejected = analysis_engine.validate_evidence_review(
            EvidenceReview(findings=[finding], correct_features=[]),
            source,
            facts,
        )

        self.assertEqual(findings, [])
        self.assertEqual(rejected, 1)

    def test_empty_model_review_still_returns_deterministic_source_features(self):
        source = (
            "def complete_password_reset(db, digest, now):\n"
            "    return db.execute(\n"
            "        '''SELECT user_id FROM password_reset_tokens\n"
            "        WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?''',\n"
            "        (digest, now),\n"
            "    ).fetchone()\n"
        )
        message = (
            "Review this source.\n\n"
            "--- BEGIN SCRIPT ---\n"
            f"{source}"
            "--- END SCRIPT ---"
        )
        calls = []

        def fake_ask_ollama(_messages, **_kwargs):
            calls.append(True)
            return EvidenceReview(findings=[], correct_features=[]).model_dump_json()

        analysis_engine.ask_ollama = fake_ask_ollama
        reply, _context = analysis_engine.verify_analysis_notes(
            message,
            "The `complete_password_reset` function checks reset tokens.",
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("retained 0 recommendation(s) and 2 confirmed feature(s)", reply)
        self.assertIn("Password-reset tokens are checked for expiry and prior use", reply)
        self.assertIn("Parameterized SQL arguments in complete_password_reset", reply)


if __name__ == "__main__":
    unittest.main()
