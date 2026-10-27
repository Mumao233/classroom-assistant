"""Local, bounded classroom archives. Imported HTML/JS are never trusted or run.

Application records and imported records have separate trust boundaries. All
archives are staged outside the classroom glob; a restore publishes one new
directory only after its metadata and every included asset have been checked.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import struct
import threading
import uuid
import wave
import warnings
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from fastapi import HTTPException
from PIL import Image

from classroom_features import write_json
from ai_settings import empty_usage
from session_order import creation_order
from slide_context import MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, SlideContextError, resolve_slide_path

PACKAGE_SCHEMA = 1
MAX_PACKAGE_BYTES = 2 * 1024**3
MAX_FILES = 10_000
MAX_MANIFEST_BYTES = 8 * 1024**2
MAX_SESSION_BYTES = 64 * 1024**2
MAX_DIRECTORY_BYTES = 16 * 1024**2
CHUNK = 1024 * 1024
ROOT = Path(__file__).resolve().parent
ID = re.compile(r'^[0-9a-f]{32}$')
REQUEST_ID = re.compile(r'^[A-Za-z0-9_-]{8,128}$')
SLIDE_PATH = re.compile(r'^slides/[0-9a-f]{32}\.(?:png|jpg)$')
AUDIO_PATH = re.compile(r'^audio/[0-9]+\.wav$')
HASH = re.compile(r'^[0-9a-f]{64}$')
MISSING_STATES = {'missing', 'corrupt', 'invalid'}


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def fail(message, status=422):
    raise HTTPException(status, message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode('utf-8')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK), b''):
            value.update(chunk)
    return value.hexdigest()


def zip_directory_limits(path):
    """Bound central-directory allocation before Python parses its entries."""
    size = path.stat().st_size
    with path.open('rb') as source:
        source.seek(max(0, size - 65557))
        tail = source.read(65557)
        offset = tail.rfind(b'PK\x05\x06')
        if offset < 0 or offset + 22 > len(tail):
            fail('课堂包缺少完整 ZIP 目录。')
        end = struct.unpack('<4s4H2LH', tail[offset:offset + 22])
        _, disk, directory_disk, disk_count, count, directory_size, directory_offset, comment = end
        if disk or directory_disk or disk_count != count or offset + 22 + comment != len(tail):
            fail('课堂包 ZIP 分卷或目录结构不受支持。')
        if count == 65535 or directory_size == 0xffffffff or directory_offset == 0xffffffff:
            absolute = max(0, size - 65557) + offset
            if absolute < 20:
                fail('ZIP64 目录不完整。')
            source.seek(absolute - 20)
            locator = source.read(20)
            signature, locator_disk, location, disks = struct.unpack('<4sLQL', locator)
            if signature != b'PK\x06\x07' or locator_disk or disks != 1 or location + 56 > absolute - 20:
                fail('ZIP64 分卷或目录结构无效。')
            source.seek(location)
            record = struct.unpack('<4sQ2H2L4Q', source.read(56))
            signature, record_size, _, _, disk, directory_disk, disk_count, count, directory_size, directory_offset = record
            if signature != b'PK\x06\x06' or record_size < 44 or disk or directory_disk or disk_count != count:
                fail('ZIP64 目录结构无效。')
        if count > MAX_FILES or directory_size > MAX_DIRECTORY_BYTES:
            fail('课堂包文件数或目录元数据超过安全上限。', 413)
        if directory_offset + directory_size > size:
            fail('课堂包目录超出文件范围。')


def json_value(body, maximum=MAX_SESSION_BYTES):
    if len(body) > maximum:
        fail(f'元数据超过 {maximum:,} 字节上限；尚未恢复任何课堂。', 413)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                fail('JSON 含重复字段，不能确定原始记录。')
            result[key] = value
        return result
    try:
        value = json.loads(body.decode('utf-8-sig'), object_pairs_hook=pairs,
                           parse_constant=lambda _: fail('JSON 时间或数值不是有限数。'))
    except (ValueError, UnicodeError, RecursionError):
        fail('课堂元数据不是有效 UTF-8 JSON，尚未恢复。')
    _bounded_json(value)
    return value


def _bounded_json(value, depth=0, count=None):
    count = [0] if count is None else count
    count[0] += 1
    if depth > 32 or count[0] > 1_000_000:
        fail('课堂元数据层级或记录数量过大。', 413)
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 256:
                fail('课堂元数据字段格式无效。')
            _bounded_json(child, depth + 1, count)
    elif isinstance(value, list):
        if len(value) > 100_000:
            fail('课堂元数据数组过大。', 413)
        for child in value:
            _bounded_json(child, depth + 1, count)
    elif isinstance(value, str):
        if len(value) > 2_000_000:
            fail('课堂元数据单项文字过大。', 413)
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            fail('课堂元数据含无效 Unicode 字符。')
    elif isinstance(value, float) and not math.isfinite(value):
        fail('课堂元数据包含无效数值。')


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def safe_name(name):
    if not isinstance(name, str) or not name or len(name) > 240 or '\\' in name or any(ord(char) < 32 or char in '<>"|?*' for char in name):
        fail('课堂包包含不安全的文件路径。')
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or any(part in ('', '.', '..') for part in path.parts):
        fail('课堂包包含绝对路径或越界路径。')
    for part in path.parts:
        if ':' in part or part.endswith((' ', '.')) or re.match(r'^(?:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)', part, re.I):
            fail('课堂包包含 Windows 保留名称或数据流路径。')
    return name


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 86400 * 7


def _records(value, label):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        fail(f'{label}记录结构无效。')
    return value


def _segment(segment, revision=False):
    if (not isinstance(segment.get('id'), str) or not re.fullmatch(r'[SR]\d{4,}' if revision else r'S\d{4,}', segment['id'])
            or not isinstance(segment.get('text'), str) or not _time(segment.get('start'))
            or not _time(segment.get('end')) or segment['end'] < segment['start']):
        fail('原文内容、编号或时间范围无效。')
    if type(segment.get('version', 0)) is not int or segment.get('version', 0) < 0:
        fail('原文版本无效。')
    for key in ('source', 'machine_text', 'edited_at'):
        if key in segment and not isinstance(segment[key], str):
            fail('原文来源或机器原稿格式无效。')
    if segment.get('audio_seq') is not None and (type(segment['audio_seq']) is not int or segment['audio_seq'] < 0):
        fail('原音定位编号无效。')


def _slide_metadata(image):
    path = image.get('path')
    if not isinstance(path, str) or not SLIDE_PATH.fullmatch(path) or not HASH.fullmatch(str(image.get('sha256', ''))):
        fail('图片路径或校验信息无效。')
    expected = 'image/png' if path.endswith('.png') else 'image/jpeg'
    if image.get('mime_type') != expected:
        fail('图片缺少有效媒体类型，或媒体类型与扩展名不一致。')
    if not isinstance(image.get('id'), str) or not re.fullmatch(r'P\d{4,}', image['id']):
        fail('图片编号无效。')
    if not _time(image.get('start')) or image.get('end') is not None and (not _time(image['end']) or image['end'] < image['start']):
        fail('课件时间范围无效。')
    if type(image.get('version', 1)) is not int or image.get('version', 1) < 1:
        fail('图片版本无效。')
    for old in _records(image.get('versions', []), '图片历史版本'):
        _slide_metadata(old)


def _evidence(evidence):
    if not isinstance(evidence, dict):
        fail('冻结依据结构无效。')
    for segment in _records(evidence.get('segments', []), '冻结原文'):
        _segment(segment)
    for slide in _records(evidence.get('slides', []), '冻结图片'):
        _slide_metadata(slide)
    if evidence.get('source_session_id') is not None and not ID.fullmatch(str(evidence['source_session_id'])):
        fail('冻结依据的来源课堂编号无效。')
    for key in ('source_ids', 'baseline_source_ids'):
        if key in evidence and (not isinstance(evidence[key], list) or any(not isinstance(x, str) for x in evidence[key])):
            fail('冻结依据来源列表无效。')
    if evidence.get('scope') == 'range':
        value = evidence.get('range')
        if not isinstance(value, list) or len(value) != 2 or not all(_time(x) for x in value) or value[0] >= value[1]:
            fail('冻结依据的时间范围无效。')


def _review_content(content, kind):
    if isinstance(content, str):
        return
    if not isinstance(content, dict):
        fail('复习正文结构无效。')
    records = _records(content.get('sections', []), '复习笔记') if kind == 'notes' else [content]
    for item in records:
        for key in ('title', 'front', 'back', 'body', 'supplement', 'english', 'english_meaning'):
            if key in item and not isinstance(item[key], str):
                fail('复习正文文字格式无效。')
        for key in ('source_ids', 'missing'):
            if key in item and (not isinstance(item[key], list) or any(not isinstance(x, str) for x in item[key])):
                fail('复习正文来源或缺失说明格式无效。')


def _no_link(path):
    """Check each existing component before resolving (including Windows junctions)."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or getattr(candidate, 'is_junction', lambda: False)():
            fail('媒体或暂存路径不能包含链接。')
    return path


