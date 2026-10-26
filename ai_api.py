"""Chat Completions adapter for text and local images; diagnostics stay private.

Each call sends exactly one HTTP request. Streaming deltas are provisional:
the caller must only persist the return value as the completed answer.
"""
from __future__ import annotations

import base64
import codecs
import copy
import ipaddress
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from codex_bridge import (CodexBridge, CodexBridgeError, _INSTRUCTIONS, _check_cancelled,
                          _failure_kind, _validate_references, _vision_unsupported)


class ApiBridgeError(CodexBridgeError):
    """Only application-authored messages may cross the backend boundary."""


_MESSAGES = {
    "auth": "API 密钥无效或没有访问权限，请检查密钥与服务商设置。",
    "quota": "API 额度不足或请求受到限流，请检查服务商账户后再试；本机转写仍会保存。",
    "network": "暂时无法连接 API 服务，请检查网络和服务地址；本机转写仍会保存。",
    "timeout": "这次 API 回答超时了，请稍后手动重试；本机转写仍会保存。",
    "vision_unsupported": "当前 API 模型明确不支持图片输入，请换用支持图片的模型，或主动选择仅用文字重新提问。",
    "cancelled": "本次请求已取消，未采用回答；已发送的请求仍可能产生用量。",
    "other": "API 没有完成回答，请检查地址、模型和接口兼容性后再试。",
}
_MAX_RESPONSE_BYTES = 2_000_000
_MAX_ANSWER_CHARS = 200_000


def _failure(kind: str = "other", message: str | None = None) -> ApiBridgeError:
    return ApiBridgeError(message or _MESSAGES[kind], kind=kind)


def validate_base_url(value: str) -> str:
    """Return an API root, optionally removing a pasted chat endpoint.

HTTP is permitted only for a literal loopback address or localhost. Nothing
is fetched or resolved during validation. A version/path is never guessed.
"""
    invalid = "API 地址无效。请填写 HTTPS API 根地址；本机服务可使用 HTTP。"
    if not isinstance(value, str):
        raise _failure(message=invalid)
    value = value.strip()
    if not value or len(value) > 2048 or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
        raise _failure(message=invalid)
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme.lower() not in {"https", "http"} or not host
                or parts.username is not None or parts.password is not None
                or "@" in parts.netloc or "?" in value or "#" in value
                or "%" in parts.netloc or parts.netloc.endswith(":")):
            raise ValueError()
        host = host.rstrip(".").lower()
        if not host or port is not None and not 1 <= port <= 65535:
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            host = host.encode("idna").decode("ascii")
            if len(host) > 253 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
                raise ValueError()
        if parts.scheme.lower() == "http" and not (host == "localhost" or address and address.is_loopback):
            raise ValueError()
        path = parts.path.rstrip("/")
        decoded = unquote(path)
        if (any(c.isspace() or ord(c) < 32 for c in decoded) or any(c in decoded for c in "\\?#%")
                or "//" in decoded or any(piece in {".", ".."} for piece in decoded.split("/"))):
            raise ValueError()
        path = decoded.rstrip("/")
        # Normalize the common full-endpoint paste without changing a custom root.
        if path.endswith("/chat/completions"):
            path = path[:-len("/chat/completions")]
        if path.endswith(("/chat", "/completions", "/responses")):
            raise ValueError()
        netloc = f"[{host}]" if ":" in host else host
        if port is not None:
            netloc += f":{port}"
        return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))
    except (ValueError, UnicodeError):
        raise _failure(message=invalid) from None


