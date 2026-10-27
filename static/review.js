'use strict';

/* Review state is scoped to a classroom. Local drafts also have a per-tab owner:
   localStorage is recovery storage, never a substitute for the server CAS. */
(function (root, factory) {
  const api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ReviewUI = api;
})(typeof window !== 'undefined' ? window : globalThis, function (root) {
  const copy = (value) => JSON.parse(JSON.stringify(value));
  const escape = (value) => root.ReviewRender?.escape(value) ?? String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  const rich = (value) => root.ReviewRender?.render(value == null ? '' : String(value)) ?? `<p>${escape(value).replace(/\n/g, '<br>')}</p>`;
  const time = (value) => root.ReviewRender?.time(value) ?? `${Math.floor(Math.max(0, Number(value) || 0) / 60)}:${String(Math.floor(Math.max(0, Number(value) || 0) % 60)).padStart(2, '0')}`;
  const identifier = () => root.crypto?.randomUUID?.() || `review-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  const empty = () => ({ marks: [], runs: [], cards: [], notes: [], selected_note_id: null, selection_revision: 0 });
  const labels = { prepared: '待确认', pending: '等待', running: '生成中', paused: '已暂停', partial: '部分完成', done: '完成', error: '失败', unknown: '结果未知', cancelled: '已取消', concept: '概念', derivation: '推导', example: '例题', pitfall: '易错点', structure: '知识结构', focus: '重点内容', questions: '未解决疑问', slides: '关联课件' };
  const unknown = (run) => Boolean(run?.repeat_billing_required || run?.result_unknown || run?.status === 'unknown');
  const draftPrefix = 'classroom-review-draft-v1:';
  const pendingPrefix = 'classroom-review-request-v1:';

  function createController({ api, storage, tabId = identifier(), onChange = () => {} }) {
    const local = new Map(), volatile = new Set(), busy = new Set(); let session = null, epoch = 0, notice = '';
    function get(key) { if (volatile.has(key)) return local.get(key) || null; try { return storage ? JSON.parse(storage.getItem(key) || 'null') : local.get(key) || null; } catch (_) { return local.get(key) || null; } }
    function put(key, value, required = false) {
      local.set(key, copy(value));
      try { if (!storage) throw new Error('storage unavailable'); storage.setItem(key, JSON.stringify(value)); volatile.delete(key); }
      catch (_) { notice = '浏览器暂存不可用，草稿目前仅在此页面；请保存到本机服务后再关闭。'; if (required) { local.delete(key); throw new Error('无法持久保存请求凭据，暂不发送。请恢复浏览器存储后重试。'); } volatile.add(key); }
    }
    function remove(key) { local.delete(key); volatile.delete(key); try { storage?.removeItem(key); } catch (_) {} }
    const pendingKey = (sid, id) => pendingPrefix + sid + (id ? `:${id}` : '');
    function pendingFor(sid) {
      const prefix = pendingKey(sid), keys = new Set([prefix, ...local.keys()]);
      try { for (let i = 0; i < (storage?.length || 0); i++) keys.add(storage.key(i)); } catch (_) {}
      for (const key of keys) if (key === prefix || key?.startsWith(prefix + ':')) { const value = get(key); if (value) return { ...value, storage_key: key }; }
      return null;
    }
    const draftKey = (sid, kind, id, owner = tabId) => `${draftPrefix}${encodeURIComponent(sid)}:${kind}:${encodeURIComponent(id)}:${owner}`;
    const active = (scope) => session?.id === scope.sid && scope.epoch === epoch;
    const scope = () => ({ sid: session?.id, epoch });
    function update(result, type, targetScope) {
      if (!active(targetScope) || !result) return;
      const review = session.review;
      if (type === 'adopt') Object.assign(review, result);
      else { const list = review[type]; if (!list) return; const index = list.findIndex((x) => x.id === result.id); if (index < 0) list.push(result); else list[index] = result; }
      onChange();
    }
    async function mutate(path, body, type, operation = 'mutation') {
      const captured = scope(); if (!captured.sid) throw new Error('请先选择课堂。');
      if (session.review.read_only) throw new Error(session.review.error || '复习数据暂不可编辑，课堂原文仍可阅读。');
      if (busy.has(captured.sid)) throw new Error('本课请求正在保存，请稍候。');
      if (pendingFor(captured.sid)) throw new Error('请先核对本课上次请求，避免重复提交。');
      const request = { request_id: identifier(), ...copy(body), session_id: captured.sid };
      const record = { request_id: request.request_id, path, body: request, operation, type, created_at: new Date().toISOString() };
      const storageKey = pendingKey(captured.sid, record.request_id);
      put(storageKey, record, true); busy.add(captured.sid); onChange();
      try {
        const result = await api(path, { method: 'POST', body: request });
        remove(storageKey); update(result, type, captured); return { result, current: active(captured) };
      } catch (error) {
        if (error.status >= 400 && error.status < 500 && error.status !== 408) remove(storageKey);
        throw error;
      } finally { busy.delete(captured.sid); onChange(); }
    }
    const controller = {
      get session() { return session; }, get review() { return session?.review || empty(); }, get notice() { return notice; }, get tabId() { return tabId; },
      sync(value) { if (session?.id !== value?.id) epoch++; session = value ? { ...value, review: { ...empty(), ...copy(value.review || {}) } } : null; },
      pending(sid = session?.id) { return pendingFor(sid); }, busy() { return busy.has(session?.id); },
      async prepare(options) { return mutate('/api/review/prepare', options, 'runs', 'prepare'); },
      async mark(targetKind, targetId, kind, values = {}) { const previous = controller.review.marks.find((x) => x.target_kind === targetKind && x.target_id === targetId && x.kind === kind); return mutate('/api/review/marks', { target_kind: targetKind, target_id: targetId, kind, revision: previous?.revision || 0, ...values }, 'marks', 'mark'); },
      async send(run, flags = {}, action = 'send') {
        if (unknown(run) && !flags.confirm_duplicate) throw new Error('请求超时、无法确定服务商是否已处理。再次请求前请确认可能重复计费。');
        if (run.source_status?.changed && !flags.confirm_original) throw new Error('材料已有变化，请核对原快照并确认。');
        return mutate(`/api/review/runs/${encodeURIComponent(run.id)}/${action}`, { confirm_plan: true, destination_token: run.destination?.token, source_token: run.source_status?.token, ...flags }, 'runs', action);
      },
      async cancel(run) { return mutate(`/api/review/runs/${encodeURIComponent(run.id)}/cancel`, {}, 'runs', 'cancel'); },
      async recover() {
        const captured = scope(), receipt = controller.pending(captured.sid); if (!receipt) return null;
        try {
          const result = await api(`/api/review/requests/${encodeURIComponent(receipt.request_id)}?session_id=${encodeURIComponent(captured.sid)}`);
          remove(receipt.storage_key); update(result.run || result.mark || result.artifact || result.item, receipt.type, captured); onChange(); return result;
        } catch (error) { if (error.status === 404) { notice = '暂未查到原请求；保留凭据，请稍后再次核对。不会自动重发。'; onChange(); return null; } throw error; }
      },
      async retryPending(confirmed) {
        if (controller.review.read_only) throw new Error(controller.review.error || '复习数据暂不可编辑。');
        const captured = scope(), original = controller.pending(captured.sid); if (!original) return null;
        if (!confirmed) throw new Error('请确认已核对原请求，仍愿意再次提交；AI 请求可能重复计费。');
        const found = await controller.recover(); if (found || !active(captured)) return found;
        if (busy.has(captured.sid)) throw new Error('原请求仍在处理中，请稍候。');
        busy.add(captured.sid);
        try {
          // The original body and key must remain byte-for-byte equivalent for
          // the server receipt fingerprint; explicit consent is local here.
          const result = await api(original.path, { method: 'POST', body: original.body });
          remove(original.storage_key); update(result, original.type, captured); return result;
        } catch (error) { if (error.status >= 400 && error.status < 500 && error.status !== 408) remove(original.storage_key); throw error; }
        finally { busy.delete(captured.sid); onChange(); }
      },
      draft(kind, id, sid = session?.id) { return get(draftKey(sid, kind, id)); },
      drafts(sid = session?.id) {
        const prefix = `${draftPrefix}${encodeURIComponent(sid)}:`; const keys = new Set(local.keys());
        try { for (let i = 0; i < (storage?.length || 0); i++) keys.add(storage.key(i)); } catch (_) {}
        return [...keys].filter((key) => key?.startsWith(prefix)).map((key) => ({ ...get(key), key })).filter((x) => x.content != null);
      },
      writeDraft(kind, id, value, sid = session?.id) {
        const draft = { ...copy(value), kind, id, session_id: sid, owner: tabId, serial: identifier(), updated_at: new Date().toISOString() };
        put(draftKey(sid, kind, id), draft); onChange(); return draft;
      },
      restoreDraft(draft) { return controller.writeDraft(draft.kind, draft.id, draft); },
      discardDraft(kind, id) { remove(draftKey(session?.id, kind, id)); onChange(); },
      async saveDraft(kind, id) {
        if (controller.review.read_only) throw new Error(controller.review.error || '复习数据暂不可编辑，输入仍保留。');
        const captured = scope(), key = draftKey(captured.sid, kind, id), draft = get(key); if (!draft) return null;
        const lock = `draft:${key}`; if (busy.has(lock)) return null; busy.add(lock);
        try {
          const result = await api(`/api/review/${kind === 'note' ? 'notes' : 'cards'}/${encodeURIComponent(id)}/draft`, { method: 'POST', body: { session_id: captured.sid, request_id: identifier(), revision: draft.revision || 0, content: draft.content, personal_note: draft.personal_note || '' } });
          const newer = get(key);
          if (newer?.serial === draft.serial) remove(key);
          else if (newer) put(key, { ...newer, revision: result.user_draft.revision });
          update(result, kind === 'note' ? 'notes' : 'cards', captured); return result;
        } catch (error) { const current = get(key); if (current) put(key, { ...current, save_error: error.message, conflict: error.status === 409 }); throw error; }
        finally { busy.delete(lock); onChange(); }
      },
      async adopt(note) { return mutate(`/api/review/notes/${encodeURIComponent(note.id)}/adopt`, { revision: controller.review.selection_revision || 0 }, 'adopt', 'adopt'); },
    };
    return controller;
  }

  function sourceHTML(session, evidence = {}, sourceStatus = {}, sourceIds = null) {
    const selected = sourceIds ? new Set(sourceIds) : null;
    const segments = (evidence.segments || []).filter((x) => !selected || selected.has(x.id));
    const slides = (evidence.slides || []).filter((x) => !selected || selected.has(x.id));
    const warnings = [...(evidence.warnings || []), ...(sourceStatus.messages || [])];
    let html = warnings.map((x) => `<p class="review-warning">${escape(x)}</p>`).join('');
    html += segments.map((segment) => {
      const audio = (session.audio || []).find((x) => x.seq === segment.audio_seq && !x.deleted);
      const availability = (sourceStatus.audio || []).find((x) => x.source_id === segment.id);
      const offset = Math.max(0, Math.min(Number(audio?.duration) || Infinity, availability?.offset ?? ((Number(segment.start) || 0) - (Number(audio?.start) || 0))));
      const clip = audio && !session.audio_deleted_at && availability?.available !== false ? `<audio controls preload="none" src="/api/audio/${encodeURIComponent(audio.seq)}?session_id=${encodeURIComponent(session.id)}#t=${offset}"></audio><span class="small muted">从片段内 ${time(offset)} 回听，非逐词对齐</span>` : `<p class="small muted">${escape(availability?.reason || (session.audio_deleted_at ? '原录音已删除' : segment.audio_seq == null ? '此段没有原音定位' : '原录音不可用或未随课堂包恢复'))}</p>`;
      return `<div class="review-source"><div class="review-source-heading"><strong>${escape(segment.id)} · ${time(segment.start)}–${time(segment.end)} · v${escape(segment.version ?? 0)}</strong><button class="text-button" data-review-jump="${escape(segment.id)}">跳到当前原文</button></div><blockquote>${escape(segment.text)}</blockquote>${clip}</div>`;
    }).join('');
    html += slides.map((slide) => {
      const state = (sourceStatus.slides || []).find((x) => x.id === slide.id);
      const missing = state?.available === false || slide.missing || slide.corrupt;
      const url = `/api/slides/${encodeURIComponent(slide.id)}?session_id=${encodeURIComponent(session.id)}&version=${encodeURIComponent(slide.version || 1)}&sha256=${encodeURIComponent(slide.sha256 || '')}`;
      return `<figure class="review-source"><figcaption>${escape(slide.title || slide.id)} · ${escape(slide.id)} · v${escape(slide.version || 1)} · 当时课件</figcaption>${missing ? '<p class="review-warning">历史图片缺失或损坏，不能用新图代替。</p>' : `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" loading="lazy" alt="${escape(slide.title || slide.id)}"></a>`}<button class="text-button" data-review-slide="${escape(slide.id)}">查看当前课件</button></figure>`;
    }).join('');
    return html || '<p class="small muted">没有可显示的原文或课件依据。</p>';
  }

  function markButtons(session, targetKind, targetId, disabled = false) {
    disabled = disabled || Boolean(session?.review?.read_only);
    return `<span class="review-mark-buttons">${[['important', '重点'], ['unclear', '没懂']].map(([kind, title]) => {
      const mark = (session?.review?.marks || []).find((x) => x.target_kind === targetKind && x.target_id === targetId && x.kind === kind && x.active);
      return `<button type="button" class="review-mark ${mark ? 'is-marked' : ''}" aria-pressed="${Boolean(mark)}" data-review-mark="${kind}" data-review-target="${escape(targetId)}" data-review-kind="${targetKind}" ${disabled ? 'disabled' : ''}>${title}${mark?.resolved ? ' · 已理解' : ''}</button>`;
    }).join('')}</span>`;
  }

  function mount({ api, getSession, getSnapshot, poll, toast, jump, showSlide, beforePrepare = async () => {} }) {
    const $ = (id) => document.getElementById(id);
    // Duplicated tabs clone sessionStorage. A fresh document owner avoids two
    // pages writing one draft; drafts from a reload remain in the recovery list.
    let storage = null; const tabId = identifier();
    try { storage = root.localStorage; } catch (_) {}
    let visible = false, lastSession = null, editor = null, autosaveTimer = null, reviewPlan = null, packagePlan = null, packageBusy = false;
    const renderKeys = new Map(), englishPreferences = new Map();
    const c = createController({ api, storage, tabId, onChange: () => render() });
    const current = (sid) => c.session?.id === sid;
    const safeAction = async (action) => { const sid = c.session?.id; try { return await action(); } catch (error) { if (current(sid)) toast(error.message, true, 9000); } };
    function setHTML(id, html) {
      const el = $(id); if (!el || renderKeys.get(id) === html) return;
      if ([...el.querySelectorAll('audio')].some((a) => !a.paused && !a.ended)) return;
      const open = new Set([...el.querySelectorAll('details[open]')].map((x) => x.dataset.reviewDetail));
      el.innerHTML = html; renderKeys.set(id, html);
      for (const item of el.querySelectorAll('details[data-review-detail]')) item.open = open.has(item.dataset.reviewDetail);
      root.ReviewRender?.hydrate(el);
    }
    function show(value) { visible = value; $('review-view').hidden = !value; document.querySelector('.app-shell').classList.toggle('review-mode', value); $('review-open').setAttribute('aria-pressed', String(value)); if (value) { render(); $('review-heading').focus(); } }
    const status = (item) => `${item.source_status?.changed || item.stale ? '<p class="review-warning">依据已变化，待核对；原版本保留。</p>' : ''}${item.source_status?.new_material || item.new_material ? '<p class="review-warning">新增内容尚未纳入本版本。</p>' : ''}`;
    function contentFor(item, kind) {
      if (item.user_draft?.revision > 0) return `<div class="review-user-content"><span class="review-attribution">个人编辑 · v${item.user_draft.revision}</span>${rich(item.user_draft.content)}</div>${kind === 'note' ? unresolvedHTML(item) : ''}`;
      const ai = item.ai_content || {};
      if (kind === 'card') return `${rich(ai.back)}${ai.english ? `<div class="review-english"><strong>英文答题表述 · AI 建议</strong>${rich(ai.english)}${rich(ai.english_meaning)}</div>` : ''}${ai.supplement ? `<div class="review-supplement"><strong>AI 补充</strong>${rich(ai.supplement)}</div>` : ''}${(ai.missing || []).map((x) => `<p class="review-warning">课堂记录不足：${escape(x)}</p>`).join('')}`;
      return (ai.sections || []).map((section) => `<section class="review-note-section"><span class="eyebrow">${escape(labels[section.section] || section.section)}</span><h4>${escape(section.title)}</h4>${rich(section.body)}${section.supplement ? `<div class="review-supplement"><strong>AI 补充</strong>${rich(section.supplement)}</div>` : ''}${(section.missing || []).map((x) => `<p class="review-warning">课堂记录不足：${escape(x)}</p>`).join('')}<details data-review-detail="${escape(`${item.id}-${section.title}`)}"><summary>本条依据</summary>${sourceHTML(c.session, item.evidence, item.source_status, section.source_ids)}</details></section>`).join('') + unresolvedHTML(item);
    }
    function unresolvedHTML(note) { return (note.unresolved_marks || []).length ? `<section class="review-note-section"><h4>生成时仍未解决的个人疑问</h4><p class="small muted">生成解释不会自动标为已理解；可在“我的标记”中自行确认。</p>${note.unresolved_marks.map((mark) => `<div class="review-personal"><strong>${escape(mark.target_id)}</strong>${rich(mark.note || '此处当时标记为没懂')}<details data-review-detail="unresolved-${escape(note.id)}-${escape(mark.id)}"><summary>疑问对应依据</summary>${sourceHTML(c.session, note.evidence, note.source_status, [mark.target_id])}</details></div>`).join('')}</section>` : ''; }
    function render() {
      if (!c.session || !$('review-view')) return;
      const review = c.review, marks = review.marks.filter((x) => x.active);
      $('review-open').textContent = `本课复习${marks.length ? ` · ${marks.length}` : ''}`;
      $('review-course-name').textContent = c.session.title || '本节课';
      $('review-status').textContent = [review.error, c.notice].filter(Boolean).join(' ');
      const pending = c.pending(); $('review-recovery').hidden = !pending;
      $('review-recovery-message').textContent = pending ? `上次${pending.operation === 'prepare' ? '生成计划' : '操作'}尚未核对。仅核对原请求，不自动重发。` : '';
      $('review-generate').disabled = c.busy() || Boolean(pending) || Boolean(c.session.demo || c.session.is_demo || review.read_only);
      setHTML('review-marks', marks.map((mark) => `<article class="review-mark-item"><div class="review-card-head"><strong>${mark.kind === 'important' ? '重点' : '没懂'}${mark.resolved ? ' · 已理解' : ''} · ${escape(mark.target_id)}</strong><button class="text-button" data-review-mark-edit="${escape(mark.id)}">备注 / 管理</button></div>${status(mark)}${mark.note ? rich(mark.note) : ''}<details data-review-detail="mark-${escape(mark.id)}"><summary>查看标记时依据</summary>${sourceHTML(c.session, mark.evidence, mark.source_status)}</details></article>`).join('') || '<p class="review-empty">在原文或课件旁标出重点和没懂的地方，再回到这里整理。</p>');
      setHTML('review-runs', review.runs.slice().reverse().map((run) => `<article class="review-run"><div class="review-card-head"><strong>${escape(labels[run.status] || run.status)} · ${escape(run.id)}</strong><span>${Number(run.coverage?.completed_batches || 0)} / ${Number(run.coverage?.batch_count ?? run.batches?.length ?? 0)} 批</span></div><p class="small muted">${Number(run.coverage?.segment_count || 0)} 段原文 · ${Number(run.coverage?.slide_count || 0)} 张图片</p>${status(run)}${run.error ? `<p class="review-warning">${escape(run.error)}</p>` : ''}${unknown(run) ? '<p class="review-warning">请求超时、无法确定服务商是否已处理。再次请求可能重复计费。</p>' : ''}<div class="review-actions">${!['done', 'cancelled', 'running', 'pending'].includes(run.status) ? `<button class="button subtle" data-review-run="${escape(run.id)}">${run.status === 'prepared' ? '核对并确认发送' : '核对 / 手动继续'}</button>` : ''}${!['done', 'cancelled'].includes(run.status) ? `<button class="text-button" data-review-cancel="${escape(run.id)}">停止后续批次</button>` : ''}<button class="text-button" data-review-new="${escape(run.id)}">用当前材料生成新版</button></div><details data-review-detail="run-${escape(run.id)}"><summary>计划、覆盖与未完成项</summary>${planHTML(run)}</details></article>`).join('') || '<p class="review-empty">生成前会先展示材料范围和批次计划，确认后才调用 AI。</p>');
      setHTML('review-cards', review.cards.map((card) => `<article class="review-flashcard" id="review-card-${escape(card.id)}"><div class="review-card-head"><span class="review-type">${escape(labels[card.ai_content?.type] || '复习卡')}</span><span class="small muted">AI 整理 · ${escape(card.id)}</span></div><h3>${escape(card.ai_content?.title || '复习卡')}</h3>${status(card)}<div class="review-front">${rich(card.ai_content?.front)}</div><details class="review-answer" data-review-detail="answer-${escape(card.id)}"><summary>揭晓答案</summary>${contentFor(card, 'card')}${card.user_draft?.personal_note ? `<div class="review-personal"><strong>个人备注</strong>${rich(card.user_draft.personal_note)}</div>` : ''}</details><div class="review-actions"><button class="text-button" data-review-edit="${escape(card.id)}" data-review-edit-kind="card">编辑个人版本</button><button class="text-button" data-review-compare="${escape(card.id)}" data-review-edit-kind="card">对照 AI 原稿 / 其他版本</button></div><details data-review-detail="card-${escape(card.id)}"><summary>查看当时依据与原音</summary>${sourceHTML(c.session, card.evidence, card.source_status, card.ai_content?.source_ids)}</details></article>`).join('') || '<p class="review-empty">概念、推导、例题和易错点会在这里变成可以揭晓答案的卡片。</p>');
      const notes = review.notes;
      const notesKey = JSON.stringify(notes.map((x) => x.id));
      if (renderKeys.get('note-options') !== notesKey) { const old = $('review-note-select').value; $('review-note-select').innerHTML = notes.map((x, i) => `<option value="${escape(x.id)}">版本 ${i + 1} · ${escape(x.id)}</option>`).join(''); $('review-note-select').value = notes.some((x) => x.id === old) ? old : review.selected_note_id || notes.at(-1)?.id || ''; renderKeys.set('note-options', notesKey); }
      const note = notes.find((x) => x.id === $('review-note-select').value);
      setHTML('review-note-content', note ? `<div class="review-card-head"><strong>${review.selected_note_id === note.id ? '当前采用的笔记' : '候选版本 · 未自动采用'}</strong><span class="small muted">AI 整理</span></div>${status(note)}${contentFor(note, 'note')}${note.user_draft?.personal_note ? `<div class="review-personal"><strong>个人备注</strong>${rich(note.user_draft.personal_note)}</div>` : ''}<div class="review-actions"><button class="button subtle" data-review-edit="${escape(note.id)}" data-review-edit-kind="note">编辑个人版本</button><button class="text-button" data-review-compare="${escape(note.id)}" data-review-edit-kind="note">对照版本</button>${review.selected_note_id !== note.id ? `<button class="button primary" data-review-adopt="${escape(note.id)}">采用这个版本</button>` : ''}</div><details data-review-detail="note-${escape(note.id)}"><summary>全部来源快照</summary>${sourceHTML(c.session, note.evidence, note.source_status)}</details>` : '<p class="review-empty">每次生成另存笔记版本。你编辑或选定的版本会保留。</p>');
      const drafts = c.drafts();
      setHTML('review-drafts', drafts.map((draft) => `<div class="review-draft-row"><span>${draft.kind === 'note' ? '笔记' : '卡片'} ${escape(draft.id)} · ${draft.owner === tabId ? '本页' : '其他页面'}${draft.conflict ? ' · 保存冲突' : ''}</span><button class="text-button" data-review-restore-draft="${escape(draft.key)}">恢复草稿</button></div>`).join(''));
      $('review-draft-panel').hidden = !drafts.length;
      if (reviewPlan && current(reviewPlan.sid)) {
        const updated = review.runs.find((x) => x.id === reviewPlan.run.id);
        if (updated) {
          if (updated.source_status?.token !== reviewPlan.run.source_status?.token || updated.destination?.token !== reviewPlan.run.destination?.token) { $('review-plan-original').checked = false; $('review-plan-consent').checked = false; $('review-plan-status').textContent = '材料或 AI 接入已变化，请重新核对并确认。'; }
          reviewPlan.run = updated; renderPlan();
        }
      }
      renderPackageRecovery();
    }
    function planHTML(run) {
      const range = run.coverage?.range || [run.evidence?.requested_range?.start, run.evidence?.requested_range?.end];
      const destination = run.destination || {};
      return `<dl class="review-destination"><dt>AI 接入</dt><dd>${escape(destination.label || destination.provider || '当前配置')}</dd><dt>地址</dt><dd>${escape(destination.base_url || '本机 Codex · 当前登录账号')}</dd><dt>模型</dt><dd>${escape(destination.model || '当前配置')}</dd><dt>覆盖范围</dt><dd>${time(range[0])}–${time(range[1])} · ${Number(run.coverage?.segment_count || 0)} 段原文 · ${Number(run.coverage?.slide_count || 0)} 张图片</dd><dt>计划请求</dt><dd>${Number(run.coverage?.batch_count ?? run.batches?.length ?? 0)} 批；依次执行，失败后停止后续批次</dd></dl>${Number(run.coverage?.pending_audio || 0) ? `<p class="review-warning">还有 ${Number(run.coverage.pending_audio)} 段音频尚未完成转写，未纳入本次。</p>` : ''}${(run.coverage?.excluded || []).map((x) => `<p class="review-warning">未纳入：${escape(typeof x === 'object' ? JSON.stringify(x) : x)}</p>`).join('')}<ol class="review-batches">${(run.batches || []).map((batch, index) => `<li>第 ${index + 1} 批 · ${escape(labels[batch.status] || batch.status || '待发送')} ${batch.error ? escape(batch.error) : ''}<p class="small muted">本批主材料：${escape((batch.primary_source_ids || batch.evidence?.source_ids || []).join(', '))}${batch.context_source_ids?.length ? `；相邻上下文：${escape(batch.context_source_ids.join(', '))}` : ''}</p>${(batch.context_warnings || []).map((x) => `<p class="review-warning">${escape(x)}</p>`).join('')}${batch.raw_ai_output || batch.attempts?.some((x) => x.raw_ai_output) ? `<details data-review-detail="raw-${escape(run.id)}-${index}"><summary>原始输出 · 未通过校验的内容不是正式笔记</summary><pre>${escape(batch.raw_ai_output || batch.attempts.filter((x) => x.raw_ai_output).map((x) => x.raw_ai_output).join('\n\n'))}</pre></details>` : ''}</li>`).join('')}</ol><details data-review-detail="plan-source-${escape(run.id)}"><summary>查看此次固定材料</summary>${sourceHTML(c.session, run.evidence, run.source_status)}</details>`;
    }
    function renderPlan() {
      const run = reviewPlan.run; setHTML('review-plan-content', planHTML(run));
      $('review-plan-original-label').hidden = !run.source_status?.changed;
      $('review-plan-duplicate-label').hidden = !unknown(run);
      $('review-plan-send').disabled = c.busy() || Boolean(c.pending()) || Boolean(run.source_status?.unavailable) || ['pending', 'running', 'done', 'cancelled'].includes(run.status);
      if (run.source_status?.unavailable) $('review-plan-status').textContent = '历史材料不可用。请关闭后选择当前材料建立新计划。';
    }
    function openPlan(run) { reviewPlan = { sid: c.session.id, run }; $('review-plan-consent').checked = false; $('review-plan-original').checked = false; $('review-plan-duplicate').checked = false; $('review-plan-status').textContent = ''; renderPlan(); $('review-plan-dialog').showModal(); }
    async function prepare(regenerateOf) {
      const sid = c.session.id;
      const options = { scope: $('review-scope').value, card_types: [...document.querySelectorAll('[data-review-type]')].filter((x) => x.checked).map((x) => x.value), english_answers: $('review-english').checked, include_images: $('review-images').checked,
        seen_segments: (c.session.segments || []).map((x) => ({ id: x.id, version: x.version || 0 })),
        seen_slides: (c.session.slides || []).filter((x) => !x.removed && !x.removed_at).map((x) => ({ id: x.id, version: x.version || 1, interval_version: x.interval_version || 0, sha256: x.sha256, start: x.start, end: x.end ?? null, title: x.title || '' })),
        seen_marks: c.review.marks.filter((x) => x.active).map((x) => ({ id: x.id, revision: x.revision || 0 })),
      };
      if (!options.card_types.length) throw new Error('请至少选择一种卡片类型。');
      if (options.scope === 'range') { options.start = Number($('review-start').value); options.end = Number($('review-end').value); if (!Number.isFinite(options.start) || !Number.isFinite(options.end) || options.start < 0 || options.end <= options.start) throw new Error('请填写有效的起止秒数，结束时间应大于开始时间。'); }
      if (regenerateOf) options.regenerate_of = regenerateOf;
      await beforePrepare(); if (!current(sid)) return;
      const result = await c.prepare(options); if (result.current) openPlan(result.result);
      await poll();
    }
    function findArtifact(kind, id) { return c.review[kind === 'note' ? 'notes' : 'cards'].find((x) => x.id === id); }
    function defaultText(item, kind) {
      if (item.user_draft?.content != null) return item.user_draft.content;
      const ai = item.ai_content || {};
      return kind === 'card' ? [ai.front, ai.back, ai.english ? `英文答题表述（AI 建议）\n${ai.english}\n${ai.english_meaning || ''}` : '', ai.supplement ? `AI 补充\n${ai.supplement}` : ''].filter(Boolean).join('\n\n') : (ai.sections || []).map((x) => `${x.title}\n${x.body}${x.supplement ? `\nAI 补充\n${x.supplement}` : ''}`).join('\n\n');
    }
    function openEditor(kind, id, recovered) {
      const item = findArtifact(kind, id); if (!item) { toast('对应版本不在当前课堂中，草稿仍保留。', true); return; }
      const draft = recovered ? c.restoreDraft(recovered) : c.draft(kind, id);
      editor = { sid: c.session.id, kind, id, revision: draft?.revision ?? item.user_draft?.revision ?? 0, conflict: Boolean(draft?.conflict) };
      $('review-editor-title').textContent = `${kind === 'note' ? '笔记' : '卡片'} · 个人版本`;
      $('review-editor-content').value = draft?.content ?? defaultText(item, kind);
      $('review-editor-note').value = draft?.personal_note ?? item.user_draft?.personal_note ?? '';
      $('review-editor-status').textContent = draft?.conflict ? '另一页面已保存新版。你的输入保留；请对照后明确以当前版本为基础再保存。' : draft ? '已恢复本地草稿，尚未保存到课堂。' : '编辑自动保存到本机；AI 原稿始终保留。';
      $('review-editor-rebase').hidden = !editor.conflict;
      renderKeys.delete('review-editor-original');
      setHTML('review-editor-original', `<strong>AI 原始整理</strong>${rich(kind === 'card' ? JSON.stringify(item.ai_content, null, 2) : (item.ai_content?.sections || []).map((x) => `${x.title}\n${x.body}`).join('\n\n'))}<strong>目前保存的个人版本</strong>${rich(defaultText(item, kind))}`);
      $('review-editor-dialog').showModal();
    }
    function captureEditor() {
      if (!editor || !current(editor.sid)) return;
      c.writeDraft(editor.kind, editor.id, { revision: editor.revision, content: $('review-editor-content').value, personal_note: $('review-editor-note').value, conflict: editor.conflict });
    }
    async function saveEditor() {
      if (!editor || !current(editor.sid)) return; const captured = editor;
      clearTimeout(autosaveTimer); captureEditor();
      if (captured.conflict) { $('review-editor-status').textContent = '保存冲突：请先对照当前版本并确认，再保存保留的输入。'; return; }
      $('review-editor-status').textContent = '正在保存…';
      try {
        const result = await c.saveDraft(captured.kind, captured.id);
        if (editor !== captured || !current(captured.sid)) return;
        if (result) {
          captured.revision = result.user_draft.revision;
          const newer = c.draft(captured.kind, captured.id);
          $('review-editor-status').textContent = newer ? '上一部分已保存，正在继续保存较新的输入…' : '已保存到本机课堂。';
          if (newer) { clearTimeout(autosaveTimer); autosaveTimer = setTimeout(saveEditor, 400); }
        }
      } catch (error) {
        if (editor !== captured || !current(captured.sid)) return;
        captured.conflict = error.status === 409; $('review-editor-rebase').hidden = !captured.conflict;
        $('review-editor-status').textContent = `${error.message} 输入已保留，可从草稿恢复。`;
        if (captured.conflict) { await poll(); if (editor === captured) setHTML('review-editor-original', `<strong>目前保存的个人版本</strong>${rich(defaultText(findArtifact(captured.kind, captured.id), captured.kind))}`); }
      }
    }
    function compare(kind, id) {
      const item = findArtifact(kind, id); if (!item) return;
      const list = c.review[kind === 'note' ? 'notes' : 'cards'].filter((x) => kind === 'note' || x.id === id || x.ai_content?.type === item.ai_content?.type && JSON.stringify([...(x.ai_content?.source_ids || [])].sort()) === JSON.stringify([...(item.ai_content?.source_ids || [])].sort()));
      $('review-compare-select').innerHTML = list.map((x) => `<option value="${escape(x.id)}">${escape(x.ai_content?.title || x.id)} · ${escape(x.id)}</option>`).join('');
      $('review-compare-select').value = list.find((x) => x.id !== id)?.id || id;
      $('review-compare-dialog').dataset.kind = kind; $('review-compare-dialog').dataset.item = id;
      const paint = () => { const other = findArtifact(kind, $('review-compare-select').value); setHTML('review-compare-left', `<h3>正在查看 · ${escape(item.id)}</h3>${contentFor(item, kind)}<details><summary>AI 原始字段</summary><pre>${escape(JSON.stringify(item.ai_content, null, 2))}</pre></details>`); setHTML('review-compare-right', other ? `<h3>对照版本 · ${escape(other.id)}</h3>${contentFor(other, kind)}` : ''); };
      $('review-compare-select').onchange = paint; paint(); $('review-compare-dialog').showModal();
    }
    function packagePending() {
      try {
        // Keep each operation under its own key: two tabs must not overwrite
        // one another's restore/export receipt while a response is in flight.
        const keys = ['classroom-package-operation'];
        for (let i = 0; i < (storage?.length || 0); i++) { const key = storage.key(i); if (key?.startsWith('classroom-package-operation:')) keys.push(key); }
        for (const key of keys) { const value = JSON.parse(storage?.getItem(key) || 'null'); if (value) return { ...value, storageKey: key }; }
      } catch (_) {} return null;
    }
    function storePackage(value) { if (!storage) throw new Error('无法保存恢复凭据，暂不执行课堂包操作。'); value.storageKey = `classroom-package-operation:${value.request_id}`; storage.setItem(value.storageKey, JSON.stringify(value)); }
    function renderPackageRecovery() { $('review-package-recovery').hidden = !packagePending(); }
    async function openPackage(mode) {
      if (packageBusy) return;
      if (packagePending()) throw new Error('请先核对上次课堂包操作。');
      packagePlan = { mode, sid: c.session.id, preview: null };
      $('review-package-title').textContent = mode === 'export' ? '导出离线课堂包' : '恢复课堂副本';
      $('review-package-audio-label').hidden = mode !== 'export'; $('review-package-file-label').hidden = mode !== 'import';
      $('review-package-audio').checked = false; $('review-package-missing').checked = false; $('review-package-file').value = '';
      $('review-package-status').textContent = mode === 'export' ? '默认不带录音，未包含的录音将无法离线回听。请先保存编辑，并处理尚未上传的图片。' : 'ZIP 或旧 JSON 恢复为独立课堂，不覆盖现有课堂，不导入账号或恢复未完成 AI 请求。';
      $('review-package-content').textContent = ''; $('review-package-confirm').disabled = true;
      $('review-package-dialog').showModal();
      if (mode === 'export') await previewPackage();
    }
    async function previewPackage() {
      if (!packagePlan || packageBusy) return; const captured = packagePlan;
      packageBusy = true; $('review-package-confirm').disabled = true; $('review-package-preview').disabled = true; $('review-package-audio').disabled = true; $('review-package-file').disabled = true;
      try {
        const requestId = identifier();
        const file = $('review-package-file').files?.[0];
        if (captured.mode === 'import' && !file) throw new Error('请先选择 ZIP 课堂包或 JSON 记录。');
        if (file && file.size > 2 * 1024 ** 3) throw new Error('课堂包不能超过 2 GiB。');
        const preview = captured.mode === 'export' ? await api('/api/packages/preview', { method: 'POST', body: { session_id: captured.sid, include_audio: $('review-package-audio').checked }, timeout: 180000 }) : await api(`/api/packages/import/preview?request_id=${encodeURIComponent(requestId)}`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' }, timeout: 180000 });
        if (packagePlan !== captured || !current(captured.sid)) return;
        captured.preview = preview; $('review-package-missing').checked = false;
        setHTML('review-package-content', `<p><strong>${escape(preview.title || c.session.title)}</strong></p><p>预计 ${(Number(preview.estimated_bytes || 0) / (1024 * 1024)).toFixed(1)} MiB${preview.files_count != null ? ` · ${Number(preview.files_count)} 个文件` : ''}；实际 ZIP 大小可能不同。</p>${(preview.warnings || []).map((x) => `<p class="review-warning">${escape(x)}</p>`).join('')}${(preview.missing || []).map((x) => `<p class="review-warning">缺失 ${escape(x.kind)} ${escape(x.id)}：${escape(x.reason)}</p>`).join('')}`);
        $('review-package-missing-label').hidden = !(preview.missing || []).length; $('review-package-confirm').disabled = false;
      } finally { packageBusy = false; $('review-package-preview').disabled = false; $('review-package-audio').disabled = false; $('review-package-file').disabled = false; }
    }
    function download(url) { if (!/^\/api\/packages\/download\/[A-Za-z0-9_-]+(?:\?[^#]*)?$/.test(url || '')) throw new Error('下载地址无效。'); const a = document.createElement('a'); a.href = url; a.download = ''; document.body.appendChild(a); a.click(); a.remove(); }
    async function finishPackage(record, result) {
      storage?.removeItem(record.storageKey || `classroom-package-operation:${record.request_id}`); renderPackageRecovery();
      if (record.mode === 'export') { if (result.download_url) download(result.download_url); if (current(record.sid)) $('review-package-status').textContent = '课堂包已构建并校验，正在交给浏览器下载。'; }
      else { if (current(record.sid)) $('review-package-status').textContent = `已恢复为独立课堂 ${result.session_id}，可从历史课程选择。`; await poll(); }
    }
    async function confirmPackage() {
      if (packageBusy || !packagePlan?.preview) return; const captured = packagePlan;
      if ((captured.preview.missing || []).length && !$('review-package-missing').checked) throw new Error('存在缺失媒体，请先确认保留缺失说明后继续。');
      const record = { mode: captured.mode, sid: captured.sid, request_id: identifier(), preview_id: captured.preview.preview_id, allow_missing: $('review-package-missing').checked };
      storePackage(record); packageBusy = true; $('review-package-confirm').disabled = true; renderPackageRecovery();
      try { const result = await api(`/api/packages/${record.mode === 'export' ? 'export' : 'import'}`, { method: 'POST', body: { preview_id: record.preview_id, request_id: record.request_id, allow_missing: record.allow_missing }, timeout: 180000 }); await finishPackage(record, result); }
      catch (error) { if (error.status >= 400 && error.status < 500 && error.status !== 408) storage?.removeItem(record.storageKey); throw error; }
      finally { packageBusy = false; renderPackageRecovery(); }
    }
    async function recoverPackage() {
      const record = packagePending(); if (!record) return;
      try { const result = await api(`/api/packages/${record.mode === 'export' ? 'exports' : 'imports'}/${encodeURIComponent(record.request_id)}${record.mode === 'export' ? `?session_id=${encodeURIComponent(record.sid)}` : ''}`); if (result.found === false) { toast('暂未找到完成记录，凭据保留；不会自动再次恢复或导出。', true); return; } await finishPackage(record, result); }
      catch (error) { if (error.status === 404) { toast('暂未找到完成记录，凭据保留；请稍后再核对。', true); return; } throw error; }
    }
    $('review-open').addEventListener('click', () => show(true)); $('review-back').addEventListener('click', () => show(false));
    $('review-generate').addEventListener('click', () => safeAction(() => prepare()));
    $('review-scope').addEventListener('change', () => { $('review-range').hidden = $('review-scope').value !== 'range'; });
    $('review-english').addEventListener('change', () => { if (c.session) englishPreferences.set(c.session.id, { manual: true, checked: $('review-english').checked }); });
    $('review-recover').addEventListener('click', () => safeAction(async () => { await c.recover(); await poll(); }));
    $('review-retry-original').addEventListener('click', () => safeAction(async () => { await c.retryPending($('review-retry-consent').checked); $('review-retry-consent').checked = false; await poll(); }));
    $('review-plan-send').addEventListener('click', () => safeAction(async () => { if (!reviewPlan || !current(reviewPlan.sid)) return; if (!$('review-plan-consent').checked) throw new Error('请确认所列材料、AI 接入与完整批次计划。'); const captured = reviewPlan; const result = await c.send(captured.run, { confirm_original: $('review-plan-original').checked, confirm_duplicate: $('review-plan-duplicate').checked }, captured.run.status === 'prepared' ? 'send' : 'continue'); if (result.current && reviewPlan === captured) $('review-plan-dialog').close(); await poll(); }));
    $('review-note-select').addEventListener('change', render);
    for (const id of ['review-editor-content', 'review-editor-note']) $(id).addEventListener('input', () => { captureEditor(); $('review-editor-status').textContent = '草稿已暂存，等待自动保存…'; clearTimeout(autosaveTimer); autosaveTimer = setTimeout(saveEditor, 900); });
    $('review-editor-save').addEventListener('click', saveEditor);
    $('review-editor-rebase').addEventListener('click', () => { if (!editor) return; const item = findArtifact(editor.kind, editor.id); if (!item) return; editor.revision = item.user_draft?.revision || 0; editor.conflict = false; captureEditor(); $('review-editor-rebase').hidden = true; $('review-editor-status').textContent = '已明确采用当前版本号，输入保持不变。请点击保存个人版本。'; });
    $('review-editor-dialog').addEventListener('close', () => { clearTimeout(autosaveTimer); editor = null; });
    $('review-package-export').addEventListener('click', () => safeAction(() => openPackage('export')));
    $('review-package-import').addEventListener('click', () => safeAction(() => openPackage('import')));
    $('review-package-preview').addEventListener('click', () => safeAction(previewPackage));
    $('review-package-audio').addEventListener('change', () => { if (packagePlan) packagePlan.preview = null; safeAction(previewPackage); });
    $('review-package-file').addEventListener('change', () => { if (packagePlan) packagePlan.preview = null; safeAction(previewPackage); });
    $('review-package-confirm').addEventListener('click', () => safeAction(confirmPackage));
    $('review-package-recover').addEventListener('click', () => safeAction(recoverPackage));
    document.addEventListener('click', (event) => {
      const button = event.target.closest('[data-review-mark], [data-review-mark-edit], [data-review-run], [data-review-cancel], [data-review-new], [data-review-edit], [data-review-compare], [data-review-adopt], [data-review-jump], [data-review-slide], [data-review-restore-draft]'); if (!button || button.disabled) return;
      safeAction(async () => {
        const d = button.dataset;
        if (d.reviewMark) { const old = c.review.marks.find((x) => x.target_kind === d.reviewKind && x.target_id === d.reviewTarget && x.kind === d.reviewMark && x.active); await c.mark(d.reviewKind, d.reviewTarget, d.reviewMark, { active: !old, note: old?.note || '', resolved: old?.resolved || false }); await poll(); }
        else if (d.reviewMarkEdit) { const mark = c.review.marks.find((x) => x.id === d.reviewMarkEdit); if (!mark) return; $('review-mark-dialog').dataset.mark = mark.id; $('review-mark-dialog').dataset.sid = c.session.id; $('review-mark-dialog').dataset.revision = String(mark.revision || 0); $('review-mark-note').value = mark.note || ''; $('review-mark-resolved').checked = Boolean(mark.resolved); $('review-mark-resolved-label').hidden = mark.kind !== 'unclear'; $('review-mark-rebind').checked = false; $('review-mark-rebind-label').hidden = !mark.source_status?.changed; $('review-mark-dialog').showModal(); }
        else if (d.reviewRun) openPlan(c.review.runs.find((x) => x.id === d.reviewRun));
        else if (d.reviewCancel) { await c.cancel(c.review.runs.find((x) => x.id === d.reviewCancel)); await poll(); }
        else if (d.reviewNew) await prepare(d.reviewNew);
        else if (d.reviewEdit) openEditor(d.reviewEditKind, d.reviewEdit);
        else if (d.reviewCompare) compare(d.reviewEditKind, d.reviewCompare);
        else if (d.reviewAdopt) { await c.adopt(findArtifact('note', d.reviewAdopt)); await poll(); }
        else if (d.reviewJump) { show(false); jump(d.reviewJump); }
        else if (d.reviewSlide) { show(false); showSlide(d.reviewSlide); }
        else if (d.reviewRestoreDraft) { const draft = c.drafts().find((x) => x.key === d.reviewRestoreDraft); if (draft) openEditor(draft.kind, draft.id, draft); }
      });
    });
    $('review-mark-save').addEventListener('click', () => safeAction(async () => { const dialog = $('review-mark-dialog'); if (!current(dialog.dataset.sid)) return; const mark = c.review.marks.find((x) => x.id === dialog.dataset.mark); if (!mark) return; await c.mark(mark.target_kind, mark.target_id, mark.kind, { revision: Number(dialog.dataset.revision), active: true, note: $('review-mark-note').value, resolved: $('review-mark-resolved').checked, rebind: $('review-mark-rebind').checked }); if (current(dialog.dataset.sid)) dialog.close(); await poll(); }));
    $('review-mark-remove').addEventListener('click', () => safeAction(async () => { const dialog = $('review-mark-dialog'); if (!current(dialog.dataset.sid)) return; const mark = c.review.marks.find((x) => x.id === dialog.dataset.mark); if (!mark) return; await c.mark(mark.target_kind, mark.target_id, mark.kind, { revision: Number(dialog.dataset.revision), active: false, note: mark.note, resolved: mark.resolved }); if (current(dialog.dataset.sid)) dialog.close(); await poll(); }));
    root.addEventListener('storage', (event) => { if (event.key?.startsWith(draftPrefix) || event.key?.startsWith(pendingPrefix) || event.key?.startsWith('classroom-package-operation')) render(); });
    root.addEventListener('beforeunload', (event) => { if (c.drafts().some((x) => x.owner === tabId) || c.pending() || packageBusy) { event.preventDefault(); event.returnValue = ''; } });
    return {
      controller: c, show,
      sync(session = getSession()) {
        if (lastSession !== session?.id) {
          clearTimeout(autosaveTimer); editor = null; reviewPlan = null; packagePlan = null;
          for (const id of ['review-plan-dialog', 'review-editor-dialog', 'review-compare-dialog', 'review-mark-dialog', 'review-package-dialog']) $(id).close();
          renderKeys.clear(); lastSession = session?.id;
        }
        const preset = (getSnapshot()?.courses || []).find((x) => x.id === (session?.course_id || session?.settings?.course_id));
        const preference = englishPreferences.get(session?.id);
        $('review-english').checked = preference?.manual ? preference.checked : /微观|microeconom/i.test(`${preset?.name || ''} ${session?.title || ''}`);
        c.sync(session); render();
      },
      hasUnsaved() { return Boolean(c.pending() || c.drafts().some((x) => x.owner === tabId)); },
    };
  }
  return { createController, sourceHTML, markButtons, mount };
});
