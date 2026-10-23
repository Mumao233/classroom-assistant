"""Synthetic HTTP only: these tests never contact a model service."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_api import ApiBridge, ApiBridgeError, validate_base_url
from codex_bridge import CodexBridge, CodexBridgeError, _subprocess_environment


KEY = "synthetic-key-for-tests-only"
CONFIG = {"base_url": "https://api.example.test/v1", "model": "synthetic-model"}
USAGE = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def event(content=None, reason=None, **delta):
    if content is not None:
        delta["content"] = content
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}


def sse(*events):
    return "".join("data: " + (item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + "\n\n" for item in events).encode()


class Chunks(httpx.SyncByteStream):
    def __init__(self, values):
        self.values = values

    def __iter__(self):
        for value in self.values:
            if isinstance(value, Exception):
                raise value
            yield value


def response(*events):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*events))


def json_response(content="答复", reason="stop", **extra):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": reason}], **extra})


class URLTests(unittest.TestCase):
    def test_supported_roots_and_full_endpoint_normalize(self):
        examples = {
            " https://API.example.test/v1/ ": "https://api.example.test/v1",
            "https://api.example.test/compatible-mode/v1": "https://api.example.test/compatible-mode/v1",
            "https://api.example.test/v1/chat/completions/": "https://api.example.test/v1",
            "https://api.example.test/v1/%63hat/completions": "https://api.example.test/v1",
            "https://api.example.test/v1/chat%2Fcompletions": "https://api.example.test/v1",
            "http://localhost:1234/v1": "http://localhost:1234/v1",
            "http://127.0.0.2:8000/v1": "http://127.0.0.2:8000/v1",
            "http://[::1]:8000/v1": "http://[::1]:8000/v1",
            "https://api.example.test": "https://api.example.test",
        }
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(validate_base_url(value), expected)

    def test_rejects_unencrypted_remote_credentials_and_ambiguous_paths(self):
        values = [None, "", "http://api.example.test/v1", "http://192.168.1.1/v1", "ftp://localhost",
                  "https://user:password@api.example.test/v1", "https://api.example.test/v1?key=secret",
                  "https://api.example.test/v1#secret", "https://api.example.test/v1?", "https://api.example.test/v1#",
                  "https://api.example.test:bad/v1", "https://api.example.test:65536/v1", "https://api.example.test:/v1",
                  "https://api.example.test/v1\\evil", "https://api.example.test/a/../v1",
                  "https://api.example.test/a/%2e%2e/v1", "https://api.example.test/v1%0d%0a",
                  "https://api.example.test/v1/responses", "http://localhost.example.test/v1",
                  "https://api.example.test/a//v1", "https://api.example.test/v1\nsecret"]
        for value in values:
            with self.subTest(value=value), self.assertRaises(ApiBridgeError) as caught:
                validate_base_url(value)
            self.assertNotIn("secret", str(caught.exception))
            self.assertNotIn("password", str(caught.exception))


class ApiTests(unittest.TestCase):
    def make(self, handler, on_delta=None, config=None):
        return ApiBridge(config or CONFIG, KEY, on_delta, transport=httpx.MockTransport(handler))

    def test_streaming_unicode_deltas_omit_reasoning_and_collect_usage(self):
        chunks = []
        raw = sse(event(reasoning_content="hidden reasoning", reasoning="also private"), event("解释 "),
                  event("[S0001]"), event(reason="stop"), {"choices": [], "usage": USAGE}, "[DONE]")
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  stream=Chunks([raw[i:i+3] for i in range(0, len(raw), 3)]))
        bridge = self.make(handler, chunks.append)
        self.assertEqual(bridge.analyze("", "[S0001] Topic."), "解释 [S0001]")
        self.assertEqual(chunks, ["解释 ", "[S0001]"])
        self.assertEqual(bridge.metadata(), {"provider": "api", "model": CONFIG["model"], "usage": USAGE})
        self.assertTrue(bridge.status()["inference_verified"])
        self.assertEqual(len(seen), 1)
        body = json.loads(seen[0].content)
        self.assertTrue(body["stream"])
        self.assertTrue(body["stream_options"]["include_usage"])
        self.assertNotIn("tools", body)
        self.assertEqual(str(seen[0].url), CONFIG["base_url"] + "/chat/completions")
        self.assertEqual(seen[0].headers["Authorization"], "Bearer " + KEY)
        self.assertIn("80–160", body["messages"][1]["content"])

    def test_nonstream_response_supported_without_usage(self):
        chunks = []
        bridge = self.make(lambda _: json_response("解释 [S0001]"), chunks.append)
        self.assertEqual(bridge.ask("[S0001] Topic.", "解释一下", []), "解释 [S0001]")
        self.assertEqual(chunks, ["解释 [S0001]"])
        self.assertIsNone(bridge.metadata()["usage"])

    def test_json_with_reasoning_fields_does_not_forward_them(self):
        def handler(_):
            return httpx.Response(200, json={"choices": [{"message": {"content": "答案", "reasoning_content": "private"}, "finish_reason": "stop"}]})
        chunks = []
        self.assertEqual(self.make(handler, chunks.append).ask("", "问题", []), "答案")
        self.assertEqual(chunks, ["答案"])

    def test_normal_stop_without_done_is_complete(self):
        bridge = self.make(lambda _: response(event("答案"), event(reason="stop")))
        self.assertEqual(bridge.ask("", "问题", []), "答案")

    def test_done_without_finish_reason_is_complete(self):
        bridge = self.make(lambda _: response(event("答案"), "[DONE]"))
        self.assertEqual(bridge.ask("", "问题", []), "答案")

    def test_eof_without_completion_is_rejected_and_keeps_usage(self):
        chunks = []
        bridge = self.make(lambda _: response(event("半句话"), {"choices": [], "usage": USAGE}), chunks.append)
        with self.assertRaisesRegex(ApiBridgeError, "未完整结束"):
            bridge.ask("", "问题", [])
        self.assertEqual(chunks, ["半句话"])
        self.assertFalse(bridge.status()["inference_verified"])
        self.assertEqual(bridge.status()["active_requests"], 0)
        self.assertEqual(bridge.metadata()["usage"], USAGE)

    def test_truncation_and_content_filter_never_become_formal_answers(self):
        for reason in ["length", "content_filter", "tool_calls", "function_call", "unknown"]:
            for streaming in [True, False]:
                with self.subTest(reason=reason, streaming=streaming):
                    bridge = self.make(lambda _: response(event("片段"), event(reason=reason), "[DONE]") if streaming else json_response("片段", reason))
                    with self.assertRaises(ApiBridgeError):
                        bridge.ask("", "问题", [])
                    self.assertFalse(bridge.status()["inference_verified"])

    def test_json_without_finish_reason_is_not_completed(self):
        bridge = self.make(lambda _: json_response("半句话", None))
        with self.assertRaises(ApiBridgeError):
            bridge.ask("", "问题", [])

    def test_content_after_finish_is_rejected(self):
        bridge = self.make(lambda _: response(event("答案", "stop"), event("追加内容"), "[DONE]"))
        with self.assertRaisesRegex(ApiBridgeError, "结束标志"):
            bridge.ask("", "问题", [])

    def test_glossary_reuses_validation_and_suppresses_ui_deltas(self):
        terms = [{"term": "derivative", "meaning": "导数", "aliases": []}]
        chunks = []
        bridge = self.make(lambda _: response(event(json.dumps(terms)), event(reason="stop"), "[DONE]"), chunks.append)
        self.assertEqual(bridge.generate_glossary("微积分"), terms)
        self.assertEqual(chunks, [])

    def test_glossary_invalid_json_is_not_adopted(self):
        bridge = self.make(lambda _: json_response("not JSON"))
        with self.assertRaises(Exception) as caught:
            bridge.generate_glossary("微积分")
        self.assertEqual(caught.exception.kind, "other")

    def test_unknown_source_reference_is_rejected(self):
        bridge = self.make(lambda _: response(event("结论 [S9999]"), event(reason="stop"), "[DONE]"))
        with self.assertRaisesRegex(ApiBridgeError, "无法对应"):
            bridge.ask("[S0001] Topic.", "什么意思", [])

    def test_connection_uses_only_short_synthetic_prompt_without_ui_delta(self):
        seen = []
        chunks = []
        def handler(request):
            seen.append(json.loads(request.content))
            return json_response("OK", usage=USAGE)
        bridge = self.make(handler, chunks.append)
        self.assertEqual(bridge.test_connection(), {"ok": True, "provider": "api", "model": CONFIG["model"], "usage": USAGE})
        self.assertEqual(chunks, [])
        self.assertEqual(seen[0]["max_tokens"], 8)
        self.assertEqual(seen[0]["messages"][1]["content"], "Reply only OK.")
        self.assertLess(len(json.dumps(seen[0]["messages"])), 200)

    def test_no_request_on_status_or_metadata(self):
        seen = []
        bridge = self.make(lambda request: seen.append(request))
        self.assertFalse(bridge.status()["inference_verified"])
        self.assertIsNone(bridge.metadata()["usage"])
        self.assertEqual(seen, [])

    def test_http_errors_are_safe_classified_and_never_retry_or_redirect(self):
        for status, kind in [(401, "auth"), (403, "auth"), (402, "quota"), (429, "quota"),
                             (408, "timeout"), (504, "timeout"), (500, "network"), (503, "network"),
                             (400, "other"), (307, "other"), (302, "other")]:
            with self.subTest(status=status):
                seen = []
                def handler(request):
                    seen.append(request)
                    return httpx.Response(status, text=KEY + " C:\\private\\classroom.json",
                                          headers={"Location": "https://another.example.test/v1/chat/completions"})
                bridge = self.make(handler)
                with self.assertRaises(ApiBridgeError) as caught:
                    bridge.ask("", "问题", [])
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn("private", str(caught.exception))
                self.assertEqual(len(seen), 1)

    def test_transport_errors_are_safe_and_do_not_retry(self):
        for error, kind in [(httpx.ReadTimeout(KEY), "timeout"), (httpx.ConnectError(KEY), "network"),
                            (httpx.RemoteProtocolError(KEY), "network")]:
            with self.subTest(kind=kind):
                seen = []
                def handler(request):
                    seen.append(request)
                    raise error
                bridge = self.make(handler)
                with self.assertRaises(ApiBridgeError) as caught:
                    bridge.ask("", "问题", [])
                self.assertEqual(caught.exception.kind, kind)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertEqual(len(seen), 1)

    def test_stream_error_payload_is_private_and_classified(self):
        bridge = self.make(lambda _: response(event("未完成"), {"error": {"code": "insufficient_quota", "message": KEY}}))
        with self.assertRaises(ApiBridgeError) as caught:
            bridge.ask("", "问题", [])
        self.assertEqual(caught.exception.kind, "quota")
        self.assertNotIn(KEY, str(caught.exception))

    def test_interrupted_stream_does_not_return_partial_answer(self):
        def handler(_):
            return httpx.Response(200, stream=Chunks([sse(event("片段")), httpx.RemoteProtocolError(KEY)]))
        bridge = self.make(handler)
        with self.assertRaises(ApiBridgeError) as caught:
            bridge.ask("", "问题", [])
        self.assertEqual(caught.exception.kind, "network")

    def test_malformed_response_cannot_expose_response_or_key(self):
        for raw in [b"data: {broken secret}\n\n", b"<html>secret</html>", b"data: \xff\n\n", b""]:
            with self.subTest(raw=raw):
                bridge = self.make(lambda _: httpx.Response(200, content=raw))
                with self.assertRaises(ApiBridgeError) as caught:
                    bridge.ask("", "问题", [])
                self.assertNotIn("secret", str(caught.exception))

    def test_usage_resets_at_each_request_and_errors_clear_after_success(self):
        replies = iter([json_response("OK", usage=USAGE), httpx.Response(401), json_response("OK")])
        bridge = self.make(lambda _: next(replies))
        bridge.ask("", "问题", [])
        self.assertEqual(bridge.metadata()["usage"], USAGE)
        with self.assertRaises(ApiBridgeError):
            bridge.ask("", "问题", [])
        self.assertIsNone(bridge.metadata()["usage"])
        self.assertEqual(bridge.status()["last_error_kind"], "auth")
        bridge.ask("", "问题", [])
        self.assertIsNone(bridge.status()["last_error_kind"])

    def test_invalid_usage_is_not_invented(self):
        for usage in [None, {"prompt_tokens": 1}, {"prompt_tokens": True, "completion_tokens": 1},
                      {"prompt_tokens": -1, "completion_tokens": 1}, {"prompt_tokens": "1", "completion_tokens": "1"}]:
            with self.subTest(usage=usage):
                bridge = self.make(lambda _: json_response("OK", usage=usage))
                bridge.ask("", "问题", [])
                self.assertIsNone(bridge.metadata()["usage"])

    def test_callback_error_is_sanitized(self):
        for error_type in [RuntimeError, CodexBridgeError, ApiBridgeError]:
            with self.subTest(error_type=error_type):
                def callback(_):
                    raise error_type(KEY)
                bridge = self.make(lambda _: response(event("答案"), event(reason="stop")), callback)
                with self.assertRaises(ApiBridgeError) as caught:
                    bridge.ask("", "问题", [])
                self.assertNotIn(KEY, str(caught.exception))
                self.assertNotIn(KEY, bridge.status()["last_error"])
                self.assertEqual(bridge.status()["active_requests"], 0)

    def test_error_event_preserves_usage_and_normalizes_auth_code(self):
        bridge = self.make(lambda _: response({"error": {"code": "invalid_api_key", "message": KEY}, "usage": USAGE}))
        with self.assertRaises(ApiBridgeError) as caught:
            bridge.ask("", "问题", [])
        self.assertEqual(caught.exception.kind, "auth")
        self.assertEqual(bridge.metadata()["usage"], USAGE)

    def test_oversized_response_is_rejected(self):
        bridge = self.make(lambda _: httpx.Response(200, stream=Chunks([b"x" * 2_000_001])))
        with self.assertRaisesRegex(ApiBridgeError, "过长"):
            bridge.ask("", "问题", [])

    def test_tools_are_rejected_and_never_forwarded(self):
        chunks = []
        bridge = self.make(lambda _: response(event(tool_calls=[{"function": {"name": "bad"}}]), "[DONE]"), chunks.append)
        with self.assertRaises(ApiBridgeError):
            bridge.ask("", "问题", [])
        self.assertEqual(chunks, [])

    def test_invalid_constructor_values_fail_safely(self):
        for value in ["", None, "contains\nnewline", "nonascii密钥"]:
            with self.subTest(key=value), self.assertRaises(ApiBridgeError):
                ApiBridge(CONFIG, value)
        for config in [{**CONFIG, "model": ""}, {**CONFIG, "model": "bad\nmodel"},
                       {**CONFIG, "timeout_seconds": "nan"}, {**CONFIG, "timeout_seconds": 0},
                       {**CONFIG, "context_chars": "bad"}]:
            with self.subTest(config=config), self.assertRaises(ApiBridgeError):
                ApiBridge(config, KEY)


class CodexEnvironmentTests(unittest.TestCase):
    def test_api_credentials_never_pass_to_codex_process(self):
        env = {"PATH": "preserved", "OPENAI_API_KEY": KEY, "DEEPSEEK_API_KEY": KEY,
               "CUSTOM_API_TOKEN": KEY, "ANTHROPIC_AUTH_TOKEN": KEY, "AZURE_OPENAI_KEY": KEY,
               "CODEX_THREAD_ID": "old-thread"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_subprocess_environment(), {"PATH": "preserved"})
            with tempfile.TemporaryDirectory() as folder:
                bridge = CodexBridge({"codex_path": "codex"}, folder)
                process = MagicMock()
                process.communicate.return_value = (json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}) + '\n{"type":"turn.completed"}', "")
                process.returncode = 0
                with patch("codex_bridge.subprocess.Popen", return_value=process) as popen:
                    bridge.ask("", "问题", [])
                self.assertEqual(popen.call_args.kwargs["env"], {"PATH": "preserved"})


if __name__ == "__main__":
    unittest.main()
