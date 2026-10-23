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
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from asr import ASREngine
from codex_bridge import CodexBridge
from classroom_features import ClassroomFeatures, wav_duration
from course_catalog import glossary_terms, validate_glossary

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


class Classroom(ClassroomFeatures):
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
        self.sessions: dict[str, dict] = {}
        self.jobs: dict[str, dict] = {}
        self.audio_queue: queue.Queue = queue.Queue()
        self.ai_queue: queue.PriorityQueue = queue.PriorityQueue()
        self.order = 0
        self.closed = threading.Event()
        self.threads = []
        self.warnings = []
        self.init_features()
        for path in sorted(data.glob("*/session.json")):
            try:
                s = json.loads(path.read_text(encoding="utf-8"))
                if s.get("id") != path.parent.name:
                    raise ValueError("session id mismatch")
                for key in ("segments", "groups", "questions", "audio", "revisions"):
                    s.setdefault(key, [])
                s.setdefault("settings", {"terms": "", "auto_analyze": True})
                s.setdefault("pending_ids", [])
                s.setdefault("flush_requested", False)
                # Interrupted AI work is not silently represented as completed.
                for item in s["groups"] + s["questions"] + s["revisions"]:
                    if item.get("status") in ("pending", "running"):
                        item.update(status="error", error="上次运行中断，可重试这一项。")
                self.sessions[s["id"]] = s
            except (OSError, ValueError, TypeError):
                self.warnings.append(f"有一份历史课程文件无法读取，原文件已保留：{path.parent.name}")
        self.current = max(self.sessions.values(), key=lambda s: s["created_at"])["id"] if self.sessions else self.new_session("新的课堂")["id"]

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
                 "demo": demo, "segments": [], "groups": [], "questions": [], "audio": [], "revisions": [], "course_id": None,
                 "settings": {"terms": "", "auto_analyze": not demo}, "pending_ids": [],
                 "flush_requested": False, "last_text_at": 0}
            self.sessions[sid] = s
            self.current = sid
            self.save(s)
            return copy.deepcopy(s)

    def state(self):
        # Hardware status can take the ASR lock; do not hold the session lock while reading it.
        asr_status = self.engine.status()
        codex_status = self.bridge.status()
        with self.lock:
            active = self.get()
            remaining = [a for a in active["audio"] if a.get("status") in ("pending", "running")]
            newest = max((a["start"] + a["duration"] for a in active["audio"]), default=0)
            lag = max(0, newest - min((a["start"] for a in remaining), default=newest))
            soundcheck = {k: v for k, v in self.soundcheck.items() if k not in ("path", "prompt")}
            return {"session": copy.deepcopy(active),
                    "courses": copy.deepcopy(self.courses), "ai": copy.deepcopy(self.ai), "soundcheck": soundcheck,
                    "sessions": [{k: s[k] for k in ("id", "title", "created_at", "demo")} for s in sorted(self.sessions.values(), key=lambda x: x["created_at"], reverse=True)],
                    "status": {"asr": asr_status, "codex": codex_status},
                    "jobs": [copy.deepcopy(j) for j in list(self.jobs.values())[-120:] if j["session_id"] == active["id"]],
                    "queue": {"audio": sum(j["kind"] == "audio" and j["status"] in ("pending", "running") for j in self.jobs.values()),
                              "ai": sum(j["kind"] in ("question", "analysis", "glossary") and j["status"] in ("pending", "running", "paused") for j in self.jobs.values()),
                              "lag_seconds": round(lag, 1)},
                    "warnings": self.warnings}

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
            s = self.get()
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
        item.update(status="pending", job_id=job["id"])
        if kind == "analysis":
            item["automatic"] = automatic
        item.pop("error", None)
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

    def ask(self, question, group_id=None, sid=None):
        with self.lock:
            s = self.get(sid)
            if s["demo"]:
                raise HTTPException(400, "演示不调用 AI。请新建课程，或用文本录入测试真实问答。")
            item = {"id": f"Q{len(s['questions']) + 1:04d}", "question": question, "answer": "", "group_id": group_id,
                    "created_at": stamp(), "context_snapshot": self.context(s, question, group_id),
                    "history_snapshot": [{"role": role, "content": value} for q in s["questions"][-5:] if q.get("status") == "done" for role, value in (("user", q["question"]), ("assistant", q["answer"]))]}
            s["questions"].append(item)
            job = self._enqueue_ai(s, item, "question")
            self.save(s)
            return copy.deepcopy(job)

    def retry(self, kind, item_id, sid=None):
        with self.lock:
            s = self.get(sid)
            if s["demo"]:
                raise HTTPException(400, "演示课程不调用 AI。")
            collection = s["groups"] if kind == "analysis" else s["questions"]
            item = next((i for i in collection if i["id"] == item_id), None)
            if not item:
                raise HTTPException(404, "找不到这条记录。")
            if item["status"] in ("pending", "running"):
                raise HTTPException(409, "这一项正在处理。")
            result = self._enqueue_ai(s, item, kind)
            self.save(s)
            return copy.deepcopy(result)

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
                item = next(x for x in items if x["id"] == item_id)
                if item.get("job_id") != jid or item.get("status") != "pending" or self.ai["paused"]:
                    self.ai_queue.task_done()
                    continue
                item["status"] = self.jobs[jid]["status"] = "running"
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
                    self.save_courses()
                else:
                    context = item.get("context_snapshot") or self.context(s, item.get("question", ""), item.get("group_id"))
                history = item.get("history_snapshot", [])
                self.save(s)
            began = time.monotonic()
            try:
                if kind == "analysis":
                    result = self.bridge.analyze(context, raw)
                elif kind == "glossary":
                    result = validate_glossary(self.bridge.generate_glossary(glossary_name, syllabus))
                else:
                    result = self.bridge.ask(context, item["question"], history)
                with self.lock:
                    item.update(status="done", elapsed=round(time.monotonic()-began, 2))
                    if kind == "glossary":
                        item.update(status="ready", glossary=result, terms=glossary_terms(result), generated_at=stamp())
                    else:
                        item["analysis" if kind == "analysis" else "answer"] = result
                        if kind == "analysis" and item.get("version", 0) == version:
                            item.update(stale=False, stale_reason=None)
                    self.jobs[jid].update(status="done", elapsed=item["elapsed"])
            except Exception as exc:
                with self.lock:
                    error = str(exc)[:400] or "AI 暂时无法回答，可稍后重试。"
                    error_kind = getattr(exc, "kind", "other")
                    paused = error_kind in ("network", "quota", "auth", "timeout")
                    item.update(status="paused" if paused else "error", error=error, error_kind=error_kind)
                    self.jobs[jid].update(status=item["status"], error=error)
                    if paused:
                        self._pause_ai(error_kind, error)
            finally:
                with self.lock:
                    self.save(s)
                    if kind == "glossary":
                        self.save_courses()
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
            if group.get("stale"):
                lines += ["待核对：" + (group.get("stale_reason") or "依据已变化，请重新分析。"), ""]
            if group.get("partial"):
                lines += ["阶段整理：老师仍在讲解，本段可能未完成。", ""]
        lines += ["## 课堂提问", ""]
        for q in s["questions"]:
            lines += [f"### {q['question']}", "", q.get("answer") or q.get("error") or "尚未回答。", ""]
            if q.get("stale"):
                lines += [q.get("stale_reason") or "回答依据后来有修改，请核对。", ""]
        for index, revision in enumerate(s.get("revisions", []), 1):
            lines += [f"## 课后修订 {index} · {revision['status']}", "", "独立修订稿；课堂原版保留。", ""]
            for seg in revision["segments"]:
                lines += [source_line(seg), ""]
            if revision.get("error"):
                lines += [revision["error"], ""]
        return "\n".join(lines)


