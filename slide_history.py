"""Immutable slide revisions and read-only comparisons with frozen evidence.

Callers hold the classroom lock and persist/roll back session changes. No helper
rewrites historical question evidence or deletes a published image asset.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import re
from datetime import datetime

from PIL import Image

from slide_context import SlideContextError, _image_bytes, _write_image_asset, resolve_slide_path


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _current(session, slide_id):
    slide = next((item for item in session.get('slides', []) if item.get('id') == slide_id), None)
    if slide is None:
        raise SlideContextError('找不到本课堂的这张课件。')
    return slide


def _version_copy(slide):
    result = copy.deepcopy({key: value for key, value in slide.items() if key != 'versions'})
    result.setdefault('version', 1)
    return result


def find_slide_version(session, slide_id, *, version=None, sha256=None):
    """Return an exact owned asset, including a removed or historical version.

    No version/hash selector means current. An unavailable requested version
    raises instead of silently resolving to the current image.
    """
    if version is not None and (type(version) is not int or version < 1):
        raise SlideContextError('课件版本必须是正整数。')
    if sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256)):
        raise SlideContextError('课件版本校验信息无效。')
    current = _current(session, slide_id)
    candidates = [current, *current.get('versions', [])]
    for candidate in candidates:
        if (candidate.get('id') == slide_id
                and (version is None or candidate.get('version', 1) == version)
                and (sha256 is None or candidate.get('sha256') == sha256)):
            return _version_copy(candidate)
    raise SlideContextError('找不到所选课件历史版本；不会改用当前图片。')


def rotate_slide(data, session, slide_id, direction):
    """Create one revision for a clockwise right-angle turn; zero is a no-op."""
    if direction == 'left':
        degrees = 270
    elif direction == 'right':
        degrees = 90
    elif type(direction) is int and direction in (0, 90, 180, 270):
        degrees = direction
    else:
        raise SlideContextError('请选择向左或向右 90 度旋转，或 0、90、180、270 度。')
    current = _current(session, slide_id)
    if current.get('removed'):
        raise SlideContextError('这张课件已移除，不能再旋转；历史副本仍保留。')
    path = resolve_slide_path(data, session['id'], current)
    if not degrees:
        return _version_copy(current)
    try:
        with Image.open(path) as image:
            image.load()
            rotated = image.transpose({90: Image.Transpose.ROTATE_270,
                                       180: Image.Transpose.ROTATE_180,
                                       270: Image.Transpose.ROTATE_90}[degrees])
            # PNG preserves the already decoded pixels, including JPEG inputs;
            # rotation does not add a second lossy JPEG compression step.
            output = io.BytesIO()
            rotated.save(output, format='PNG')
        encoded, extension, mime_type, width, height = _image_bytes(output.getvalue())
    except SlideContextError:
        raise
    except (OSError, ValueError, SyntaxError):
        raise SlideContextError('这张课件无法解码，不能旋转；原文件和旧证据未改变。') from None
    relative = _write_image_asset(data, session['id'], encoded, extension)
    previous = _version_copy(current)
    replacement = {**previous, 'version': previous['version'] + 1,
                   'path': relative, 'mime_type': mime_type,
                   'sha256': hashlib.sha256(encoded).hexdigest(), 'width': width, 'height': height,
                   'rotation_degrees': (current.get('rotation_degrees', 0) + degrees) % 360,
                   'rotated_at': _now()}
    versions = [*copy.deepcopy(current.get('versions', [])), previous]
    current.clear()
    current.update(replacement, versions=versions)
    return _version_copy(current)


def remove_slide(session, slide_id):
    """Stop selecting this logical slide, retaining every historical asset."""
    current = _current(session, slide_id)
    if not current.get('removed'):
        current.update(removed=True, removed_at=_now())
    return _version_copy(current)


def source_status(data, session, evidence):
    """Project source changes and each frozen image's availability, without writes.

    The token describes only sources in this receipt. Normal transcript growth
    and an automatic closure of an open slide interval do not invalidate it.
    """
    changes, slides, segments, signatures = [], [], [], []
    by_slide = {item['id']: item for item in session.get('slides', [])}
    by_segment = {item['id']: item for item in session.get('segments', [])}

    def change(kind, identifier, message):
        changes.append({'kind': kind, 'id': identifier, 'message': message})

    for frozen in evidence.get('segments', []):
        identifier = frozen['id']
        current = by_segment.get(identifier)
        missing = current is None
        changed = missing or any(current.get(key, 0 if key == 'version' else None)
                                 != frozen.get(key, 0 if key == 'version' else None)
                                 for key in ('text', 'start', 'end', 'version'))
        if changed:
            change('text', identifier, f'原文 {identifier} 已不可用；本次仍保留原文快照。' if missing
                   else f'原文 {identifier} 已修改；本次仍使用修改前的快照。')
        segments.append({'id': identifier, 'changed': changed, 'missing': missing})
        signatures.append(['text', identifier, None if missing else
                           [current.get(key, 0 if key == 'version' else None)
                            for key in ('text', 'start', 'end', 'version')]])

    for frozen in evidence.get('slides', []):
        identifier = frozen['id']
        current = by_slide.get(identifier)
        removed = current is None or bool(current.get('removed'))
        rotated = current is not None and (current.get('version', 1) != frozen.get('version', 1)
                                           or current.get('sha256') != frozen.get('sha256'))
        time_changed = title_changed = False
        if current is not None:
            manual_revision = current.get('interval_version', 0) != frozen.get('interval_version', 0)
            # Adding the next page closes the preceding open interval normally.
            # Explicit edits increment interval_version, including an open end.
            natural_close = (current.get('end') is not None
                             and (current.get('auto_closed_end') == current.get('end')
                                  and current.get('auto_closed_interval_version') == frozen.get('interval_version', 0)
                                  and frozen.get('end') in current.get('auto_end_history', [])
                                  or frozen.get('end') is None and 'interval_version' not in current and 'auto_closed_end' not in current))
            time_changed = (manual_revision or current.get('start') != frozen.get('start')
                            or current.get('end') != frozen.get('end') and not natural_close)
            title_changed = current.get('title', '') != frozen.get('title', '')
        start_index = len(changes)
        if removed:
            change('removal', identifier, f'课件 {identifier} 已从当前选择移除；本次历史副本仍保留。')
        if rotated:
            change('rotation', identifier, f'课件 {identifier} 已旋转为新版本；本次仍使用原图片版本。')
        if time_changed:
            change('time', identifier, f'课件 {identifier} 的关联时间已修改；本次仍使用原时间范围。')
        if title_changed:
            change('title', identifier, f'课件 {identifier} 的名称已修改；本次仍显示原名称。')
        availability, file_message = 'available', ''
        try:
            resolve_slide_path(data, session['id'], frozen, force=False)
        except SlideContextError as error:
            availability = error.code if error.code in ('missing', 'corrupt') else 'invalid'
            file_message = str(error)
        item_messages = [item['message'] for item in changes[start_index:]]
        if file_message:
            item_messages.append(file_message)
        slides.append({'id': identifier, 'version': frozen.get('version', 1),
                       'sha256': frozen.get('sha256'), 'available': availability == 'available',
                       'availability': availability, 'removed': removed, 'rotated': rotated,
                       'time_changed': time_changed, 'title_changed': title_changed,
                       'message': ' '.join(item_messages)})
        signatures.append(['slide', identifier, frozen.get('version', 1), frozen.get('sha256'),
                           frozen.get('path'), availability, removed,
                           None if current is None else [current.get('version', 1), current.get('sha256'),
                               current.get('title', ''), current.get('interval_version', 0),
                               current.get('start'), current.get('end') if time_changed else frozen.get('end')]])

    messages = [item['message'] for item in changes]
    messages.extend(item['message'] for item in slides if not item['available'])
    token = hashlib.sha256(json.dumps(signatures, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'token': token, 'changed': bool(changes), 'unavailable': any(not item['available'] for item in slides),
            'messages': list(dict.fromkeys(messages)), 'changes': changes, 'slides': slides, 'segments': segments}
