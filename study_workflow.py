"""Local slide receipts and explicit, destination-bound study requests."""
from __future__ import annotations

import copy
import re
import threading
import uuid
from datetime import datetime

from fastapi import HTTPException

from slide_context import (SlideContextError, save_slide, update_slide_interval,
                           snapshot_context, resolve_slide_path)
from classroom_features import write_json


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


class StudyWorkflow:
    def init_study(self):
        # A restart requires fresh consent. Tokens identify configuration revisions,
        # never encode a credential; they are safe to expose with the receipt.
        self.study_route = uuid.uuid4().hex
        self.study_grants = set()
        self.study_vision = {}
        self.active_cancel = None

    def study_busy(self):
        return bool(self.active_ai or any(j['kind'] in ('question', 'analysis', 'glossary')
                    and j['status'] in ('pending', 'running') for j in self.jobs.values()))

    def study_destination(self):
        v = self.connection.values
        provider = v['provider']
        return {'provider': provider, 'label': 'Codex 账号服务（会发送到云端）' if provider == 'codex' else 'OpenAI 兼容 API',
                'base_url': v['base_url'] if provider == 'api' else None,
                'model': v['model'] if provider == 'api' else self.config.get('codex_model') or 'Codex 默认模型',
                'token': self.study_route}

    @staticmethod
    def study_question(session, qid):
        q = next((q for q in session['questions'] if q['id'] == qid and q.get('study')), None)
        if q is None:
            raise HTTPException(404, '找不到这条图文或即时解释记录。')
        return q

    def study_public(self, item, sid):
        value = copy.deepcopy(item)
        if value.get('status') == 'prepared':
            value['destination'] = self.study_destination()
        token = value['destination']['token']
        slides = value['evidence']['slides']
        value['requires_confirmation'] = bool(slides) and any(
            (sid, token, p['sha256']) not in self.study_grants for p in slides)
        tested = self.study_vision.get(token, {})
        count = len(slides)
        unsupported = count in tested.get('unsupported_counts', [])
        supported = count in tested.get('supported_counts', [])
        value['vision'] = {'state': 'unsupported' if unsupported else 'supported' if supported else 'unknown',
                           'image_count': count,
                           'message': '当前配置明确拒绝了此数量的图片，请更换模型或仅用原文。' if unsupported
                           else '此配置已完成相同图片数量的请求；不代表答案一定正确。' if supported
                           else '图片能力尚未验证；可确认发送后尝试一次。'}
        return value

    def study_session_public(self, s):
        value = copy.deepcopy(s)
        value.setdefault('slides', [])
        value['questions'] = [self.study_public(q, s['id']) if q.get('study') else q for q in value['questions']]
        return value

    def add_slide(self, body, *, start, sid=None, title=''):
        with self.lock:
            s = self.get(sid)
            previous = copy.deepcopy(s.get('slides', []))
            try:
                slide = save_slide(self.data, s, body, start=start, title=title)
                self.save(s)
            except SlideContextError as exc:
                raise HTTPException(422, str(exc)) from None
            except OSError:
                s['slides'] = previous
                raise HTTPException(507, '图片未能保存，请检查本机可用空间后重试。') from None
            return copy.deepcopy(slide)

    def update_slide(self, pid, payload):
        with self.lock:
            s = self.get(payload.get('session_id'))
            if not any(p['id'] == pid for p in s.get('slides', [])):
                raise HTTPException(404, '找不到这张课件。')
            before = copy.deepcopy(s.get('slides', []))
            try:
                result = update_slide_interval(s, pid, payload['start'], payload.get('end'))
                if payload.get('title') is not None:
                    slide = next(p for p in s['slides'] if p['id'] == pid)
                    slide['title'] = payload['title'].strip()[:160]
                    result = copy.deepcopy(slide)
                self.save(s)
            except SlideContextError as exc:
                raise HTTPException(422, str(exc)) from None
            except OSError:
                s['slides'] = before
                raise HTTPException(507, '关联时间未能保存，请重试。') from None
            return result

    def slide_file(self, pid, sid=None):
        with self.lock:
            s = self.get(sid)
            p = next((p for p in s.get('slides', []) if p['id'] == pid), None)
            if not p:
                raise HTTPException(404, '找不到这张课件。')
            try:
                return resolve_slide_path(self.data, s['id'], p), p['mime_type']
            except SlideContextError:
                raise HTTPException(404, '课件文件不可用或内容已变化；历史答案依据仍保留。') from None

    def prepare_study(self, payload):
        with self.lock:
            s = self.get(payload.get('session_id'))
            if s['demo']:
                raise HTTPException(400, '演示课堂不调用 AI，请新建课堂使用课件。')
            observed = self.study_observed_session(s, payload)
            try:
                evidence = snapshot_context(observed, mode=payload.get('mode', 'recent120'), anchor=payload['anchor'],
                           group_id=payload.get('group_id'), slide_ids=payload.get('slide_ids'),
                           include_images=payload.get('include_images', True))
                for p in evidence['slides']:
                    resolve_slide_path(self.data, s['id'], p)
            except SlideContextError as exc:
                raise HTTPException(422, str(exc)) from None
            question = (payload.get('question') or '').strip()
            if not question and not payload.get('explain', False):
                raise HTTPException(422, '请填写问题，或选择“刚才没听懂”。')
            q = {'id': f"Q{len(s['questions']) + 1:04d}", 'study': True, 'status': 'prepared',
                 'question': question or '刚才没听懂，请直白解释原因并举一个具体例子。',
                 'answer': '', 'group_id': evidence.get('group_id') or payload.get('group_id'),
                 'created_at': now(), 'explain': bool(payload.get('explain')), 'evidence': evidence,
                 'context_snapshot': evidence['context'], 'history_snapshot': [],
                 'request_params': {k: copy.deepcopy(payload[k]) for k in ('mode', 'anchor', 'group_id', 'slide_ids', 'include_images', 'explain', 'question') if k in payload},
                 'destination': self.study_destination()}
            current_versions = {seg['id']: seg.get('version', 0) for seg in s['segments']}
            if any(seg.get('version', 0) != current_versions.get(seg['id']) for seg in evidence['segments']):
                q.update(stale=True, stale_reason='点击之后原文已修改；本次仍使用点击时显示的版本。')
            s['questions'].append(q)
            try:
                self.save(s)
            except OSError:
                s['questions'].pop()
                raise HTTPException(507, '请求依据未能保存在本机，尚未发送。') from None
            return self.study_public(q, s['id'])

    @staticmethod
    def study_observed_session(session, payload):
        """Freeze what the browser saw, including across a delayed HTTP request.

        Only identifiers/versions come from the browser. Transcript text always
        comes from this classroom's persisted originals/edit history.
        """
        view = dict(session)
        if payload.get('seen_segments') is not None:
            available = {seg['id']: seg for seg in session['segments']}
            chosen = []
            seen = set()
            for ref in payload['seen_segments']:
                identifier, version = ref.get('id'), ref.get('version', 0)
                if identifier in seen or identifier not in available or type(version) is not int or version < 0:
                    raise HTTPException(409, '点击时的原文记录已不可用，请刷新后重新选择。')
                seen.add(identifier)
                segment = copy.deepcopy(available[identifier])
                current = segment.get('version', 0)
                if version != current:
                    edits = segment.get('edits', [])
                    if version >= current or version >= len(edits) or len(edits) != current:
                        raise HTTPException(409, '无法恢复点击时的原文版本，请刷新后重新选择。')
                    segment['text'] = edits[version]['text']
                    segment['version'] = version
                    segment['edited'] = version > 0
                    segment.pop('edited_at', None)
                    segment['edits'] = edits[:version]
                chosen.append(segment)
            view['segments'] = chosen
        if payload.get('seen_slides') is not None:
            available = {p['id']: p for p in session.get('slides', [])}
            chosen = []
            seen = set()
            for ref in payload['seen_slides']:
                identifier = ref.get('id')
                actual = available.get(identifier)
                if not actual or identifier in seen or ref.get('sha256') != actual['sha256']:
                    raise HTTPException(409, '点击时的课件图片已不可用，请刷新后重新选择。')
                seen.add(identifier)
                chosen.append({**actual, **{k: ref[k] for k in ('start', 'end', 'title') if k in ref}})
            view['slides'] = chosen
        if payload.get('mode') == 'group' and payload.get('seen_group_segment_ids') is not None:
            group = next((g for g in session['groups'] if g['id'] == payload.get('group_id')), None)
            ids = payload['seen_group_segment_ids']
            if group is None or len(ids) != len(set(ids)) or not set(ids).issubset(group['segment_ids']):
                raise HTTPException(409, '点击时的意群已不可用，请刷新后重新选择。')
            members = {seg['id']: seg for seg in view['segments']}
            if not ids or any(identifier not in members for identifier in ids):
                raise HTTPException(409, '点击时的意群原文已不可用，请刷新后重新选择。')
            # Pending groups can grow while the browser request is in flight.
            # Reconstruct their completion marker from the observed text, too.
            last_text = members[ids[-1]]['text']
            partial = not bool(re.search(r"[.!?。！？][\"'”’）)]?\s*$", last_text))
            view['groups'] = [{**group, 'segment_ids': list(ids), 'partial': partial}]
        return view

    def send_study(self, qid, payload):
        with self.lock:
            s = self.get(payload.get('session_id'))
            q = self.study_question(s, qid)
            if q['status'] != 'prepared':
                raise HTTPException(409, '这一请求已发送或正在处理，请勿重复发送。')
            destination = self.study_destination()
            if payload.get('destination_token') != destination['token']:
                raise HTTPException(409, '接入目的地已变化，请重新查看并确认后发送。')
            public = self.study_public(q, s['id'])
            if public['vision']['state'] == 'unsupported' and q['evidence']['slides']:
                raise HTTPException(409, public['vision']['message'])
            if public['requires_confirmation'] and not payload.get('confirm_images'):
                raise HTTPException(409, '发送图片前请确认服务商、图片及原文范围。')
            try:
                for p in q['evidence']['slides']:
                    resolve_slide_path(self.data, s['id'], p)
            except SlideContextError as exc:
                raise HTTPException(422, str(exc)) from None
            if destination['provider'] == 'api' and not (self.connection.key and self.connection.values['model']):
                raise HTTPException(409, '请先填写 API 模型和密钥，再确认目的地。')
            q['destination'] = destination
            q['confirmed_at'] = now()
            # A switch must not cause unrelated paused work to resume. Only the
            # explicitly confirmed request is newly enqueued here.
            if self.ai.get('paused'):
                resumed = {**self.ai, 'paused': False, 'kind': None, 'reason': None}
                write_json(self.data / 'ai-state.json', resumed)
                self.ai.update(resumed)
                self.ai_ready.set()
            before = copy.deepcopy(q)
            try:
                self._enqueue_ai(s, q, 'question')
                self.save(s)
            except OSError:
                if q.get('job_id') in self.jobs:
                    self.jobs[q['job_id']]['status'] = 'cancelled'
                q.clear()
                q.update(before)
                raise HTTPException(507, '本机保存失败，请求尚未发送。') from None
            if payload.get('remember') and payload.get('confirm_images'):
                for p in q['evidence']['slides']:
                    self.study_grants.add((s['id'], destination['token'], p['sha256']))
            return self.study_public(q, s['id'])

    def clone_study(self, qid, sid=None, *, text_only=False):
        with self.lock:
            s = self.get(sid)
            original = self.study_question(s, qid)
            if original['status'] in ('running', 'pending'):
                raise HTTPException(409, '这一请求仍在处理或暂停，请先结束当前请求。')
            q = copy.deepcopy(original)
            for key in ('job_id', 'error', 'error_kind', 'streaming_text', 'stream_status', 'ai_provider',
                        'ai_model', 'usage', 'elapsed', 'confirmed_at', 'attempts'):
                q.pop(key, None)
            q.update(id=f"Q{len(s['questions']) + 1:04d}", status='prepared', answer='',
                     created_at=now(), retry_of=qid, destination=self.study_destination())
            if text_only:
                q['evidence']['slides'] = []
                q['evidence']['source_ids'] = [p['id'] for p in q['evidence']['segments']]
                q['evidence']['warnings'].append('按你的选择仅使用原文，未发送或读取图片。')
                q['evidence']['context'] = self.text_evidence(q['evidence'])
                q['context_snapshot'] = q['evidence']['context']
                q['request_params']['include_images'] = False
            previous = copy.deepcopy(original)
            if original['status'] == 'paused':
                old_job = original.pop('job_id', None)
                if old_job in self.jobs:
                    self.jobs[old_job]['status'] = 'superseded'
                original['status'] = 'error'
            s['questions'].append(q)
            try:
                self.save(s)
            except OSError:
                s['questions'].pop()
                original.clear()
                original.update(previous)
                raise HTTPException(507, '请求依据未能保存，尚未发送。') from None
            return self.study_public(q, s['id'])

    @staticmethod
    def text_evidence(evidence):
        def t(value):
            return f'{int(value) // 60:02d}:{int(value) % 60:02d}'
        return ('以下是点击时固定的课堂原文，仅这些原文可作为课堂事实。\n'
                + '\n'.join(f"[{p['id']}] {t(p['start'])}–{t(p['end'])} {p['text']}" for p in evidence['segments'])
                + '\n范围说明：' + '；'.join(evidence.get('warnings', [])))

    def invalidate_study_destination(self):
        self.study_route = uuid.uuid4().hex
        self.study_grants.clear()
        self.study_vision.clear()
        for s in self.sessions.values():
            for q in s['questions']:
                if not q.get('study') or q['status'] not in ('pending', 'paused', 'running', 'prepared'):
                    continue
                if q['status'] == 'running':
                    q.setdefault('attempts', []).append({'destination': copy.deepcopy(q['destination']),
                        'status': 'result_unknown', 'at': now(), 'note': '已请求停止，服务商可能已经处理或计费。'})
                    if self.active_cancel:
                        self.active_cancel.set()
                jid = q.pop('job_id', None)
                if jid in self.jobs:
                    self.jobs[jid]['status'] = 'superseded'
                q.update(status='prepared', stream_status='interrupted', streaming_text='',
                         error='接入已更换；原图片和原文保留，请重新确认目的地后发送。')
                q.pop('confirmed_at', None)
        # Revoke every classroom in memory BEFORE any fallible disk operation.
        # Otherwise a full disk in the first classroom leaves a later active
        # request able to publish its answer after a forced destination change.
        failures = []
        for s in self.sessions.values():
            try:
                self.save(s)
            except OSError as exc:
                failures.append(exc)
        if failures:
            raise failures[0]

    def study_images(self, s, item):
        result = []
        try:
            for slide in item['evidence']['slides']:
                result.append({**slide, 'path': str(resolve_slide_path(self.data, s['id'], slide))})
        except SlideContextError as exc:
            from codex_bridge import CodexBridgeError
            raise CodexBridgeError(str(exc)) from None
        return result

    def record_study_vision(self, token, count, supported):
        if not count:
            return
        record = self.study_vision.setdefault(token, {})
        target = 'supported_counts' if supported else 'unsupported_counts'
        record.setdefault(target, [])
        if count not in record[target]:
            record[target].append(count)

    @staticmethod
    def study_export_lines(q):
        if not q.get('study'):
            return []
        e = q['evidence']
        d = q['destination']
        lines = ['本次依据（发送时快照；后续改字、关联调整不覆盖）：',
                 f"接入：{d['label']}；地址：{d.get('base_url') or 'Codex 账号服务'}；模型：{d.get('model')}。",
                 f"所选范围：{e['requested_range']}；实际原文范围：{e['actual_range']}。", '']
        for p in e['slides']:
            lines += [f"[{p['id']}] {p['title']} · {p['start']}–{p.get('end')}秒 · {p['path']}",
                      f"SHA256：{p['sha256']}", '']
        if e['slides']:
            lines += ['此文本导出不包含图片本体，请连同本课堂 slides/ 目录一起备份。', '']
        lines += [StudyWorkflow.text_evidence(e), '']
        return lines