def validate_session(value):
    """Validate external records before any existing loader may see them."""
    if not isinstance(value, dict) or not ID.fullmatch(str(value.get('id', ''))):
        fail('课堂 JSON 缺少有效课堂标识。')
    if not isinstance(value.get('title'), str) or not isinstance(value.get('created_at'), str):
        fail('课堂 JSON 缺少名称或创建时间。')
    try:
        datetime.fromisoformat(value['created_at'].replace('Z', '+00:00'))
    except ValueError:
        fail('课堂创建时间格式无效。')
    for record in walk(value):
        if any(key.lower() in ('config', 'api_key', 'credentials', 'password', 'api_settings', 'authorization') for key in record):
            fail('课堂包不能包含账号配置或凭据。')
        if 'evidence' in record:
            _evidence(record['evidence'])
    if type(value.get('schema_version', 1)) is not int or value.get('schema_version', 1) != 1:
        fail('这份课堂使用未来或不兼容的数据格式，请更新应用。')
    session = copy.deepcopy(value)
    for key in ('segments', 'groups', 'questions', 'audio', 'revisions', 'slides'):
        session.setdefault(key, [])
        if not isinstance(session[key], list) or any(not isinstance(item, dict) for item in session[key]):
            fail(f'课堂 {key} 记录格式无效。')
    for key in ('segments', 'groups', 'questions', 'revisions', 'slides'):
        identifiers = [item.get('id') for item in session[key]]
        if any(not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier) for identifier in identifiers) or len(identifiers) != len(set(identifiers)):
            fail(f'课堂 {key} 存在重复或无效标识。')
    segment_ids = {item['id'] for item in session['segments']}
    for segment in session['segments']:
        _segment(segment)
    for group in session['groups']:
        ids = group.get('segment_ids', [])
        if not isinstance(ids, list) or any(not isinstance(x, str) or x not in segment_ids for x in ids):
            fail('意群引用的原文不存在。')
        group.setdefault('text', ' '.join(s['text'] for s in session['segments'] if s['id'] in ids))
        group.setdefault('segment_ids', ids)
        group.setdefault('status', 'saved')
    for question in session['questions']:
        if not isinstance(question.get('question'), str):
            fail('问题正文格式无效。')
        question.setdefault('status', 'error')
        question.setdefault('answer', '')
        if question.get('study') and (not isinstance(question.get('evidence'), dict) or not isinstance(question.get('destination'), dict)
                                      or not isinstance(question['destination'].get('token'), str)
                                      or not isinstance(question['evidence'].get('slides'), list)):
            fail('图文问题缺少原始依据或接入记录。')
    seen_audio = set()
    for audio in session['audio']:
        seq = audio.get('seq')
        if type(seq) is not int or seq < 0 or seq in seen_audio or not AUDIO_PATH.fullmatch(str(audio.get('path', ''))):
            fail('录音编号或路径无效。')
        if audio['path'] != f'audio/{seq}.wav' or not _time(audio.get('start')) or not _time(audio.get('duration')) or not .1 <= audio['duration'] <= 20:
            fail('录音时间或格式范围无效。')
        seen_audio.add(seq)
        audio.setdefault('status', 'error')
    image_paths = {}
    for image in image_references(session):
        _slide_metadata(image)
        if image['path'] in image_paths and image_paths[image['path']] != image['sha256']:
            fail('同一图片路径登记了不同内容，不能安全恢复。')
        image_paths[image['path']] = image['sha256']
        if 'versions' in image:
            _records(image['versions'], '图片历史版本')
    for slide in session['slides']:
        _slide_metadata(slide)
    for task in session['groups'] + session['questions'] + session['revisions'] + session['audio']:
        for key in ('status', 'answer', 'analysis', 'error', 'text', 'streaming_text', 'stream_status'):
            if task.get(key) is not None and not isinstance(task[key], str):
                fail('课堂任务状态或文字格式无效。')
    review = session.get('review')
    if review is not None:
        if not isinstance(review, dict) or type(review.get('schema_version', 1)) is not int or review.get('schema_version', 1) != 1:
            fail('复习数据格式不兼容，请更新应用。')
        for key in ('marks', 'runs', 'cards', 'notes'):
            if not isinstance(review.get(key, []), list) or any(not isinstance(x, dict) for x in review.get(key, [])):
                fail('复习记录结构无效。')
            review.setdefault(key, [])
            ids = [x.get('id') for x in review[key]]
            if any(not isinstance(x, str) for x in ids) or len(ids) != len(set(ids)):
                fail('复习记录编号无效或重复。')
            for item in review[key]:
                if not isinstance(item.get('evidence'), dict):
                    fail('复习记录缺少冻结依据。')
                if key in ('cards', 'notes'):
                    _review_content(item.get('ai_content'), key)
                    if key == 'notes' and 'unresolved_marks' in item:
                        _records(item['unresolved_marks'], '笔记未解标记')
                    if 'user_draft' in item:
                        draft = item['user_draft']
                        if not isinstance(draft, dict) or type(draft.get('revision')) is not int or draft['revision'] < 0 or not isinstance(draft.get('personal_note', ''), str):
                            fail('复习个人编辑版本无效。')
                        _review_content(draft.get('content'), key)
                if key == 'runs':
                    for field in ('coverage', 'destination', 'options'):
                        if not isinstance(item.get(field), dict):
                            fail('复习任务设置或覆盖记录无效。')
                    if not isinstance(item.get('status'), str):
                        fail('复习任务状态无效。')
                    for batch in _records(item.get('batches'), '复习批次'):
                        if not isinstance(batch.get('status'), str) or not isinstance(batch.get('evidence'), dict):
                            fail('复习批次状态或依据无效。')
                        _records(batch.get('attempts'), '复习请求')
    session.setdefault('demo', False)
    session.setdefault('settings', {'terms': '', 'auto_analyze': False})
    if not isinstance(session['settings'], dict):
        fail('课堂设置格式无效。')
    if not isinstance(session['settings'].get('terms', ''), str):
        fail('课堂术语设置格式无效。')
    usage = session.setdefault('ai_usage', empty_usage())
    if not isinstance(usage, dict):
        fail('课堂使用量记录格式无效。')
    for key, default in empty_usage().items():
        if key not in usage:
            usage[key] = default
        if type(default) is int and (type(usage[key]) is not int or usage[key] < 0):
            fail('课堂使用量记录数值无效。')
    if not isinstance(session.get('source_classroom_map', {}), dict) or any(not ID.fullmatch(str(key)) for key in session.get('source_classroom_map', {})):
        fail('课堂来源映射无效。')
    if 'portable_media' in session:
        for media in _records(session['portable_media'], '媒体可用性'):
            if not isinstance(media.get('kind'), str) or not isinstance(media.get('original_path'), str) or not isinstance(media.get('reason', ''), str):
                fail('媒体可用性记录格式无效。')
    for revision in session['revisions']:
        for segment in _records(revision.get('segments', []), '修订原文'):
            _segment(segment, revision=True)
    if review is not None:
        from review_workflow import validate_saved_review
        try:
            validate_saved_review(review)
        except (ValueError, TypeError, KeyError):
            fail('复习元数据缺少运行所需字段或格式不兼容，尚未恢复任何课堂。')
    session.setdefault('pending_ids', [])
    session.setdefault('flush_requested', False)
    return session


