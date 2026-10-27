"""Local review records, explicit batch consent and one-at-a-time AI scheduling."""
from __future__ import annotations

import copy
import math
import re
import threading
import time
import uuid

from fastapi import HTTPException

from ai_settings import record_usage
from classroom_features import write_json
from codex_bridge import CodexBridgeError
from review_sources import digest, evidence_subset, freeze_review, frozen_segment, frozen_slide, plan_batches
from slide_context import SlideContextError, resolve_slide_path
from slide_history import source_status
from study_workflow import now, UNKNOWN_RESULT, UNKNOWN_TIMEOUT


CARD_TYPES = ('concept', 'derivation', 'example', 'pitfall')
TERMINAL = ('done', 'cancelled')


def empty_review():
    return {'schema_version': 1, 'marks': [], 'runs': [], 'cards': [], 'notes': [],
            'selected_note_id': None, 'selection_revision': 0, 'receipts': {}}


def restore_in_place(target, snapshot):
    """Rollback local mutations without detaching records held by the AI worker."""
    for key in list(target):
        if key not in snapshot:
            del target[key]
    for key, value in snapshot.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            restore_in_place(existing, value)
        elif isinstance(existing, list) and isinstance(value, list) and all(isinstance(x, dict) and 'id' in x for x in value):
            by_id = {x['id']: x for x in existing if isinstance(x, dict) and 'id' in x}
            restored = []
            for record in value:
                item = by_id.get(record['id'])
                if item is None:
                    item = copy.deepcopy(record)
                else:
                    restore_in_place(item, record)
                restored.append(item)
            existing[:] = restored
        else:
            target[key] = copy.deepcopy(value)


def validate_saved_review(review):
    """Validate only the review block; damaged optional data cannot hide a class."""
    if not isinstance(review, dict) or type(review.get('schema_version')) is not int or review['schema_version'] != 1:
        raise ValueError('unsupported review schema')
    def evidence(value):
        if not isinstance(value, dict):
            raise ValueError('invalid evidence')
        if not isinstance(value.get('source_session_id'), str):
            raise ValueError('invalid source classroom')
        for key in ('segments', 'slides'):
            if not isinstance(value.get(key), list):
                raise ValueError('invalid evidence sources')
            for item in value[key]:
                if not isinstance(item, dict) or not isinstance(item.get('id'), str):
                    raise ValueError('invalid evidence source')
                for field in ('start', 'end'):
                    number = item.get(field)
                    if field == 'end' and key == 'slides' and number is None:
                        continue
                    if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                        raise ValueError('invalid evidence time')
                if key == 'segments' and not isinstance(item.get('text'), str):
                    raise ValueError('invalid evidence text')
        if not isinstance(value.get('source_ids'), list) or any(not isinstance(x, str) for x in value['source_ids']):
            raise ValueError('invalid source identity')
    for key in ('marks', 'runs', 'cards', 'notes'):
        values = review.get(key, [])
        if not isinstance(values, list):
            raise ValueError('invalid collection')
        seen = set()
        for item in values:
            if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item['id'] in seen:
                raise ValueError('invalid review item')
            seen.add(item['id'])
            evidence(item.get('evidence'))
            if key == 'runs':
                for field in ('status', 'fingerprint'):
                    if not isinstance(item.get(field), str):
                        raise ValueError('invalid run')
                if not isinstance(item.get('batches'), list) or not isinstance(item.get('options'), dict) or not isinstance(item.get('coverage'), dict):
                    raise ValueError('invalid run plan')
                for batch in item['batches']:
                    if not isinstance(batch, dict) or not isinstance(batch.get('id'), str) or not isinstance(batch.get('status'), str):
                        raise ValueError('invalid batch')
                    evidence(batch.get('evidence'))
                    if not isinstance(batch.get('attempts'), list) or any(not isinstance(a, dict) for a in batch['attempts']):
                        raise ValueError('invalid attempts')
                    if any(not isinstance(a.get('id'), str) or not isinstance(a.get('status'), str) for a in batch['attempts']):
                        raise ValueError('invalid attempt identity')
                    if batch['status'] == 'done' and (not batch['attempts'] or not isinstance(batch['attempts'][-1].get('validated_output'), dict)):
                        raise ValueError('completed batch missing result')
            elif key in ('cards', 'notes'):
                if not isinstance(item.get('ai_content'), dict) or not isinstance(item.get('user_draft'), dict):
                    raise ValueError('invalid artifact')
                draft = item['user_draft']
                if type(draft.get('revision')) is not int or draft['revision'] < 0 or not isinstance(draft.get('content'), str):
                    raise ValueError('invalid draft')
                if not isinstance(draft.get('personal_note'), str):
                    raise ValueError('invalid personal note')
                content = item['ai_content']
                if key == 'cards' and any(not isinstance(content.get(field), str) for field in ('title', 'front', 'back')):
                    raise ValueError('invalid card content')
                if key == 'notes' and (not isinstance(content.get('sections'), list) or any(
                        not isinstance(x, dict) or any(not isinstance(x.get(field), str) for field in ('section', 'title', 'body'))
                        for x in content['sections'])):
                    raise ValueError('invalid note sections')
            else:
                if item.get('target_kind') not in ('segment', 'slide') or item.get('kind') not in ('important', 'unclear') or not isinstance(item.get('target_id'), str):
                    raise ValueError('invalid mark')
    if not isinstance(review.get('receipts', {}), dict):
        raise ValueError('invalid receipts')
    if any(not isinstance(r, dict) or any(not isinstance(r.get(k), str) for k in ('kind', 'fingerprint', 'item_id'))
           for r in review.get('receipts', {}).values()):
        raise ValueError('invalid request receipt')


