# -*- coding: utf-8 -*-
"""ai_llm.LLMClient 单测：本地假 HTTP 服务，不联网、不真等退避。

用 ``ThreadingHTTPServer(("127.0.0.1", 0), ...)`` 起一个只活在本进程里的
服务端，每个用例自己决定第几次请求返回什么；``sleep`` 注入成
``list.append``，这样「退避了 1s/2s」只是记下数字，测试瞬间跑完。
"""

import base64
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ai_llm  # noqa: E402


# --------------------------------------------------------------------------
# 假服务端
# --------------------------------------------------------------------------
def _reply(text: str = "OK", model: str = "fake-model") -> bytes:
    """一份标准的 OpenAI chat.completion 响应。"""
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                  "total_tokens": 12},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class _FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):                      # noqa: N802（http.server 约定）
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            payload = {}

        record = {
            "path": self.path,
            "headers": dict(self.headers),
            "payload": payload,
        }
        self.server.records.append(record)
        index = len(self.server.records) - 1

        try:
            status, extra, body = self.server.responder(
                index, payload, self.headers)
        except Exception as exc:            # 测试里的 responder 写错也别卡死
            status = 500
            extra = {}
            body = json.dumps({"error": str(exc)}).encode("utf-8")

        self.send_response(int(status))
        for key, value in (extra or {}).items():
            self.send_header(str(key), str(value))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def log_message(self, *args):          # 静音，别把访问日志喷到测试输出
        pass


class _FakeServer:
    """本地假 LLM 服务。responder(index, payload, headers) -> (status, headers, body)。"""

    def __init__(self):
        self.records: list[dict] = []
        self.responder = lambda index, payload, headers: (200, {}, _reply())
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self.httpd.records = self.records
        self.httpd.responder = self._dispatch
        # poll_interval 调小：每个用例都要 shutdown，默认 0.5s 会白等十几秒
        self.thread = threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.01),
            daemon=True)
        self.thread.start()

    def _dispatch(self, index, payload, headers):
        return self.responder(index, payload, headers)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        try:
            self.httpd.shutdown()
        finally:
            self.httpd.server_close()
            self.thread.join(timeout=3)