def image_references(session):
    # Includes removed images, immutable versions, study receipts and every
    # review evidence snapshot. Never resolves a path using a source classroom.
    refs, seen = [], set()
    for item in walk(session):
        if 'sha256' in item and 'path' in item and ('width' in item or str(item['path']).startswith('slides/')):
            if not isinstance(item['path'], str) or not isinstance(item['sha256'], str):
                fail('图片路径或校验信息格式无效。')
            key = (item.get('path'), item.get('sha256'))
            if key not in seen:
                refs.append(item)
                seen.add(key)
    return refs


def validate_image(path):
    if path.stat().st_size > MAX_IMAGE_BYTES:
        fail('包内图片超过 10 MiB。', 413)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in ('PNG', 'JPEG') or getattr(image, 'n_frames', 1) != 1 or image.width * image.height > MAX_IMAGE_PIXELS:
                    fail('包内图片格式、帧数或像素超限。')
                if image.format != {'.png': 'PNG', '.jpg': 'JPEG'}.get(path.suffix.lower()):
                    fail('包内图片实际格式与文件扩展名不一致。')
                image.load()
            with Image.open(path) as image:
                image.verify()
    except HTTPException:
        raise
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        fail('包内图片无法完整校验。')


def validate_audio(path):
    if path.stat().st_size > 2_000_000:
        fail('包内录音片段过大。', 413)
    try:
        with wave.open(str(path), 'rb') as audio:
            frames = audio.getnframes()
            if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, 16000, 2, 'NONE') or not .1 <= frames / 16000 <= 20 or len(audio.readframes(frames)) != frames * 2:
                fail('包内录音必须为 16 kHz 单声道 16 bit PCM WAV，0.1–20 秒。')
    except (OSError, wave.Error, EOFError):
        fail('包内录音损坏或格式无效。')


def media_inventory(session, data, include_audio):
    result = []
    for slide in image_references(session):
        item = {'kind': 'image', 'id': slide.get('id'), 'version': slide.get('version', 1),
                'original_path': slide['path'], 'sha256': slide['sha256'], 'availability': 'available'}
        try:
            _no_link(Path(data) / session['id'] / slide['path'])
            path = resolve_slide_path(data, session['id'], slide)
            validate_image(path)
            item.update(size=path.stat().st_size, archive_path=f"assets/{slide['sha256']}{path.suffix}")
        except (SlideContextError, HTTPException, OSError) as exc:
            code = getattr(exc, 'code', 'missing')
            item.update(availability=code if code in MISSING_STATES else 'invalid', reason=str(getattr(exc, 'detail', exc)))
        result.append(item)
    inherited = {(x.get('kind'), x.get('original_path')): x for x in session.get('portable_media', [])}
    folder = Path(data) / session['id'] / 'audio'
    for audio in session.get('audio', []):
        item = {'kind': 'audio', 'id': audio['seq'], 'original_path': audio['path'],
                'start': audio['start'], 'duration': audio['duration'], 'availability': 'available'}
        path = Path(data) / session['id'] / audio['path']
        if audio.get('deleted'):
            item.update(availability='deleted', reason='原录音已删除，原文与定位仍保留。')
        elif not include_audio:
            item.update(availability='not_included', reason='本包未勾选携带录音，无法离线回听。')
        else:
            try:
                _no_link(path)
                if not path.resolve().is_relative_to(folder.resolve()):
                    fail('录音路径不在本课堂目录。')
                validate_audio(path)
                sha = digest(path)
                item.update(size=path.stat().st_size, sha256=sha, archive_path=f'assets/{sha}.wav')
            except (OSError, HTTPException) as exc:
                prior = inherited.get(('audio', audio['path']), {})
                item.update(availability='missing', reason=prior.get('reason') or str(getattr(exc, 'detail', exc)) or '录音文件缺失。')
        result.append(item)
    return result


class _ExportSourceView:
    """Minimal read-only interface for the existing review source comparator.

    No Classroom, ASR engine, provider configuration or worker is initialized.
    """
    def __init__(self, data):
        self.data = Path(data)

    def registered_audio_path(self, session, audio):
        if audio.get('deleted') or not AUDIO_PATH.fullmatch(str(audio.get('path', ''))):
            fail('原录音文件不可用。', 404)
        path = _no_link(self.data / session['id'] / audio['path'])
        if not path.is_file():
            fail('原录音文件不可用。', 404)
        return path


