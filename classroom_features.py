"""Course preparation and local recording maintenance, separate from live notes."""
from __future__ import annotations

import copy
import io
import json
import os
import queue
import re
import threading
import uuid
import wave
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException

from course_catalog import glossary_terms, load_catalog, make_asr_prompt, validate_glossary


def stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def wav_duration(body, maximum=20):
    if len(body) > 2_000_000:
        raise HTTPException(413, "音频分段过大。")
    try:
        with wave.open(io.BytesIO(body)) as wav:
            frames = wav.getnframes()
            if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth(), wav.getcomptype()) != (1, 16000, 2, "NONE"):
                raise ValueError()
            duration = frames / 16000
            if not .1 <= duration <= maximum or len(wav.readframes(frames)) != frames * 2:
                raise ValueError()
            return duration
    except (wave.Error, EOFError, ValueError):
        raise HTTPException(400, f"需要 16kHz、单声道、16位 PCM WAV（0.1–{maximum}秒）。") from None


class ClassroomFeatures:
    def init_features(self):
        self.maintenance_queue = queue.Queue()
        self.soundcheck = {"status": "idle"}
        self.ai = {"paused": False, "reason": None, "kind": None}
        self.ai_ready = threading.Event()
        try:
            if (self.data / "ai-state.json").exists():
                saved = json.loads((self.data / "ai-state.json").read_text(encoding="utf-8"))
                self.ai.update({k: saved[k] for k in self.ai if k in saved})
        except (OSError, ValueError, TypeError):
            self.warnings.append("AI 暂停状态未能读取；历史课堂仍保留。")
        if not self.ai["paused"]:
            self.ai_ready.set()
        target = self.data / "courses.json"
        try:
            catalog = json.loads(target.read_text(encoding="utf-8")) if target.exists() else load_catalog()
            self.courses = catalog["courses"]
            if not isinstance(self.courses, list) or any(not isinstance(c, dict) or not c.get("id") or not c.get("name") for c in self.courses):
                raise ValueError("invalid courses")
            for course in self.courses:
                if course.get("status") in ("pending", "running"):
                    course.update(status="error", error="上次术语生成中断，请重新生成。")
                    if course.get('dispatch_started_at') and not course.get('dispatch_completed_at'):
                        course.update(result_unknown=True, dispatch_state='unknown',
                                      error='请求结果未知，无法确定服务商是否已处理；未自动重新提交。')
            if not target.exists():
                self.save_courses()
        except (OSError, ValueError, KeyError, TypeError):
            self.courses = []
            self.warnings.append("课程词表未能读取，原文件已保留。")

    def save_courses(self):
        write_json(self.data / "courses.json", {"courses": self.courses})

    def course(self, cid):
        course = next((c for c in self.courses if c["id"] == cid), None)
        if not course:
            raise HTTPException(404, "找不到课程预设。")
        return course

    def add_course(self, name):
        name = name.strip()
        if not name:
            raise HTTPException(400, "请填写课程名称。")
        with self.lock:
            course = {"id": uuid.uuid4().hex, "name": name, "glossary": [], "terms": "", "syllabus": "", "status": "empty"}
            self.courses.append(course)
            self.save_courses()
            return copy.deepcopy(course)

    def rename_course(self, cid, name):
        with self.lock:
            course = self.course(cid)
            if not name.strip():
                raise HTTPException(400, "请填写课程名称。")
            if course.get("status") in ("pending", "running", "paused"):
                raise HTTPException(409, "请等待当前词表生成结束后重命名。")
            course["name"] = name.strip()
            self.save_courses()
            return copy.deepcopy(course)

    def select_course(self, cid):
        with self.lock:
            if cid:
                self.course(cid)
            s = self.get()
            s["course_id"] = cid or None
            self.save(s)
            return {"ok": True}

    def generate_glossary(self, cid, syllabus):
        with self.lock:
            c = self.course(cid)
            if c.get("status") in ("pending", "running", "paused"):
                raise HTTPException(409, "该课程词表正在等待生成。")
            c["syllabus"] = syllabus
            job = self._enqueue_ai(self.get(), c, "glossary")
            self.save_courses()
            return copy.deepcopy(job)

    def asr_prompt(self, s):
        c = next((c for c in self.courses if c["id"] == s.get("course_id")), {})
        return make_asr_prompt(s["settings"].get("terms", ""), c.get("glossary") or c.get("terms", ""))

    def set_mode(self, mode):
        if mode not in ("auto", "battery", "quality"):
            raise HTTPException(400, "无效的转写模式。")
        result = self.engine.set_mode(mode)
        with self.lock:
            self.config["mode"] = mode
            path = self.data / "preferences.json"
            try:
                preferences = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
                if not isinstance(preferences, dict):
                    preferences = {}
            except (OSError, ValueError):
                preferences = {}
            preferences["mode"] = mode
            write_json(path, preferences)
        return result

    def edit_segment(self, sid, segid, text):
        if not text.strip():
            raise HTTPException(400, "转写文字不能为空。")
        with self.lock:
            s = self.get(sid)
            seg = next((x for x in s["segments"] if x["id"] == segid), None)
            if not seg:
                raise HTTPException(404, "找不到这段转写。")
            if seg["text"] == text.strip():
                return copy.deepcopy(seg)
            seg.setdefault("machine_text", seg["text"])
            seg.setdefault("edits", []).append({"text": seg["text"], "changed_at": stamp()})
            seg["text"] = text.strip()
            seg["edited_at"] = stamp()
            seg["edited"] = True
            seg["version"] = seg.get("version", 0) + 1
            for g in s["groups"]:
                direct = segid in g["segment_ids"]
                dependent = segid in g.get("source_versions", {})
                legacy = "source_versions" not in g and g.get("analysis") and g.get("start", 0) >= seg["end"]
                if direct or dependent or legacy:
                    ids = set(g["segment_ids"])
                    if direct:
                        g["text"] = " ".join(x["text"] for x in s["segments"] if x["id"] in ids)
                        g["partial"] = not bool(re.search(r"[.!?。！？][\"'”’）)]?\s*$", g["text"]))
                    g.update(stale=True, stale_reason="原文或引用的前文已修改，请重新分析。", version=g.get("version", 0) + 1)
                    # Unstarted work must not silently analyze a user's edit.
                    if g.get("status") in ("pending", "paused"):
                        if g.get("job_id") in self.jobs:
                            self.jobs[g["job_id"]]["status"] = "cancelled"
                        g["status"] = "saved"
            for q in s["questions"]:
                if re.search(r"\b" + re.escape(segid) + r"\b", q.get("context_snapshot", "")):
                    q.update(stale=True, stale_reason="提问时的原文后来已修改；回答仍基于提问时的版本。")
            self.save(s)
            return copy.deepcopy(seg)

    def _pause_ai(self, kind, reason):
        self.ai.update(paused=True, kind=kind, reason=reason, paused_at=stamp())
        self.ai_ready.clear()
        if hasattr(self, 'review_pause_pending'):
            self.review_pause_pending(reason)
        for s in self.sessions.values():
            for item in s["groups"] + s["questions"]:
                if item.get("status") == "pending":
                    item["status"] = "paused"
                    if item.get("job_id") in self.jobs:
                        self.jobs[item["job_id"]]["status"] = "paused"
        for c in self.courses:
            if c.get("status") == "pending":
                c["status"] = "paused"
                if c.get("job_id") in self.jobs:
                    self.jobs[c["job_id"]]["status"] = "paused"
        write_json(self.data / "ai-state.json", self.ai)
        for s in self.sessions.values():
            self.save(s)
        self.save_courses()

    def resume_ai(self, strategy):
        if strategy not in ("backfill", "latest"):
            raise HTTPException(400, "请选择补分析或从最新继续。")
        with self.lock:
            if self.ai_switch_pending:
                raise HTTPException(409, "当前 AI 请求尚未结束，请等待接入切换完成后恢复。")
            self.ai.update(paused=False, kind=None, reason=None)
            for s in self.sessions.values():
                pending = [g for g in s["groups"] if g.get("status") == "paused"]
                for g in pending:
                    if g.get('result_unknown'):
                        g['status'] = 'error'
                        if g.get('job_id') in self.jobs:
                            self.jobs[g['job_id']]['status'] = 'error'
                    elif strategy == "latest" and g.get("automatic") and g is not pending[-1]:
                        g.update(status="skipped", error="按你的选择略过旧的自动解释；原文保留，可单独分析。")
                        if g.get("job_id") in self.jobs:
                            self.jobs[g["job_id"]]["status"] = "skipped"
                    else:
                        self._enqueue_ai(s, g, "analysis", automatic=g.get("automatic", False))
                for q in s["questions"]:
                    if q.get("status") == "paused":
                        if q.get('result_unknown') or (q.get('study') and q.get('dispatch_started_at')):
                            q['status'] = 'error'
                            if q.get('job_id') in self.jobs:
                                self.jobs[q['job_id']]['status'] = 'error'
                        else:
                            self._enqueue_ai(s, q, "question")
                self.save(s)
            for c in self.courses:
                if c.get("status") == "paused":
                    if c.get('result_unknown'):
                        c['status'] = 'error'
                        if c.get('job_id') in self.jobs:
                            self.jobs[c['job_id']]['status'] = 'error'
                    else:
                        self._enqueue_ai(self.get(c.get("ai_session_id")), c, "glossary")
            self.save_courses()
            write_json(self.data / "ai-state.json", self.ai)
            self.ai_ready.set()
            return {"ok": True}

    def accept_soundcheck(self, body):
        duration = wav_duration(body, 11)
        with self.lock:
            if self.soundcheck.get("status") in ("pending", "running"):
                raise HTTPException(409, "上一段试音还在处理。")
            job = self.job(self.current, "soundcheck")
            folder = self.data / "soundchecks"
            folder.mkdir(exist_ok=True)
            path = folder / (job["id"] + ".wav")
            path.write_bytes(body)
            self.soundcheck = {"id": job["id"], "job_id": job["id"], "status": "pending", "duration": duration,
                               "audio_url": f"/api/soundcheck/{job['id']}/audio", "text": "", "path": str(path),
                               "prompt": self.asr_prompt(self.get())}
            self.maintenance_queue.put(("soundcheck", self.current, job["id"]))
            return {"job_id": job["id"]}

    def registered_audio_path(self, s, a, allow_missing=False):
        folder = (self.data / s["id"] / "audio").resolve()
        path = (self.data / s["id"] / a["path"]).resolve()
        if a.get("deleted") or not path.is_relative_to(folder) or (not path.is_file() and (not allow_missing or path.exists())):
            raise HTTPException(404, "原录音文件不可用。")
        return path

    def retranscribe(self, sid):
        if not self.engine.status().get("high_quality_available"):
            raise HTTPException(409, "课后高质量修订需要插电、可用的 NVIDIA 独显及完整模型。")
        with self.lock:
            s = self.get(sid)
            if any(r["status"] in ("pending", "running") for r in s["revisions"]):
                raise HTTPException(409, "这节课已有修订正在进行。")
            if any(a["status"] in ("pending", "running") for a in s["audio"]):
                raise HTTPException(409, "请先结束录音并等待课堂字幕处理完成。")
            audio = [a for a in s["audio"] if not a.get("deleted")]
            if not audio:
                raise HTTPException(409, "这节课没有可用的原始录音。")
            for a in audio:
                self.registered_audio_path(s, a)
            job = self.job(s["id"], "revision")
            revision = {"id": job["id"], "created_at": stamp(), "status": "pending", "segments": [],
                        "audio_seqs": [a["seq"] for a in sorted(audio, key=lambda a: a["start"])],
                        "completed_audio": 0, "total_audio": len(audio), "prompt": self.asr_prompt(s),
                        "source_snapshot": copy.deepcopy(s["segments"])}
            s["revisions"].append(revision)
            self.maintenance_queue.put(("revision", s["id"], revision["id"]))
            self.save(s)
            return {"job_id": job["id"], "revision_id": revision["id"]}

    def _maintenance_step(self):
        """One chunk per step; live audio gets priority before the next step."""
        try:
            kind, sid, jid = self.maintenance_queue.get_nowait()
        except queue.Empty:
            return
        s = None
        item = {}
        try:
            with self.lock:
                if kind == "soundcheck":
                    item = self.soundcheck
                    path, prompt = Path(item["path"]), item["prompt"]
                else:
                    s = self.get(sid)
                    item = next(r for r in s["revisions"] if r["id"] == jid)
                    seq = item["audio_seqs"][item["completed_audio"]]
                    a = next(a for a in s["audio"] if a["seq"] == seq)
                    path, prompt = self.registered_audio_path(s, a), item["prompt"]
                item["status"] = self.jobs[jid]["status"] = "running"
            result = (self.engine.transcribe_high_quality if kind == "revision" else self.engine.transcribe)(path, prompt=prompt)
            with self.lock:
                if kind == "soundcheck":
                    item.update(status="done", text=result.get("text", ""), elapsed=result.get("elapsed"),
                                warning=result.get("warning") or ("没有识别出内容，请回放确认老师声音是否清晰。" if not result.get("text") else "试音有文字不代表远处老师能被准确识别，请同时回放核对。"))
                    self.jobs[jid]["status"] = "done"
                else:
                    original = next((x for x in item["source_snapshot"] if x.get("audio_seq") == seq), None)
                    text = result.get("text", "").strip()
                    segid = original["id"] if original else f"R{item['completed_audio'] + 1:04d}"
                    # Empty results are retained explicitly, never silently erase speech.
                    item["segments"].append({"id": segid, "audio_seq": seq, "start": a["start"], "end": a["start"] + a["duration"],
                                             "text": text, "needs_review": not text or any(x.get("needs_review") for x in result.get("segments", []))})
                    item["completed_audio"] += 1
                    if item["completed_audio"] == item["total_audio"]:
                        item.update(status="done", finished_at=stamp())
                        changed = {x["id"] for x in item["segments"] if x["text"] != next((o["text"] for o in s["segments"] if o["id"] == x["id"]), "")}
                        for g in s["groups"]:
                            if changed.intersection(g["segment_ids"]) or changed.intersection(g.get("source_versions", {})):
                                g.update(stale=True, stale_reason="课后修订与课堂原版有差异，请核对后编辑原文并重新分析。", version=g.get("version", 0) + 1)
                        self.jobs[jid]["status"] = "done"
                    else:
                        self.maintenance_queue.put((kind, sid, jid))
        except Exception as exc:
            with self.lock:
                error = str(exc)[:300] or "处理失败，原记录保留。"
                item.update(status="error", error=error)
                self.jobs[jid].update(status="error", error=error)
        finally:
            with self.lock:
                if s is not None:
                    self.save(s)
            self.maintenance_queue.task_done()

    def delete_audio(self, sid):
        with self.lock:
            s = self.get(sid)
            if any(a["status"] in ("pending", "running") for a in s["audio"]) or any(r["status"] in ("pending", "running") for r in s["revisions"]):
                raise HTTPException(409, "请先停止录音并等待转写或课后修订结束。")
            # Validate every registered path before deleting any; never walk a directory.
            paths = [(a, self.registered_audio_path(s, a, allow_missing=True)) for a in s["audio"] if not a.get("deleted")]
            for a, path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    raise HTTPException(409, "部分录音文件正在使用或无法删除。已完成的删除已保存，请稍后重试。") from None
                a.update(deleted=True, deleted_at=stamp())
                self.save(s)
            s["audio_deleted_at"] = stamp()
            self.save(s)
            return {"ok": True, "deleted": len(paths)}
