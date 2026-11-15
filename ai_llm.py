# -*- coding: utf-8 -*-
"""AI 托管用的大模型客户端（OpenAI 兼容接口，只用标准库）

为什么不用 requests
------------------------------------------------------------
打包成 exe 之后多一个第三方依赖就多一份体积和踩坑机会；这里只要
一个 POST + Bearer 头，``urllib.request`` 足够，Windows 上也不用装
证书之外的任何东西（系统证书链由 ssl 自己认）。

调用形态（契约见 AI-INTEGRATION.md）
------------------------------------------------------------
    client = LLMClient(base_url, api_key, model)
    text = client.chat(messages)                       # 纯文本
    text = client.chat(messages, json_mode=True)       # 要 JSON
    text = client.chat(messages, image_jpeg=jpeg)      # 带一张截图

几条硬规则
------------------------------------------------------------
1. **api_key 绝不进异常信息**：服务端把 key 回显在错误 body 里也会被
   替换成 ``***``；本模块不打日志，自然也不会把 key 写进日志。
2. **json_mode 兼容性**：火山方舟等兼容服务对 ``response_format`` 的
   支持并不一致，遇到 HTTP 400 就**去掉该字段重发一次**；其它 4xx
   （401/403/404/422…）说明不是这个原因，直接报错，不浪费用户时间。
3. **重试**：429 / 5xx / 超时 / 连不上，最多 ``MAX_ATTEMPTS`` 次（首发
   + 2 次重试），指数退避 1s、2s（基数翻倍，下一档 4s）。429 若带
   ``Retry-After`` 听服务端的，但夹到 ``RETRY_AFTER_CAP`` 秒以内，
   免得被一个 3600 钉死。
4. **测试友好**：退避基数、最大尝试次数、sleep 函数都可注入，单测里
   用假 sleep 记录延迟，绝不真的等 7 秒。

关于 base_url 的拼法
------------------------------------------------------------
方舟的 base_url 是 ``https://ark.cn-beijing.volces.com/api/v3``，
接口在它后面接 ``/chat/completions`` —— 只接一次 ``/v3``。这里做了
防呆：如果调用方已经把完整 URL 传进来（以 ``/chat/completions`` 结尾），
就不再重复拼接。
"""

from __future__ import annotations

import base64
import json
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

__all__ = [
    "LLMError",
    "LLMHTTPError",
    "LLMRequestError",
    "LLMClient",
    "build_data_uri",
    "content_to_text",
    "BACKOFF_BASE",
    "MAX_ATTEMPTS",
    "RETRY_AFTER_CAP",
]

#: 指数退避基数（秒）。第 n 次失败后等 ``BACKOFF_BASE * 2 ** n``：
#: 1s、2s、4s……测试里传 ``backoff=0.001`` 或注入假 sleep 即可。
BACKOFF_BASE = 1.0

#: 最多尝试次数（首发 1 次 + 重试 2 次）。再多用户也等不起。
MAX_ATTEMPTS = 3

#: ``Retry-After`` 的上限（秒）。服务端说等一小时也不能真等。
RETRY_AFTER_CAP = 10.0

#: 错误 body 只留前这么多字符，避免把整页 HTML 塞进异常。
_BODY_LIMIT = 600


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------
class LLMError(RuntimeError):
    """大模型调用失败的总类。**信息里永远不出现 api_key**。"""


class LLMHTTPError(LLMError):
    """服务端返回了 HTTP 错误（保留 status / body，方便上层判断）。"""

    def __init__(self, message: str, *, status: int = 0, body: str = ""):
        super().__init__(message)
        self.status = int(status)
        self.body = body


class LLMRequestError(LLMError):
    """网络层错误（连不上 / 超时 / DNS）。"""


# --------------------------------------------------------------------------
# 纯函数：图片 data URI / 回复文本抽取
# --------------------------------------------------------------------------
def build_data_uri(data: bytes | bytearray | memoryview,
                   mime: str = "image/jpeg") -> str:
    """bytes → ``data:image/jpeg;base64,...``（OpenAI 视觉消息要的格式）。"""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"build_data_uri 需要 bytes，收到 {type(data).__name__}")
    payload = base64.b64encode(bytes(data)).decode("ascii")
    return f"data:{mime};base64,{payload}"


def content_to_text(message: Mapping[str, Any] | None) -> str:
    """从 assistant message 里抽文本。

    大多数服务端 ``content`` 是字符串，但有的兼容层会返回
    ``[{"type":"text","text":...}]`` 这种数组，这里一起兼容。
    """
    if not isinstance(message, Mapping):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping):
                for key in ("text", "content", "output_text"):
                    value = part.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
                        break
        return "".join(parts)
    if content is None:
        reasoning = message.get("reasoning_content")
        return reasoning if isinstance(reasoning, str) else ""
    return str(content)