class ReviewWorkflow:
    def study_busy(self):
        return super().study_busy() or any(j['kind'] == 'review' and j['status'] in ('pending', 'running')
                                          for j in self.jobs.values())

    @staticmethod
    def review_data(session):
        if session.get('review_error'):
            raise HTTPException(409, session['review_error'])
        return session.setdefault('review', empty_review())

    def review_restore(self, session):
        if 'review' not in session:
            return
        review = session['review']
        try:
            validate_saved_review(review)
        except (ValueError, TypeError, KeyError):
            session['review_error'] = '复习数据损坏或来自不兼容的新版本，原始数据已保留；课堂原文仍可阅读，当前不修改复习记录。'
            self.warnings.append(f"课堂 {session['id']} 的复习数据无法安全加载；原文与原文件保留。")
            return
        session.pop('review_error', None)
        for key, value in empty_review().items():
            review.setdefault(key, value)
        for run in review['runs']:
            run.pop('confirmed_at', None)
            for batch in run['batches']:
                batch.pop('job_id', None)
                attempt = batch['attempts'][-1] if batch.get('attempts') else None
                if batch['status'] in ('pending', 'running'):
                    unknown = bool(attempt and attempt.get('dispatch_started_at') and not attempt.get('dispatch_completed_at'))
                    batch['status'] = 'unknown' if unknown else 'paused'
                    if attempt:
                        attempt.update(status=batch['status'], result_unknown=unknown)
                    run.update(status=batch['status'], error=UNKNOWN_RESULT if unknown else '上次运行中断，请核对计划后继续。')
            if run['status'] in ('pending', 'running'):
                run.update(status='paused', error='上次运行中断，请核对计划后继续。')

    def review_require(self, payload, kind):
        if not isinstance(payload.get('session_id'), str) or not payload['session_id'] or not payload.get('request_id'):
            raise HTTPException(422, '复习操作需要明确课堂和请求标识。')
        self.request_key(payload['request_id'])
        session = self.get(payload['session_id'])
        review = self.review_data(session)
        try:
            fingerprint = digest({k: v for k, v in payload.items() if k != 'request_id'})
        except (TypeError, ValueError):
            raise HTTPException(422, '请求材料包含无效数值或格式。') from None
        previous = review['receipts'].get(payload['request_id'])
        if previous and (previous['kind'] != kind or previous['fingerprint'] != fingerprint):
            raise HTTPException(409, '同一请求标识已用于不同复习操作，请核对原请求。')
        return session, review, fingerprint, previous

    @staticmethod
    def review_record(review, payload, kind, fingerprint, item_id):
        review['receipts'][payload['request_id']] = {'kind': kind, 'fingerprint': fingerprint,
            'item_id': item_id, 'created_at': now()}

    def review_save_or_rollback(self, session, before):
        try:
            self.save(session)
        except OSError:
            restore_in_place(session['review'], before)
            raise HTTPException(507, '本机复习数据保存失败，未发送新请求；请保留输入后重试。') from None

    @staticmethod
    def review_run(session, rid):
        if session.get('review_error'):
            raise HTTPException(409, session['review_error'])
        value = next((x for x in session.get('review', {}).get('runs', []) if x['id'] == rid), None)
        if value is None:
            raise HTTPException(404, '找不到这次复习任务。')
        return value

    def review_source_status(self, session, evidence):
        origin = evidence.get('source_session_id', session['id'])
        mapped = session.get('source_classroom_map', {}).get(origin)
        if origin != session['id'] and mapped != session['id']:
            return {'token': 'invalid-classroom', 'changed': True, 'unavailable': True,
                    'new_material': False, 'messages': ['来源不属于当前课堂副本。'], 'audio': [], 'slides': [], 'segments': []}
        status = source_status(self.data, session, evidence)
        ids = set(evidence.get('baseline_source_ids', evidence.get('source_ids', [])))
        current = session['segments'] + ([x for x in session.get('slides', []) if not x.get('removed')]
                                        if evidence.get('include_images', True) else [])
        if evidence.get('scope') == 'range':
            start, end = evidence['range']
            current = [x for x in current if x['start'] < end and (x.get('end') is None or x['end'] > start)]
        status['new_material'] = evidence.get('scope') != 'marked' and any(x['id'] not in ids for x in current)
        audio_by_seq = {x['seq']: x for x in session.get('audio', [])}
        status['audio'] = []
        for segment in evidence.get('segments', []):
            seq = segment.get('audio_seq')
            audio = audio_by_seq.get(seq)
            reason, available = '这段原文没有原音定位。', False
            if audio:
                reason = '录音已删除。' if audio.get('deleted') or session.get('audio_deleted_at') else '录音文件缺失或不可用。'
                try:
                    path = self.registered_audio_path(session, audio)
                    available = path.is_file()
                except (HTTPException, OSError):
                    pass
                if available:
                    reason = ''
            status['audio'].append({'source_id': segment['id'], 'seq': seq, 'available': available,
                'reason': reason, 'offset': min(max(0, segment['start'] - audio['start']), audio['duration']) if audio else 0,
                'url': f"/api/audio/{seq}?session_id={session['id']}" if available else None})
        return status

    def review_run_public(self, session, run):
        value = copy.deepcopy(run)
        value['source_status'] = self.review_source_status(session, run['evidence'])
        value['destination'] = self.study_destination() if run['status'] not in ('pending', 'running', 'done') else value['destination']
        value['repeat_billing_required'] = bool(run.get('duplicate_billing_risk') or any(
            b['status'] == 'unknown' or any(a.get('result_unknown') for a in b['attempts']) for b in run['batches']))
        value['coverage']['completed_batches'] = sum(b['status'] == 'done' for b in run['batches'])
        return value

    def review_public(self, session):
        if session.get('review_error'):
            return {**empty_review(), 'error': session['review_error'], 'read_only': True,
                    'destination': self.study_destination()}
        result = copy.deepcopy(session.get('review') or empty_review())
        result['runs'] = [self.review_run_public(session, r) for r in result['runs']]
        for collection in ('marks', 'cards', 'notes'):
            for item in result[collection]:
                item['source_status'] = self.review_source_status(session, item['evidence'])
        result['destination'] = self.study_destination()
        return result

    def review_lookup(self, sid, request_id):
        with self.lock:
            session = self.get(sid)
            if session.get('review_error'):
                raise HTTPException(409, session['review_error'])
            receipt = session.get('review', {}).get('receipts', {}).get(request_id)
            if not receipt:
                raise HTTPException(404, '找不到此复习请求，可保留相同请求标识重新核对。')
            result = copy.deepcopy(receipt)
            if receipt['kind'] in ('prepare', 'send', 'continue', 'cancel'):
                result['run'] = self.review_run_public(session, self.review_run(session, receipt['item_id']))
            elif receipt['kind'] == 'mark':
                result['mark'] = next(x for x in self.review_public(session)['marks'] if x['id'] == receipt['item_id'])
            elif receipt['kind'].startswith('edit-'):
                result['artifact'] = next(x for key in ('cards', 'notes') for x in session['review'][key]
                                          if x['id'] == receipt['item_id'])
            return result

    def set_review_mark(self, payload):
        with self.lock:
            session, review, fingerprint, receipt = self.review_require(payload, 'mark')
            if receipt:
                return next(x for x in self.review_public(session)['marks'] if x['id'] == receipt['item_id'])
            target_kind, target_id, kind = payload.get('target_kind'), payload.get('target_id'), payload.get('kind')
            if target_kind not in ('segment', 'slide') or kind not in ('important', 'unclear'):
                raise HTTPException(422, '标记类型或材料类型无效。')
            note = payload.get('note', '')
            if not isinstance(note, str) or len(note) > 2000:
                raise HTTPException(422, '标记备注最多 2000 字符。')
            for key in ('active', 'resolved', 'rebind'):
                if key in payload and type(payload[key]) is not bool:
                    raise HTTPException(422, '标记状态无效。')
            mark = next((m for m in review['marks'] if (m['target_kind'], m['target_id'], m['kind']) ==
                         (target_kind, target_id, kind)), None)
            if 'revision' in payload and (type(payload['revision']) is not int or
                    payload['revision'] != (mark.get('revision', 0) if mark else 0)):
                raise HTTPException(409, '此标记已在其他页面修改，请保留备注并核对最新版本。')
            before = copy.deepcopy(review)
            if mark is None or payload.get('rebind'):
                key = 'segments' if target_kind == 'segment' else 'slides'
                source = next((x for x in session.get(key, []) if x['id'] == target_id and not x.get('removed')), None)
                if source is None:
                    raise HTTPException(404, '找不到当前课堂的这份材料。')
                frozen = frozen_segment(source) if target_kind == 'segment' else frozen_slide(source)
                evidence = {'schema_version': 1, 'source_session_id': session['id'], 'scope': 'marked',
                    'segments': [frozen] if key == 'segments' else [], 'slides': [frozen] if key == 'slides' else [],
                    'source_ids': [target_id], 'range': [source['start'], source.get('end')], 'marks': []}
                if mark is None:
                    mark = {'id': 'M' + uuid.uuid4().hex, 'target_kind': target_kind, 'target_id': target_id,
                            'kind': kind, 'created_at': now(), 'history': []}
                    review['marks'].append(mark)
                else:
                    mark['history'].append(copy.deepcopy(mark['evidence']))
                mark['evidence'] = evidence
            mark.update(active=payload.get('active', True), resolved=payload.get('resolved', mark.get('resolved', False)),
                        note=note, updated_at=now(), revision=mark.get('revision', 0) + 1)
            self.review_record(review, payload, 'mark', fingerprint, mark['id'])
            self.review_save_or_rollback(session, before)
            return next(x for x in self.review_public(session)['marks'] if x['id'] == mark['id'])

    def prepare_review(self, payload):
        with self.lock:
            session, review, fingerprint, receipt = self.review_require(payload, 'prepare')
            if receipt:
                return self.review_run_public(session, self.review_run(session, receipt['item_id']))
            if session.get('demo'):
                raise HTTPException(400, '演示课堂不调用 AI，请新建课堂。')
            types = payload.get('card_types', list(CARD_TYPES))
            if not isinstance(types, list) or not types or any(x not in CARD_TYPES for x in types):
                raise HTTPException(422, '请选择有效卡片类型。')
            english = payload.get('english_answers', False)
            if type(english) is not bool:
                raise HTTPException(422, '英文表述开关无效。')
            options = {'card_types': sorted(set(types)), 'english': english}
            course = session.get('course_snapshot') or next((c for c in self.courses if c['id'] == session.get('course_id')), {})
            course = {k: copy.deepcopy(course[k]) for k in ('id', 'name', 'syllabus', 'terms', 'glossary') if k in course}
            observed = self.review_observed_session(session, payload)
            evidence = freeze_review(observed, payload, course)
            destination = self.study_destination()
            routing = {k: destination[k] for k in ('provider', 'base_url', 'model')}
            identity = digest({'evidence': evidence, 'options': options, 'destination': routing,
                               'prompt_version': 1, 'schema_version': 1, 'planner_version': 1,
                               'regenerate_of': payload.get('regenerate_of')})
            previous = next((r for r in review['runs'] if r['fingerprint'] == identity), None)
            before = copy.deepcopy(review)
            if previous:
                self.review_record(review, payload, 'prepare', fingerprint, previous['id'])
                self.review_save_or_rollback(session, before)
                return self.review_run_public(session, previous)
            parent = self.review_run(session, payload['regenerate_of']) if payload.get('regenerate_of') else None
            chars = getattr(self.bridge, 'context_chars', 48000) if destination['provider'] == 'codex' else int(self.connection.values.get('context_chars', 48000))
            batches = plan_batches(evidence, options, chars)
            status = self.review_source_status(session, evidence)
            excluded = [x['message'] for x in status.get('slides', []) if not x['available']]
            run = {'id': 'R' + uuid.uuid4().hex, 'status': 'prepared', 'created_at': now(), 'fingerprint': identity,
                'evidence': evidence, 'options': options, 'destination': destination, 'batches': batches,
                'card_ids': [], 'note_id': None, 'parent_generation_id': payload.get('regenerate_of'),
                'duplicate_billing_risk': bool(parent and self.review_run_public(session, parent)['repeat_billing_required']),
                'coverage': {'segment_count': len(evidence['segments']), 'slide_count': len(evidence['slides']),
                    'batch_count': len(batches), 'completed_batches': 0, 'range': evidence['range'],
                    'pending_audio': sum(a.get('status') in ('pending', 'running') for a in session['audio']),
                    'excluded': excluded, 'claim': '截至生成时已收录材料；提交覆盖不保证知识点语义完整。'}}
            review['runs'].append(run)
            self.review_record(review, payload, 'prepare', fingerprint, run['id'])
            self.review_save_or_rollback(session, before)
            return self.review_run_public(session, run)

    def review_observed_session(self, session, payload):
        for key in ('seen_segments', 'seen_slides', 'seen_marks'):
            if key in payload and (not isinstance(payload[key], list) or len(payload[key]) > 100000 or
                    any(not isinstance(x, dict) or not isinstance(x.get('id'), str) for x in payload[key])):
                raise HTTPException(422, '点击时的材料列表格式无效。')
        for ref in payload.get('seen_slides', []):
            for key in ('version', 'interval_version'):
                if key in ref and (type(ref[key]) is not int or ref[key] < 0):
                    raise HTTPException(422, '点击时的课件版本无效。')
            if 'title' in ref and (not isinstance(ref['title'], str) or len(ref['title']) > 160):
                raise HTTPException(422, '点击时的课件标题无效。')
            for key in ('start', 'end'):
                if key in ref and ref[key] is not None and (type(ref[key]) not in (int, float) or
                        not math.isfinite(ref[key]) or ref[key] < 0):
                    raise HTTPException(422, '点击时的课件时间无效。')
        observed = self.study_observed_session(session, payload)
        if 'seen_marks' in payload:
            current = {x['id']: x for x in session.get('review', {}).get('marks', [])}
            chosen, seen = [], set()
            for ref in payload['seen_marks']:
                mark = current.get(ref['id'])
                if ref['id'] in seen or mark is None or type(ref.get('revision')) is not int or ref['revision'] != mark.get('revision', 0):
                    raise HTTPException(409, '点击后的标记已修改，请核对后重新取材。')
                chosen.append(copy.deepcopy(mark))
                seen.add(ref['id'])
            observed['review'] = {**session.get('review', {}), 'marks': chosen}
        return observed

    def review_gate(self, session, run):
        if run.get('destination', {}).get('token') != self.study_route or not run.get('confirmed_at'):
            return '接入已变化，请重新确认完整计划和目的地。'
        status = self.review_source_status(session, run['evidence'])
        if status['unavailable']:
            return '计划中的图片缺失或损坏，请核对材料或新建纯文字计划。'
        if status['token'] != run.get('confirmed_source_token'):
            return '原材料已修改，请核对并明确继续旧快照，或新建计划。'
        return None

    def send_review(self, rid, payload, continuing=False):
        with self.lock:
            for field in ('confirm_plan', 'confirm_original', 'confirm_duplicate'):
                if field in payload and type(payload[field]) is not bool:
                    raise HTTPException(422, '发送确认必须为明确的布尔值。')
            kind = 'continue' if continuing else 'send'
            session, review, fingerprint, receipt = self.review_require(payload, kind)
            run = self.review_run(session, rid)
            if receipt:
                if receipt['item_id'] != rid:
                    raise HTTPException(409, '请求标识已关联另一复习任务。')
                return self.review_run_public(session, run)
            if run['status'] in ('pending', 'running', 'done'):
                before = copy.deepcopy(review)
                self.review_record(review, payload, kind, fingerprint, rid)
                self.review_save_or_rollback(session, before)
                return self.review_run_public(session, run)
            if run['status'] == 'cancelled':
                raise HTTPException(409, '已取消的任务不能继续；可明确新建生成版本。')
            if not continuing and run['status'] != 'prepared':
                raise HTTPException(409, '任务已中断，请核对后使用继续。')
            if payload.get('confirm_plan') is not True or payload.get('destination_token') != self.study_route:
                raise HTTPException(409, '请确认完整批次计划和当前服务商/模型。')
            source = self.review_source_status(session, run['evidence'])
            if source['unavailable']:
                raise HTTPException(422, '计划中的图片缺失或损坏，不能发送。')
            if source['token'] != payload.get('source_token') or source['changed'] and payload.get('confirm_original') is not True:
                raise HTTPException(409, '材料依据已变化，请查看后选择继续旧快照或重新取材。')
            if self.review_run_public(session, run)['repeat_billing_required'] and payload.get('confirm_duplicate') is not True:
                raise HTTPException(409, '之前的批次结果未知，可能已计费；再次请求须确认重复计费风险。')
            if self.active_ai and self.ai_switch_pending:
                raise HTTPException(409, '正在切换接入，请等待当前调用结束。')
            for slide in run['evidence']['slides']:
                try:
                    resolve_slide_path(self.data, session['id'], slide)
                except SlideContextError as exc:
                    raise HTTPException(422, str(exc)) from None
            before = copy.deepcopy(review)
            run.update(destination=self.study_destination(), confirmed_at=now(), confirmed_source_token=source['token'],
                       status='pending')
            run.pop('error', None)
            # A returned response surviving a disk failure/restart is work to
            # validate and save locally, never permission for another paid call.
            local_result_error = self.review_recover_returned(session, run)
            self.review_record(review, payload, kind, fingerprint, rid)
            self.review_save_or_rollback(session, before)
            if local_result_error or run['status'] == 'done':
                return self.review_run_public(session, run)
            # Resume the worker, never unrelated pending requests or old review batches.
            if self.ai['paused']:
                resumed = {**self.ai, 'paused': False, 'kind': None, 'reason': None}
                try:
                    write_json(self.data / 'ai-state.json', resumed)
                except OSError:
                    run.update(status='paused', error='本机存储失败，尚未发送。')
                    raise HTTPException(507, run['error']) from None
                self.ai.update(resumed)
                self.ai_ready.set()
            self.review_schedule_next(session, run)
            return self.review_run_public(session, run)

    def review_recover_returned(self, session, run):
        from review_schema import validate_review_output, ReviewValidationError
        for batch in run['batches']:
            if batch['status'] == 'done':
                continue
            attempt = batch['attempts'][-1] if batch['attempts'] else None
            if not attempt or 'raw_ai_output' not in attempt or attempt.get('error_kind') == 'validation':
                break
            try:
                validated = validate_review_output(attempt['raw_ai_output'], batch['evidence'], run['options'])
            except ReviewValidationError as exc:
                attempt.update(status='error', error_kind='validation', error=str(exc))
                batch['status'] = 'error'
                run.update(status='partial' if any(b['status'] == 'done' for b in run['batches']) else 'error', error=str(exc))
                return True
            attempt.update(validated_output=validated, status='done', result_unknown=False, dispatch_state='complete')
            attempt.pop('error_kind', None)
            batch['status'] = 'done'
            self.review_publish_batch(session, run, batch, validated)
        return False

    def review_schedule_next(self, session, run):
        if run['status'] not in ('pending', 'running') or self.ai['paused']:
            return
        batch = next((b for b in run['batches'] if b['status'] != 'done'), None)
        if batch is None:
            return
        if batch.get('job_id') in self.jobs and self.jobs[batch['job_id']]['status'] in ('pending', 'running'):
            return
        job = self.job(session['id'], 'review')
        attempt = {'id': uuid.uuid4().hex, 'status': 'pending', 'created_at': now(),
                   'destination': copy.deepcopy(run['destination']), 'dispatch_state': 'not_sent', 'result_unknown': False}
        batch.update(status='pending', job_id=job['id'])
        batch['attempts'].append(attempt)
        try:
            self.save(session)
        except OSError:
            batch['attempts'].pop()
            batch.update(status='paused')
            job['status'] = 'cancelled'
            run.update(status='paused', error='本机保存失败，批次尚未发送。')
            self._pause_ai_storage()
            return
        self.order += 1
        self.ai_queue.put((15, self.order, session['id'], run['id'], 'review', job['id']))

    def cancel_review(self, rid, payload):
        with self.lock:
            session, review, fingerprint, receipt = self.review_require(payload, 'cancel')
            run = self.review_run(session, rid)
            if receipt:
                if receipt['item_id'] != rid:
                    raise HTTPException(409, '请求标识已关联另一任务。')
                return self.review_run_public(session, run)
            before = copy.deepcopy(review)
            if run['status'] == 'done':
                self.review_record(review, payload, 'cancel', fingerprint, rid)
                self.review_save_or_rollback(session, before)
                return self.review_run_public(session, run)
            run.update(status='cancelled', cancelled_at=now())
            for batch in run['batches']:
                if batch['status'] in ('pending', 'running'):
                    if batch['status'] == 'running' and self.active_cancel:
                        self.active_cancel.set()
                    if batch['status'] == 'running' and batch['attempts'][-1].get('dispatch_started_at'):
                        batch['attempts'][-1].update(result_unknown=True, dispatch_state='unknown')
                        batch['status'] = 'unknown'
                    else:
                        batch['status'] = 'cancelled'
                    if batch.get('job_id') in self.jobs:
                        self.jobs[batch['job_id']]['status'] = 'cancelled'
            self.review_record(review, payload, 'cancel', fingerprint, rid)
            self.review_save_or_rollback(session, before)
            return self.review_run_public(session, run)

    def edit_review_artifact(self, collection, aid, payload):
        with self.lock:
            session, review, fingerprint, receipt = self.review_require(payload, 'edit-' + collection + '-' + aid)
            item = next((x for x in review[collection] if x['id'] == aid), None)
            if item is None:
                raise HTTPException(404, '找不到这份复习内容。')
            if receipt:
                return copy.deepcopy(item)
            if type(payload.get('revision')) is not int or payload['revision'] != item['user_draft']['revision']:
                raise HTTPException(409, '这份内容已在其他页面更新，请保留你的输入并核对最新版本。')
            content, personal_note = payload.get('content'), payload.get('personal_note', '')
            if not isinstance(content, str) or len(content) > 120000 or not isinstance(personal_note, str) or len(personal_note) > 12000:
                raise HTTPException(422, '正文最多 120000 字符，个人备注最多 12000 字符。')
            before = copy.deepcopy(review)
            item.setdefault('draft_history', []).append(copy.deepcopy(item['user_draft']))
            item['user_draft'] = {'revision': payload['revision'] + 1, 'content': content,
                                  'personal_note': personal_note, 'updated_at': now()}
            self.review_record(review, payload, 'edit-' + collection + '-' + aid, fingerprint, aid)
            self.review_save_or_rollback(session, before)
            return copy.deepcopy(item)

    def adopt_review_note(self, nid, payload):
        with self.lock:
            session, review, fingerprint, receipt = self.review_require(payload, 'adopt-' + nid)
            if not any(x['id'] == nid for x in review['notes']):
                raise HTTPException(404, '找不到这份笔记。')
            if not receipt:
                if type(payload.get('revision')) is not int or payload['revision'] != review['selection_revision']:
                    raise HTTPException(409, '当前采用版本已改变，请刷新后核对。')
                before = copy.deepcopy(review)
                review.update(selected_note_id=nid, selection_revision=review['selection_revision'] + 1)
                self.review_record(review, payload, 'adopt-' + nid, fingerprint, nid)
                self.review_save_or_rollback(session, before)
            return {k: review[k] for k in ('selected_note_id', 'selection_revision')}

    @staticmethod
    def review_draft(content):
        if 'sections' in content:
            return '\n\n'.join(f"## {x['title']}\n{x['body']}" + (f"\nAI 补充：{x['supplement']}" if x.get('supplement') else '')
                                 for x in content['sections'])
        return '\n\n'.join(x for x in (content['title'], content['front'], content['back'],
            ('AI 补充：' + content['supplement']) if content.get('supplement') else '', content.get('english'), content.get('english_meaning')) if x)

    def review_publish_batch(self, session, run, batch, validated):
        review = self.review_data(session)
        for card in validated['cards']:
            evidence = evidence_subset(batch['evidence'], card['source_ids'])
            from review_schema import card_fingerprint
            source_versions = {k: evidence[k] for k in ('source_session_id', 'segments', 'slides')}
            key = digest({'content': card_fingerprint(card), 'sources': source_versions, 'english': run['options']['english']})
            existing = next((x for x in review['cards'] if x.get('fingerprint') == key), None)
            if existing:
                if existing['id'] not in run['card_ids']:
                    run['card_ids'].append(existing['id'])
                continue
            value = {'id': 'C' + uuid.uuid4().hex, 'run_id': run['id'], 'fingerprint': key, 'created_at': now(),
                     'destination': copy.deepcopy(batch['attempts'][-1]['destination']),
                     'ai_content': copy.deepcopy(card), 'evidence': evidence, 'user_draft':
                     {'revision': 0, 'content': self.review_draft(card), 'personal_note': '', 'updated_at': now()}}
            review['cards'].append(value)
            run['card_ids'].append(value['id'])
        if all(b['status'] == 'done' for b in run['batches']):
            if run.get('note_id') and any(n['id'] == run['note_id'] for n in review['notes']):
                run['status'] = 'done'
                return
            sections = [copy.deepcopy(entry) for b in run['batches'] for entry in b['attempts'][-1]['validated_output']['notes']]
            content = {'sections': sections}
            note = {'id': 'N' + uuid.uuid4().hex, 'run_id': run['id'], 'created_at': now(),
                    'destinations': [copy.deepcopy(b['attempts'][-1]['destination']) for b in run['batches']],
                    'unresolved_marks': [copy.deepcopy(m) for m in run['evidence'].get('marks', [])
                                         if m.get('kind') == 'unclear' and not m.get('resolved')],
                    'ai_content': content, 'evidence': copy.deepcopy(run['evidence']), 'user_draft':
                    {'revision': 0, 'content': self.review_draft(content), 'personal_note': '', 'updated_at': now()}}
            review['notes'].append(note)
            run.update(note_id=note['id'], status='done', completed_at=now())
            if review['selected_note_id'] is None:
                review.update(selected_note_id=note['id'], selection_revision=review['selection_revision'] + 1)
        else:
            run['status'] = 'pending'

    def review_process_job(self, sid, rid, jid):
        from review_schema import validate_review_output, ReviewValidationError
        dispatched = returned = False
        api_attempt = usage_recorded = False
        began = time.monotonic()
        with self.lock:
            session = self.get(sid)
            run = self.review_run(session, rid)
            batch = next((b for b in run['batches'] if b.get('job_id') == jid), None)
            if not batch or batch['status'] != 'pending' or run['status'] not in ('pending', 'running') or self.ai['paused']:
                return
            reason = self.review_gate(session, run)
            if reason:
                run.update(status='paused', error=reason)
                batch['status'] = self.jobs[jid]['status'] = 'paused'
                try:
                    self.save(session)
                except OSError:
                    self._pause_ai_storage()
                return
            attempt = batch['attempts'][-1]
            run['status'] = batch['status'] = attempt['status'] = self.jobs[jid]['status'] = 'running'
            destination = copy.deepcopy(run['destination'])
            api_config, api_key = dict(self.connection.values), self.connection.key
            provider = destination['provider']
            self.active_ai = {'provider': provider, 'model': destination['model'], 'review_run_id': rid}
            self.active_cancel = threading.Event()
            cancel_event = self.active_cancel
            bridge = self.bridge
        try:
            images = self.study_images(session, {'evidence': batch['evidence']})
            if provider == 'api':
                if not api_key or not api_config.get('model'):
                    raise CodexBridgeError('请先填写 API 模型和密钥。', kind='auth')
                bridge = self.connection.api_factory({**api_config, 'timeout_seconds': self.connection.timeout}, api_key, on_delta=None)
            if not hasattr(bridge, 'generate_review'):
                raise CodexBridgeError('当前接入没有结构化复习能力，请检查版本。')
            with self.lock:
                reason = self.review_gate(session, run)
                if reason or run['status'] == 'cancelled':
                    if run['status'] != 'cancelled':
                        run.update(status='paused', error=reason)
                        batch['status'] = 'paused'
                    return
                attempt.update(dispatch_started_at=now(), dispatch_state='started')
                if provider == 'api':
                    session['ai_usage']['requests'] += 1
                try:
                    self.save(session)
                except OSError:
                    attempt.pop('dispatch_started_at', None)
                    attempt['dispatch_state'] = 'not_sent'
                    if provider == 'api':
                        session['ai_usage']['requests'] -= 1
                    raise
                dispatched = True
                api_attempt = provider == 'api'
            raw = bridge.generate_review(batch['evidence'], run['options'], images, cancel_event=cancel_event)
            returned = True
            with self.lock:
                attempt.update(raw_ai_output=raw, dispatch_completed_at=now(), dispatch_state='complete', result_unknown=False)
                # Preserve even invalid or cancelled provider output before interpretation.
                self.save(session)
            validated = validate_review_output(raw, batch['evidence'], run['options'])
            with self.lock:
                attempt['validated_output'] = validated
                if api_attempt:
                    record_usage(session['ai_usage'], bridge.metadata(), True)
                    usage_recorded = True
                if run['status'] == 'cancelled' or destination['token'] != self.study_route:
                    attempt['status'] = 'cancelled'
                    if run['status'] == 'cancelled':
                        batch['status'] = 'cancelled'
                    else:
                        run.update(status='paused', error='接入已变化，返回结果保留，未采用。')
                        batch['status'] = 'paused'
                    return
                before_publish = copy.deepcopy(session['review'])
                batch['status'] = attempt['status'] = 'done'
                self.review_publish_batch(session, run, batch, validated)
                try:
                    self.save(session)
                except OSError:
                    restore_in_place(session['review'], before_publish)
                    raise
                self.jobs[jid]['status'] = 'done'
                self.review_schedule_next(session, run)
        except Exception as exc:
            with self.lock:
                kind = 'storage' if isinstance(exc, OSError) else 'validation' if isinstance(exc, ReviewValidationError) else getattr(exc, 'kind', 'other')
                unknown = dispatched and not returned and kind not in ('auth', 'quota', 'vision_unsupported')
                error = UNKNOWN_TIMEOUT if unknown and kind == 'timeout' else UNKNOWN_RESULT if unknown else str(exc)[:400]
                state = 'unknown' if unknown else 'error'
                attempt.update(status=state, error=error, error_kind=kind, result_unknown=unknown,
                    dispatch_state='unknown' if unknown else 'complete' if returned else 'failed' if dispatched else 'not_sent')
                if dispatched and not unknown:
                    attempt['dispatch_completed_at'] = now()
                if run['status'] != 'cancelled':
                    batch['status'] = state
                    run.update(status=state if unknown or not any(b['status'] == 'done' for b in run['batches']) else 'partial', error=error)
                self.jobs[jid].update(status=state, error=error)
                if kind == 'vision_unsupported':
                    self.record_study_vision(destination['token'], len(images), False)
                if kind == 'storage':
                    run.update(status='paused', error='本机保存失败，已有结果暂留内存；请勿退出。恢复存储后继续，不能重发已返回的请求。')
                    self._pause_ai_storage()
        finally:
            with self.lock:
                if api_attempt and not usage_recorded:
                    record_usage(session['ai_usage'], bridge.metadata(), returned)
                    if not returned and attempt.get('result_unknown'):
                        session['ai_usage']['failed'] -= 1
                        session['ai_usage']['unknown'] += 1
                self.active_ai = None
                self.active_cancel = None
                self.ai_switch_pending = False
                attempt['elapsed'] = round(time.monotonic() - began, 2)
                try:
                    self.save(session)
                except OSError:
                    self._pause_ai_storage()

    def review_pause_pending(self, reason):
        for session in self.sessions.values():
            if session.get('review_error'):
                continue
            for run in session.get('review', {}).get('runs', []):
                for batch in run['batches']:
                    if batch['status'] == 'pending':
                        batch['status'] = 'paused'
                        run.update(status='paused', error=reason)
                        if batch.get('job_id') in self.jobs:
                            self.jobs[batch['job_id']]['status'] = 'paused'

    def invalidate_review_destination(self):
        self.review_pause_pending('接入已变化，请重新确认计划和目的地。')
        if self.active_ai and self.active_ai.get('review_run_id') and self.active_cancel:
            self.active_cancel.set()

    def review_export_lines(self, session):
        review = self.review_public(session)
        if not any(review[key] for key in ('marks', 'cards', 'notes')):
            return []
        lines = ['## 本课复习', '', 'AI 整理与个人修改分开保存；本 Markdown 不含媒体。', '']
        for mark in review['marks']:
            if mark['active']:
                lines += [f"标记 [{mark['target_id']}] {mark['kind']} · {'已理解' if mark.get('resolved') else '未解决'} · {mark['note']}", '']
        for key in ('cards', 'notes'):
            for item in review[key]:
                lines += [f"### {item['id']}" + (' · 当前采用笔记' if item['id'] == review['selected_note_id'] else ''), '',
                          'AI 原始整理：', self.review_draft(item['ai_content']), '']
                if item['user_draft']['revision']:
                    lines += ['个人修改：', item['user_draft']['content'], item['user_draft']['personal_note'], '']
                lines += ['来源：' + ', '.join(item['evidence']['source_ids']), '']
                if item['source_status']['changed']:
                    lines += ['待核对：来源已变化，保留生成时快照。', '']
                if item['source_status']['new_material']:
                    lines += ['新增内容尚未纳入。', '']
        return lines


