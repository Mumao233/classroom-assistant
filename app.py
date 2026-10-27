"""Local classroom recorder, transcription queue and Codex study companion."""
from __future__ import annotations

import copy
import io
import json
import math
import os
import queue
import re
import threading
import time
import uuid
import wave
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from asr import ASREngine
from codex_bridge import CodexBridge, CodexBridgeError, _validate_references
from ai_settings import AISettings, empty_usage, record_usage
from version import __version__
from classroom_features import ClassroomFeatures, wav_duration
from course_catalog import glossary_terms, validate_glossary
from session_order import creation_order, newest_first
from input_limits import INPUT_LIMITS, require_content, validation_message
from study_workflow import StudyWorkflow, UNKNOWN_TIMEOUT, UNKNOWN_RESULT
from slide_context import image_dimensions, image_size_message

ROOT = Path(__file__).resolve().parent


def stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def speech_units(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+|[\u3400-\u9fff]", text))


def sentence_finished(text: str) -> bool:
    return bool(re.search(r"[.!?。！？][\"'”’）)]?\s*$", text))


def boundary_before(pending: list[dict], segment: dict) -> bool:
    """Conservative discourse cues; these are candidates, not perfect semantics."""
    if not pending or speech_units(" ".join(x["text"] for x in pending)) < 40:
        return False
    if not sentence_finished(pending[-1]["text"]):
        return False
    return bool(re.match(
        r"\s*(?:now (?:let['’]?s|we)|next[, ]|moving on|in summary|to summarize|"
        r"another (?:point|example)|接下来|下面我们|换一个|总结一下|另外一个)",
        segment["text"], re.I))


def boundary_after(pending: list[dict]) -> bool:
    if not pending:
        return False
    text = " ".join(s["text"] for s in pending)
    span = pending[-1]["end"] - pending[0]["start"]
    units = speech_units(text)
    # A long unfinished argument gets a labelled partial note, not a conclusion.
    return (sentence_finished(text) and (span >= 28 or units >= 100)) or span >= 45 or units >= 180


def classroom_time(seconds):
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def source_line(seg):
    return f"[{seg['id']}] {classroom_time(seg['start'])}–{classroom_time(seg['end'])} {seg['text']}"


class Classroom(StudyWorkflow, ClassroomFeatures):
    def __init__(self, data: Path, config: dict, engine=None, bridge=None):
        self.data = data
        data.mkdir(parents=True, exist_ok=True)
        self.config = dict(config)
        try:
            prefs = json.loads((data / "preferences.json").read_text(encoding="utf-8"))
            if prefs.get("mode") in ("auto", "battery", "quality"):
                self.config["mode"] = prefs["mode"]
        except (OSError, ValueError, TypeError):
            pass
        self.lock = threading.RLock()
        self.engine = engine or ASREngine(self.config)
        self.bridge = bridge or CodexBridge(self.config, data / "ai-work")
        self.connection = AISettings(data, self.config)
        self.active_ai = None
        self.ai_switch_pending = False
        self.api_last_status = {}
        self.sessions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.audio_queue: queue.Queue = queue.Queue()
        self.ai_queue: queue.PriorityQueue = queue.PriorityQueue()
        self.order = 0
        self.closed = threading.Event()
        self.threads = []
        self.warnings = []
        self.init_study()
        self.init_features()
        for path in sorted(data.glob("*/session.json")):
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
                if s.get("id") != path.parent.name:
                    raise ValueError("session id mismatch")
                for key in ("segments", "groups", "questions", "audio", "revisions", "slides"):
                    s.setdefault(key, [])
                s.setdefault("settings", {"terms": "", "auto_analyze": True})
                s.setdefault("pending_ids", [])
                s.setdefault("flush_requested", False)
                s.setdefault("ai_usage", empty_usage())
                usage = s["ai_usage"]
                usage.setdefault("unknown", 0)
                # An attempt saved before submission may have reached the
                # provider even when this process never received its result.
                usage["unknown"] += max(0, usage["requests"] - usage["completed"] - usage["failed"] - usage["unknown"])
                # Interrupted AI work is not silently represented as completed.
                for item, is_ai in ([(record, True) for record in s['groups'] + s['questions']]
                                    + [(record, False) for record in s['revisions']]):
                    legacy_unknown = is_ai and (item.get('status') == 'running' or
                                      item.get('status') in ('paused', 'error') and item.get('error_kind') in ('timeout', 'network'))
                    if legacy_unknown and not item.get('dispatch_completed_at'):
                        item.update(result_unknown=True, dispatch_state='unknown',
                                    error=UNKNOWN_TIMEOUT if item.get('error_kind') == 'timeout' else UNKNOWN_RESULT)
                    if item.get("status") in ("pending", "running"):
                        if item.get('result_unknown') or item.get('dispatch_started_at') and not item.get('dispatch_completed_at'):
                            item.update(status='error', result_unknown=True, dispatch_state='unknown', error=UNKNOWN_RESULT)
                        else:
                            item.update(status="error", error="上次运行中断，可重试这一项。")
                    if item.get("stream_status") == "streaming":
                        item["stream_status"] = "interrupted"
                self.sessions[s["id"]] = s
            except (OSError, ValueError, TypeError):
                self.warnings.append(f"有一份历史课程文件无法读取，原文件已保留：{path.parent.name}")
        self.current = newest_first(self.sessions.values())[0]["id"] if self.sessions else self.new_session("新的课堂")["id"]

    def start(self):
        for fn, name in ((self._audio_loop, "transcription"), (self._ai_loop, "study"), (self._idle_loop, "grouping")):
            thread = threading.Thread(target=fn, name=name, daemon=True)
            thread.start()
            self.threads.append(thread)
        # WAV files already accepted are recoverable after a server restart.
        with self.lock:
            for s in self.sessions.values():
                for audio in s["audio"]:
                    if audio.get("status") in ("pending", "running"):
                        self._schedule_audio(s, audio)

    def stop(self):
        self.closed.set()
        for thread in self.threads:
            thread.join(timeout=1)
        # The single ASR worker owns unloading after its current chunk finishes.
        if not self.threads and hasattr(self.engine, "unload"):
            self.engine.unload()

    def save(self, s):
        folder = self.data / s["id"]
        folder.mkdir(exist_ok=True)
        atomic_json(folder / "session.json", s)

    def get(self, sid=None):
        try:
            return self.sessions[sid or self.current]
        except KeyError:
            raise HTTPException(404, "找不到这节课。")

    def new_session(self, title="新的课堂", demo=False):
        with self.lock:
            sid = uuid.uuid4().hex
            s = {"id": sid, "title": title[:160] or "新的课堂", "created_at": stamp(),
                 "creation_order": max((creation_order(existing) for existing in self.sessions.values()), default=0) + 1,
                 "demo": demo, "segments": [], "groups": [], "questions": [], "audio": [], "revisions": [], "slides": [], "course_id": None,
                 "settings": {"terms": "", "auto_analyze": not demo and self.connection.values["provider"] != "api"}, "pending_ids": [],
                 "ai_usage": empty_usage(),
                 "flush_requested": False, "last_text_at": 0}
            self.sessions[sid] = s
            self.current = sid
            self.save(s)
            return copy.deepcopy(s)

    def state(self):
        # Hardware status can take the ASR lock; do not hold the session lock while reading it.
        asr_status = self.engine.status()
        with self.lock:
            provider = self.connection.values["provider"]
            active = copy.deepcopy(self.active_ai)
        codex_status = self.bridge.status() if provider == "codex" or (active and active["provider"] == "codex") else {}
        with self.lock:
            active = self.get()
            remaining = [a for a in active["audio"] if a.get("status") in ("pending", "running")]
            newest = max((a["start"] + a["duration"] for a in active["audio"]), default=0)
            lag = max(0, newest - min((a["start"] for a in remaining), default=newest))
            soundcheck = {k: v for k, v in self.soundcheck.items() if k not in ("path", "prompt")}
            return {"session": self.study_session_public(active),
                    "courses": copy.deepcopy(self.courses), "ai": copy.deepcopy(self.ai), "soundcheck": soundcheck,
                    "sessions": [{k: s[k] for k in ("id", "title", "created_at", "demo")} for s in newest_first(self.sessions.values())],
                    "status": {"asr": asr_status, "codex": codex_status, "ai": self.ai_status(codex_status)},
                    "version": __version__, "input_limits": dict(INPUT_LIMITS), "ai_settings": self.ai_settings(),
                    "ai_usage": copy.deepcopy(active.get("ai_usage", empty_usage())),
                    "jobs": [copy.deepcopy(j) for j in list(self.jobs.values())[-120:] if j["session_id"] == active["id"]],
                    "queue": {"audio": sum(j["kind"] == "audio" and j["status"] in ("pending", "running") for j in self.jobs.values()),
                              "ai": sum(j["kind"] in ("question", "analysis", "glossary") and j["status"] in ("pending", "running", "paused") for j in self.jobs.values()),
                              "lag_seconds": round(lag, 1)},
                    "warnings": self.warnings}

    def ai_settings(self):
        with self.lock:
            values = self.connection.public()
            current = self.active_ai or {"provider": values["provider"], "model": values["model"] if values["provider"] == "api" else self.config.get("codex_model")}
            return {**values, "pending": self.ai_switch_pending, "busy": self.study_busy(),
                    "active_provider": current["provider"], "active_model": current.get("model")}

    def ai_status(self, codex_status=None):
        values = self.ai_settings()
        if values["active_provider"] == "codex":
            status = dict(codex_status or {})
            configured = bool(status.get("installed") and status.get("logged_in"))
        else:
            status = dict(self.api_last_status)
            configured = bool(values["key_configured"] and values["model"])
        return {**status, "provider": values["active_provider"], "model": values["active_model"],
                "configured": configured, "busy": self.active_ai is not None,
                "pending": values["pending"]}

    def configure_ai(self, payload):
        with self.lock:
            try:
                desired, key = self.connection.prepare(payload)
                routing_changed = any(desired[k] != self.connection.values[k] for k in ("provider", "base_url", "model")) or key != self.connection.key
                if routing_changed and self.study_busy() and not payload.get("force"):
                    raise HTTPException(409, "正在加载，暂不能更换 AI 接入；可选择强制更换并重新确认发送目的地。")
                if routing_changed:
                    has_waiting = any(j["kind"] in ("question", "analysis", "glossary") and j["status"] in ("pending", "paused", "running") for j in self.jobs.values())
                    keep_paused = bool(self.active_ai or has_waiting or self.ai["paused"])
                    self.ai_switch_pending = self.active_ai is not None
                    # Freeze before ANY routing/default persistence. Even a
                    # full disk must never move queued work to a new provider.
                    self._pause_ai("switch", "AI 接入设置正在切换。当前请求完成后，请选择补分析或从最新继续。")
                    if desired["provider"] == "api":
                        for session in self.sessions.values():
                            session["settings"]["auto_analyze"] = False
                        for session in self.sessions.values():
                            self.save(session)
                self.connection.save(payload)
                if routing_changed:
                    self.invalidate_study_destination()
                    self.api_last_status = {}
                    if not keep_paused:
                        resumed = {**self.ai, "paused": False, "kind": None, "reason": None}
                        atomic_json(self.data / "ai-state.json", resumed)
                        self.ai.update(resumed)
                        self.ai_ready.set()
            except CodexBridgeError as exc:
                raise HTTPException(400, str(exc)) from None
            except OSError:
                raise HTTPException(507, "本机存储写入失败，设置可能已部分保存；AI 队列保持暂停，请重新查看当前接入，再检查存储后重试。") from None
            return self.ai_settings()

    def test_ai(self, payload):
        # Explicit user action only. Do not save draft settings or submit a
        # classroom transcript, and never silently retry a billable request.
        with self.lock:
            provider = payload.get("provider", self.connection.values["provider"])
            if provider == "codex":
                bridge = None
            else:
                try:
                    bridge = self.connection.api_bridge(payload=payload)
                except CodexBridgeError as exc:
                    raise HTTPException(400, str(exc)) from None
        if bridge is None:
            status = self.bridge.status()
            ok = bool(status.get("installed") and status.get("logged_in"))
            return {"ok": ok, "provider": "codex", "model": self.config.get("codex_model"),
                    "usage": None, "message": "已检测到本地 Codex 登录；未提交 AI 请求。" if ok else "请先安装并登录本地 Codex。"}
        try:
            result = bridge.test_connection()
            return {**result, "provider": "api", "message": "API 测试成功；本次仅发送简短测试文字。"}
        except CodexBridgeError as exc:
            raise HTTPException(400, str(exc)) from None

    def job(self, sid, kind):
        job = {"id": uuid.uuid4().hex, "session_id": sid, "kind": kind, "status": "pending", "created_at": stamp()}
        self.jobs[job["id"]] = job
        # Keep bounded display history while preserving all unfinished work.
        if len(self.jobs) > 500:
            for key, old in list(self.jobs.items()):
                if old["status"] in ("done", "error", "cancelled", "skipped", "superseded"):
                    del self.jobs[key]
                    if len(self.jobs) <= 300:
                        break
        return job

    def settings(self, values):
        with self.lock:
            s = self.get(values.get('session_id'))
            if "title" in values:
                s["title"] = values["title"][:160] or "新的课堂"
            for key in ("terms", "auto_analyze"):
                if key in values:
                    s["settings"][key] = values[key]
            if values.get("auto_analyze") is False:
                for g in s["groups"]:
                    if g.get("automatic") and g.get("status") in ("pending", "paused"):
                        g["status"] = "saved"
                        if g.get("job_id") in self.jobs:
                            self.jobs[g["job_id"]]["status"] = "cancelled"
            self.save(s)

    def _pending(self, s):
        wanted = set(s["pending_ids"])
        return [seg for seg in s["segments"] if seg["id"] in wanted]

    def append_text(self, sid, text, start=None, end=None, source="manual", audio_seq=None):
        with self.lock:
            s = self.get(sid)
            text = text.strip()
            if not text:
                return None
            if start is None:
                start = s["segments"][-1]["end"] if s["segments"] else 0
                end = start + max(2, speech_units(text) / 2.5)
            seg = {"id": f"S{len(s['segments']) + 1:04d}", "start": round(start, 3), "end": round(end, 3),
                   "text": text, "source": source, "created_at": stamp()}
            if audio_seq is not None:
                seg["audio_seq"] = audio_seq
            pending = self._pending(s)
            if boundary_before(pending, seg):
                self._flush(s)
            s["segments"].append(seg)
            s["pending_ids"].append(seg["id"])
            s["last_text_at"] = time.time()
            if boundary_after(self._pending(s)):
                self._flush(s)
            self.save(s)
            return copy.deepcopy(seg)

    def _flush(self, s):
        pending = self._pending(s)
        if not pending:
            return None
        group = {"id": f"G{len(s['groups']) + 1:04d}", "segment_ids": [p["id"] for p in pending],
                 "start": pending[0]["start"], "end": pending[-1]["end"],
                 "text": " ".join(p["text"] for p in pending), "analysis": "", "status": "saved",
                 "partial": not sentence_finished(pending[-1]["text"]), "version": 0}
        previous = s["groups"][-1] if s["groups"] else None
        if (s["settings"].get("auto_analyze") and not s["demo"] and previous
                and previous.get("automatic") and previous["status"] in ("pending", "paused")
                and not previous.get("stale") and group["end"] - previous["start"] <= 120):
            previous["segment_ids"].extend(group["segment_ids"])
            previous.update(text=previous["text"] + " " + group["text"], end=group["end"], partial=group["partial"],
                            merged_count=previous.get("merged_count", 1) + 1)
            s["pending_ids"] = []
            self.save(s)
            return previous
        s["groups"].append(group)
        s["pending_ids"] = []
        if s["settings"].get("auto_analyze") and not s["demo"]:
            self._enqueue_ai(s, group, "analysis", automatic=True)
        self.save(s)
        return group

    def flush(self, sid=None):
        with self.lock:
            s = self.get(sid)
            if any(a["status"] in ("pending", "running") for a in s["audio"]):
                s["flush_requested"] = True
                self.save(s)
                return {"ok": True, "waiting_for_audio": True}
            group = self._flush(s)
            return {"ok": True, "group_id": group["id"] if group else None}

    def accept_audio(self, sid, seq, start, body):
        duration = wav_duration(body)
        if not math.isfinite(start) or start < 0 or start > 86400 * 7:
            raise HTTPException(400, "音频时间无效。")
        with self.lock:
            s = self.get(sid)
            if s["demo"]:
                raise HTTPException(400, "请新建真实课程后开始录音。")
            old = next((a for a in s["audio"] if a["seq"] == seq), None)
            if old:
                return {"job_id": old.get("job_id"), "duplicate": True, "status": old["status"]}
            if any(r["status"] in ("pending", "running") for r in s["revisions"]):
                raise HTTPException(409, "这节课正在课后修订，请等待完成，或新建课程录音。")
            if self.audio_queue.qsize() >= 120:
                raise HTTPException(429, "转写队列已满，音频仍留在浏览器等待重试。")
            folder = self.data / sid / "audio"
            folder.mkdir(exist_ok=True)
            path = folder / f"{seq}.wav"
            temp = path.with_suffix(".tmp")
            temp.write_bytes(body)
            os.replace(temp, path)
            a = {"seq": seq, "start": start, "duration": duration, "path": f"audio/{seq}.wav", "status": "pending"}
            s["audio"].append(a)
            s.pop("audio_deleted_at", None)
            self._schedule_audio(s, a)
            self.save(s)
            return {"job_id": a["job_id"], "duration": duration}

    def _schedule_audio(self, s, audio):
        job = self.job(s["id"], "audio")
        audio.update(job_id=job["id"], status="pending")
        audio.pop("error", None)
        self.audio_queue.put_nowait((s["id"], audio["seq"], job["id"]))

    def _audio_loop(self):
        try:
            self._audio_work()
        finally:
            if hasattr(self.engine, "unload"):
                self.engine.unload()

    def _audio_work(self):
        while not self.closed.is_set():
            try:
                sid, seq, jid = self.audio_queue.get(timeout=.4)
            except queue.Empty:
                self._maintenance_step()
                continue
            with self.lock:
                s = self.get(sid)
                a = next(x for x in s["audio"] if x["seq"] == seq)
                a["status"] = self.jobs[jid]["status"] = "running"
                terms = self.asr_prompt(s)
            try:
                with self.lock:
                    recovered = next((seg for seg in s["segments"] if seg.get("audio_seq") == seq), None)
                result = {"text": "", "reason": "recovered"} if recovered else self.engine.transcribe(self.registered_audio_path(s, a), prompt=terms)
                text = result.get("text", "").strip()
                with self.lock:
                    if text:
                        seg = self.append_text(sid, text, a["start"], a["start"] + a["duration"], "audio", seq)
                        saved = s["segments"][-1]
                        saved["needs_review"] = any(p.get("needs_review", False) for p in result.get("segments", []))
                    # A crash before this marker is recovered by matching audio_seq.
                    a.update(status="done", elapsed=result.get("elapsed"), device=result.get("device"), language=result.get("language"),
                             reason=result.get("reason"), warning=result.get("warning") if result.get("discarded_suspected_hallucinations") else None,
                             retry_without_prompt=result.get("retry_without_prompt", False),
                             recovered_from_hallucination=result.get("recovered_from_hallucination", False),
                             model_name=result.get("model_name"),
                             transcription_segments=result.get("segments", []))
                    self.jobs[jid].update(status="done", elapsed=result.get("elapsed"))
            except Exception as exc:
                with self.lock:
                    error = "转写失败，原音频已保存。" + str(exc)[:260]
                    a.update(status="error", error=error)
                    self.jobs[jid].update(status="error", error=error)
            finally:
                with self.lock:
                    if s.get("flush_requested") and not any(x["status"] in ("pending", "running") for x in s["audio"]):
                        s["flush_requested"] = False
                        self._flush(s)
                    self.save(s)
                self.audio_queue.task_done()

    def _enqueue_ai(self, s, item, kind, automatic=False):
        if item.get("job_id") in self.jobs and self.jobs[item["job_id"]]["status"] in ("pending", "paused"):
            self.jobs[item["job_id"]]["status"] = "superseded"
        job = self.job(s["id"], kind)
        if kind == "glossary":
            item["ai_session_id"] = s["id"]
        item.update(status="pending", job_id=job["id"])
        if kind == "analysis":
            item["automatic"] = automatic
        item.pop("error", None)
        item.pop("streaming_text", None)
        item.pop("stream_status", None)
        if self.ai["paused"]:
            item["status"] = job["status"] = "paused"
            return job
        self.order += 1
        # Questions jump ahead of queued automatic notes, not the currently running call.
        self.ai_queue.put((0 if kind == "question" else 20 if kind == "glossary" else 10, self.order, s["id"], item["id"], kind, job["id"]))
        return job

    def context(self, s, question="", group_id=None):
        segments = s["segments"]
        chosen = set()
        focused = set()
        if group_id:
            group = next((g for g in s["groups"] if g["id"] == group_id), None)
            if group:
                focused.update(group["segment_ids"])
                positions = [i for i, seg in enumerate(segments) if seg["id"] in focused]
                if positions:
                    chosen.update(seg["id"] for seg in segments[max(0, min(positions)-4):max(positions)+5])
        words = set(re.findall(r"[a-z]{3,}|[\u3400-\u9fff]{2,4}", question.lower()))
        ranked = sorted(segments[:-20], key=lambda seg: sum(w in seg["text"].lower() for w in words), reverse=True)
        for seg in ranked[:8]:
            if words and any(w in seg["text"].lower() for w in words):
                chosen.add(seg["id"])
        if not focused:
            chosen.update(seg["id"] for seg in segments[-20:])
        raw = "\n".join(source_line(seg) for seg in segments if seg["id"] in chosen and seg["id"] not in focused)
        focus_raw = "\n".join(source_line(seg) for seg in segments if seg["id"] in focused)
        summaries = "\n".join(f"[{g['id']}; 原文 {','.join(g['segment_ids'])}] {g['analysis']}" for g in s["groups"] if g.get("analysis") and not g.get("stale") and (not focused or chosen.intersection(g["segment_ids"])))
        course = next((c for c in self.courses if c["id"] == s.get("course_id")), {})
        return (f"课程：{s['title']}\n预设课程：{course.get('name', '')}\n术语提示（准备资料，不是课堂证据）：{self.asr_prompt(s)}\n"
                f"章节/大纲（准备资料，不是课堂证据）：{course.get('syllabus', '')[:4000]}\n"
                "以下只含近期和检索命中的转写及部分此前摘要，不保证覆盖整节课；找不到证据要明确说明。"
                f"\n此前 AI 笔记（可能有误，原文优先）：\n{summaries[-8000:]}\n课堂转写：\n{raw[-16000:]}"
                + (f"\n用户明确选中的旧段落，请优先回答这一段及其邻近语境：\n{focus_raw[-16000:]}" if focused else ""))

    def ask(self, question, group_id=None, sid=None, *, request_id=None):
        with self.lock:
            s = self.get(sid)
            fingerprint = self.request_fingerprint({'question': question, 'group_id': group_id})
            if self.existing_request(s, request_id, 'ask', fingerprint):
                return self.lookup_request(request_id, s['id'])['job']
            if s["demo"]:
                raise HTTPException(400, "演示不调用 AI。请新建课程，或用文本录入测试真实问答。")
            item = {"id": f"Q{len(s['questions']) + 1:04d}", "question": question, "answer": "", "group_id": group_id,
                    "created_at": stamp(), "context_snapshot": self.context(s, question, group_id),
                    "history_snapshot": [{"role": role, "content": value} for q in s["questions"][-5:] if q.get("status") == "done" and not q.get("stale") for role, value in (("user", q["question"]), ("assistant", q["answer"]))]}
            s["questions"].append(item)
            job = self._enqueue_ai(s, item, "question")
            if request_id:
                item['request_id'] = request_id
            self.remember_request(s, request_id, 'ask', fingerprint, item['id'], job=copy.deepcopy(job))
            try:
                self.save(s)
            except OSError:
                s['questions'].pop()
                self.jobs[job['id']]['status'] = 'cancelled'
                s.get('request_receipts', {}).pop(request_id, None)
                raise HTTPException(507, '本机保存失败，问题尚未发送；原输入保留，可手动重试。') from None
            return copy.deepcopy(job)

    def retry(self, kind, item_id, sid=None, *, confirm_duplicate=False, request_id=None):
        with self.lock:
            s = self.get(sid)
            fingerprint = self.request_fingerprint({'kind': kind, 'item_id': item_id})
            if self.existing_request(s, request_id, 'legacy-retry', fingerprint):
                return self.lookup_request(request_id, s['id'])['job']
            if s["demo"]:
                raise HTTPException(400, "演示课程不调用 AI。")
            collection = s["groups"] if kind == "analysis" else s["questions"]
            item = next((i for i in collection if i["id"] == item_id), None)
            if not item:
                raise HTTPException(404, "找不到这条记录。")
            if item.get("study"):
                raise HTTPException(409, "图文与即时解释须先检查原快照及发送目的地，请使用该条记录的确认重试入口。")
            if item["status"] in ("pending", "running"):
                raise HTTPException(409, "这一项正在处理。")
            if item.get('result_unknown') and not confirm_duplicate:
                raise HTTPException(409, '之前的请求可能已处理或计费；请确认可能重复计费后再重试。')
            previous = copy.deepcopy(item)
            if item.get('result_unknown'):
                item.setdefault('attempts', []).append({'status': 'result_unknown', 'at': stamp()})
            for key in ('dispatch_started_at', 'dispatch_completed_at', 'dispatch_state', 'result_unknown'):
                item.pop(key, None)
            result = self._enqueue_ai(s, item, kind)
            self.remember_request(s, request_id, 'legacy-retry', fingerprint, item_id,
                                  item_kind=kind, job=copy.deepcopy(result))
            try:
                self.save(s)
            except OSError:
                self.jobs[result['id']]['status'] = 'cancelled'
                item.clear()
                item.update(previous)
                s.get('request_receipts', {}).pop(request_id, None)
                raise HTTPException(507, '本机保存失败，重试尚未发送；请保留原记录。') from None
            return copy.deepcopy(result)

    def _pause_ai_storage(self):
        reason = '本机保存失败，AI 已暂停；最新结果尚未保存，可能只在内存中，请勿退出。检查磁盘后显式恢复，保存成功前不要关闭后台。'
        try:
            self._pause_ai('storage', reason)
        except OSError:
            # _pause_ai updates all in-memory states before touching disk.
            # A full disk must not kill the worker or trigger another paid call.
            pass

    def _ai_loop(self):
        while not self.closed.is_set():
            if not self.ai_ready.wait(.4):
                continue
            try:
                _, _, sid, item_id, kind, jid = self.ai_queue.get(timeout=.4)
            except queue.Empty:
                continue
            with self.lock:
                s = self.get(sid)
                items = self.courses if kind == "glossary" else s["groups"] if kind == "analysis" else s["questions"]
                item = next((x for x in items if x["id"] == item_id), None)
                if item is None or item.get("job_id") != jid or item.get("status") != "pending" or self.ai["paused"]:
                    self.ai_queue.task_done()
                    continue
                blocked_reason = self.study_dispatch_block_reason(s, item) if item.get('study') else None
                if blocked_reason:
                    try:
                        self.defer_study_dispatch(s, item, jid, blocked_reason)
                    except OSError:
                        self._pause_ai_storage()
                    self.ai_queue.task_done()
                    continue
                item["status"] = self.jobs[jid]["status"] = "running"
                provider = self.connection.values["provider"]
                model = self.connection.values["model"] if provider == "api" else self.config.get("codex_model")
                self.active_ai = {"provider": provider, "model": model}
                self.active_cancel = threading.Event()
                cancel_event = self.active_cancel
                request_token = self.study_route
                image_count = len(item.get("evidence", {}).get("slides", []))
                item.update(stream_provider=provider, stream_model=model)
                if provider == "api" and kind != "glossary":
                    item.update(streaming_text="", stream_status="streaming")
                api_config = dict(self.connection.values)
                api_key = self.connection.key
                version = item.get("version", 0)
                raw = ""
                if kind == "analysis":
                    # New text must not also be labelled previous context: that
                    # makes the model dismiss its own input as already covered.
                    previous = dict(s)
                    first = next((i for i, seg in enumerate(s["segments"]) if seg["id"] in item["segment_ids"]), len(s["segments"]))
                    previous["segments"] = s["segments"][:first]
                    group_index = s["groups"].index(item)
                    previous["groups"] = s["groups"][:group_index]
                    context = self.context(previous)
                    ids = set(item["segment_ids"])
                    raw = "\n".join(source_line(seg) for seg in s["segments"] if seg["id"] in ids)
                    if item.get("partial"):
                        raw += "\n【阶段整理：老师仍在讲解，本段可能未完成，不要凑结论。】"
                    source_ids = set(re.findall(r"\bS\d{4,}\b", context + "\n" + raw))
                    item["source_versions"] = {seg["id"]: seg.get("version", 0) for seg in s["segments"] if seg["id"] in source_ids}
                elif kind == "glossary":
                    context = ""
                    glossary_name, syllabus = item["name"], item.get("syllabus", "")
                else:
                    context = item.get("context_snapshot") or self.context(s, item.get("question", ""), item.get("group_id"))
                history = item.get("history_snapshot", [])
            began = time.monotonic()
            last_draft_save = [0.0]
            request_bridge = self.bridge
            success = False
            api_attempt = False
            usage_recorded = False
            dispatched = False
            provider_returned = False
            metadata = {"provider": provider, "model": model, "usage": None}

            def stream_delta(delta):
                with self.lock:
                    if item.get("job_id") != jid:
                        return
                    item["streaming_text"] = (item.get("streaming_text", "") + delta)[:120000]
                    if time.monotonic() - last_draft_save[0] >= .5:
                        self.save(s)
                        last_draft_save[0] = time.monotonic()
            try:
                with self.lock:
                    self.save(s)
                    if kind == 'glossary':
                        self.save_courses()
                # Resolve immutable assets before any provider submission or usage count.
                images = self.study_images(s, item) if item.get("study") else []
                if item.get('study') and not hasattr(request_bridge, 'ask_visual') and provider != 'api':
                    raise CodexBridgeError('当前接入尚未提供图文解释能力，请检查版本。')
                if provider == "api":
                    # Snapshot destination and credential before releasing the
                    # queue lock; changing settings cannot redirect this call.
                    if not api_key or not model:
                        raise CodexBridgeError("请在 AI 设置中填写 API 地址、模型和密钥。", kind="auth")
                    request_bridge = self.connection.api_factory({**api_config, "timeout_seconds": self.connection.timeout}, api_key,
                                                                 on_delta=stream_delta if kind != "glossary" else None)
                with self.lock:
                    # The final gate uses only the frozen receipt's sources.
                    # New teaching does not revoke consent; source edits do.
                    if item.get('job_id') != jid:
                        continue
                    blocked_reason = self.study_dispatch_block_reason(s, item) if item.get('study') else None
                    if blocked_reason:
                        self.defer_study_dispatch(s, item, jid, blocked_reason)
                        continue
                    previous_dispatch = {key: item.get(key) for key in ('dispatch_started_at', 'dispatch_completed_at', 'dispatch_state', 'result_unknown')}
                    item.update(dispatch_started_at=stamp(), dispatch_state='started', result_unknown=False)
                    item.pop('dispatch_completed_at', None)
                    if provider == 'api':
                        s['ai_usage']['requests'] += 1
                    try:
                        self.save(s)
                        if kind == 'glossary':
                            self.save_courses()
                    except OSError:
                        for key, value in previous_dispatch.items():
                            if value is None:
                                item.pop(key, None)
                            else:
                                item[key] = value
                        if provider == 'api':
                            s['ai_usage']['requests'] -= 1
                        raise
                    dispatched = True
                    api_attempt = provider == 'api'
                if kind == "analysis":
                    result = request_bridge.analyze(context, raw)
                elif kind == "glossary":
                    result = validate_glossary(request_bridge.generate_glossary(glossary_name, syllabus))
                elif item.get("study"):
                    if not hasattr(request_bridge, "ask_visual"):
                        raise CodexBridgeError("当前接入尚未提供图文解释能力，请检查版本。")
                    result = request_bridge.ask_visual(context, item["question"], images,
                                explain=item.get("explain", False), cancel_event=cancel_event)
                    provider_returned = True
                    # Ordinary lecture text may itself mention an S/P-shaped
                    # identifier. Only actual receipt sources authorize citations.
                    _validate_references(result, context, set(item['evidence']['source_ids']))
                else:
                    result = request_bridge.ask(context, item["question"], history)
                provider_returned = True
                if provider == "api":
                    metadata = request_bridge.metadata()
                success = True
                with self.lock:
                    if api_attempt:
                        record_usage(s["ai_usage"], metadata, True)
                        usage_recorded = True
                    if item.get("job_id") != jid:
                        continue
                    if item.get("study"):
                        self.record_study_vision(request_token, image_count, True)
                    item.update(status="done", elapsed=round(time.monotonic()-began, 2),
                                dispatch_completed_at=stamp(), dispatch_state='complete', result_unknown=False)
                    item.update(ai_provider=provider, ai_model=metadata.get("model"), usage=metadata.get("usage"),
                                stream_status="complete", streaming_text="")
                    if kind == "glossary":
                        item.update(status="ready", glossary=result, terms=glossary_terms(result), generated_at=stamp())
                    else:
                        item["analysis" if kind == "analysis" else "answer"] = result
                        if kind == "analysis" and item.get("version", 0) == version:
                            item.update(stale=False, stale_reason=None)
                    self.jobs[jid].update(status="done", elapsed=item["elapsed"])
            except Exception as exc:
                with self.lock:
                    if api_attempt and not usage_recorded:
                        record_usage(s["ai_usage"], request_bridge.metadata(), False)
                        usage_recorded = True
                    if item.get("job_id") != jid:
                        continue
                    error = ('本机存储写入失败，请检查磁盘后恢复；未自动重发。' if isinstance(exc, OSError)
                             else str(exc)[:400] if isinstance(exc, CodexBridgeError)
                             else "AI 暂时无法完成回答，请检查设置后重试。")
                    error_kind = 'storage' if isinstance(exc, OSError) else getattr(exc, "kind", "other")
                    unknown = dispatched and not provider_returned and error_kind not in ('auth', 'quota', 'vision_unsupported')
                    if unknown:
                        error = UNKNOWN_TIMEOUT if error_kind == 'timeout' else UNKNOWN_RESULT
                    elif error_kind == 'timeout':
                        error = UNKNOWN_TIMEOUT
                    item.update(result_unknown=unknown,
                                dispatch_state='unknown' if unknown else 'failed' if dispatched else 'not_sent')
                    if dispatched and not unknown:
                        item['dispatch_completed_at'] = stamp()
                    if item.get("study") and error_kind == "vision_unsupported":
                        self.record_study_vision(request_token, image_count, False)
                    paused = error_kind in ("network", "quota", "auth", "timeout", "storage")
                    item.update(status="paused" if paused else "error", error=error, error_kind=error_kind)
                    if provider == "api" and kind != "glossary":
                        item["stream_status"] = "interrupted"
                    self.jobs[jid].update(status=item["status"], error=error)
                    if paused:
                        try:
                            self._pause_ai(error_kind, error)
                        except OSError:
                            self._pause_ai_storage()
            finally:
                with self.lock:
                    if api_attempt and not usage_recorded:
                        record_usage(s["ai_usage"], request_bridge.metadata(), success)
                    if provider == "api" and self.connection.values == api_config and self.connection.key == api_key:
                        self.api_last_status = {"inference_verified": success, "last_inference_seconds": round(time.monotonic() - began, 2),
                                                "last_error": None if success else item.get("error"),
                                                "last_error_kind": None if success else item.get("error_kind")}
                    self.active_ai = None
                    self.active_cancel = None
                    self.ai_switch_pending = False
                    try:
                        self.save(s)
                        if kind == "glossary":
                            self.save_courses()
                    except OSError:
                        self._pause_ai_storage()
                self.ai_queue.task_done()

    def _idle_loop(self):
        while not self.closed.wait(2):
            with self.lock:
                for s in self.sessions.values():
                    if s["pending_ids"] and time.time() - s.get("last_text_at", 0) > 20 and not any(a["status"] in ("pending", "running") for a in s["audio"]):
                        self._flush(s)

    def demo(self):
        demo = self.new_session("示例课堂 · Opportunity cost", demo=True)
        sid = demo["id"]
        samples = [(0, 11, "Opportunity cost is the value of the next best alternative that you give up when making a choice."),
                   (11, 23, "比如你花两个小时上课，这段时间也可以用来做兼职。The opportunity cost is what you would have earned from that next best alternative."),
                   (23, 36, "It is not the sum of every possible alternative. 注意，是你放弃的、最好的那一个选择。")]
        for start, end, text in samples:
            self.append_text(sid, text, start, end, "demo")
        with self.lock:
            s = self.get(sid)
            self._flush(s)
            g = s["groups"][0]
            g.update(status="done", analysis="【固定演示内容，未调用 AI】\n\n核心意思：机会成本（opportunity cost）是做出选择时，放弃的最佳替代选项的价值。[S0001]\n\n例子：上课与兼职冲突时，放弃的兼职收入可以作为机会成本的一个例子。[S0002]\n\n容易混淆：不能把所有被放弃的选项相加，只考虑最佳替代项。[S0003]")
            self.save(s)
        return self.state()

    def markdown(self, s):
        lines = [f"# {s['title']}", "", f"创建时间：{s['created_at']}", "", "机器转写可能有误。AI 解释与教师原话分开保存；引用重要内容前请核对录音。", "", "## 原始转写", ""]
        for seg in s["segments"]:
            lines += [f"[{seg['id']}] {classroom_time(seg['start'])}–{classroom_time(seg['end'])} · {seg['source']}", seg["text"], ""]
            if "machine_text" in seg:
                lines += ["修改前的机器识别：" + seg["machine_text"], ""]
        lines += ["## 意群与解释", ""]
        for group in s["groups"]:
            lines += [f"### {group['id']} · {', '.join(group['segment_ids'])}", "", group["text"], "", group.get("analysis") or group.get("error") or "尚未分析。", ""]
            lines += self.ai_export_lines(group)
            if group.get("stale"):
                lines += ["待核对：" + (group.get("stale_reason") or "依据已变化，请重新分析。"), ""]
            if group.get("partial"):
                lines += ["阶段整理：老师仍在讲解，本段可能未完成。", ""]
        lines += ["## 课堂提问", ""]
        for q in s["questions"]:
            lines += [f"### {q['question']}", "", q.get("answer") or q.get("error") or "尚未回答。", ""]
            lines += self.ai_export_lines(q)
            lines += self.study_export_lines(q)
            if q.get("stale"):
                lines += [q.get("stale_reason") or "回答依据后来有修改，请核对。", ""]
        for index, revision in enumerate(s.get("revisions", []), 1):
            lines += [f"## 课后修订 {index} · {revision['status']}", "", "独立修订稿；课堂原版保留。", ""]
            for seg in revision["segments"]:
                lines += [source_line(seg), ""]
            if revision.get("error"):
                lines += [revision["error"], ""]
        usage = s.get("ai_usage", {})
        if usage.get("requests"):
            lines += ["## API 用量", "", f"请求 {usage['requests']} 次；成功 {usage.get('completed', 0)} 次；失败 {usage.get('failed', 0)} 次。",
                      f"中断后结果未知 {usage.get('unknown', 0)} 次（可能已计费）。",
                      f"其中 {usage.get('usage_reported_requests', 0)} 次返回 token 用量，已报告总量 {usage.get('total_tokens', 0)}；未返回的部分未知，实际费用以服务商账单为准。", ""]
        return "\n".join(lines)

    @staticmethod
    def ai_export_lines(item):
        lines = []
        if item.get("ai_provider"):
            lines += [f"正式结果接入：{item['ai_provider']}；模型：{item.get('ai_model') or '由本地 Codex 选择'}。", ""]
        if item.get("streaming_text") and item.get("stream_status") in ("streaming", "interrupted"):
            lines += [f"未完成草稿（尚未校验，不作为正式答案；接入 {item.get('stream_provider', 'api')}，模型 {item.get('stream_model') or '未知'}）：",
                      item["streaming_text"], ""]
        return lines


class Settings(BaseModel):
    session_id: str | None = None
    title: str | None = Field(default=None, max_length=160)
    terms: str | None = Field(default=None, max_length=2000)
    auto_analyze: bool | None = None


class NewSession(BaseModel):
    title: str = Field(default="新的课堂", max_length=160)


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=INPUT_LIMITS["text"])
    session_id: str | None = None

    _nonblank = field_validator("text")(require_content)