def _with_image(messages: Sequence[Mapping[str, Any]],
                image_jpeg: bytes) -> list[dict[str, Any]]:
    """把一张 JPEG 塞进**最后一条 user 消息**，不动调用方的原列表。

    ``content`` 是字符串 → 换成 [text, image_url]；已经是数组 → 在后面
    追一个 image_url。system/assistant 消息原样复制，保证标准 OpenAI
    多模态格式不被破坏。
    """
    data_uri = build_data_uri(image_jpeg)
    image_part = {"type": "image_url", "image_url": {"url": data_uri}}

    last_user = -1
    for index, message in enumerate(messages):
        if isinstance(message, Mapping) and message.get("role") == "user":
            last_user = index

    out: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise LLMError(
                f"messages[{index}] 必须是对象，收到 {type(message).__name__}")
        item = dict(message)
        if index == last_user:
            content = item.get("content")
            if isinstance(content, str):
                item["content"] = [{"type": "text", "text": content}, image_part]
            elif isinstance(content, list):
                item["content"] = [dict(part) if isinstance(part, Mapping) else part
                                   for part in content] + [image_part]
            elif content is None:
                item["content"] = [image_part]
            else:
                item["content"] = [{"type": "text", "text": str(content)},
                                   image_part]
        out.append(item)

    if last_user < 0:
        # 没有 user 消息（少见）：补一条只有图的 user，服务端也认
        out.append({"role": "user", "content": [image_part]})
    return out


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------
class LLMClient:
    """OpenAI 兼容 ``/chat/completions`` 的最小客户端。

    参数缺省值按契约来；``backoff`` / ``attempts`` / ``sleep`` 是给单测
    和上层调优留的扩展参数（都有缺省值，不影响契约调用方式）。
    """

    def __init__(self, base_url: str, api_key: str, model: str,
                 temperature: float = 0.3, max_tokens: int = 1500,
                 timeout: float = 90.0, *,
                 backoff: float = BACKOFF_BASE,
                 attempts: int = MAX_ATTEMPTS,
                 sleep: Callable[[float], None] | None = None):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self._api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.temperature = float(temperature)
        self.max_tokens = int(max_tokens)
        self.timeout = float(timeout)
        self.backoff = max(0.0, float(backoff))
        self.attempts = max(1, int(attempts))
        self._sleep = sleep or time.sleep

    # -- 展示 ---------------------------------------------------------
    def __repr__(self) -> str:
        # 只报「有没有 key」，绝不带 key 本身
        return (f"LLMClient(base_url={self.base_url!r}, model={self.model!r}, "
                f"has_key={bool(self._api_key)})")

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def describe(self) -> dict[str, Any]:
        """预检/UI 用：不含完整 key。"""
        return {
            "base_url": self.base_url,
            "model": self.model,
            "has_key": self.has_key,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout": self.timeout,
        }

    # -- 内部 ---------------------------------------------------------
    def _redact(self, text: Any) -> str:
        """把文本里可能出现的 api_key 换成 ``***``。

        服务端出错时偶尔会把 Authorization 回显进 body；异常继续往上抛
        之前必须擦掉。这是最后一道保险，调用方拿到的错误信息可以安全
        地显示在界面上。
        """
        out = str(text)
        if self._api_key:
            out = out.replace(self._api_key, "***")
        return out

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }

    def _retry_delay(self, attempt: int, headers: Any = None) -> float:
        """第 ``attempt`` 次失败后等多久。

        429 优先用 ``Retry-After``（夹到 ``RETRY_AFTER_CAP`` 秒以内）；
        没有就指数退避（1s、2s、4s…）。
        """
        retry_after = None
        try:
            if headers is not None:
                retry_after = headers.get("Retry-After")
        except Exception:
            retry_after = None

        if retry_after is not None:
            try:
                seconds = float(str(retry_after).strip())
                if seconds >= 0:
                    return min(seconds, RETRY_AFTER_CAP)
            except (TypeError, ValueError):
                pass

        return min(self.backoff * (2 ** attempt), 60.0)

    def _parse_response(self, raw: bytes, status: int) -> dict[str, Any]:
        text = raw.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"LLM 响应不是合法 JSON（HTTP {status}）：{exc.msg}；"
                f"开头 {self._redact(text[:160])!r}") from exc
        if not isinstance(data, Mapping):
            raise LLMError(
                f"LLM 响应顶层不是对象（HTTP {status}）："
                f"{type(data).__name__}")
        return dict(data)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST 一次（内含 429/5xx/网络错误的重试）。"""
        url = self._endpoint()
        if not url.startswith(("http://", "https://")):
            raise LLMError(
                f"base_url 不规范（需要 http/https 开头）：{url!r}")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = self._headers()

        last_error: LLMError | None = None
        for attempt in range(self.attempts):
            request = urllib.request.Request(
                url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request,
                                            timeout=self.timeout) as response:
                    raw = response.read()
                    status = int(getattr(response, "status", 200) or 200)
                return self._parse_response(raw, status)

            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                try:
                    detail = exc.read(_BODY_LIMIT * 4).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                finally:
                    # HTTPError 是文件对象，不关会触发 ResourceWarning
                    try:
                        exc.close()
                    except Exception:
                        pass
                detail = self._redact(detail)

                if status == 429:
                    if "ModelAccountRpmRate" in detail:
                        hint = ("（账号 RPM 限流：等 1~2 分钟再测；"
                                "如果填的是 Seedream 图片生成接入点，请换成"
                                "对话/视觉理解模型接入点）")
                    else:
                        hint = "（被限流）"
                elif status in (401, 403):
                    hint = "（鉴权失败：检查 api_key 是否正确/过期；本程序不会回显 key）"
                elif status == 404 or "InvalidEndpointOrModel" in detail:
                    hint = ("（模型/接入点不存在：到火山方舟控制台复制 Model ID "
                            "或推理接入点 ID（ep-...），确认 base_url 区域一致，"
                            "并先在控制台开通该模型）")
                elif status >= 500:
                    hint = "（服务端错误）"
                else:
                    hint = ""
                message = f"LLM 请求失败：HTTP {status}{hint}"
                if detail:
                    message += f"：{detail[:_BODY_LIMIT]}"
                error = LLMHTTPError(message, status=status, body=detail)
                last_error = error

                retryable = status == 429 or status >= 500
                if retryable and attempt + 1 < self.attempts:
                    self._sleep(self._retry_delay(attempt, exc.headers))
                    continue
                raise error

            except (urllib.error.URLError, socket.timeout,
                    TimeoutError, OSError) as exc:
                # HTTPError 是 URLError 的子类，上面已经先接走了
                reason = getattr(exc, "reason", exc)
                last_error = LLMRequestError(
                    self._redact(f"LLM 网络错误（{url}）：{reason}"))
                if attempt + 1 < self.attempts:
                    self._sleep(self._retry_delay(attempt))
                    continue
                raise last_error

        raise last_error or LLMError("LLM 请求失败（未知原因）")

    def _extract_text(self, response: Mapping[str, Any]) -> str:
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            error = response.get("error")
            raise LLMError(
                "LLM 响应里没有 choices"
                f"（error={self._redact(error)[:300]!r}）")
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message")
        if not isinstance(message, Mapping):
            message = {}
        text = content_to_text(message)
        if not text:
            # 个别兼容服务把文本放在 choices[0].text
            fallback = first.get("text")
            if isinstance(fallback, str):
                text = fallback
        if not text.strip():
            finish = first.get("finish_reason")
            raise LLMError(
                f"LLM 返回了空内容（finish_reason={finish!r}）")
        return text

    # -- 对外 ---------------------------------------------------------
    def chat(self, messages: list[dict], *, json_mode: bool = False,
             image_jpeg: bytes | None = None,
             max_tokens: int | None = None) -> str:
        """发一组 OpenAI 格式消息，返回模型回复的纯文本。

        ``json_mode=True`` 只是**要求**服务端输出 JSON（带
        ``response_format``），本函数不解析，交给 ``ai_skill.parse_plan``。
        """
        if not self._api_key:
            raise LLMError(
                "没有配置 api_key：请到 AI 托管页填写 API Key（本程序不会回显 key）")
        if not self.model:
            raise LLMError("没有配置模型名（llm.model）")
        if isinstance(messages, (str, bytes)) or not messages:
            raise LLMError("messages 不能为空，且必须是消息对象列表")

        if image_jpeg is not None:
            if not isinstance(image_jpeg, (bytes, bytearray, memoryview)):
                raise LLMError(
                    f"image_jpeg 需要 bytes，收到 {type(image_jpeg).__name__}")
            body_messages = _with_image(list(messages), bytes(image_jpeg))
        else:
            body_messages = []
            for index, message in enumerate(messages):
                if not isinstance(message, Mapping):
                    raise LLMError(
                        f"messages[{index}] 必须是对象，"
                        f"收到 {type(message).__name__}")
                body_messages.append(dict(message))

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": body_messages,
            "temperature": self.temperature,
            "max_tokens": int(self.max_tokens if max_tokens is None
                              else max_tokens),
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        try:
            response = self._post(payload)
        except LLMHTTPError as exc:
            # 有些兼容服务不认 response_format，会回 400；去掉字段再发一次。
            # 只对 400 做这件事：401/403 是 key 的问题，去掉字段也没用。
            if json_mode and exc.status == 400:
                payload.pop("response_format", None)
                response = self._post(payload)
            else:
                raise

        return self._extract_text(response)

    def test(self) -> str:
        """测试连接：发一句 ping，返回模型回复。"""
        return self.chat(
            [{"role": "user", "content": "连通性测试，请只回复两个字符：OK"}],
            max_tokens=min(32, self.max_tokens),
        )
