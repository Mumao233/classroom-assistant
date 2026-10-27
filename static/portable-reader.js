(function () {
  'use strict';
  // No fetch, remote URL, eval or imported markup. A package is read-only.
  const root = document.getElementById('reader');
  const fallbackEscape = text => String(text ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const render = window.ReviewRender || {escape:fallbackEscape, render:text => `<p>${fallbackEscape(text)}</p>`, hydrate:() => {}, time:value => String(value ?? 0)};
  let payload;
  try { payload = JSON.parse(document.getElementById('classroom-data').textContent); }
  catch (_) { root.textContent = '课堂元数据无法读取，请打开 notes.md 或 session.json 核对。'; return; }
  const session = payload.session || {}, media = payload.manifest?.media || [];
  const list = value => Array.isArray(value) ? value : [];
  const title = document.getElementById('title');
  title.textContent = session.title || '离线课堂';
  document.getElementById('summary').textContent = '只读副本 · 图片、原文和答案使用包内版本 · 不连接 AI 或网络；修改请导入课间后操作。';
  const esc = render.escape;
  const text = value => render.render(value ?? '');
  const time = value => render.time(value || 0);
  const range = item => `${time(item.start)}–${item.end == null ? '未指定结束' : time(item.end)}`;
  // Stable ASCII anchors avoid fragment decoding ambiguities and duplicate IDs
  // when the same historical evidence appears under several cards.
  const sourceAnchor = (kind, id) => `source-${kind}-${Array.from(String(id ?? '')).map(char => char.codePointAt(0).toString(16)).join('-')}`;
  function sourceLink(kind, id) {
    const sources = kind === 'segment' ? list(session.segments) : list(session.slides);
    return sources.some(item => item.id === id) ? `<p><a href="#${sourceAnchor(kind, id)}">跳到本包当前${kind === 'segment' ? '原文' : '课件'} ${esc(id)}</a></p>` : `<p class="missing">当前${kind === 'segment' ? '原文' : '课件'}中没有 ${esc(id)}；当时快照仍保留在下方。</p>`;
  }
  function sourceState(item) {
    const state = item.source_status || {};
    return `${state.changed || item.stale ? `<p class="missing">依据已变化，待核对；此处保留生成时的旧版本。${esc(item.stale_reason || '')}</p>` : ''}${state.new_material ? '<p class="missing">新增内容尚未纳入本版本。</p>' : ''}${state.unavailable ? '<p class="missing">部分历史依据不可用，请核对媒体缺失说明。</p>' : ''}${list(state.messages).map(message => `<p class="missing">${esc(message)}</p>`).join('')}`;
  }
  const safeAsset = value => typeof value === 'string' && /^assets\/[0-9a-f]{64}\.(png|jpg|wav)$/.test(value) ? value : null;
  const availability = item => ({not_included:'本包未携带', deleted:'原媒体已删除', missing:'媒体缺失', corrupt:'媒体损坏', invalid:'媒体无效', available:'包内文件'})[item?.availability] || '媒体不可用';
  function sourceImage(slide, anchor = false) {
    const asset = media.find(item => item.kind === 'image' && item.original_path === slide.path && item.sha256 === slide.sha256);
    const src = asset?.availability === 'available' ? safeAsset(asset.archive_path) : null;
    return `<figure${anchor ? ` id="${sourceAnchor('slide', slide.id)}"` : ''}><figcaption>[${esc(slide.id)}] ${esc(slide.title || '课件图片')} · v${esc(slide.version || 1)} · ${esc(range(slide))}${slide.removed || slide.removed_at ? ' · 已从课堂选择移除' : ''}</figcaption>${src ? `<a href="${src}" target="_blank" rel="noopener"><img loading="lazy" src="${src}" alt="${esc(slide.title || slide.id)}"></a>` : `<p class="missing">${esc(asset?.reason || availability(asset))}。保留图片编号与原时间范围。</p>`}<small>SHA256 ${esc(slide.sha256 || '未记录')}</small></figure>`;
  }
  let audioCount = 0;
  function sourceAudio(segment) {
    if (segment.audio_seq == null) return '<p class="muted">原音：没有可验证的对应录音（手动补充或旧记录）。</p>';
    const asset = media.find(item => item.kind === 'audio' && item.id === segment.audio_seq);
    const src = asset?.availability === 'available' ? safeAsset(asset.archive_path) : null;
    if (!src) return `<p class="missing">原音 ${esc(segment.audio_seq)}：${esc(asset?.reason || availability(asset))}</p>`;
    const offset = Math.max(0, Math.min(Number(asset.duration) || 0, Number(segment.start) - Number(asset.start)));
    const id = `audio-${++audioCount}`;
    return `<div class="audio-source"><p>原音 ${esc(segment.audio_seq)} · 片段内 ${offset.toFixed(2)} 秒</p><audio id="${id}" controls preload="none" src="${src}"></audio><button type="button" data-seek="${id}" data-offset="${offset}">从对应位置回听</button></div>`;
  }
  function segmentHTML(segment, machine = true, anchor = false) {
    return `<article class="source"${anchor ? ` id="${sourceAnchor('segment', segment.id)}"` : ''}><p class="eyebrow">教师材料 · 机器转写可能有误</p><h3>[${esc(segment.id)}] ${esc(range(segment))}</h3><small>来源 ${esc(segment.source || '未记录')} · 原文 v${esc(segment.version || 0)}${segment.needs_review ? ' · 待核对' : ''}</small>${text(segment.text)}${machine && segment.machine_text != null ? `<details><summary>保留的机器原稿</summary>${text(segment.machine_text)}</details>` : ''}${sourceAudio(segment)}</article>`;
  }
  function evidenceHTML(evidence) {
    if (!evidence || !list(evidence.segments).length && !list(evidence.slides).length) return '<p class="muted">此旧记录没有完整冻结依据，不能补认当前原文为原答案依据。</p>';
    return `<details class="evidence"><summary>回看生成时的冻结依据 · ${list(evidence.segments).length} 段原文 / ${list(evidence.slides).length} 张图片</summary><p class="muted">保留生成当时的内容、版本和范围；不会替换成后来修改的材料。</p>${evidence.course_snapshot ? `<details><summary>当时课程预设（辅助理解，不代表老师原话）</summary>${text(JSON.stringify(evidence.course_snapshot, null, 2))}</details>` : ''}${list(evidence.segments).map(segment => sourceLink('segment', segment.id) + segmentHTML(segment, true)).join('')}${list(evidence.slides).map(slide => sourceLink('slide', slide.id) + sourceImage(slide)).join('')}</details>`;
  }
  function adopted(item) {
    const draft = item.user_draft || {};
    const changed = draft.revision > 0 && draft.content != null;
    return {content:changed ? draft.content : item.ai_content || {}, label:changed ? '个人采用的编辑版' : 'AI 整理（依据课堂材料）', personal:draft.personal_note || '', changed};
  }
  function missingHTML(value) { return list(value).length ? `<aside class="missing"><strong>课堂记录不足</strong>${list(value).map(item => text(item)).join('')}</aside>` : ''; }
  function cardHTML(item) {
    const chosen = adopted(item), content = typeof chosen.content === 'string' ? {front:chosen.content} : chosen.content;
    return `<article class="card"><p class="eyebrow">${esc(chosen.label)} · ${esc(item.id)}</p>${sourceState(item)}<h3>${esc(content.title || '复习卡片')}</h3><strong>问题</strong>${text(content.front)}<strong>解释／步骤</strong>${text(content.back)}${content.supplement ? `<aside class="supplement"><strong>AI 补充（不代表老师讲过）</strong>${text(content.supplement)}</aside>` : ''}${content.english ? `<aside><strong>AI 建议英文答题表述</strong>${text(content.english)}${text(content.english_meaning)}</aside>` : ''}${missingHTML(content.missing)}${chosen.personal ? `<aside class="personal"><strong>个人备注</strong>${text(chosen.personal)}</aside>` : ''}${chosen.changed ? `<details><summary>保留的 AI 原版</summary>${text(JSON.stringify(item.ai_content, null, 2))}</details>` : ''}${evidenceHTML(item.evidence)}</article>`;
  }
  function noteHTML(item) {
    const chosen = adopted(item), content = chosen.content;
    const body = typeof content === 'string' ? text(content) : list(content.sections).map(section => `<section><h3>${esc(section.title || section.section || '笔记')}</h3>${text(section.body)}${section.supplement ? `<aside class="supplement"><strong>AI 补充</strong>${text(section.supplement)}</aside>` : ''}${missingHTML(section.missing)}</section>`).join('');
    const unresolved = list(item.unresolved_marks).map(mark => `<p>${esc(mark.target_id)}：${esc(mark.note || '标记为没懂，尚未手动确认理解。')}</p>`).join('');
    return `<article class="card"><p class="eyebrow">${esc(chosen.label)} · ${esc(item.id)}</p>${sourceState(item)}${body}${unresolved ? `<aside class="missing"><strong>生成笔记时仍未解决的疑问</strong>${unresolved}</aside>` : ''}${chosen.personal ? `<aside class="personal"><strong>个人笔记</strong>${text(chosen.personal)}</aside>` : ''}${chosen.changed ? `<details><summary>保留的 AI 原版</summary>${text(JSON.stringify(item.ai_content, null, 2))}</details>` : ''}${evidenceHTML(item.evidence)}</article>`;
  }
  function runHTML(run) {
    const labels = {prepared:'待确认',pending:'等待',running:'生成中',paused:'已暂停',partial:'部分完成',done:'完成',error:'失败',unknown:'结果未知',cancelled:'已取消'};
    const coverage = run.coverage || {}, batches = list(run.batches);
    const completed = Number(coverage.completed_batches ?? batches.filter(batch => batch.status === 'done').length) || 0;
    const total = Number(coverage.batch_count ?? batches.length) || 0;
    const bounds = list(coverage.range).length ? ` · ${time(coverage.range[0])}–${time(coverage.range[1])}` : '';
    return `<article class="card"><h3>生成任务 ${esc(run.id)} · ${esc(labels[run.status] || run.status || '状态未记录')}</h3><p>已完成 ${completed} / ${total} 批${bounds}；${Number(coverage.segment_count) || 0} 段原文、${Number(coverage.slide_count) || 0} 张图片。</p>${sourceState(run)}${run.error ? `<p class="missing">${esc(run.error)}</p>` : ''}${run.repeat_billing_required || run.result_unknown || run.status === 'unknown' ? '<p class="missing">请求超时、无法确定服务商是否已处理。可能已经计费；离线页不会重新请求。</p>' : ''}${Number(coverage.pending_audio) ? `<p class="missing">生成时还有 ${Number(coverage.pending_audio)} 段音频未完成转写，未纳入本次。</p>` : ''}${list(coverage.excluded).map(item => `<p class="missing">未纳入：${esc(typeof item === 'string' ? item : JSON.stringify(item))}</p>`).join('')}${coverage.claim ? `<p class="muted">${esc(coverage.claim)}</p>` : ''}<details><summary>各批次状态（离线只读）</summary><ol>${batches.map(batch => `<li>${esc(labels[batch.status] || batch.status || '状态未记录')}${batch.error ? `：${esc(batch.error)}` : ''}${list(batch.context_warnings).map(message => `<p class="missing">${esc(message)}</p>`).join('')}</li>`).join('')}</ol></details></article>`;
  }
  function answerHTML(item, group) {
    const status = item.result_unknown ? '结果未知；未自动重新请求' : item.status || '未记录';
    return `<article class="card"><p class="eyebrow">AI 解答 · ${esc(item.id)} · ${esc(status)}</p><h3>${esc(group ? `意群 ${item.id}` : item.question || '问题')}</h3>${group ? `<details><summary>教师材料摘录</summary>${text(item.text)}</details>` : ''}${text(item.analysis || item.answer || item.error || '尚未完成')}${item.stale ? `<p class="missing">待核对：${esc(item.stale_reason || '依据已变化')}</p>` : ''}${item.streaming_text && item.stream_status !== 'complete' ? `<details><summary>未完成的 AI 草稿（未作为正式结果）</summary>${text(item.streaming_text)}</details>` : ''}${evidenceHTML(item.evidence)}</article>`;
  }
  const review = session.review || {};
  const selected = list(review.notes).find(item => item.id === review.selected_note_id);
  const marks = list(review.marks).filter(item => item.active !== false);
  const section = (id, heading, content, empty) => `<section id="${id}"><h2>${heading}</h2>${content || `<p class="muted">${empty}</p>`}</section>`;
  root.innerHTML = section('transcripts', '课堂原文', list(session.segments).map(segment => segmentHTML(segment, true, true)).join(''), '尚无课堂原文。')
    + section('slides', '课件图片', list(session.slides).map(slide => sourceImage(slide, true)).join(''), '尚无课件图片。')
    + section('marks', '本课标记', marks.map(mark => `<article class="card"><p>${esc(mark.kind === 'important' ? '重点' : '没懂')} · ${esc(mark.target_id)} · ${mark.resolved ? '已理解' : '待核对'}</p>${sourceState(mark)}${text(mark.note)}${evidenceHTML(mark.evidence)}</article>`).join(''), '没有已保存标记。')
    + section('review-progress', '复习生成状态与覆盖', list(review.runs).map(runHTML).join(''), '没有复习生成任务记录。')
    + section('cards', '复习卡片', list(review.cards).map(cardHTML).join(''), '尚无复习卡片。')
    + section('notes', '当前采用的课后笔记', selected ? noteHTML(selected) : '', '尚未选择采用的课后笔记；候选原稿仍保存在 session.json。')
    + section('answers', 'AI 解答', list(session.groups).map(item => answerHTML(item, true)).join('') + list(session.questions).map(item => answerHTML(item, false)).join(''), '尚无 AI 解答。')
    + section('revisions', '课后独立修订', list(session.revisions).map(revision => `<details><summary>${esc(revision.id)} · ${esc(revision.status)}（保留原课堂）</summary>${list(revision.segments).map(segment => segmentHTML(segment)).join('')}</details>`).join(''), '尚无独立修订。')
    + '<footer>本地课堂包 · session.json 保留完整记录与 AI 原始结果 · notes.md 可独立阅读 · 导入时只恢复数据与媒体，不执行包内网页。</footer>';
  root.addEventListener('click', event => {
    const button = event.target.closest('button[data-seek]');
    if (!button || button.disabled) return;
    const audio = document.getElementById(button.dataset.seek), offset = Number(button.dataset.offset) || 0;
    if (!audio) { button.disabled = true; button.textContent = '录音不可用'; return; }
    const play = () => { audio.currentTime = Math.min(offset, Number.isFinite(audio.duration) ? Math.max(0, audio.duration - .01) : offset); audio.play().catch(() => {}); };
    if (audio.readyState >= 1) play();
    else { audio.addEventListener('loadedmetadata', play, {once:true}); audio.load(); }
  });
  root.addEventListener('error', event => {
    if (!['IMG', 'AUDIO'].includes(event.target.tagName)) return;
    if (event.target.tagName === 'AUDIO') for (const button of root.querySelectorAll('button[data-seek]')) {
      if (button.dataset.seek === event.target.id) { button.disabled = true; button.textContent = '录音不可用'; }
    }
    const note = document.createElement('p'); note.className = 'missing'; note.textContent = '包内媒体无法打开。文件可能未完整解压或已损坏；编号与文字仍保留。';
    event.target.replaceWith(note);
  }, true);
  render.hydrate(root);
})();