class AskInput(BaseModel):
    question: str = Field(min_length=1, max_length=INPUT_LIMITS["question"])
    group_id: str | None = None
    session_id: str | None = None
    request_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{8,128}$')

    _nonblank = field_validator("question")(require_content)


class SessionInput(BaseModel):
    session_id: str | None = None


class RequestInput(SessionInput):
    request_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{8,128}$')
    confirm_duplicate: bool = False


class ModeInput(BaseModel):
    mode: str


class CourseInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class CourseSelection(BaseModel):
    course_id: str | None = None


class GlossaryInput(BaseModel):
    syllabus: str = Field(default="", max_length=INPUT_LIMITS["syllabus"])


class ResumeInput(BaseModel):
    strategy: str


class AISettingsInput(BaseModel):
    provider: str | None = Field(default=None, max_length=20)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=160)
    api_key: str | None = Field(default=None, max_length=4096)
    remember_key: bool | None = None
    clear_key: bool = False
    force: bool = False


class SlideIntervalInput(SessionInput):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    title: str | None = Field(default=None, max_length=160)


class SlideRotateInput(RequestInput):
    degrees: Literal[0, 90, 180, 270] | None = None
    direction: Literal['left', 'right'] = 'right'


class SeenSegment(BaseModel):
    id: str = Field(max_length=80)
    version: int = Field(default=0, ge=0, strict=True)