def _usage(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    keys = ("prompt_tokens", "completion_tokens", "total_tokens")
    if not all(type(value.get(key)) is int and 0 <= value[key] <= 2**53 - 1 for key in keys[:2]):
        return None
    total = value.get("total_tokens", value["prompt_tokens"] + value["completion_tokens"])
    if type(total) is not int or not 0 <= total <= 2**53 - 1:
        return None
    return {"prompt_tokens": value["prompt_tokens"], "completion_tokens": value["completion_tokens"], "total_tokens": total}


def _check_payload_error(payload):
    if not isinstance(payload, dict):
        raise _failure()
    if "error" in payload and payload["error"] is not None:
        # Classify privately; never expose the server's arbitrary message.
        diagnostic = json.dumps(payload["error"], ensure_ascii=True).lower()
        kind = _failure_kind(diagnostic)
        if any(code in diagnostic for code in ("invalid_api_key", "authentication_error", "permission_denied", "forbidden")):
            kind = "auth"
        elif "resource_exhausted" in diagnostic:
            kind = "quota"
        elif "server_error" in diagnostic:
            kind = "network"
        raise _failure(kind)


class ApiBridge(CodexBridge):
    """Reuse the classroom prompts while replacing only their transport."""

    def __init__(self, config: dict, api_key: str, on_delta: Callable[[str], None] | None = None,
                 *, transport: httpx.BaseTransport | None = None):
        # Do not initialize CodexBridge: API use needs no CLI or working folder.
        self.base_url = validate_base_url(config.get("base_url", ""))
        model = config.get("model", "")
        if not isinstance(model, str) or not model.strip() or len(model) > 256 or any(ord(c) < 32 for c in model):
            raise _failure(message="请填写有效的 API 模型名称。")
        if (not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 4096
                or any(not 33 <= ord(c) <= 126 for c in api_key.strip())):
            raise _failure("auth", "请填写有效的 API 密钥。")
        try:
            timeout = float(config.get("timeout_seconds", 120))
            if not math.isfinite(timeout) or not 1 <= timeout <= 600:
                raise ValueError()
            context_chars = int(config.get("context_chars", 48000))
        except (TypeError, ValueError, OverflowError):
            raise _failure(message="API 超时或上下文设置无效。") from None
        self.model = model.strip()
        self.timeout = timeout
        self.context_chars = max(4000, min(120000, context_chars))
        self._api_key = api_key.strip()
        self.on_delta = on_delta
        self._transport = transport
        self._lock = threading.Lock()
        self._request_lock = threading.RLock()
        self._local = threading.local()
        self._active = 0
        self._inference_verified = False
        self._last_seconds = None
        self._last_success_at = None
        self._last_error = None
        self._last_error_kind = None
        self._last_usage = None

    def metadata(self) -> dict:
        with self._lock:
            return {"provider": "api", "model": self.model, "usage": copy.deepcopy(self._last_usage)}

    def status(self) -> dict:
        """Configuration is distinct from a verified inference; never probes."""
        with self._lock:
            return {"provider": "api", "model": self.model, "configured": True,
                    "inference_verified": self._inference_verified,
                    "last_inference_seconds": self._last_seconds, "last_success_at": self._last_success_at,
                    "last_error": self._last_error, "last_error_kind": self._last_error_kind,
                    "busy": self._active > 0, "active_requests": self._active}

    def generate_glossary(self, course_name: str, syllabus: str = "") -> list[dict]:
        previous = getattr(self._local, "suppress_delta", False)
        self._local.suppress_delta = True
        try:
            return super().generate_glossary(course_name, syllabus)
        finally:
            self._local.suppress_delta = previous

    def test_connection(self) -> dict:
        self._run("Reply only OK.", "", emit=False, max_tokens=8,
                  instructions="This is a synthetic connection test. Reply only OK.")
        return {"ok": True, **self.metadata()}

    def _save_usage(self, value):
        usage = _usage(value)
        if usage is not None:
            with self._lock:
                self._last_usage = usage

    def _emit(self, content, cancel_event=None):
        _check_cancelled(cancel_event)
        if self.on_delta:
            try:
                self.on_delta(content)
            except Exception:
                raise _failure(message="暂时无法显示 API 回答，本次回答未完成。") from None
        _check_cancelled(cancel_event)

    def _run(self, prompt: str, source: str, *, emit: bool = True,
             max_tokens: int | None = None, instructions: str = _INSTRUCTIONS,
             images: list[dict] | None = None, cancel_event=None, allowed_refs: set[str] | None = None) -> str:
        if cancel_event is None:
            self._request_lock.acquire()
        else:
            _check_cancelled(cancel_event)
            while not self._request_lock.acquire(timeout=.1):
                _check_cancelled(cancel_event)
        try:
            _check_cancelled(cancel_event)
            return self._request(prompt, source, emit=emit and not getattr(self._local, "suppress_delta", False),
                                 max_tokens=max_tokens, instructions=instructions, images=images,
                                 cancel_event=cancel_event, allowed_refs=allowed_refs)
        finally:
            self._request_lock.release()

    def _request(self, prompt, source, *, emit, max_tokens, instructions,
                 images=None, cancel_event=None, allowed_refs=None):
        started = time.monotonic()
        with self._lock:
            self._active += 1
            self._last_usage = None
        cancel_watch_stop = threading.Event()
        cancel_watcher = None
        current_response = [None]
        try:
            _check_cancelled(cancel_event)
            content = prompt
            if images is not None:
                content = [{"type": "text", "text": prompt}]
                for image in images:
                    _check_cancelled(cancel_event)
                    # Files have already been validated/normalized by the server.
                    encoded = base64.b64encode(Path(image["path"]).read_bytes()).decode("ascii")
                    content.extend([{"type": "text", "text": image["id"]},
                                    {"type": "image_url", "image_url": {
                                        "url": "data:" + image["mime_type"] + ";base64," + encoded}}])
            body = {"model": self.model, "messages": [{"role": "system", "content": instructions},
                    {"role": "user", "content": content}], "stream": True, "stream_options": {"include_usage": True}}
            if max_tokens is not None:
                body["max_tokens"] = max_tokens
            _check_cancelled(cancel_event)
            # No redirects, SDK retries, environment proxies, cookies, or tools.
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False,
                              transport=self._transport) as client:
                if cancel_event is not None:
                    def watch_cancel():
                        while not cancel_watch_stop.wait(.05):
                            if cancel_event.is_set():
                                # Best effort: close the current socket/stream to
                                # unblock reads. This cannot undo remote billing.
                                response = current_response[0]
                                for resource in (response, client):
                                    if resource is not None:
                                        try:
                                            resource.close()
                                        except Exception:
                                            pass
                                return
                    cancel_watcher = threading.Thread(target=watch_cancel, daemon=True,
                                                       name="classroom-api-cancel")
                    cancel_watcher.start()
                _check_cancelled(cancel_event)
                with client.stream("POST", self.base_url + "/chat/completions", json=body,
                                   headers={"Authorization": "Bearer " + self._api_key,
                                            "Accept": "text/event-stream, application/json"}) as response:
                    current_response[0] = response
                    _check_cancelled(cancel_event)
                    code = response.status_code
                    if not 200 <= code < 300:
                        kind = "auth" if code in (401, 403) else "quota" if code in (402, 429) else "timeout" if code in (408, 504) else "network" if code >= 500 else "other"
                        if images and code in (400, 415, 422):
                            diagnostic = self._error_diagnostic(response, started, cancel_event)
                            if _vision_unsupported(diagnostic):
                                kind = "vision_unsupported"
                        raise _failure(kind)
                    answer = self._read_response(response, started, emit, cancel_event)
            _check_cancelled(cancel_event)
            _validate_references(answer, source, allowed_refs)
            with self._lock:
                self._inference_verified = True
                self._last_success_at = time.time()
                self._last_error = self._last_error_kind = None
            return answer
        except httpx.TimeoutException:
            error = _failure("timeout")
        except (httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError):
            error = _failure("network")
        except CodexBridgeError as caught:
            error = _failure(caught.kind, str(caught))
        except Exception:
            # Never serialize raw HTTP errors, response data or callback errors.
            error = _failure()
        finally:
            cancel_watch_stop.set()
            if cancel_watcher is not None:
                cancel_watcher.join(timeout=.2)
            with self._lock:
                self._active -= 1
                self._last_seconds = round(time.monotonic() - started, 2)
        if cancel_event is not None and cancel_event.is_set():
            error = _failure("cancelled")
        with self._lock:
            self._last_error, self._last_error_kind = str(error), error.kind
        raise error from None

    def _error_diagnostic(self, response, started, cancel_event):
        """Read a small private error prefix, solely for capability detection."""
        chunks = bytearray()
        for chunk in response.iter_bytes():
            _check_cancelled(cancel_event)
            if time.monotonic() - started > self.timeout:
                raise _failure("timeout")
            chunks.extend(chunk[:32768 - len(chunks)])
            if len(chunks) >= 32768:
                break
        return chunks.decode("utf-8", errors="replace")

    def _lines(self, response, started, cancel_event=None):
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        buffer = ""
        size = 0
        for chunk in response.iter_bytes():
            _check_cancelled(cancel_event)
            if time.monotonic() - started > self.timeout:
                raise _failure("timeout")
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise _failure(message="API 返回的内容过长，本次回答未采用。")
            buffer += decoder.decode(chunk)
            while "\n" in buffer:
                _check_cancelled(cancel_event)
                line, buffer = buffer.split("\n", 1)
                yield line.rstrip("\r")
        buffer += decoder.decode(b"", final=True)
        _check_cancelled(cancel_event)
        if buffer:
            yield buffer.rstrip("\r")

    def _read_response(self, response, started, emit, cancel_event=None):
        lines = self._lines(response, started, cancel_event)
        # Some compatible services return JSON even when stream=true.
        first = next(lines, "").lstrip("\ufeff")
        while not first.strip():
            try:
                first = next(lines)
            except StopIteration:
                raise _failure(message="API 没有返回可显示的回答。") from None
        if first.lstrip().startswith("{") or "application/json" in response.headers.get("content-type", "").lower():
            payload = json.loads("\n".join([first, *lines]))
            answer = self._read_json(payload)
            if emit:
                self._emit(answer, cancel_event)
            return answer
        data_lines = []
        answer_parts = []
        finished = False
        done = False
        answer_size = 0

        def consume():
            nonlocal finished, done, answer_size
            _check_cancelled(cancel_event)
            if not data_lines:
                return
            data = "\n".join(data_lines)
            data_lines.clear()
            if data.strip() == "[DONE]":
                done = True
                return
            event = json.loads(data)
            if isinstance(event, dict):
                self._save_usage(event.get("usage"))
            _check_payload_error(event)
            choices = event.get("choices", [])
            if not isinstance(choices, list):
                raise _failure()
            for choice in choices:
                if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                    continue
                delta = choice.get("delta", {})
                if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("function_call") or delta.get("refusal"):
                    raise _failure()
                content = delta.get("content")
                if content is not None and not isinstance(content, str):
                    raise _failure()
                if content:
                    if finished or done:
                        raise _failure(message="API 在结束标志后继续输出，本次回答未采用。")
                    answer_size += len(content)
                    if answer_size > _MAX_ANSWER_CHARS:
                        raise _failure(message="API 返回的内容过长，本次回答未采用。")
                    answer_parts.append(content)
                    if emit:
                        self._emit(content, cancel_event)
                reason = choice.get("finish_reason")
                if reason is not None:
                    self._check_finish(reason)
                    finished = True

        for line in _prepend(first, lines):
            _check_cancelled(cancel_event)
            if line == "":
                consume()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif not line.startswith((":", "event:", "id:", "retry:")):
                raise _failure(message="API 返回格式不兼容，请检查 Chat Completions 接口地址。")
        consume()
        if not (finished or done):
            raise _failure(message="API 回答未完整结束，本次回答未采用，请手动重试。")
        answer = "".join(answer_parts).strip()
        if not answer:
            raise _failure(message="API 没有返回可显示的回答。")
        return answer

    def _read_json(self, payload):
        if isinstance(payload, dict):
            self._save_usage(payload.get("usage"))
        _check_payload_error(payload)
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _failure()
        choice = choices[0]
        self._check_finish(choice.get("finish_reason"))
        message = choice.get("message", {})
        if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
            raise _failure()
        answer = message.get("content")
        if not isinstance(answer, str) or not answer.strip():
            raise _failure(message="API 没有返回可显示的回答。")
        if len(answer) > _MAX_ANSWER_CHARS:
            raise _failure(message="API 返回的内容过长，本次回答未采用。")
        return answer.strip()

    @staticmethod
    def _check_finish(reason):
        if reason == "length":
            raise _failure(message="API 回答因长度限制被截断，本次回答未采用。请调整模型设置后重试。")
        if reason == "content_filter":
            raise _failure(message="API 服务限制了这次输出，本次回答未采用。")
        if reason != "stop":
            raise _failure(message="API 回答未正常结束，本次回答未采用。")


def _prepend(first, iterator):
    yield first
    yield from iterator