def portable_session_projection(session, data, *, media=None, includes_media=False):
    """One read-only export projection for JSON, Markdown and offline packages."""
    from review_workflow import ReviewWorkflow
    value = copy.deepcopy(session)
    media = copy.deepcopy(media) if media is not None else media_inventory(value, Path(data), True)
    view = _ExportSourceView(data)
    review = value.get('review')
    if isinstance(review, dict):
        for collection in ('marks', 'cards', 'notes', 'runs'):
            for item in review.get(collection, []) if isinstance(review.get(collection, []), list) else []:
                if not isinstance(item, dict):
                    continue
                try:
                    item['source_status'] = ReviewWorkflow.review_source_status(view, value, item.get('evidence') or {})
                except (KeyError, TypeError, ValueError):
                    item['source_status'] = {'changed': True, 'unavailable': True, 'new_material': False,
                        'messages': ['此旧复习记录的依据不完整，无法验证；原始内容已保留。']}
                for audio in item['source_status'].get('audio', []):
                    match = next((x for x in media if x['kind'] == 'audio' and x['id'] == audio['seq']), None)
                    if match and match['availability'] != 'available':
                        audio.update(available=False, url=None, reason=match.get('reason') or '媒体不可用。')
                    elif match and includes_media:
                        audio['url'] = match.get('archive_path')
                if collection == 'runs':
                    batches = item.get('batches', [])
                    item.setdefault('coverage', {})['completed_batches'] = sum(b.get('status') == 'done' for b in batches)
                    item['repeat_billing_required'] = bool(item.get('duplicate_billing_risk') or any(
                        b.get('status') == 'unknown' or any(a.get('result_unknown') for a in b.get('attempts', [])) for b in batches))
    from slide_history import source_status
    for question in value.get('questions', []):
        if isinstance(question.get('evidence'), dict):
            question['source_status'] = source_status(Path(data), value, question['evidence'])
            if question['source_status']['changed']:
                question.update(stale=True, stale_reason='；'.join(question['source_status']['messages']))
    value['portable_sources'] = {'schema_version': 1, 'exported_at': now(),
                                'media': media, 'includes_media': bool(includes_media)}
    return value


def adopted(item):
    draft = item.get('user_draft') or {}
    if draft.get('revision', 0) > 0 and isinstance(draft.get('content'), (dict, str)):
        return draft['content'], '用户采用的编辑版', draft.get('personal_note', '')
    return item.get('ai_content') or {}, 'AI 整理', draft.get('personal_note', '')


def clock(value):
    value = max(0, int(value or 0))
    return f'{value // 60:02d}:{value % 60:02d}'


def _evidence_lines(evidence):
    lines = ['教师材料依据（机器转写可能有误）：', '']
    for segment in evidence.get('segments', []):
        lines += [f"[{segment['id']}] {clock(segment.get('start'))}–{clock(segment.get('end'))}", segment.get('text', ''), '']
    for slide in evidence.get('slides', []):
        lines += [f"[{slide['id']}] 图片 v{slide.get('version', 1)} · {slide.get('path', '')} · SHA256 {slide.get('sha256', '')}", '']
    return lines


def _source_status_lines(item):
    status = item.get('source_status') or {}
    lines = []
    if status.get('changed') or item.get('stale'):
        lines += ['依据已变化，待核对；以下仍保留生成时的旧版本。', '']
    if status.get('new_material'):
        lines += ['新增内容尚未纳入本版本。', '']
    if status.get('unavailable'):
        lines += ['部分历史依据不可用，请核对媒体清单。', '']
    for message in status.get('messages', []):
        lines += [message, '']
    return lines


def portable_markdown(session, data=None, media=None):
    if data is not None:
        session = portable_session_projection(session, data, media=media)
        media = session['portable_sources']['media']
    media = media if media is not None else media_inventory(session, Path(data), True) if data else []
    lines = [f"# {session.get('title', '课堂笔记')}", '', f"导出时间：{now()}", '',
             '教师材料、机器识别、AI 内容与个人修改分别标注。此 Markdown 文件不包含图片本体或录音；完整课堂包携带范围以媒体清单为准。', '', '## 课堂原文', '']
    for segment in session.get('segments', []):
        lines += [f"### [{segment['id']}] {clock(segment.get('start'))}–{clock(segment.get('end'))}",
                  f"来源：{segment.get('source', '未知')}；原文版本 {segment.get('version', 0)}", segment.get('text', ''), '']
        if 'machine_text' in segment:
            lines += ['修改前的机器识别：' + segment['machine_text'], '']
        if segment.get('audio_seq') is not None:
            audio = next((x for x in media if x['kind'] == 'audio' and x['id'] == segment['audio_seq']), None)
            lines += [f"原音定位：录音 {segment['audio_seq']}；片段内偏移 {max(0, segment['start'] - (audio or {}).get('start', segment['start'])):.3f} 秒；{(audio or {}).get('reason') or (audio or {}).get('availability', '文件未包含')}。", '']
        else:
            lines += ['原音定位：没有可验证的对应录音（手动补充或旧记录）。', '']
    for group in session.get('groups', []):
        lines += [f"## 意群 {group['id']}", '教师材料摘录：', group.get('text', ''), '',
                  'AI 解释：', group.get('analysis') or group.get('error') or '尚未完成。', '']
        if group.get('stale'):
            lines += ['待核对：' + group.get('stale_reason', '依据已变化。'), '']
        if group.get('partial'):
            lines += ['阶段整理：老师仍在讲解，本段可能未完成。', '']
    for question in session.get('questions', []):
        lines += [f"## 问题 {question['id']}：{question.get('question', '')}", 'AI 回答：',
                  question.get('answer') or question.get('error') or '尚未完成。', '']
        if question.get('evidence'):
            lines += _evidence_lines(question['evidence'])
        if question.get('stale'):
            lines += ['待核对：' + question.get('stale_reason', '回答依据已变化。'), '']
    for task in session.get('groups', []) + session.get('questions', []):
        if task.get('ai_provider'):
            lines += [f"{task['id']} 正式结果接入：{task['ai_provider']}；模型：{task.get('ai_model') or '由本地 Codex 选择'}。", '']
        if task.get('result_unknown'):
            lines += [f"{task['id']} 请求结果未知，无法确定服务商是否已处理；未自动重新提交。", '']
    review = session.get('review') or {}
    lines += ['## 本课标记', '']
    for mark in review.get('marks', []):
        if mark.get('active', True):
            lines += [f"- {mark.get('kind')} · {mark.get('target_id')} · {'已理解' if mark.get('resolved') else '待核对'} · {mark.get('note', '')}"]
            lines += _source_status_lines(mark)
    if review.get('runs'):
        lines += ['', '## 复习生成状态与覆盖', '']
    for run in review.get('runs', []):
        batches = run.get('batches', [])
        coverage = run.get('coverage') or {}
        completed = sum(batch.get('status') == 'done' for batch in batches)
        total = coverage.get('batch_count', len(batches))
        lines += [f"### 任务 {run.get('id')} · {run.get('status')}",
                  f"已完成 {completed} / {total} 批；{coverage.get('segment_count', 0)} 段原文、{coverage.get('slide_count', 0)} 张图片。", '']
        lines += _source_status_lines(run)
        if run.get('error'):
            lines += [run['error'], '']
        if run.get('repeat_billing_required') or run.get('result_unknown') or run.get('status') == 'unknown':
            lines += ['请求结果未知，无法确定服务商是否已处理；可能已计费，未自动重新提交。', '']
        if coverage.get('pending_audio'):
            lines += [f"生成时还有 {coverage['pending_audio']} 段录音未完成转写，未纳入本次。", '']
        for excluded in coverage.get('excluded', []):
            lines += ['未纳入：' + (excluded if isinstance(excluded, str) else json.dumps(excluded, ensure_ascii=False)), '']
        for index, batch in enumerate(batches, 1):
            lines += [f"- 批次 {index}：{batch.get('status')}" + ('（未完成）' if batch.get('status') != 'done' else '')]
            if batch.get('error'):
                lines += [batch['error']]
            lines += batch.get('context_warnings', [])
    for card in review.get('cards', []):
        content, label, personal = adopted(card)
        if isinstance(content, str):
            content = {'front': content}
        lines += ['', f"## 卡片 {card['id']} · {content.get('title', '')}", label, '',
                  '问题：', content.get('front', ''), '', '解释／步骤：', content.get('back', ''), '']
        lines += _source_status_lines(card)
        if content.get('supplement'):
            lines += ['AI 补充（不代表老师讲过）：', content['supplement'], '']
        if content.get('english'):
            lines += ['AI 建议英文答题表述：', content['english'], content.get('english_meaning', ''), '']
        if content.get('missing'):
            lines += ['课堂记录不足：' + str(content['missing']), '']
        if personal:
            lines += ['个人备注：', personal, '']
        lines += _evidence_lines(card.get('evidence') or {})
    selected = next((x for x in review.get('notes', []) if x.get('id') == review.get('selected_note_id')), None)
    if selected:
        content, label, personal = adopted(selected)
        lines += ['', '## 当前采用的课后笔记', label, '']
        lines += _source_status_lines(selected)
        if isinstance(content, str):
            lines += [content, '']
        else:
            for section in content.get('sections', []):
                lines += [f"### {section.get('title', section.get('section', ''))}", section.get('body', ''), '']
                if section.get('supplement'):
                    lines += ['AI 补充：', section['supplement'], '']
        if personal:
            lines += ['个人笔记：', personal, '']
        if selected.get('unresolved_marks'):
            lines += ['生成笔记时仍未解决的疑问：', '']
            for mark in selected['unresolved_marks']:
                lines += [f"- {mark.get('target_id', '')}：{mark.get('note') or '标记为没懂，尚未手动确认理解。'}"]
        lines += _evidence_lines(selected.get('evidence') or {})
    for revision in session.get('revisions', []):
        lines += ['', '## 课后独立修订（原课堂文字保留）', f"状态：{revision.get('status')}", '']
        for segment in revision.get('segments', []):
            lines += [f"[{segment.get('id')}] {clock(segment.get('start'))}–{clock(segment.get('end'))}", segment.get('text', ''), '']
        if revision.get('error'):
            lines += [revision['error'], '']
    for item in walk(session):
        if item.get('streaming_text') and item.get('stream_status') != 'complete':
            lines += ['', '未完成 AI 草稿（未作为正式结果）：', item['streaming_text'], '']
    lines += ['', '## 媒体清单', '']
    for item in media:
        lines += [f"- {item['kind']} {item['id']}：{item.get('archive_path', item['original_path'])}；{item['availability']}；{item.get('reason', '')}"]
    usage = session.get('ai_usage') or {}
    if usage.get('requests'):
        lines += ['', '## API 用量', '', f"请求 {usage['requests']} 次；成功 {usage.get('completed', 0)} 次；失败 {usage.get('failed', 0)} 次。",
                  f"中断后结果未知 {usage.get('unknown', 0)} 次（可能已计费）。",
                  f"其中 {usage.get('usage_reported_requests', 0)} 次返回 token 用量，已报告总量 {usage.get('total_tokens', 0)}；未返回部分未知，实际费用以服务商账单为准。", '']
    return '\n'.join(str(x) for x in lines)


