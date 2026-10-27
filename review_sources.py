"""Versioned review evidence and deterministic, non-truncating batch planning."""
from __future__ import annotations

import copy
import hashlib
import json
import math

from fastapi import HTTPException

from slide_context import MAX_SLIDES


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def frozen_segment(seg):
    fields = ('id', 'text', 'start', 'end', 'source', 'audio_seq', 'machine_text')
    value = {k: copy.deepcopy(seg[k]) for k in fields if k in seg}
    # The first correction adds machine_text to the mutable segment. An
    # observed version-0 replay must retain the same identity as before that edit.
    value.setdefault('machine_text', seg['text'])
    value['version'] = seg.get('version', 0)
    value['content_hash'] = digest(value)
    return value


def frozen_slide(slide):
    fields = ('id', 'path', 'sha256', 'mime_type', 'width', 'height', 'bytes',
              'title', 'start', 'end', 'interval_version', 'version')
    value = {k: copy.deepcopy(slide[k]) for k in fields if k in slide}
    value.setdefault('version', 1)
    value['content_hash'] = digest(value)
    return value


def evidence_subset(evidence, ids):
    ids = set(ids)
    value = {k: copy.deepcopy(v) for k, v in evidence.items()
             if k not in ('segments', 'slides', 'source_ids', 'marks', 'snapshot_hash')}
    value['segments'] = [copy.deepcopy(x) for x in evidence['segments'] if x['id'] in ids]
    value['slides'] = [copy.deepcopy(x) for x in evidence['slides'] if x['id'] in ids]
    value['source_ids'] = [x['id'] for x in value['segments'] + value['slides']]
    value['marks'] = [copy.deepcopy(x) for x in evidence.get('marks', []) if x['target_id'] in ids]
    value['snapshot_hash'] = digest(value)
    return value


def freeze_review(session, payload, course):
    scope = payload.get('scope', 'all')
    if scope not in ('all', 'marked', 'range'):
        raise HTTPException(422, '请选择整课现有材料、标记或时间范围。')
    start, end = payload.get('start'), payload.get('end')
    if scope == 'range' and (type(start) not in (int, float) or type(end) not in (int, float)
            or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start):
        raise HTTPException(422, '时间范围无效。')
    include_images = payload.get('include_images', True)
    if type(include_images) is not bool:
        raise HTTPException(422, '图片开关无效。')
    marks = [copy.deepcopy(m) for m in session.get('review', {}).get('marks', []) if m.get('active')]
    segments = [frozen_segment(x) for x in session['segments']]
    slides = [frozen_slide(x) for x in session.get('slides', []) if not x.get('removed')]
    if scope == 'marked':
        selected = {'segments': {}, 'slides': {}}
        for mark in marks:
            for key in selected:
                for source in mark['evidence'].get(key, []):
                    previous = selected[key].get(source['id'])
                    if previous and previous != source:
                        raise HTTPException(409, '同一材料的标记指向不同历史版本，请先核对并重新关联。')
                    selected[key][source['id']] = copy.deepcopy(source)
        segments, slides = list(selected['segments'].values()), list(selected['slides'].values())
    elif scope == 'range':
        segments = [x for x in segments if x['start'] < end and x['end'] > start]
        slides = [x for x in slides if x['start'] < end and (x.get('end') is None or x['end'] > start)]
    if not include_images:
        slides = []
    if not segments and not slides:
        raise HTTPException(422, '所选范围没有可整理材料，请先添加原文或课件。')
    segments.sort(key=lambda x: (x['start'], x['id']))
    slides.sort(key=lambda x: (x['start'], x['id']))
    source_ids = [x['id'] for x in segments + slides]
    selected_marks = [{k: copy.deepcopy(v) for k, v in m.items() if k != 'evidence'}
                      for m in marks if m['target_id'] in source_ids]
    times = segments + slides
    value = {'schema_version': 1, 'source_session_id': session['id'], 'scope': scope,
        'range': [start, end] if scope == 'range' else [min(x['start'] for x in times),
            max(x.get('end') if x.get('end') is not None else x['start'] for x in times)],
        'segments': segments, 'slides': slides, 'source_ids': source_ids,
        'baseline_source_ids': list(source_ids), 'marks': selected_marks,
        'course_snapshot': copy.deepcopy(course or {}), 'include_images': include_images}
    value['snapshot_hash'] = digest(value)
    return value


def plan_batches(evidence, options, context_chars):
    from review_schema import review_prompt, REVIEW_INSTRUCTIONS
    # The configured character ceiling is an application budget, not a token guarantee.
    budget = int(context_chars) - 4000 - len(REVIEW_INSTRUCTIONS)
    marked = {x['target_id'] for x in evidence.get('marks', [])}
    sources = sorted(evidence['segments'] + evidence['slides'],
                     key=lambda x: (x['id'] not in marked, x['start'], x['id']))
    batches, selected, context_ids, warnings = [], [], [], []

    def batch_evidence(ids, primary, messages=()):
        trial = evidence_subset(evidence, ids)
        trial.update(primary_source_ids=list(primary), context_source_ids=[x for x in trial['source_ids'] if x not in primary],
                     context_warnings=list(messages))
        return trial

    def fits(ids, primary=None, messages=()):
        trial = batch_evidence(ids, primary or ids, messages)
        return len(trial['slides']) <= MAX_SLIDES and len(review_prompt(trial, options)) <= budget

    def nearby(source):
        if source['id'].startswith('P'):
            candidates = [x for x in evidence['segments'] if x['end'] > source['start'] and
                          (source.get('end') is None or x['start'] < source['end'])]
            return [x['id'] for x in sorted(candidates, key=lambda x: abs(x['start'] - source['start']))[:2]]
        candidates = [x for x in evidence['slides'] if x['start'] < source['end'] and
                      (x.get('end') is None or x['end'] > source['start'])]
        return [x['id'] for x in sorted(candidates, key=lambda x: abs(x['start'] - source['start']))[:1]]

    def combined(primary, context):
        return list(dict.fromkeys(primary + context))

    def append(ids, context, messages):
        full = combined(ids, context)
        batches.append({'id': f'B{len(batches) + 1:04d}', 'status': 'prepared',
                        'evidence': batch_evidence(full, ids, messages), 'attempts': [],
                        'primary_source_ids': list(ids),
                        'context_source_ids': [x for x in full if x not in ids], 'context_warnings': list(messages)})

    for source in sources:
        adjacent = nearby(source)
        unit_warnings = []
        while adjacent and not fits([source['id']] + adjacent, [source['id']], unit_warnings):
            removed = adjacent.pop()
            unit_warnings.append(f'邻近材料 {removed} 超出本批预算；仍在其主批次完整处理，当前图文关联可能不完整。')
        trial = selected + [source['id']]
        extra = context_ids + adjacent
        if fits(combined(trial, extra), trial, warnings + unit_warnings):
            selected = trial
            context_ids = extra
            warnings.extend(unit_warnings)
            continue
        if selected:
            append(selected, context_ids, warnings)
        selected = [source['id']]
        context_ids, warnings = adjacent, unit_warnings
        if not fits(combined(selected, context_ids), selected, warnings):
            raise HTTPException(422, f"材料 {source['id']} 超出当前应用输入预算，请缩短单段材料或调整预算；尚未发送。")
    if selected:
        append(selected, context_ids, warnings)
    return batches
