"""Immutable local slide files and frozen, time-based classroom evidence.

The caller owns the classroom lock and persists its session JSON. This module
does not call models, read configuration, or rewrite existing snapshots.
"""
from __future__ import annotations

import copy
import hashlib
import io
import math
import os
import re
import uuid
import warnings as python_warnings
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 24_000_000
MAX_SLIDES = 4
_SLIDE_PATH = re.compile(r"slides/[0-9a-f]{32}\.(?:png|jpg)")


class SlideContextError(ValueError):
    """An application-authored message safe to show in the local interface."""


def _time(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SlideContextError(f"{label}必须是有效的非负时间。")
    if not math.isfinite(value) or value < 0:
        raise SlideContextError(f"{label}必须是有效的非负时间。")
    return float(value)


def _interval(start, end, *, open_end=False):
    start = _time(start, "开始时间")
    end = None if end is None and open_end else _time(end, "结束时间")
    if end is not None and end < start:
        raise SlideContextError("结束时间不能早于开始时间。")
    return start, end


def _overlaps(start, end, wanted_start, wanted_end):
    # Adjacent pages do not both own a boundary instant; an empty interval
    # contributes no evidence to a nonempty selection.
    return (end is None or end > start) and start < wanted_end and (end is None or end > wanted_start)


def _slide_directory(data, session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise SlideContextError("课堂标识无效。")
    root = Path(data).resolve()
    session_dir = (root / session_id).resolve()
    slide_dir = (session_dir / "slides").resolve()
    if session_dir.parent != root or slide_dir.parent != session_dir:
        raise SlideContextError("课件图片路径必须位于本课堂目录内。")
    return slide_dir


def _image_bytes(content):
    if not isinstance(content, bytes) or not content or len(content) > MAX_IMAGE_BYTES:
        raise SlideContextError("请选择不超过 10 MiB 的 PNG、JPEG 或 WebP 图片。")
    try:
        with python_warnings.catch_warnings():
            python_warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as probe:
                original_format = probe.format
                if original_format not in ("PNG", "JPEG", "WEBP"):
                    raise SlideContextError("只支持实际格式为 PNG、JPEG 或 WebP 的图片。")
                if probe.width * probe.height > MAX_IMAGE_PIXELS:
                    raise SlideContextError("图片不能超过 2400 万像素。")
                if getattr(probe, "n_frames", 1) != 1:
                    raise SlideContextError("请上传单帧图片，不支持动画或多帧图片。")
                probe.verify()
            with Image.open(io.BytesIO(content)) as opened:
                opened.load()
                oriented = ImageOps.exif_transpose(opened)
                has_alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                mode = "RGBA" if has_alpha and original_format != "JPEG" else "RGB"
                # A fresh image does not inherit EXIF, GPS, comments or profiles.
                clean = Image.new(mode, oriented.size)
                clean.paste(oriented.convert(mode))
                output = io.BytesIO()
                if original_format == "JPEG":
                    clean.save(output, format="JPEG", quality=95, subsampling=0)
                    extension, mime_type = "jpg", "image/jpeg"
                else:
                    clean.save(output, format="PNG")
                    extension, mime_type = "png", "image/png"
                encoded = output.getvalue()
                if len(encoded) > MAX_IMAGE_BYTES:
                    raise SlideContextError("图片重新编码后超过 10 MiB，请先缩小图片再上传；未降低清晰度或保存文件。")
                return encoded, extension, mime_type, clean.width, clean.height
    except SlideContextError:
        raise
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise SlideContextError("无法完整解码这张图片，请重新导出为 PNG、JPEG 或 WebP。") from None


def save_slide(data: Path, session: dict, content: bytes, *, start: float, title: str = "") -> dict:
    start = _time(start, "图片开始时间")
    if not isinstance(title, str) or len(title) > 160:
        raise SlideContextError("图片标题最多 160 个字符。")
    slides = session.get("slides", [])
    if not isinstance(slides, list):
        raise SlideContextError("课堂图片记录格式无效。")
    open_slides = [slide for slide in slides if slide.get("end") is None]
    for slide in open_slides:
        previous_start = _time(slide.get("start"), "已有图片开始时间")
        if previous_start > start:
            raise SlideContextError("新图片时间早于当前图片，请先手动调整图片时间区间。")
    encoded, extension, mime_type, width, height = _image_bytes(content)
    slide_dir = _slide_directory(data, session.get("id"))
    sequence = max((int(match.group(1)) for slide in slides
                    if (match := re.fullmatch(r"P(\d{4,})", str(slide.get("id", ""))))), default=0) + 1
    filename = f"{uuid.uuid4().hex}.{extension}"
    target = slide_dir / filename
    created = False
    try:
        slide_dir.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            created = True
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        if created:
            try:
                target.unlink()
            except OSError:
                pass
        raise SlideContextError("课件图片未能保存，请检查本机存储后重试。") from None
    slide = {"id": f"P{sequence:04d}", "title": title.strip(), "start": start, "end": None,
             "path": f"slides/{filename}", "mime_type": mime_type,
             "sha256": hashlib.sha256(encoded).hexdigest(), "width": width, "height": height,
             "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    for previous in open_slides:
        previous["end"] = start
    session.setdefault("slides", []).append(slide)
    return copy.deepcopy(slide)


def update_slide_interval(session: dict, slide_id: str, start: float, end: float | None) -> dict:
    start, end = _interval(start, end, open_end=True)
    slide = next((item for item in session.get("slides", []) if item.get("id") == slide_id), None)
    if slide is None:
        raise SlideContextError("找不到本课堂的这张图片。")
    slide.update(start=start, end=end)
    return copy.deepcopy(slide)


def resolve_slide_path(data: Path, session_id: str, slide: dict) -> Path:
    relative = slide.get("path")
    expected_hash = slide.get("sha256")
    if (not isinstance(relative, str) or not _SLIDE_PATH.fullmatch(relative)
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)):
        raise SlideContextError("课件图片路径或校验信息无效。")
    slide_dir = _slide_directory(data, session_id)
    path = slide_dir / relative.split("/")[1]
    resolved = path.resolve()
    if path.is_symlink() or resolved.parent != slide_dir:
        raise SlideContextError("课件图片路径必须位于本课堂目录内。")
    try:
        digest = hashlib.sha256()
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise SlideContextError("课件图片文件不可用，请重新上传。") from None
    if digest.hexdigest() != expected_hash:
        raise SlideContextError("课件图片校验失败，文件可能已变化；旧引用不会改用这张图片。")
    return resolved


def _clock(value):
    minutes, seconds = divmod(int(value), 60)
    return f"{minutes:02d}:{seconds:02d}"


def _latest_group(groups, intervals, anchor):
    candidates = []
    for index, group in enumerate(groups):
        if group.get("start") is not None and group.get("end") is not None:
            start, end = _interval(group["start"], group["end"])
        else:
            members = [intervals[sid] for sid in group.get("segment_ids", []) if sid in intervals]
            if not members:
                continue
            start, end = min(pair[0] for pair in members), max(pair[1] for pair in members)
        # A chunk/group crossing the anchor stays whole, but a group entirely
        # after the click cannot be treated as the latest earlier discussion.
        if start <= anchor:
            candidates.append((end, start, index, group))
    return max(candidates, key=lambda candidate: candidate[:3])[3] if candidates else None


def snapshot_context(session: dict, *, mode, anchor, group_id=None, slide_ids=None, include_images=True) -> dict:
    anchor = _time(anchor, "选取时刻")
    if mode not in ("recent30", "recent120", "group"):
        raise SlideContextError("请选择最近 30 秒、最近 2 分钟或一个意群。")
    if not isinstance(include_images, bool):
        raise SlideContextError("图片开关格式无效。")
    segments = session.get("segments", [])
    intervals = {}
    for segment in segments:
        intervals[segment["id"]] = _interval(segment.get("start"), segment.get("end"))
    group = None
    if mode == "group":
        groups = session.get("groups", [])
        group = next((g for g in groups if g.get("id") == group_id), None) if group_id else _latest_group(groups, intervals, anchor)
        if group is None:
            raise SlideContextError("还没有可选的意群，请先整理原文或选择最近一段时间。")
        group_id = group["id"]
        wanted = set(group.get("segment_ids", []))
        if wanted - intervals.keys():
            raise SlideContextError("所选意群有原文缺失，请重新选择。")
        selected = [segment for segment in segments if segment["id"] in wanted]
        if not selected:
            raise SlideContextError("所选意群还没有可用原文，请等待转写或手动补充。")
        range_start = min(intervals[s["id"]][0] for s in selected)
        range_end = max(intervals[s["id"]][1] for s in selected)
    else:
        group_id = None
        range_start, range_end = max(0.0, anchor - (30 if mode == "recent30" else 120)), anchor
        selected = [s for s in segments if _overlaps(*intervals[s["id"]], range_start, range_end)]
    selected = [s for s in selected if isinstance(s.get("text"), str) and s["text"].strip()]
    if not selected:
        raise SlideContextError("所选范围没有可用的课堂原文，请等待转写或手动补充；不会仅凭图片猜测刚才讲了什么。")
    selected = sorted(selected, key=lambda s: (*intervals[s["id"]], s["id"]))
    actual_start = min(intervals[s["id"]][0] for s in selected)
    actual_end = max(intervals[s["id"]][1] for s in selected)
    notices = ["按整块原文选取，尚无词级精确裁切；实际范围 "
               f"{_clock(actual_start)}–{_clock(actual_end)}，可能包含所选边界之外的内容。"]
    incomplete = False
    if actual_end + .001 < range_end or actual_start > range_start + .001:
        incomplete = True
        notices.append("所选范围边缘尚不能确认原文完整；可能仍有转写延迟、未收到的录音或静音，请核对。")
    if group and group.get("partial"):
        incomplete = True
        notices.append("这个意群标记为尚未讲完，请把当前内容作为阶段材料。")
    pending = failed = untranscribed = False
    source_audio = {s.get("audio_seq") for s in segments if "audio_seq" in s
                    and isinstance(s.get("text"), str) and s["text"].strip()}
    for audio in session.get("audio", []):
        audio_start = _time(audio.get("start"), "录音开始时间")
        audio_end = audio_start + _time(audio.get("duration"), "录音时长")
        if not _overlaps(audio_start, audio_end, range_start, range_end):
            continue
        pending |= audio.get("status") in ("pending", "running")
        failed |= audio.get("status") == "error"
        untranscribed |= audio.get("status") == "done" and audio.get("seq") not in source_audio
    if pending:
        notices.append("所选范围有录音正在等待或进行转写，尚不能确认原文完整。")
    if failed:
        notices.append("所选范围有录音转写失败，尚不能确认原文完整；可先重试转写。")
    if untranscribed:
        notices.append("所选范围有录音未形成可用原文，可能为静音或未识别出文字，不能据此确认没有讲课内容。")
    incomplete |= pending or failed or untranscribed
    frozen_segments = copy.deepcopy(selected)
    for segment in frozen_segments:
        if segment.get("source") in ("manual", "text"):
            segment["time_estimated"] = True
            incomplete = True
    if any(s.get("time_estimated") for s in frozen_segments):
        notices.append("所选原文包含手动补充，其时间为估算值，不能视为准确的录音定位。")

    available = session.get("slides", [])
    by_id = {slide["id"]: slide for slide in available}
    if slide_ids is not None:
        if (not isinstance(slide_ids, (list, tuple)) or any(not isinstance(sid, str) for sid in slide_ids)
                or len(slide_ids) != len(set(slide_ids))):
            raise SlideContextError("请选择不重复的本课堂图片编号。")
        if len(slide_ids) > MAX_SLIDES:
            raise SlideContextError("一次最多使用 4 张课件图片，请缩小范围或手动选择。")
        if any(sid not in by_id for sid in slide_ids):
            raise SlideContextError("所选图片不属于本课堂或已经不存在。")
    if not include_images:
        chosen_slides = []
    elif slide_ids is not None:
        chosen_slides = [by_id[sid] for sid in slide_ids]
    else:
        chosen_slides = [slide for slide in available
                         if _overlaps(*_interval(slide.get("start"), slide.get("end"), open_end=True), range_start, range_end)]
    if len(chosen_slides) > MAX_SLIDES:
        raise SlideContextError("所选时间关联了超过 4 张课件图片，请缩小范围或手动选择，系统不会静默漏图。")
    chosen_slides = sorted(chosen_slides, key=lambda slide: (_time(slide.get("start"), "图片开始时间"), slide["id"]))
    for slide in chosen_slides:
        _interval(slide.get("start"), slide.get("end"), open_end=True)
    if slide_ids is not None and any(not _overlaps(slide["start"], slide.get("end"), range_start, range_end) for slide in chosen_slides):
        notices.append("手选图片包含所选时间之外的课件；图片内容不等于老师在该时间已经讲过。")
    frozen_slides = copy.deepcopy(chosen_slides)
    lines = [f"固定选取范围：{_clock(range_start)}–{_clock(range_end)}。",
             "课堂原文证据（机器转写可能有误；手动补充时间为估算）："]
    for segment in frozen_segments:
        lines.extend([f"[{segment['id']}] {_clock(segment['start'])}–{_clock(segment['end'])} · 来源 {segment.get('source', 'unknown')}", segment["text"]])
    if frozen_slides:
        lines.append("课件图片证据（课件上写的内容不等于老师已经讲过；图片本身随本次请求另行提供）：")
        for slide in frozen_slides:
            end_label = _clock(slide["end"]) if slide.get("end") is not None else "尚未标记结束"
            lines.append(f"[{slide['id']}] {slide.get('title', '')} · 关联时间 {_clock(slide['start'])}–{end_label}")
    lines.extend(["范围与完整性提示：", *notices])
    return {"mode": mode, "anchor": anchor, "group_id": group_id,
            "include_images": include_images, "requested_slide_ids": copy.deepcopy(slide_ids),
            "requested_range": {"start": range_start, "end": range_end},
            "actual_range": {"start": actual_start, "end": actual_end},
            "segments": frozen_segments, "slides": frozen_slides, "warnings": notices,
            "incomplete": bool(incomplete), "context": "\n".join(lines),
            "source_ids": [s["id"] for s in frozen_segments] + [s["id"] for s in frozen_slides]}