def offline_html(session, manifest):
    payload = json.dumps({'session': session, 'manifest': manifest}, ensure_ascii=False, allow_nan=False)
    for old, new in (('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'), ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
        payload = payload.replace(old, new)
    return ("<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'self\'; style-src \'self\' \'unsafe-inline\'; img-src \'self\' data:; media-src \'self\'; font-src \'self\'; connect-src \'none\'; object-src \'none\'; base-uri \'none\'">'
            f'<title>{html.escape(session["title"])} · 离线课堂</title>'
            '<link rel="stylesheet" href="reader/portable-reader.css"><link rel="stylesheet" href="reader/vendor/katex/katex.min.css">'
            '<body><header><p>课间 · 离线课堂包</p><h1 id="title"></h1><p id="summary"></p>'
            '<nav><a href="#transcripts">课堂原文</a><a href="#slides">图片</a><a href="#marks">标记</a><a href="#cards">卡片</a><a href="#notes">课后笔记</a><a href="#answers">AI 解答</a></nav></header>'
            '<main id="reader"><noscript>请启用应用自带的本地脚本，或打开同目录 notes.md 阅读文字。</noscript></main>'
            f'<script id="classroom-data" type="application/json">{payload}</script>'
            '<script src="reader/vendor/katex/katex.min.js"></script><script src="reader/review-render.js"></script>'
            '<script src="reader/portable-reader.js"></script></body></html>').encode('utf-8')


def reader_assets():
    assets = [(ROOT / 'static' / 'portable-reader.js', 'reader/portable-reader.js'),
              (ROOT / 'static' / 'portable-reader.css', 'reader/portable-reader.css'),
              (ROOT / 'static' / 'review-render.js', 'reader/review-render.js')]
    katex = ROOT / 'static' / 'vendor' / 'katex'
    if not all((katex / name).is_file() for name in ('katex.min.js', 'katex.min.css', 'LICENSE')) or not (katex / 'fonts').is_dir():
        fail('应用离线公式资源缺失，请重新安装完整应用后导出。', 503)
    assets += [(p, 'reader/vendor/katex/' + p.relative_to(katex).as_posix()) for p in katex.rglob('*') if p.is_file()]
    if not all(path.is_file() for path, _ in assets):
        fail('应用离线阅读器资源缺失，请重新安装完整应用后导出。', 503)
    return assets


class PortableStore:
    def __init__(self, room):
        self.room = room
        self.root = room.data / '.portable'
        _no_link(self.root)
        self.root.mkdir(exist_ok=True)
        if self.root.is_symlink() or self.root.resolve().parent != room.data.resolve():
            fail('课堂包暂存目录不安全。')
        self.lock = threading.RLock()
        for name in ('previews', 'imports', 'exports', 'uploads'):
            (self.root / name).mkdir(exist_ok=True)
            if (self.root / name).is_symlink():
                fail('课堂包暂存目录不能是链接。')

    @staticmethod
    def _key(value):
        if not isinstance(value, str) or not REQUEST_ID.fullmatch(value):
            fail('操作标识无效。')
        return value

    def _preview(self, preview_id, kind):
        if not isinstance(preview_id, str) or not ID.fullmatch(preview_id):
            fail('预检标识无效。')
        folder = self.root / 'previews' / preview_id
        try:
            value = json_value((folder / 'preview.json').read_bytes())
        except OSError:
            fail('预检记录不可用，请重新选择文件。', 404)
        if value.get('kind') != kind:
            fail('预检类型不匹配。')
        return folder, value

    def lookup_export(self, request_id, sid):
        self._key(request_id)
        path = self.root / 'exports' / f'{request_id}.json'
        if not path.exists():
            return {'found': False}
        value = json_value(path.read_bytes())
        if value['session_id'] != sid:
            return {'found': False}
        return {'found': True, **value}

    def lookup_import(self, request_id):
        self._key(request_id)
        # Classroom metadata is the durable receipt: publishing the directory
        # also publishes the receipt, even if the HTTP response is lost.
        with self.room.lock:
            item = next((s for s in self.room.sessions.values() if s.get('import_origin', {}).get('request_id') == request_id), None)
            return {'found': True, 'session_id': item['id'], 'created': False} if item else {'found': False}

    def preview_export(self, sid, include_audio=False):
        with self.lock:
            with self.room.lock:
                session = copy.deepcopy(self.room.get(sid))
                course = next((copy.deepcopy(c) for c in self.room.courses if c['id'] == session.get('course_id')), None)
            if course:
                session['course_snapshot'] = {k: course.get(k) for k in ('name', 'syllabus', 'glossary', 'terms')}
            media = media_inventory(session, self.room.data, include_audio)
            session = portable_session_projection(session, self.room.data, media=media, includes_media=True)
            body = encoded(session)
            if len(body) > MAX_SESSION_BYTES:
                fail('课堂 JSON 超过 64 MiB；未自动删减内容。', 413)
            missing = [x for x in media if x['availability'] in MISSING_STATES]
            assets = {x['archive_path']: x['size'] for x in media if x['availability'] == 'available'}
            reader = reader_assets()
            count = len(assets) + len(reader) + 4
            estimated = sum(assets.values()) + sum(p.stat().st_size for p, _ in reader) + len(body) * 3 + count * 1024
            if estimated > MAX_PACKAGE_BYTES or count > MAX_FILES:
                fail('课堂包预计超过 2 GiB 或 10,000 文件上限；请取消录音或缩小课堂范围。', 413)
            preview_id = uuid.uuid4().hex
            folder = self.root / 'previews' / preview_id
            folder.mkdir()
            (folder / 'session.json').write_bytes(body)
            preview = {'kind': 'export', 'preview_id': preview_id, 'session_id': session['id'], 'title': session['title'],
                       'include_audio': bool(include_audio), 'estimated_bytes': estimated, 'files_count': count,
                       'media': media, 'missing': missing, 'created_at': now(),
                       'warnings': ['只包含已保存到后台的资料；未上传图片和未保存草稿不在包内。'] +
                                   ([] if include_audio else ['本包未携带录音，离线无法回听。'])}
            write_json(folder / 'preview.json', preview)
            return copy.deepcopy(preview)

    def build_export(self, preview_id, *, request_id, allow_missing=False):
        with self.lock:
            self._key(request_id)
            folder, preview = self._preview(preview_id, 'export')
            existing_receipt = self.root / 'exports' / f'{request_id}.json'
            if existing_receipt.exists() and json_value(existing_receipt.read_bytes()).get('session_id') != preview['session_id']:
                fail('此导出标识已用于另一节课堂，请使用原记录或新建导出。', 409)
            prior = self.lookup_export(request_id, preview['session_id'])
            if prior.get('found'):
                if prior['preview_id'] != preview_id:
                    fail('这个操作标识已经用于另一份课堂包。', 409)
                return prior
            if preview['missing'] and not allow_missing:
                fail('预检发现缺失或损坏媒体；请明确选择带缺失说明导出。', 409)
            session = json_value((folder / 'session.json').read_bytes())
            media = copy.deepcopy(preview['media'])
            manifest = {'format': 'classroom-assistant-package', 'schema_version': PACKAGE_SCHEMA,
                        'source_session_id': session['id'], 'exported_at': now(), 'include_audio': preview['include_audio'],
                        'partial': bool(preview['missing']), 'media': media, 'files': []}
            package_id = uuid.uuid4().hex
            temporary = self.root / 'exports' / f'{package_id}.tmp'
            target = temporary.with_suffix('.zip')
            seen, total = set(), 0
            try:
                with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                    def add_bytes(name, content):
                        nonlocal total
                        safe_name(name)
                        if name in seen:
                            return
                        total += len(content)
                        if total > MAX_PACKAGE_BYTES or len(seen) + 1 > MAX_FILES:
                            fail('课堂包实际大小或文件数超过上限。', 413)
                        archive.writestr(name, content)
                        seen.add(name)
                        manifest['files'].append({'path': name, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()})
                    for item in media:
                        if item['availability'] != 'available' or item['archive_path'] in seen:
                            continue
                        original = self.room.data / session['id'] / item['original_path']
                        _no_link(original)
                        if not original.resolve().is_relative_to((self.room.data / session['id']).resolve()):
                            fail('媒体路径不在原课堂内。')
                        actual_hash, copied = hashlib.sha256(), 0
                        with original.open('rb') as source, archive.open(item['archive_path'], 'w', force_zip64=True) as output:
                            while chunk := source.read(CHUNK):
                                copied += len(chunk)
                                total += len(chunk)
                                if copied > item['size'] or total > MAX_PACKAGE_BYTES:
                                    fail('导出期间媒体变化或包大小超限，请重新预检。', 409)
                                actual_hash.update(chunk)
                                output.write(chunk)
                        if copied != item['size'] or actual_hash.hexdigest() != item['sha256']:
                            fail('导出期间媒体发生变化，请重新预检；未生成成功课堂包。', 409)
                        seen.add(item['archive_path'])
                        manifest['files'].append({'path': item['archive_path'], 'size': copied, 'sha256': item['sha256']})
                    add_bytes('session.json', encoded(session))
                    add_bytes('notes.md', portable_markdown(session, media=media).encode('utf-8'))
                    for local, name in reader_assets():
                        add_bytes(name, local.read_bytes())
                    # The reader needs media bindings, not the self-referential
                    # file manifest. It never loads session.json via fetch.
                    add_bytes('index.html', offline_html(session, {k: v for k, v in manifest.items() if k != 'files'}))
                    final_manifest = encoded(manifest)
                    if len(final_manifest) > MAX_MANIFEST_BYTES or total + len(final_manifest) > MAX_PACKAGE_BYTES or len(seen) + 1 > MAX_FILES:
                        fail('课堂包清单或总量超过上限。', 413)
                    archive.writestr('manifest.json', final_manifest)
                if temporary.stat().st_size > MAX_PACKAGE_BYTES:
                    fail('最终 ZIP 超过 2 GiB 上限。', 413)
                with temporary.open('r+b') as complete:
                    os.fsync(complete.fileno())
                with zipfile.ZipFile(temporary) as check:
                    if check.testzip() is not None:
                        fail('课堂包写入校验失败，未发布下载文件。', 507)
                os.replace(temporary, target)
                result = {'id': package_id, 'preview_id': preview_id, 'session_id': session['id'],
                          'status': 'ready', 'download_url': f'/api/packages/download/{package_id}',
                          'size': target.stat().st_size, 'sha256': digest(target), 'partial': manifest['partial']}
                write_json(self.root / 'exports' / f'{request_id}.json', result)
                return result
            except OSError:
                fail('本机写入失败或媒体在导出中消失；没有完成课堂包，请检查磁盘并重新预检。', 507)
            finally:
                temporary.unlink(missing_ok=True)

    def download_path(self, package_id):
        if not isinstance(package_id, str) or not ID.fullmatch(package_id):
            fail('课堂包标识无效。')
        path = self.root / 'exports' / f'{package_id}.zip'
        if not path.is_file() or path.is_symlink():
            fail('课堂包不可用，请重新导出。', 404)
        return path

    def preview_import(self, path, *, request_id):
        with self.lock:
            self._key(request_id)
            path = Path(path)
            if path.stat().st_size > MAX_PACKAGE_BYTES:
                fail('文件超过 2 GiB 上限。', 413)
            fingerprint = digest(path)
            receipt = self.root / 'imports' / f'preview-{request_id}.json'
            if receipt.exists():
                saved = json_value(receipt.read_bytes())
                if saved['sha256'] != fingerprint:
                    fail('同一上传标识对应的文件已变化，请新建导入操作。', 409)
                return saved['result']
            preview_id = uuid.uuid4().hex
            folder = self.root / 'previews' / preview_id
            folder.mkdir()
            try:
                if zipfile.is_zipfile(path):
                    session, manifest = self._inspect_zip(path, folder)
                    media = manifest['media']
                    format_name = 'zip'
                else:
                    if path.stat().st_size > MAX_SESSION_BYTES:
                        fail('旧 JSON 超过 64 MiB 上限。', 413)
                    session = validate_session(json_value(path.read_bytes()))
                    media = [{'kind': 'image', 'id': p.get('id'), 'version': p.get('version', 1),
                              'original_path': p['path'], 'sha256': p['sha256'], 'availability': 'missing',
                              'reason': '旧 JSON 没有携带图片，未从电脑其他目录寻找。'} for p in image_references(session)]
                    media += [{'kind': 'audio', 'id': a['seq'], 'original_path': a['path'], 'start': a['start'],
                               'duration': a['duration'], 'availability': 'deleted' if a.get('deleted') else 'missing',
                               'reason': '原录音已删除。' if a.get('deleted') else '旧 JSON 没有携带录音。'} for a in session['audio']]
                    format_name = 'json'
                (folder / 'session.json').write_bytes(encoded(session))
                result = {'kind': 'import', 'preview_id': preview_id, 'source_session': session['id'], 'title': session['title'],
                          'format': format_name, 'sha256': fingerprint, 'media': media,
                          'estimated_bytes': sum(x.get('size', 0) for x in media if x['availability'] == 'available'),
                          'missing': [x for x in media if x['availability'] in MISSING_STATES],
                          'warnings': ['恢复为独立课堂副本；不会覆盖已有课堂、恢复账号配置或启动旧任务。']}
                write_json(folder / 'preview.json', result)
                write_json(receipt, {'sha256': fingerprint, 'result': result})
                return result
            except (zipfile.BadZipFile, RuntimeError, OSError):
                fail('课堂包无法读取或写入暂存；尚未恢复任何课堂。')
            finally:
                # Failed preflights publish no receipt and retain no extracted data.
                if not (folder / 'preview.json').is_file() and folder.exists():
                    if folder.resolve().parent == (self.root / 'previews').resolve() and not folder.is_symlink():
                        shutil.rmtree(folder)

    def _inspect_zip(self, path, folder):
        zip_directory_limits(path)
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(x.file_size for x in entries) > MAX_PACKAGE_BYTES:
                fail('课堂包文件数或展开大小超过上限。', 413)
            names = set()
            for item in entries:
                if item.orig_filename != item.filename:
                    fail('课堂包包含被截断或不安全的原始文件名。')
                name = safe_name(item.filename)
                if name.casefold() in names or item.is_dir() or item.flag_bits & 1:
                    fail('课堂包包含重复文件、大小写冲突、目录条目或加密内容。')
                names.add(name.casefold())
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or mode & 0o170000 not in (0, stat.S_IFREG):
                    fail('课堂包包含链接或特殊文件。')
                if item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    fail('课堂包压缩格式不受支持。')
            try:
                if archive.getinfo('manifest.json').file_size > MAX_MANIFEST_BYTES:
                    fail('课堂包清单超过 8 MiB。', 413)
                manifest = json_value(archive.read('manifest.json'), MAX_MANIFEST_BYTES)
            except KeyError:
                fail('课堂包缺少校验清单。')
            if not isinstance(manifest, dict) or manifest.get('format') != 'classroom-assistant-package' or type(manifest.get('schema_version')) is not int or manifest.get('schema_version') != PACKAGE_SCHEMA:
                fail('课堂包格式来自未来或不兼容版本，请更新应用。')
            listing = manifest.get('files')
            if not isinstance(listing, list) or not isinstance(manifest.get('media'), list):
                fail('课堂包校验清单结构无效。')
            expected = {}
            for item in listing:
                if not isinstance(item, dict) or not HASH.fullmatch(str(item.get('sha256', ''))) or type(item.get('size')) is not int or item['size'] < 0:
                    fail('课堂包文件校验条目无效。')
                name = safe_name(item.get('path'))
                if name in expected:
                    fail('课堂包清单重复。')
                expected[name] = item
            actual_names = {x.filename for x in entries} - {'manifest.json'}
            absent = set(expected) - actual_names
            if actual_names - set(expected) or any(not name.startswith('assets/') for name in absent) or 'session.json' not in expected:
                fail('课堂包清单与实际文件不一致。')
            if expected['session.json']['size'] > MAX_SESSION_BYTES:
                fail('课堂 JSON 超过 64 MiB。', 413)
            total, file_issues = 0, {}
            for name, wanted in expected.items():
                if name in absent:
                    file_issues[name] = ('missing', '包内媒体文件缺失；原编号、文字与引用仍保留。')
                    continue
                if archive.getinfo(name).file_size != wanted['size']:
                    if name.startswith('assets/'):
                        file_issues[name] = ('corrupt', '包内媒体长度与清单不一致；不会使用损坏文件。')
                        continue
                    fail('课堂包元数据文件长度与清单不一致。')
                sha, actual = hashlib.sha256(), 0
                # Only media and session are retained. HTML/JS/CSS are hashed,
                # then discarded; the application never trusts imported code.
                keep = name == 'session.json' or name.startswith('assets/')
                destination = folder / name if keep else None
                if destination:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                output = destination.open('xb') if destination else None
                try:
                    try:
                        with archive.open(name) as source:
                            while chunk := source.read(CHUNK):
                                total += len(chunk)
                                actual += len(chunk)
                                if total > MAX_PACKAGE_BYTES or actual > wanted['size']:
                                    fail('课堂包实际解压量超限。', 413)
                                sha.update(chunk)
                                if output:
                                    output.write(chunk)
                    except (zipfile.BadZipFile, EOFError):
                        if not name.startswith('assets/'):
                            fail('课堂包元数据 ZIP 校验失败，未恢复任何课堂。')
                        file_issues[name] = ('corrupt', '包内媒体 ZIP 校验失败；不会使用损坏文件。')
                finally:
                    if output:
                        output.close()
                if actual != wanted['size'] or sha.hexdigest() != wanted['sha256']:
                    if name.startswith('assets/'):
                        file_issues[name] = ('corrupt', '包内媒体哈希校验失败；不会使用损坏文件。')
                    else:
                        fail('课堂包元数据文件校验失败，未恢复任何课堂。')
            session = validate_session(json_value((folder / 'session.json').read_bytes()))
            if manifest.get('source_session_id') != session['id']:
                fail('课堂包的来源课堂与元数据不一致。')
            registered = {('image', x['path'], x['sha256']) for x in image_references(session)}
            image_ids = {(x['path'], x['sha256']): x['id'] for x in image_references(session)}
            audio_ids = {x['path']: x['seq'] for x in session['audio']}
            audio_paths = {x['path'] for x in session['audio']}
            media_seen, claimed_assets = set(), set()
            for item in manifest['media']:
                if not isinstance(item, dict) or item.get('kind') not in ('image', 'audio') or item.get('availability') not in ('available', 'not_included', 'deleted', 'missing', 'corrupt', 'invalid'):
                    fail('媒体清单结构无效。')
                original = item.get('original_path')
                if not isinstance(original, str) or item['kind'] == 'image' and not isinstance(item.get('sha256'), str):
                    fail('媒体清单路径或校验信息无效。')
                valid = ('image', original, item.get('sha256')) in registered if item['kind'] == 'image' else original in audio_paths
                if not valid or (item['kind'], original, item.get('sha256') if item['kind'] == 'image' else None) in media_seen:
                    fail('媒体清单包含未登记或重复的素材。')
                if item.get('id') != (image_ids[(original, item['sha256'])] if item['kind'] == 'image' else audio_ids[original]):
                    fail('媒体清单编号与课堂记录不一致。')
                media_seen.add((item['kind'], original, item.get('sha256') if item['kind'] == 'image' else None))
                if not isinstance(item.get('reason', ''), str) or item.get('size') is not None and (type(item['size']) is not int or item['size'] < 0):
                    fail('媒体清单大小或缺失说明无效。')
                if item['availability'] == 'available':
                    name = item.get('archive_path')
                    if not isinstance(name, str) or not re.fullmatch(r'assets/[0-9a-f]{64}\.(?:png|jpg|wav)', name) or name not in expected or expected[name]['sha256'] != item.get('sha256') or expected[name]['size'] != item.get('size'):
                        fail('媒体文件与校验清单不符。')
                    if (item['kind'] == 'audio') != name.endswith('.wav'):
                        fail('媒体扩展名与内容类型不符。')
                    if Path(name).suffix != Path(original).suffix:
                        fail('媒体文件扩展名与原记录不一致。')
                    claimed_assets.add(name)
                    if name not in file_issues:
                        try:
                            (validate_image if item['kind'] == 'image' else validate_audio)(folder / name)
                        except HTTPException as error:
                            file_issues[name] = ('corrupt', error.detail)
                    if name in file_issues:
                        item.update(availability=file_issues[name][0], reason=file_issues[name][1])
            all_registered = registered | {('audio', path, None) for path in audio_paths}
            if media_seen != all_registered:
                fail('课堂包未明确列出所有媒体的可用或缺失状态。')
            if claimed_assets != {name for name in expected if name.startswith('assets/')}:
                fail('课堂包包含未登记的媒体文件。')
            manifest['partial'] = any(item['availability'] in MISSING_STATES for item in manifest['media'])
            return session, manifest

    def restore(self, preview_id, *, request_id, allow_missing=False):
        with self.lock:
            self._key(request_id)
            folder, preview = self._preview(preview_id, 'import')
            previous = self.lookup_import(request_id)
            if previous.get('found'):
                imported = self.room.get(previous['session_id'])
                if imported['import_origin']['package_sha256'] != preview['sha256']:
                    fail('此恢复标识已经用于其他课堂包。', 409)
                return previous
            if preview['missing'] and not allow_missing:
                fail('资料存在缺失；请明确确认保留缺失说明后恢复。', 409)
            source = validate_session(json_value((folder / 'session.json').read_bytes()))
            sid = uuid.uuid4().hex
            staging = self.root / 'imports' / sid
            staging.mkdir()
            try:
                copied_paths = set()
                for media in preview['media']:
                    if media['availability'] != 'available':
                        continue
                    original = media['original_path']
                    if original in copied_paths:
                        continue
                    copied_paths.add(original)
                    asset = folder / media['archive_path']
                    if asset.is_symlink() or digest(asset) != media['sha256']:
                        fail('预检之后暂存媒体发生变化，请重新选择文件。', 409)
                    destination = staging / original
                    destination.parent.mkdir(exist_ok=True)
                    shutil.copyfile(asset, destination)
                    if digest(destination) != media['sha256']:
                        fail('恢复媒体写入校验失败，尚未登记课堂。', 507)
                session = copy.deepcopy(source)
                session['id'] = sid
                session['title'] = source['title'][:150] + '（导入副本）'
                session['created_at'] = now()
                session['import_origin'] = {'source_session_id': source['id'], 'source_created_at': source['created_at'],
                                            'source_title': source['title'], 'imported_at': now(),
                                            'package_sha256': preview['sha256'], 'request_id': request_id}
                old_map = source.get('source_classroom_map', {})
                session['source_classroom_map'] = {str(key): sid for key in old_map if isinstance(key, str)}
                session['source_classroom_map'][source['id']] = sid
                session['portable_media'] = copy.deepcopy(preview['media'])
                session['course_id'] = None
                session['settings']['auto_analyze'] = False
                session.update(pending_ids=[], flush_requested=False, request_receipts={})
                if isinstance(session.get('review'), dict):
                    session['review']['receipts'] = {}
                for item in session['groups'] + session['questions'] + session['revisions'] + session['audio']:
                    # Runtime task state is disarmed only at known task paths;
                    # never recurse through immutable evidence or AI originals.
                    for key in ('job_id', 'confirmed_at', 'confirmed_source_token', 'duplicate_billing_confirmed'):
                        item.pop(key, None)
                    if item.get('stream_status') == 'streaming':
                        item['stream_status'] = 'interrupted'
                    if item.get('status') in ('pending', 'running', 'paused', 'waiting', 'generating', 'queued', 'prepared', 'partial'):
                        item['imported_status'] = item['status']
                        if item['status'] in ('running', 'generating') or item.get('dispatch_started_at') and not item.get('dispatch_completed_at'):
                            item['result_unknown'] = True
                        item.update(status='error', error='从课堂包恢复的历史任务未启动；请核对原结果后手动新建请求。')
                for run in session.get('review', {}).get('runs', []):
                    for key in ('confirmed_at', 'confirmed_source_token'):
                        run.pop(key, None)
                    if run.get('status') in ('pending', 'running'):
                        run.update(imported_status=run['status'], status='paused', error='导入的复习任务未启动；请核对后明确继续。')
                    for batch in run.get('batches', []):
                        batch.pop('job_id', None)
                        if batch.get('status') in ('pending', 'running'):
                            attempt = batch.get('attempts', [])[-1] if batch.get('attempts') else {}
                            unknown = bool(attempt.get('dispatch_started_at') and not attempt.get('dispatch_completed_at'))
                            batch.update(imported_status=batch['status'], status='unknown' if unknown else 'paused')
                with self.room.lock:
                    session['creation_order'] = max((creation_order(s) for s in self.room.sessions.values()), default=0) + 1
                    write_json(staging / 'session.json', session)
                    target = self.room.data / sid
                    if target.exists():
                        fail('恢复目标发生冲突，请重试。', 409)
                    os.replace(staging, target)
                    self.room.sessions[sid] = session
                return {'found': True, 'created': True, 'session_id': sid, 'title': session['title'],
                        'missing': preview['missing'], 'source_session_id': source['id']}
            except OSError:
                fail('恢复写入失败，原课堂未改动；请检查磁盘后重试。', 507)
            finally:
                # This exact UUID staging directory was created by this call;
                # it is never a classroom or a user-selected filesystem path.
                if staging.exists() and staging.resolve().parent == (self.root / 'imports').resolve() and not staging.is_symlink():
                    shutil.rmtree(staging)