def register_review_routes(app, room):
    @app.get('/api/review')
    def review(session_id: str):
        with room.lock:
            return room.review_public(room.get(session_id))

    @app.get('/api/review/requests/{request_id}')
    def lookup(request_id: str, session_id: str):
        return room.review_lookup(session_id, request_id)

    @app.post('/api/review/marks')
    def mark(body: dict):
        return room.set_review_mark(body)

    @app.post('/api/review/prepare')
    def prepare(body: dict):
        return room.prepare_review(body)

    @app.post('/api/review/runs/{rid}/send')
    def send(rid: str, body: dict):
        return room.send_review(rid, body)

    @app.post('/api/review/runs/{rid}/continue')
    def resume(rid: str, body: dict):
        return room.send_review(rid, body, continuing=True)

    @app.post('/api/review/runs/{rid}/cancel')
    def cancel(rid: str, body: dict):
        return room.cancel_review(rid, body)

    @app.post('/api/review/cards/{aid}/draft')
    def card_draft(aid: str, body: dict):
        return room.edit_review_artifact('cards', aid, body)

    @app.post('/api/review/notes/{aid}/draft')
    def note_draft(aid: str, body: dict):
        return room.edit_review_artifact('notes', aid, body)

    @app.post('/api/review/notes/{aid}/adopt')
    def adopt(aid: str, body: dict):
        return room.adopt_review_note(aid, body)
