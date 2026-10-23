"""Local AI preferences. Public settings never contain the API credential."""
from __future__ import annotations

import json
import os
from pathlib import Path

from ai_api import ApiBridge, ApiBridgeError, validate_base_url
from classroom_features import write_json
from key_store import KeyStore, KeyStoreError


class AISettings:
    def __init__(self, data: Path, config: dict, *, store=None):
        self.path = data / "ai-settings.json"
        self.store = store or KeyStore(data / "api-key.dpapi")
        self.values = {"provider": "codex", "base_url": "https://api.openai.com/v1",
                       "model": "", "remember_key": False}
        self.key = ""
        self.warning = None
        self.timeout = config.get("codex_timeout_seconds", 120)
        self.api_factory = ApiBridge
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self.values = self._fields(saved)
                if self.values["remember_key"]:
                    secret = self.store.load()
                    if secret:
                        bound = json.loads(secret)
                        if bound.get("base_url") == self.values["base_url"]:
                            self.key = self._key(bound.get("key", ""))
            except (OSError, ValueError, TypeError, AttributeError, KeyStoreError, ApiBridgeError):
                self.warning = "AI 配置或已保存密钥未能读取，请在 AI 设置中重新配置。"

    @staticmethod
    def _key(value):
        if not isinstance(value, str) or len(value) > 4096 or any(not 33 <= ord(c) <= 126 for c in value.strip()):
            raise ApiBridgeError("API 密钥格式无效，请重新输入。", kind="auth")
        return value.strip()

    def _fields(self, payload):
        provider = payload.get("provider", self.values["provider"])
        if provider not in ("codex", "api"):
            raise ApiBridgeError("请选择本地 Codex 或 API。")
        model = payload.get("model", self.values["model"])
        if not isinstance(model, str) or len(model) > 160 or any(ord(c) < 32 for c in model):
            raise ApiBridgeError("模型名称格式无效。")
        remember = payload.get("remember_key", self.values["remember_key"])
        if not isinstance(remember, bool):
            raise ApiBridgeError("记住密钥必须为开启或关闭。")
        return {"provider": provider,
                "base_url": validate_base_url(payload.get("base_url", self.values["base_url"])),
                "model": model.strip(), "remember_key": remember}

    def prepare(self, payload):
        values = self._fields(payload)
        supplied = self._key(payload.get("api_key") or "")
        # A saved key is bound to its destination. Editing an address must not
        # silently send the previous provider's secret to the new host/path.
        key = supplied or (self.key if values["base_url"] == self.values["base_url"] else "")
        if payload.get("clear_key"):
            key = ""
            values["remember_key"] = False
        return values, key

    def save(self, payload):
        values, key = self.prepare(payload)
        changed = values != self.values or key != self.key
        routing_changed = any(values[k] != self.values[k] for k in ("provider", "base_url", "model")) or key != self.key
        try:
            previous_secret = self.store.load()
        except (OSError, KeyStoreError):
            if payload.get("clear_key"):
                previous_secret = None
            else:
                raise ApiBridgeError("无法读取已有密钥以安全保存设置，请先清除或修复本机密钥存储。") from None
        try:
            if values["remember_key"] and key:
                self.store.save(json.dumps({"base_url": values["base_url"], "key": key}, separators=(",", ":")))
            else:
                self.store.delete()
            write_json(self.path, values)
        except (OSError, KeyStoreError):
            try:
                if previous_secret:
                    self.store.save(previous_secret)
                else:
                    self.store.delete()
            except (OSError, KeyStoreError):
                # Keep the working in-memory credential, but never claim it is
                # safely persisted after an unsuccessful rollback.
                self.values["remember_key"] = False
                self.warning = "密钥存储恢复失败；当前密钥仅按本次使用处理，请重新保存配置。"
            raise ApiBridgeError("未能保存 AI 设置或密钥；可取消“记住密钥”后重试。") from None
        self.values, self.key = values, key
        self.warning = None
        return changed, routing_changed

    def public(self):
        return {**self.values, "key_configured": bool(self.key),
                "key_persisted": bool(self.key and self.values["remember_key"]),
                "storage_available": os.name == "nt", "warning": self.warning}

    def api_bridge(self, on_delta=None, payload=None):
        values, key = self.prepare(payload) if payload is not None else (dict(self.values), self.key)
        if not values["model"]:
            raise ApiBridgeError("请先填写 API 模型名称。", kind="auth")
        if not key:
            raise ApiBridgeError("请先填写 API 密钥；更换接口地址后需要重新输入密钥。", kind="auth")
        return self.api_factory({**values, "timeout_seconds": self.timeout}, key, on_delta=on_delta)


def empty_usage():
    return {"requests": 0, "completed": 0, "failed": 0, "unknown": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0, "usage_reported_requests": 0}


def record_usage(counter, metadata, success):
    counter["completed" if success else "failed"] += 1
    usage = metadata.get("usage")
    if not isinstance(usage, dict):
        return
    reported = False
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counter[key] += value
            reported = True
    if reported:
        counter["usage_reported_requests"] += 1