class SeenSlide(BaseModel):
    id: str = Field(max_length=80)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    title: str = Field(default='', max_length=160)
    version: int | None = Field(default=None, ge=1, strict=True)
    interval_version: int = Field(default=0, ge=0, strict=True)


class StudyInput(RequestInput):
    mode: Literal['recent30', 'recent120', 'group'] = 'recent120'
    anchor: float = Field(ge=0, allow_inf_nan=False)
    group_id: str | None = Field(default=None, max_length=80)
    question: str = Field(default='', max_length=INPUT_LIMITS['question'])
    explain: bool = False
    slide_ids: list[str] | None = Field(default=None, max_length=4)
    include_images: bool = True
    seen_segments: list[SeenSegment] | None = Field(default=None, max_length=20000)
    seen_slides: list[SeenSlide] | None = Field(default=None, max_length=2000)
    seen_group_segment_ids: list[str] | None = Field(default=None, max_length=20000)


class StudySendInput(SessionInput):
    destination_token: str = Field(min_length=1, max_length=128)
    confirm_images: bool = False
    remember: bool = False
    source_token: str | None = Field(default=None, max_length=128)
    confirm_original: bool = False
    confirm_duplicate: bool = False


def create_app(config=None, data=None, engine=None, bridge=None):
    if config is None:
        path = ROOT / "config.json"
        if not path.exists():
            path = ROOT / "config.example.json"
        config = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    room = Classroom(Path(data or ROOT / "data"), config, engine, bridge)

    @asynccontextmanager
    async def lifespan(app):
        room.start()
        yield
        room.stop()

    app = FastAPI(title="课间 · Classroom", version=__version__, lifespan=lifespan)
    app.state.room = room

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Pydantic's default response can echo a credential supplied in an
        # invalid field. Never reflect request bodies from the settings form.
        return JSONResponse({"detail": validation_message(request.url.path, exc.errors())}, status_code=422)

    @app.middleware("http")
    async def local_only(request, call_next):
        host = request.headers.get("host", "").split(":")[0]
        origin = request.headers.get("origin")
        if host not in ("127.0.0.1", "localhost", "testserver"):
            return JSONResponse({"detail": "仅允许本机访问。"}, status_code=403)
        if origin and origin != f"http://{request.headers.get('host')}":
            return JSONResponse({"detail": "拒绝来自其他网页的请求。"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; worker-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/api/state")
    def state():
        return room.state()

    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "classroom-assistant", "version": __version__}

    @app.get("/api/ai/settings")
    def ai_settings():
        return room.ai_settings()

    @app.put("/api/ai/settings")
    def configure_ai(body: AISettingsInput):
        return room.configure_ai(body.model_dump(exclude_none=True))

    @app.post("/api/ai/test")
    def test_ai(body: AISettingsInput):
        return room.test_ai(body.model_dump(exclude_none=True))

    @app.post('/api/slides')
    async def add_slide(request: Request, session_id: str, start: float, title: str = '', request_id: str | None = None):
        chunks = bytearray()
        async for chunk in request.stream():
            if len(chunks) + len(chunk) > 10 * 1024 * 1024:
                length = request.headers.get('content-length', '')
                actual = int(length) if length.isdecimal() and len(length) < 20 else len(chunks) + len(chunk)
                width, height = image_dimensions(bytes(chunks[:1_048_576]) if chunks else chunk[:1_048_576])
                detail = image_size_message(actual, width=width, height=height)
                if not length.isdecimal():
                    detail += '已接收字节已超限；完整文件大小未知。'
                raise HTTPException(413, detail)
            chunks.extend(chunk)
        if len(title) > 160:
            raise HTTPException(422, '课件名称最多 160 个字符。')
        return await run_in_threadpool(room.add_slide, bytes(chunks), start=start, sid=session_id, title=title, request_id=request_id)

    @app.get('/api/slides/requests/{request_id}')
    def slide_request(request_id: str, session_id: str):
        return room.lookup_slide_request(request_id, session_id)

    @app.post('/api/slides/{pid}/rotate')
    def rotate_slide_route(pid: str, body: SlideRotateInput):
        return room.rotate_study_slide(pid, body.model_dump())

    @app.delete('/api/slides/{pid}')
    def remove_slide_route(pid: str, session_id: str):
        return room.remove_study_slide(pid, session_id)

    @app.patch('/api/slides/{pid}')
    def update_slide(pid: str, body: SlideIntervalInput):
        return room.update_slide(pid, body.model_dump())

    @app.get('/api/slides/{pid}')
    def slide_file(pid: str, session_id: str, version: int | None = None, sha256: str | None = None):
        path, mime = room.slide_file(pid, session_id, version=version, sha256=sha256)
        return FileResponse(path, media_type=mime)

    @app.post('/api/study/prepare')
    def prepare_study(body: StudyInput):
        return room.prepare_study(body.model_dump())

    @app.get('/api/study/requests/{request_id}')
    def study_request(request_id: str, session_id: str):
        return room.lookup_study_request(request_id, session_id)

    @app.get('/api/requests/{request_id}')
    def any_request(request_id: str, session_id: str):
        return room.lookup_request(request_id, session_id)

    @app.get('/api/study/{qid}')
    def get_study(qid: str, session_id: str):
        with room.lock:
            return room.study_public(room.study_question(room.get(session_id), qid), session_id)

    @app.post('/api/study/{qid}/send')
    def send_study(qid: str, body: StudySendInput):
        return room.send_study(qid, body.model_dump())

    @app.post('/api/study/{qid}/retry')
    def retry_study(qid: str, body: RequestInput):
        return room.clone_study(qid, body.session_id, request_id=body.request_id, confirm_duplicate=body.confirm_duplicate)

    @app.post('/api/study/{qid}/text-only')
    def text_only_study(qid: str, body: RequestInput):
        return room.clone_study(qid, body.session_id, text_only=True, request_id=body.request_id,
                                confirm_duplicate=body.confirm_duplicate)

    @app.post("/api/mode")
    def mode(body: ModeInput):
        return room.set_mode(body.mode)

    @app.post("/api/courses")
    def add_course(body: CourseInput):
        return room.add_course(body.name)

    @app.put("/api/courses/{cid}")
    def rename_course(cid: str, body: CourseInput):
        return room.rename_course(cid, body.name)

    @app.post("/api/course/select")
    def course_select(body: CourseSelection):
        return room.select_course(body.course_id)

    @app.post("/api/courses/{cid}/glossary")
    def glossary(cid: str, body: GlossaryInput):
        return room.generate_glossary(cid, body.syllabus)

    @app.post("/api/segments/{segid}/edit")
    def edit(segid: str, body: TextInput):
        return room.edit_segment(body.session_id, segid, body.text)

    @app.post("/api/ai/resume")
    def resume(body: ResumeInput):
        return room.resume_ai(body.strategy)

    @app.post("/api/retranscribe")
    def retranscribe(body: SessionInput):
        return room.retranscribe(body.session_id)

    @app.post("/api/audio/delete")
    def delete_audio(body: SessionInput):
        return room.delete_audio(body.session_id)

    @app.post("/api/soundcheck")
    async def soundcheck(request: Request):
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 2_000_000:
                raise HTTPException(413, "试音超过大小限制。")
        return room.accept_soundcheck(bytes(body))

    @app.get("/api/soundcheck/{jid}/audio")
    def soundcheck_audio(jid: str):
        with room.lock:
            check = room.soundcheck
            if jid != check.get("job_id"):
                raise HTTPException(404, "找不到这次试音。")
            path = Path(check["path"]).resolve()
            if not path.is_relative_to((room.data / "soundchecks").resolve()) or not path.is_file():
                raise HTTPException(404, "试音录音不可用。")
        return FileResponse(path, media_type="audio/wav")

    @app.put("/api/settings")
    def settings(body: Settings):
        room.settings(body.model_dump(exclude_none=True))
        return {"ok": True}

    @app.post("/api/sessions")
    def new_session(body: NewSession):
        return room.new_session(body.title)

    @app.post("/api/sessions/{sid}/select")
    def select(sid: str):
        with room.lock:
            room.get(sid)
            room.current = sid
        return {"ok": True}

    @app.post("/api/audio")
    async def audio(request: Request, session_id: str, seq: int, start: float = 0):
        if seq < 0 or seq > 9007199254740991:
            raise HTTPException(400, "音频编号无效。")
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > 2_000_000:
                raise HTTPException(413, "音频分段过大。")
        return room.accept_audio(session_id, seq, start, bytes(body))

    @app.post("/api/text")
    def text(body: TextInput):
        return room.append_text(body.session_id or room.current, body.text)

    @app.post("/api/flush")
    def flush(body: SessionInput | None = None):
        return room.flush(body.session_id if body else None)

    @app.post("/api/ask")
    def ask(body: AskInput):
        return room.ask(body.question, body.group_id, body.session_id, request_id=body.request_id)

    @app.post("/api/groups/{gid}/retry")
    def retry_group(gid: str, body: RequestInput | None = None):
        return room.retry("analysis", gid, body.session_id if body else None,
                          confirm_duplicate=body.confirm_duplicate if body else False,
                          request_id=body.request_id if body else None)

    @app.post("/api/questions/{qid}/retry")
    def retry_question(qid: str, body: RequestInput | None = None):
        return room.retry("question", qid, body.session_id if body else None,
                          confirm_duplicate=body.confirm_duplicate if body else False,
                          request_id=body.request_id if body else None)

    @app.post("/api/audio/{seq}/retry")
    def retry_audio(seq: int, body: SessionInput | None = None):
        with room.lock:
            s = room.get(body.session_id if body else None)
            a = next((a for a in s["audio"] if a["seq"] == seq), None)
            has_text = any(seg.get("audio_seq") == seq for seg in s["segments"])
            if not a or a.get("deleted") or a["status"] not in ("error", "done") or has_text:
                raise HTTPException(409, "只有失败或尚未产生文字的音频可以重试。")
            room.registered_audio_path(s, a)
            if room.audio_queue.qsize() >= 120:
                raise HTTPException(429, "转写队列已满。")
            room._schedule_audio(s, a)
            room.save(s)
            return {"ok": True}

    @app.get("/api/audio/{seq}")
    def audio_file(seq: int, session_id: str):
        with room.lock:
            s = room.get(session_id)
            a = next((a for a in s["audio"] if a["seq"] == seq), None)
            if not a:
                raise HTTPException(404, "找不到这段录音。")
            path = room.registered_audio_path(s, a)
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/demo")
    def demo():
        return room.demo()

    @app.get("/api/export")
    def export(format: str = "markdown", session_id: str | None = None):
        with room.lock:
            s = copy.deepcopy(room.get(session_id))
        if format == "json":
            return Response(json.dumps(s, ensure_ascii=False, indent=2), media_type="application/json", headers={"Content-Disposition": 'attachment; filename="classroom-notes.json"'})
        return Response(room.markdown(s), media_type="text/markdown; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="classroom-notes.md"'})

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "static", check_dir=False), name="static")
    return app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.environ.get("CLASSROOM_PORT", "8766")), log_level="warning")