class LLMClientTest(unittest.TestCase):
    """客户端行为总集。"""

    def setUp(self):
        self.server = _FakeServer()
        self.addCleanup(self.server.close)
        self.sleeps: list[float] = []
        self.client = self._client()

    def _client(self, **kwargs) -> ai_llm.LLMClient:
        kwargs.setdefault("timeout", 5.0)
        kwargs.setdefault("backoff", 0.001)
        kwargs.setdefault("attempts", ai_llm.MAX_ATTEMPTS)
        kwargs.setdefault("sleep", self.sleeps.append)
        return ai_llm.LLMClient(
            self.server.base_url, "sk-test-SECRET-123", "fake-model", **kwargs)

    def _last_payload(self) -> dict:
        return self.server.records[-1]["payload"]

    def _ask(self, client=None, **kwargs):
        client = client or self.client
        return client.chat([{"role": "user", "content": "该下哪"}], **kwargs)

    # -- 正常路径 -----------------------------------------------------
    def test_normal_chat_returns_text_and_sends_auth(self):
        self.server.responder = lambda i, p, h: (200, {}, _reply("白棋 5-5"))
        text = self._ask()
        self.assertEqual(text, "白棋 5-5")

        record = self.server.records[0]
        self.assertEqual(record["path"], "/chat/completions")
        self.assertEqual(record["headers"]["Authorization"],
                         "Bearer sk-test-SECRET-123")
        self.assertEqual(record["headers"]["Content-Type"], "application/json")

    def test_request_keeps_messages_and_sampling(self):
        messages = [
            {"role": "system", "content": "你是操作员"},
            {"role": "user", "content": "该下哪"},
        ]
        self._ask(client=self.client)
        # 重新发一次带自定义 messages 的请求
        self.client.chat(messages)
        payload = self._last_payload()
        self.assertEqual(payload["model"], "fake-model")
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["temperature"], 0.3)
        self.assertEqual(payload["max_tokens"], 1500)
        self.assertFalse(payload["stream"])
        self.assertNotIn("response_format", payload)

    def test_max_tokens_can_be_overridden(self):
        self._ask(max_tokens=42)
        self.assertEqual(self._last_payload()["max_tokens"], 42)

    def test_base_url_v3_is_not_duplicated(self):
        client = ai_llm.LLMClient(
            self.server.base_url + "/api/v3", "sk-test-SECRET-123",
            "fake-model", sleep=self.sleeps.append)
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(self.server.records[-1]["path"],
                         "/api/v3/chat/completions")

    def test_full_endpoint_url_is_used_as_is(self):
        client = ai_llm.LLMClient(
            self.server.base_url + "/api/v3/chat/completions",
            "sk-test-SECRET-123", "fake-model", sleep=self.sleeps.append)
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(self.server.records[-1]["path"],
                         "/api/v3/chat/completions")

    def test_list_content_reply_is_joined(self):
        body = {
            "choices": [{
                "message": {"role": "assistant", "content": [
                    {"type": "text", "text": "第一段"},
                    {"type": "text", "text": "第二段"},
                ]},
                "finish_reason": "stop",
            }]
        }
        self.server.responder = lambda i, p, h: (
            200, {}, json.dumps(body).encode("utf-8"))
        self.assertEqual(self._ask(), "第一段第二段")

    def test_test_method_sends_ping(self):
        self.server.responder = lambda i, p, h: (200, {}, _reply("OK"))
        self.assertEqual(self.client.test(), "OK")
        payload = self._last_payload()
        self.assertNotIn("response_format", payload)
        self.assertLessEqual(payload["max_tokens"], 32)
        self.assertEqual(payload["messages"][0]["role"], "user")

    # -- json_mode ----------------------------------------------------
    def test_json_mode_adds_response_format(self):
        self.server.responder = lambda i, p, h: (
            200, {}, _reply('{"reply":"好的"}'))
        text = self._ask(json_mode=True)
        self.assertEqual(text, '{"reply":"好的"}')
        self.assertEqual(self._last_payload()["response_format"],
                         {"type": "json_object"})

    def test_json_mode_400_retries_without_response_format(self):
        def responder(index, payload, headers):
            if index == 0:
                return 400, {}, json.dumps(
                    {"error": {"message": "response_format is not supported"}}
                ).encode("utf-8")
            return 200, {}, _reply('{"reply":"ok"}')

        self.server.responder = responder
        text = self._ask(json_mode=True)

        self.assertEqual(text, '{"reply":"ok"}')
        self.assertEqual(len(self.server.records), 2)
        self.assertIn("response_format", self.server.records[0]["payload"])
        self.assertNotIn("response_format", self.server.records[1]["payload"])
        self.assertEqual(self.sleeps, [])       # 400 不属于网络层重试

    def test_json_mode_two_400s_raises_after_one_fallback(self):
        self.server.responder = lambda i, p, h: (
            400, {}, b'{"error":"response_format not allowed"}')
        with self.assertRaises(ai_llm.LLMHTTPError) as ctx:
            self._ask(json_mode=True)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(self.server.records), 2)   # 不会无限循环

    def test_400_without_json_mode_is_not_retried(self):
        self.server.responder = lambda i, p, h: (400, {}, b'{"error":"bad"}')
        with self.assertRaises(ai_llm.LLMHTTPError) as ctx:
            self._ask()
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(len(self.server.records), 1)

    # -- 重试 / 退避 ---------------------------------------------------
    def test_429_uses_retry_after_header(self):
        def responder(index, payload, headers):
            if index == 0:
                return 429, {"Retry-After": "0"}, b'{"error":"rate limit"}'
            return 200, {}, _reply("好了")

        self.server.responder = responder
        self.assertEqual(self._ask(), "好了")
        self.assertEqual(len(self.server.records), 2)
        self.assertEqual(self.sleeps, [0.0])

    def test_429_retry_after_is_clamped_to_ten_seconds(self):
        def responder(index, payload, headers):
            if index == 0:
                return 429, {"Retry-After": "99"}, b'{"error":"rate limit"}'
            return 200, {}, _reply("好了")

        self.server.responder = responder
        self.assertEqual(self._ask(), "好了")
        self.assertEqual(self.sleeps, [10.0])

    def test_429_without_retry_after_uses_backoff(self):
        def responder(index, payload, headers):
            if index == 0:
                return 429, {}, b'{"error":"rate limit"}'
            return 200, {}, _reply("好了")

        self.server.responder = responder
        client = self._client(backoff=0.5)
        self.assertEqual(self._ask(client=client), "好了")
        self.assertEqual(self.sleeps, [0.5])

    def test_server_error_retries_three_attempts_then_raises(self):
        self.server.responder = lambda i, p, h: (500, {}, b'{"error":"boom"}')
        client = self._client(backoff=1.0, attempts=3)
        with self.assertRaises(ai_llm.LLMHTTPError) as ctx:
            self._ask(client=client)
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(len(self.server.records), 3)
        # 指数退避 1s、2s（假 sleep，不会真的等）
        self.assertEqual(self.sleeps, [1.0, 2.0])

    def test_network_error_retries_then_raises(self):
        # 先占一个端口再关掉，得到一个确定「连不上」的地址
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        client = ai_llm.LLMClient(
            f"http://127.0.0.1:{dead_port}", "sk-test-SECRET-123",
            "fake-model", timeout=1.0, backoff=0.25, attempts=3,
            sleep=self.sleeps.append)
        with self.assertRaises(ai_llm.LLMRequestError) as ctx:
            self._ask(client=client)
        self.assertIn("网络", str(ctx.exception))
        self.assertEqual(self.sleeps, [0.25, 0.5])

    def test_timeout_is_retried(self):
        def slow(index, payload, headers):
            time.sleep(0.4)
            return 200, {}, _reply()

        self.server.responder = slow
        client = self._client(timeout=0.1, attempts=2)
        with self.assertRaises(ai_llm.LLMError):
            self._ask(client=client)
        self.assertEqual(len(self.sleeps), 1)

    # -- 图片 / 多模态 -------------------------------------------------
    def test_image_jpeg_is_inserted_as_openai_content_parts(self):
        image = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
        messages = [
            {"role": "system", "content": "你是操作员"},
            {"role": "user", "content": "看这张图"},
        ]
        self.client.chat(messages, image_jpeg=image)

        payload = self._last_payload()
        self.assertEqual(payload["messages"][0], messages[0])
        content = payload["messages"][1]["content"]
        self.assertIsInstance(content, list)
        self.assertEqual(content[0], {"type": "text", "text": "看这张图"})
        self.assertEqual(content[1]["type"], "image_url")
        url = content[1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), image)
        # 调用方传进来的 messages 不能被就地改掉
        self.assertEqual(messages[1]["content"], "看这张图")

    def test_existing_multimodal_messages_are_preserved(self):
        original = [
            {"role": "user", "content": [
                {"type": "text", "text": "现有的"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ]}
        ]
        self.client.chat(original, image_jpeg=b"new-image")

        content = self._last_payload()["messages"][0]["content"]
        self.assertEqual(len(content), 3)
        self.assertEqual(content[0], original[0]["content"][0])
        self.assertEqual(content[1], original[0]["content"][1])
        self.assertEqual(content[2]["type"], "image_url")
        # 原列表还是两条
        self.assertEqual(len(original[0]["content"]), 2)

    def test_non_bytes_image_rejected(self):
        with self.assertRaises(ai_llm.LLMError):
            self._ask(image_jpeg="not-bytes")
        self.assertEqual(self.server.records, [])

    # -- 错误 / 保密 ---------------------------------------------------
    def test_error_never_contains_api_key(self):
        self.server.responder = lambda i, p, h: (
            401, {},
            b'{"error":"invalid key sk-test-SECRET-123"}')
        with self.assertRaises(ai_llm.LLMHTTPError) as ctx:
            self._ask()
        error = ctx.exception
        self.assertEqual(error.status, 401)
        self.assertEqual(len(self.server.records), 1)
        self.assertNotIn("sk-test-SECRET-123", str(error))
        self.assertNotIn("sk-test-SECRET-123", repr(error))
        self.assertNotIn("sk-test-SECRET-123", error.body)
        self.assertIn("***", str(error))

    def test_repr_and_describe_hide_api_key(self):
        self.assertNotIn("sk-test-SECRET-123", repr(self.client))
        described = self.client.describe()
        self.assertNotIn("sk-test-SECRET-123", json.dumps(described))
        self.assertTrue(described["has_key"])

    def test_empty_messages_raise_without_request(self):
        with self.assertRaises(ai_llm.LLMError):
            self.client.chat([])
        self.assertEqual(self.server.records, [])

    def test_empty_api_key_gives_clear_error(self):
        client = ai_llm.LLMClient(self.server.base_url, "", "fake-model",
                                  sleep=self.sleeps.append)
        with self.assertRaises(ai_llm.LLMError) as ctx:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertIn("api_key", str(ctx.exception))
        self.assertEqual(self.server.records, [])

    def test_non_json_response_raises(self):
        self.server.responder = lambda i, p, h: (
            200, {}, b"<html>gateway error</html>")
        with self.assertRaises(ai_llm.LLMError) as ctx:
            self._ask()
        self.assertIn("JSON", str(ctx.exception))

    def test_missing_choices_raises(self):
        self.server.responder = lambda i, p, h: (
            200, {}, b'{"error":{"message":"bad model"}}')
        with self.assertRaises(ai_llm.LLMError) as ctx:
            self._ask()
        self.assertIn("choices", str(ctx.exception))

    def test_module_constants_follow_contract(self):
        self.assertEqual(ai_llm.MAX_ATTEMPTS, 3)
        self.assertEqual(ai_llm.BACKOFF_BASE, 1.0)
        self.assertEqual(ai_llm.RETRY_AFTER_CAP, 10.0)


if __name__ == "__main__":
    unittest.main()