class Settings(BaseModel):
    title: str | None = Field(default=None, max_length=160)
    terms: str | None = Field(default=None, max_length=2000)
    auto_analyze: bool | None = None


class NewSession(BaseModel):
    title: str = Field(default="新的课堂", max_length=160)


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    session_id: str | None = None


class AskInput(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    group_id: str | None = None
    session_id: str | None = None


class SessionInput(BaseModel):
    session_id: str | None = None


class ModeInput(BaseModel):
    mode: str


class CourseInput(BaseModel):
    name: str = Field(min_length=1, max_length=160)


class CourseSelection(BaseModel):
    course_id: str | None = None


class GlossaryInput(BaseModel):
    syllabus: str = Field(default="", max_length=12000)


class ResumeInput(BaseModel):
    strategy: str


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

    app = FastAPI(title="课间 · Classroom", lifespan=lifespan)
    app.state.room = room

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
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; media-src 'self' blob:; object-src 'none'; frame-ancestors 'none'"
        return response

    @app.get("/api/state")
    def state():
        return room.state()

    @app.get("/api/health")
    def health():
        return {"ok": True, "app": "classroom-assistant"}

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
        return room.ask(body.question, body.group_id, body.session_id)

    @app.post("/api/groups/{gid}/retry")
    def retry_group(gid: str, body: SessionInput | None = None):
        return room.retry("analysis", gid, body.session_id if body else None)

    @app.post("/api/questions/{qid}/retry")
    def retry_question(qid: str, body: SessionInput | None = None):
        return room.retry("question", qid, body.session_id if body else None)

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
