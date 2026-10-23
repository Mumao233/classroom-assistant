'use strict';

(() => {
  const $ = (selector) => document.querySelector(selector);
  const SAMPLE_RATE = 16000;
  const MAX_QUEUE = 60;
  const state = {
    snapshot: null, session: null, selectedGroup: null, connected: false,
    pollRunning: false, pollPromise: null, settingsDirty: false, settingsTimer: null,
    settingsPromise: null, saveGeneration: 0, uploadQueue: [], uploadPaused: false,
    uploadPromise: null, db: null, queueLoaded: false, capture: null,
    captureMode: 'idle', clockSeconds: 0, flushResolvers: new Map(),
    finalizing: new Set(), switching: false, renderKeys: {}, toastTimer: null,
    focusMode: false, followLive: true, courseAction: 'create', editingSegment: null,
    modeSaving: false, selectedCourseBusy: false, glossaryCourseId: null,
    soundcheck: null, captureKind: 'classroom', flushing: false, retranscribeSubmitting: false, glossarySubmitting: false,
  };

  const escapeHTML = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const timecode = (seconds = 0) => {
    const time = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(time / 3600);
    return `${hours ? `${hours}:` : ''}${String(Math.floor(time / 60) % 60).padStart(2, '0')}:${String(time % 60).padStart(2, '0')}`;
  };
  const statusLabel = (value) => ({ pending: '待处理', queued: '排队中', paused: '已暂停', running: '处理中', processing: '处理中', done: '已完成', completed: '已完成', error: '未完成', failed: '未完成', saved: '已归档', disabled: '未开启分析', skipped: '已略过', merged: '已合并' })[value] || '已归档';
  const isPending = (value) => ['pending', 'queued', 'running', 'processing'].includes(value);
  const isDemo = (session = state.session) => Boolean(session?.demo || session?.is_demo || session?.settings?.demo || session?.source === 'demo');

  function toast(message, error = false, duration = 5000) {
    clearTimeout(state.toastTimer);
    const element = $('#toast');
    element.textContent = message;
    element.classList.toggle('error', error);
    element.hidden = false;
    state.toastTimer = setTimeout(() => { element.hidden = true; }, duration);
  }

  async function api(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), options.timeout || 30000);
    const headers = { ...options.headers };
    let body = options.body;
    if (body && !(body instanceof ArrayBuffer) && !(body instanceof Blob) && typeof body !== 'string') {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }
    try {
      const response = await fetch(path, { ...options, body, headers, signal: controller.signal, cache: 'no-store' });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        const detail = typeof result.detail === 'string' ? result.detail : result.detail ? JSON.stringify(result.detail) : `请求失败（${response.status}）`;
        const error = new Error(detail);
        error.status = response.status;
        throw error;
      }
      return options.raw ? response : response.status === 204 ? null : response.json();
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('本地服务响应超时，可重新打开启动课间。');
      if (error instanceof TypeError) throw new Error('无法连接本地服务，请双击启动课间后重试。');
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }

  // Render only escaped text. AI replies never become executable HTML.
  function markdown(value) {
    if (value == null || value === '') return '';
    if (typeof value === 'object') {
      if (Array.isArray(value)) return `<ul>${value.map((item) => `<li>${markdown(item)}</li>`).join('')}</ul>`;
      return Object.entries(value).map(([key, item]) => `<h4>${escapeHTML(key)}</h4>${markdown(item)}`).join('');
    }
    const inline = (text) => escapeHTML(text).replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>');
    const lines = String(value).replace(/\r\n/g, '\n').split('\n');
    const result = [];
    let list = null, code = null, paragraph = [];
    const flushParagraph = () => { if (paragraph.length) result.push(`<p>${paragraph.map(inline).join('<br>')}</p>`); paragraph = []; };
    const closeList = () => { if (list) result.push(`</${list}>`); list = null; };
    for (const line of lines) {
      if (/^\s*```/.test(line)) {
        flushParagraph(); closeList();
        if (code) { result.push(`<pre><code>${escapeHTML(code.join('\n'))}</code></pre>`); code = null; } else code = [];
        continue;
      }
      if (code) { code.push(line); continue; }
      if (!line.trim()) { flushParagraph(); closeList(); continue; }
      const heading = line.match(/^#{1,6}\s+(.+)$/);
      const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (heading) { flushParagraph(); closeList(); result.push(`<h4>${inline(heading[1])}</h4>`); }
      else if (bullet || numbered) {
        flushParagraph();
        const type = bullet ? 'ul' : 'ol';
        if (type !== list) { closeList(); list = type; result.push(`<${type}>`); }
        result.push(`<li>${inline((bullet || numbered)[1])}</li>`);
      } else { closeList(); paragraph.push(line); }
    }
    flushParagraph(); closeList();
    if (code) result.push(`<pre><code>${escapeHTML(code.join('\n'))}</code></pre>`);
    return result.join('');
  }

  function replaceList(selector, html, key) {
    if (state.renderKeys[selector] === key) return;
    const element = $(selector);
    // Do not interrupt a user listening to a source clip when a poll receives
    // new transcript text. The next poll refreshes it once playback is paused.
    if ([...element.querySelectorAll('audio')].some((audio) => !audio.paused && !audio.ended)) return;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 70;
    const scroll = element.scrollTop;
    element.innerHTML = html;
    state.renderKeys[selector] = key;
    const typing = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName) || Boolean(document.querySelector('dialog[open]'));
    if (nearBottom && state.followLive && !typing) element.scrollTop = element.scrollHeight;
    else element.scrollTop = scroll;
  }

  const emptyTranscript = $('#transcript-list').innerHTML;
  const emptyGroups = $('#groups-list').innerHTML;
  const emptyQuestions = $('#questions-list').innerHTML;

  function render() {
    if (!state.session) return;
    const session = state.session;
    const segments = session.segments || [];
    const groups = session.groups || [];
    const visibleSegments = state.focusMode ? segments.slice(-2) : segments;
    const visibleGroups = state.focusMode ? groups.slice(-1) : groups;
    const questions = session.questions || [];
    $('#demo-banner').hidden = !isDemo();
    const sessions = state.snapshot.sessions || [];
    const sessionsKey = JSON.stringify(sessions.map((item) => [item.id, item.title, item.demo, item.is_demo]));
    if (state.renderKeys.sessions !== sessionsKey) {
      $('#session-select').innerHTML = sessions.map((item) => `<option value="${escapeHTML(item.id)}">${escapeHTML(item.title || '未命名课程')}${isDemo(item) ? ' · 演示' : ''}</option>`).join('');
      state.renderKeys.sessions = sessionsKey;
    }
    $('#session-select').value = session.id;
    if (!state.settingsDirty && !state.settingsPromise) {
      if (document.activeElement !== $('#course-title')) $('#course-title').value = session.title || '';
      if (document.activeElement !== $('#course-terms')) $('#course-terms').value = Array.isArray(session.settings?.terms) ? session.settings.terms.join(', ') : session.settings?.terms || '';
      $('#auto-analyze').checked = Boolean(session.settings?.auto_analyze);
    }
    $('#segment-count').textContent = segments.length;
    $('#group-count').textContent = groups.length;
    let flaggedAudio = (session.audio || []).filter((item) => !item.deleted && (['error', 'failed'].includes(item.status) || item.warning || ['no_speech', 'no_signal', 'suspected_hallucination'].includes(item.reason)));
    if (state.focusMode) flaggedAudio = flaggedAudio.slice(-1);
    const replay = (seq) => seq == null || session.audio_deleted_at || !(session.audio || []).some((item) => item.seq === seq && !item.deleted) ? '' : `<details class="audio-replay"><summary>回听原音频</summary><audio controls preload="none" src="/api/audio/${encodeURIComponent(seq)}?session_id=${encodeURIComponent(session.id)}">浏览器不支持音频播放。</audio></details>`;
    const reasonLabels = { no_speech: '未识别到清晰语音。', no_signal: '这段音频几乎没有声音。', suspected_hallucination: '这段识别结果可能不可靠，已暂不作为课堂原文。' };
    const audioErrorsHTML = flaggedAudio.map((item) => `<article class="transcript-item audio-error"><div class="transcript-meta"><span class="timecode">${timecode(item.start)} — ${timecode(Number(item.start) + Number(item.duration))}</span><span>音频已保存在本机</span></div><p class="analysis-error">${escapeHTML(item.error || item.warning || reasonLabels[item.reason] || '请核对这段录音。')}</p>${!segments.some((segment) => segment.audio_seq === item.seq) ? `<button class="text-button" data-retry-audio="${escapeHTML(item.seq)}">重试这段转写</button>` : ''}${replay(item.seq)}</article>`).join('');
    const transcriptHTML = visibleSegments.map((segment) => `<article class="transcript-item${segment.needs_review ? ' needs-review' : ''}" id="segment-${escapeHTML(segment.id)}"><div class="transcript-meta"><span class="timecode">${timecode(segment.start)} — ${timecode(segment.end)}</span><span>${escapeHTML(segment.id)}</span><span class="source-tag">${segment.source === 'manual' || segment.source === 'text' ? '手动补充' : segment.source === 'demo' ? '演示原文' : '音频转写'}</span>${segment.edited ? '<span class="edited-badge">已修改</span>' : ''}</div>${segment.needs_review ? '<p class="review-note">这段识别需要核对，请回听原音频</p>' : ''}<p class="transcript-text">${escapeHTML(segment.text)}</p><div class="transcript-actions">${replay(segment.audio_seq)}${!isDemo() ? `<button class="text-button" data-edit-segment="${escapeHTML(segment.id)}">修改原文</button>` : ''}</div>${segment.edited && segment.machine_text != null ? `<details class="original-text"><summary>保留的机器原稿</summary><p>${escapeHTML(segment.machine_text)}</p></details>` : ''}</article>`).join('');
    replaceList('#transcript-list', transcriptHTML + audioErrorsHTML || emptyTranscript, `${session.id}:${state.focusMode}:${session.audio_deleted_at}:${JSON.stringify(segments)}:${JSON.stringify(flaggedAudio)}:${JSON.stringify((session.audio || []).map((item) => [item.seq, item.deleted]))}`);
    const groupsHTML = visibleGroups.map((group) => {
      const index = groups.indexOf(group);
      const currentSource = (group.segment_ids || []).map((id) => segments.find((segment) => segment.id === id)?.text || '').join(' ');
      const source = group.stale ? currentSource || group.text : group.text || currentSource;
      const analysis = group.analysis || group.summary;
      const pending = isPending(group.status);
      const error = group.error;
      let content = analysis ? `<div class="analysis-content">${markdown(analysis)}</div>` : error ? `<p class="analysis-error">${escapeHTML(error)}</p>` : `<p class="analysis-pending">${pending ? '已加入处理队列，完成后会显示在这里。你可以继续听课。' : '原文已按意群保存。可以针对这一段提问，或复制给 Codex。'}</p>`;
      if (group.partial) content = '<p class="review-note">阶段整理：老师可能仍在继续讲解。</p>' + content;
      const retry = (group.stale && !pending || ['saved', 'error', 'failed', 'disabled', 'skipped'].includes(group.status)) && !isDemo() ? `<button class="text-button" data-retry-group="${escapeHTML(group.id)}">${group.stale ? '重新分析' : error ? '重试分析' : '分析本段'}</button>` : '';
      return `<article class="group-card${state.selectedGroup === group.id ? ' selected' : ''}" id="group-${escapeHTML(group.id)}"><div class="group-topline"><strong>意群 ${String(index + 1).padStart(2, '0')}</strong><div class="group-meta"><span class="timecode">${timecode(group.start)}</span><span class="group-state ${escapeHTML(group.status || '')}">${escapeHTML(statusLabel(group.status))}</span></div></div>${group.stale ? `<p class="stale-note">${escapeHTML(group.stale_reason || '原文已修改，以下解释可能需要更新。')} 可点击「重新分析」。</p>` : ''}<blockquote class="group-source">${escapeHTML(source)}</blockquote>${content}<div class="group-bottom"><span>${escapeHTML(group.id)} · ${(group.segment_ids || []).length} 段原文</span><span class="group-actions">${retry}<button class="text-button" data-group="${escapeHTML(group.id)}">基于本段提问 ↗</button></span></div></article>`;
    }).join('');
    replaceList('#groups-list', groupsHTML || emptyGroups, `${session.id}:${state.focusMode}:${state.selectedGroup}:${JSON.stringify(groups)}:${JSON.stringify(segments.map((segment) => [segment.id, segment.text]))}`);
    const questionsHTML = questions.map((question) => `<article class="question-block"><p class="user-question">${escapeHTML(question.question)}</p><div class="answer-label">${isDemo() ? '示例回答 · 非 AI 实时生成' : 'CODEX · 随堂解答'}${question.group_id ? ` · ${escapeHTML(question.group_id)}` : ''}</div>${question.stale ? `<p class="stale-note">本次问答基于提问时的原文，之后相关内容已有修改。${question.answer ? '原回答保留供对照。' : ''}${question.stale_reason ? ` ${escapeHTML(question.stale_reason)}` : ''}</p>` : ''}<div class="question-answer">${question.answer ? `<div class="analysis-content">${markdown(question.answer)}</div>` : question.error ? `<p class="analysis-error">${escapeHTML(question.error)}</p><button class="text-button" data-retry-question="${escapeHTML(question.id)}">重试回答</button>` : `<p class="analysis-pending">${isPending(question.status) ? '正在等待 Codex 解答，你可以继续提问…' : '暂未获得回答。'}</p>`}</div></article>`).join('');
    replaceList('#questions-list', questionsHTML || emptyQuestions, `${session.id}:${JSON.stringify(questions)}`);
    renderEngineStatus();
    renderQueue();
    renderCapture();
    renderCourseTools();
    renderSoundcheck();
    renderAfterClass();
    renderAnalysisMode();
    updateCaptureHealth();
  }

  function renderEngineStatus() {
    const statuses = state.snapshot?.status || {};
    for (const [key, label] of [['asr', '本机转写'], ['codex', 'Codex']]) {
      const info = statuses[key] || {};
      const ready = info.ready === true || info.available === true || info.authenticated === true || info.logged_in === true || ['ready', 'connected', 'authenticated', 'ok'].includes(info.status);
      const error = info.error || info.last_error || info.status === 'error' || info.status === 'unavailable';
      const element = $(`#${key}-status`);
      const detail = info.message || info.detail || info.error || info.last_error || info.warning || '';
      const model = key === 'asr' ? info.active_model_name || info.model_name || info.model : '';
      const device = info.active_device || info.device;
      const stateText = key === 'codex' ? info.inference_verified ? '调用已验证' : info.logged_in ? '已登录 · 待验证调用' : info.installed === false ? '未安装' : '待登录' : ready ? `可用 · ${(device || '本机').toUpperCase()}` : error ? '需检查' : info.loading || info.status === 'loading' ? '准备中' : '待就绪';
      element.textContent = `${label} · ${stateText}${model ? ` / ${model}` : ''}`;
      element.title = [detail, device && `运行设备：${device}`, info.pending_switch && '模式请求已保存，将在下一段音频切换。', key === 'codex' && info.logged_in === false && '请在启动终端运行 codex login', info.last_elapsed != null && `最近一段转写耗时：${Number(info.last_elapsed).toFixed(1)} 秒`, info.last_inference_seconds != null && `最近一次 Codex 耗时：${Number(info.last_inference_seconds).toFixed(1)} 秒`].filter(Boolean).join('\n');
      element.className = `status-pill ${error ? 'error' : ready ? 'ready' : 'warning'}`;
    }
    const jobs = state.snapshot?.jobs || [];
    const audioJobs = state.snapshot?.queue?.audio ?? jobs.filter((job) => ['audio', 'asr', 'transcribe'].includes(job.kind) && isPending(job.status)).length;
    const aiJobs = state.snapshot?.queue?.ai ?? jobs.filter((job) => !['audio', 'asr', 'transcribe'].includes(job.kind) && isPending(job.status)).length;
    $('#ai-queue').textContent = aiJobs ? `AI 待处理 ${aiJobs}` : '';
    const pending = [];
    if (audioJobs) pending.push(`本机转写 ${audioJobs} 项`);
    if (aiJobs) pending.push(`Codex ${aiJobs} 项`);
    const lag = Number(state.snapshot?.queue?.lag_seconds) || 0;
    if (lag > 0) pending.push(`转写落后约 ${Math.ceil(lag)} 秒`);
    $('#processing-status').textContent = pending.length ? `${pending.join(' · ')}，完成后自动显示` : '音频 → 本机转写 → 意群 → Codex';
    const errors = jobs.filter((job) => ['error', 'failed'].includes(job.status));
    if (errors.length && !pending.length) $('#processing-status').textContent = `${errors.length} 项处理失败，可在对应记录中重试`;
    if (errors.length) $('#processing-status').title = errors.map((job) => `${job.kind}: ${job.error || '未知错误'}`).join('\n');
    else $('#processing-status').title = '';
    const notices = [];
    const asr = statuses.asr || {};
    if (asr.warning) notices.push(asr.warning);
    if (asr.last_error) notices.push(`转写提示：${asr.last_error}`);
    if (asr.last_elapsed != null) notices.push(`最近一段转写用时 ${Number(asr.last_elapsed).toFixed(1)} 秒`);
    if (Array.isArray(state.snapshot?.warnings)) notices.push(...state.snapshot.warnings);
    $('#runtime-note').textContent = notices.join(' · ');
    $('#runtime-note').hidden = notices.length === 0;
    const modes = { auto: '自动', battery: '电池省电', quality: '插电高质量' };
    if (!state.modeSaving && document.activeElement !== $('#runtime-mode')) $('#runtime-mode').value = asr.requested_mode || 'auto';
    const power = asr.on_ac_power === true ? '已插电' : asr.on_ac_power === false ? '电池供电' : '电源状态未知';
    const activeModel = asr.active_model_name || asr.model_name;
    const activeDevice = asr.active_device || asr.device;
    $('#actual-mode').textContent = asr.pending_switch ? `${power} · 当前 ${activeModel || '模型未加载'} / ${(activeDevice || '待确认').toUpperCase()}，下一段切换` : asr.model_loaded === false ? `${power} · 将用 ${activeModel || '待确认模型'} / ${(activeDevice || '待确认').toUpperCase()}（首次使用时加载）` : `${power} · 实际${modes[asr.effective_mode] || asr.effective_mode || '模式待确认'}${activeModel ? ` / ${activeModel}` : ''}`;
    $('#actual-mode').title = `请求模式：${modes[asr.requested_mode] || '自动'}。${asr.warning || ''}`;
    const ai = state.snapshot?.ai || {};
    $('#ai-recovery').hidden = !ai.paused;
    $('#ai-pause-title').textContent = ({ network: '网络不可用，AI 已暂停', quota: '额度不足，AI 已暂停', auth: '需要重新登录 Codex', timeout: 'AI 响应超时，已暂停' })[ai.kind] || 'AI 已暂停';
    $('#ai-pause-reason').textContent = ai.reason || '尚未分析的课堂内容和问题已保留。';
  }

  function renderAnalysisMode() {
    const enabled = $('#auto-analyze').checked;
    $('#analysis-mode-label').textContent = enabled ? '逐段解释' : '只在提问时分析';
    $('#analysis-mode-hint').textContent = enabled ? '积累完整上下文后整理' : '原文和意群仍会保存';
  }

  function currentCourse() {
    const id = state.session?.course_id || state.session?.settings?.course_id;
    return (state.snapshot?.courses || []).find((course) => course.id === id);
  }

  function renderCourseTools() {
    const courses = state.snapshot?.courses || [];
    const key = JSON.stringify(courses.map((course) => [course.id, course.name]));
    if (state.renderKeys.courses !== key) {
      $('#course-preset').innerHTML = '<option value="">不使用预设</option>' + courses.map((course) => `<option value="${escapeHTML(course.id)}">${escapeHTML(course.name)}</option>`).join('');
      state.renderKeys.courses = key;
    }
    const course = currentCourse();
    if (!state.selectedCourseBusy) $('#course-preset').value = course?.id || '';
    $('#course-rename').disabled = !course;
    $('#course-manage').disabled = !course;
    if ($('#glossary-dialog').open) {
      const selected = courses.find((item) => item.id === state.glossaryCourseId);
      if (!selected) return;
      $('#glossary-title').textContent = `${selected.name} · 术语与大纲`;
      const pending = isPending(selected.status);
      $('#generate-glossary').disabled = pending || state.glossarySubmitting;
      $('#glossary-status').textContent = selected.error ? `生成未完成：${selected.error}` : pending ? 'Codex 正在生成术语，可以关闭窗口继续听课。' : selected.glossary?.length ? `已保存 ${selected.glossary.length} 个术语${selected.generated_at ? ` · ${new Date(selected.generated_at).toLocaleString('zh-CN')}` : ''}` : '尚无术语表。点击生成后会使用现有 Codex 账号。';
      const glossaryKey = JSON.stringify(selected.glossary || []);
      if (state.renderKeys.glossary !== glossaryKey) {
        $('#glossary-content').innerHTML = Array.isArray(selected.glossary) && selected.glossary.length ? `<table class="glossary-table"><thead><tr><th>英文术语</th><th>中文释义</th><th>缩写 / 别名</th></tr></thead><tbody>${selected.glossary.map((term) => `<tr><td>${escapeHTML(term.term || '')}</td><td>${escapeHTML(term.meaning || '')}</td><td>${escapeHTML(Array.isArray(term.aliases) ? term.aliases.join('、') : term.aliases || '')}</td></tr>`).join('')}</tbody></table>` : '';
        state.renderKeys.glossary = glossaryKey;
      }
    }
  }

  function safeLocalAudioURL(value) {
    if (!value) return null;
    try { const url = new URL(value, location.href); return url.origin === location.origin ? url.href : null; }
    catch (_) { return null; }
  }

  function renderSoundcheck() {
    const local = state.soundcheck;
    const remote = state.snapshot?.soundcheck;
    const remoteMatches = !local?.jobId || [remote?.job_id, remote?.id].includes(local.jobId);
    const current = ['uploading', 'upload_error', 'capturing'].includes(local?.status) || local?.status === 'error' && !local?.jobId ? local : remoteMatches && remote ? remote : local;
    if (!current || current.status === 'idle') { $('#soundcheck-result').hidden = true; return; }
    $('#soundcheck-result').hidden = false;
    const statuses = { capturing: '正在录制十秒试音…', uploading: '正在把试音发送到本机转写器…', upload_error: '试音发送失败，录音仍可在本页回听。', pending: '试音已保存，等待本机转写…', queued: '试音已保存，等待本机转写…', running: '正在试转写…', processing: '正在试转写…', done: '试音转写完成', completed: '试音转写完成', error: '试音转写未完成' };
    $('#soundcheck-status').textContent = statuses[current.status] || '试音结果';
    $('#soundcheck-text').textContent = current.text || (['done', 'completed'].includes(current.status) ? '未识别到清晰文字。请试着调整距离或麦克风；这不会阻止你开始课堂录音。' : '');
    $('#soundcheck-warning').textContent = [local?.volumeWarning, current.warning, current.error, current.status === 'upload_error' ? current.error : ''].filter((value, index, all) => value && all.indexOf(value) === index).join(' ');
    const source = safeLocalAudioURL(current.audio_url) || local?.audioUrl;
    const player = $('#soundcheck-player');
    if (source && player.getAttribute('src') !== source) player.src = source;
    player.hidden = !source;
    $('#soundcheck-retry').hidden = !local?.buffer || !['error', 'upload_error'].includes(current.status);
  }

  function renderAfterClass() {
    if (!state.session) return;
    const revisions = state.session.revisions || [];
    const pending = revisions.some((revision) => isPending(revision.status));
    const available = state.snapshot?.status?.asr?.high_quality_available === true;
    const audio = (state.session.audio || []).filter((item) => !item.deleted && item.path);
    const busy = state.captureMode !== 'idle' || state.uploadQueue.length > 0 || state.finalizing.size > 0 || audio.some((item) => isPending(item.status));
    $('#retranscribe-button').disabled = busy || pending || state.retranscribeSubmitting || !available || !audio.length || isDemo();
    $('#retranscribe-note').textContent = pending ? '高质量修订正在队列中处理，原始课堂记录会保留。' : !audio.length ? '本节课没有可用录音。课堂原稿与已有修订仍保留。' : !available ? '高质量重转写需要插电并启用可用的 NVIDIA 独显；当前条件未满足。' : '当前可手动进行高质量重转写。课堂原版保留，修订稿另存；不会自动启动。';
    $('#delete-audio-button').disabled = busy || pending || !audio.length || isDemo();
    $('#revision-count').textContent = revisions.length ? ` · ${revisions.length} 份修订` : '';
    const key = `${state.session.id}:${JSON.stringify(revisions)}`;
    if (state.renderKeys.revisions !== key) {
      const open = new Set([...$('#revisions-list').querySelectorAll('details[open]')].map((element) => element.dataset.revision));
      $('#revisions-list').innerHTML = revisions.map((revision, index) => `<details class="revision-card" data-revision="${escapeHTML(revision.id)}"${open.has(String(revision.id)) ? ' open' : ''}><summary><strong>修订 ${index + 1}</strong><span>${escapeHTML(statusLabel(revision.status))}</span><span class="small muted">${escapeHTML(revision.created_at ? new Date(revision.created_at).toLocaleString('zh-CN') : '')}</span></summary>${revision.error ? `<p class="analysis-error">${escapeHTML(revision.error)}</p>` : ''}<div class="revision-transcript">${(revision.segments || []).map((segment) => `<p><span class="timecode">${timecode(segment.start)}–${timecode(segment.end)} · ${escapeHTML(segment.id || '')}</span><br>${escapeHTML(segment.text)}</p>`).join('') || '<p class="muted">修订稿完成后会显示在这里，课堂原版保持不变。</p>'}</div></details>`).join('');
      state.renderKeys.revisions = key;
    }
  }

  function renderQueue() {
    const queue = state.uploadQueue;
    $('#audio-queue').hidden = queue.length === 0;
    if (queue.length) {
      const duration = queue.reduce((sum, item) => sum + item.duration, 0);
      const failure = queue.find((item) => item.error);
      $('#audio-queue-text').textContent = `${queue.length} 段音频待发送（${Math.ceil(duration)} 秒）${failure ? `：${failure.error}` : state.uploadPromise ? ' · 正在传到本机服务' : ' · 等待发送'}${state.db ? ' · 已暂存浏览器' : ' · 仅暂存在当前页面'}`;
      $('#retry-audio').hidden = !state.uploadPaused;
    }
    const busy = state.captureMode !== 'idle' || queue.length > 0 || state.finalizing.size > 0 || state.switching;
    $('#session-select').disabled = busy;
    $('#new-session').disabled = busy;
    $('#demo-button').disabled = busy;
    $('#course-preset').disabled = state.selectedCourseBusy || state.switching;
  }

  function updateCaptureHealth() {
    const capture = state.capture;
    let message = '';
    if (state.captureMode === 'recording' && capture?.kind === 'classroom') {
      const count = (state.session?.segments || []).length;
      if (count !== capture.observedSegments) { capture.observedSegments = count; capture.lastTextAt = Date.now(); }
      if (capture.quietSince && Date.now() - capture.quietSince > 15000) message = '持续收音音量偏低。可以继续录音，或调整电脑、麦克风与老师的距离。';
      else if (Date.now() - (capture.lastTextAt || capture.startedAt) > 35000) message = '暂未新增课堂文字，音频仍在保存。请查看转写队列；音量正常也不保证能听清内容。';
    }
    $('#capture-health').textContent = message;
    $('#capture-health').hidden = !message;
  }

  function renderCapture() {
    const active = state.captureMode === 'recording';
    const preparing = state.captureMode === 'starting';
    const stopping = state.captureMode === 'stopping';
    const testing = state.captureKind === 'soundcheck' && state.captureMode !== 'idle';
    $('#record-button').classList.toggle('recording', active);
    $('#record-button').disabled = testing || preparing || stopping || !state.session || !state.queueLoaded || state.uploadQueue.length >= MAX_QUEUE - 1;
    $('#record-label').textContent = testing ? '正在试音' : active ? '暂停录音' : preparing ? '正在开启麦克风…' : stopping ? '正在保存最后一段…' : state.clockSeconds > 0 ? '继续录音' : '开始录音';
    $('#capture-status').textContent = active ? testing ? '试音中 · 到十秒自动停止' : '正在收音 · 等待停顿后转写' : preparing ? '请允许浏览器使用麦克风' : stopping ? '正在收尾' : '麦克风未开启';
    $('#recording-clock').textContent = timecode(testing ? (state.capture?.testSamples || 0) / SAMPLE_RATE : state.clockSeconds);
    $('#recording-clock').classList.toggle('active', active);
    $('#microphone').disabled = state.captureMode !== 'idle';
    $('#refresh-mics').disabled = state.captureMode !== 'idle';
    $('#soundcheck-button').disabled = preparing || stopping || (active && !testing) || state.soundcheck?.status === 'uploading';
    $('#soundcheck-button').textContent = testing && active ? '结束试音' : '十秒试音';
    $('#flush-button').disabled = testing || state.flushing;
    if (!active && !stopping) setVolume(0);
    renderQueue();
    renderAfterClass();
    updateCaptureHealth();
  }

  function setVolume(rms) {
    const volume = Math.min(100, Math.max(0, Math.round(rms * 600)));
    $('#volume-meter').style.width = `${volume}%`;
    $('.meter').setAttribute('aria-valuenow', volume);
  }

  function poll(force = false) {
    if (state.pollRunning) return force ? state.pollPromise.then(() => poll(true)) : state.pollPromise;
    state.pollRunning = true;
    state.pollPromise = (async () => {
    try {
      const snapshot = await api('/api/state', { timeout: 10000 });
      const previousSession = state.session?.id;
      state.snapshot = snapshot;
      state.session = snapshot.session;
      state.connected = true;
      $('#connection-banner').hidden = true;
      $('#connection-dot').className = 'connection-dot ready';
      $('#connection-label').textContent = '已连接本地服务';
      if (previousSession !== state.session?.id) {
        state.selectedGroup = null;
        state.renderKeys = {};
        state.settingsDirty = false;
        const persistedTime = Number(localStorage.getItem(`classroom-time-${state.session?.id}`)) || 0;
        state.clockSeconds = Math.max(persistedTime, ...(state.session?.segments || []).map((segment) => Number(segment.end) || 0), ...(state.session?.audio || []).map((item) => (Number(item.start) || 0) + (Number(item.duration) || 0)));
        $('#question-context').hidden = true;
      }
      render();
    } catch (error) {
      state.connected = false;
      $('#connection-banner').textContent = error.message + ' 录音中的片段会暂存在当前浏览器。';
      $('#connection-banner').hidden = false;
      $('#connection-dot').className = 'connection-dot error';
      $('#connection-label').textContent = '本地服务未连接';
    } finally {
      state.pollRunning = false;
    }
    })();
    return state.pollPromise;
  }

  function markSettingsDirty() {
    state.settingsDirty = true;
    state.saveGeneration++;
    $('#save-status').textContent = '正在保存…';
    clearTimeout(state.settingsTimer);
    state.settingsTimer = setTimeout(() => saveSettings().catch((error) => toast(error.message, true)), 550);
  }

  async function saveSettings() {
    clearTimeout(state.settingsTimer);
    if (state.settingsPromise) await state.settingsPromise;
    if (!state.settingsDirty || !state.session) return;
    const generation = state.saveGeneration;
    const payload = { title: $('#course-title').value.trim() || '未命名课程', terms: $('#course-terms').value.trim(), auto_analyze: $('#auto-analyze').checked };
    state.settingsPromise = api('/api/settings', { method: 'PUT', body: payload });
    try {
      await state.settingsPromise;
      if (generation === state.saveGeneration) state.settingsDirty = false;
      state.session.title = payload.title;
      state.session.settings = { ...state.session.settings, ...payload };
      $('#save-status').textContent = state.settingsDirty ? '正在保存…' : '已保存';
    } catch (error) {
      $('#save-status').textContent = '保存失败';
      throw error;
    } finally {
      state.settingsPromise = null;
    }
  }

  function canSwitch() {
    if (state.captureMode !== 'idle' || state.uploadQueue.length || state.finalizing.size || state.switching) {
      toast('请先暂停录音，并发送完待处理音频后再切换课程。', true);
      return false;
    }
    return true;
  }

  async function newSession(title) {
    await saveSettings();
    state.switching = true;
    renderQueue();
    try {
      await api('/api/sessions', { method: 'POST', body: { title: title || `课堂笔记 ${new Date().toLocaleDateString('zh-CN')}` } });
      state.renderKeys = {};
      await poll(true);
    } finally {
      state.switching = false;
      renderQueue();
    }
  }

  async function ensureRealSession() {
    if (isDemo()) {
      await newSession(`课堂笔记 ${new Date().toLocaleDateString('zh-CN')}`);
      toast('已新建真实课程，演示内容保留在历史课程中。');
    }
  }

  async function enumerateMicrophones() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const current = $('#microphone').value;
    const devices = await navigator.mediaDevices.enumerateDevices();
    const microphones = devices.filter((device) => device.kind === 'audioinput');
    $('#microphone').innerHTML = '<option value="">系统默认麦克风</option>' + microphones.filter((device) => device.deviceId && device.deviceId !== 'default').map((device, index) => `<option value="${escapeHTML(device.deviceId)}">${escapeHTML(device.label || `麦克风 ${index + 1}（开启后显示名称）`)}</option>`).join('');
    if ([...$('#microphone').options].some((option) => option.value === current)) $('#microphone').value = current;
  }

  // Samples are segmented independently of network/ASR latency. Silence before
  // speech retains only 250 ms of pre-roll, and a fragment never exceeds 12 s.
  class AudioSegmenter {
    constructor(offset, onSegment) {
      this.offset = offset; this.onSegment = onSegment; this.cursor = 0;
      this.frames = []; this.length = 0; this.silence = 0; this.start = 0;
      this.active = false; this.preRoll = new Float32Array(0);
    }
    accept(samples) {
      let power = 0;
      for (let i = 0; i < samples.length; i++) power += samples[i] * samples[i];
      const rms = Math.sqrt(power / Math.max(1, samples.length));
      // A sensitive front gate preserves quieter, distant classroom voices;
      // the server's speech detector performs the second pass.
      const speech = rms >= 0.003;
      setVolume(rms);
      let consumed = 0;
      while (consumed < samples.length) {
        if (!this.active && !speech) {
          const incoming = samples.subarray(consumed);
          const length = Math.min(4000, this.preRoll.length + incoming.length);
          const next = new Float32Array(length);
          const fromIncoming = Math.min(length, incoming.length);
          const fromPrevious = length - fromIncoming;
          if (fromPrevious) next.set(this.preRoll.subarray(this.preRoll.length - fromPrevious));
          next.set(incoming.subarray(incoming.length - fromIncoming), fromPrevious);
          this.preRoll = next;
          this.cursor += incoming.length;
          return;
        }
        if (!this.active) {
          this.active = true;
          this.start = this.cursor - this.preRoll.length;
          this.frames = this.preRoll.length ? [this.preRoll] : [];
          this.length = this.preRoll.length;
          this.preRoll = new Float32Array(0);
        }
        const take = Math.min(samples.length - consumed, 12 * SAMPLE_RATE - this.length);
        this.frames.push(samples.subarray(consumed, consumed + take));
        this.length += take;
        this.cursor += take;
        consumed += take;
        this.silence = speech ? 0 : this.silence + take;
        if (this.length >= 12 * SAMPLE_RATE || this.length >= 4 * SAMPLE_RATE && this.silence >= .7 * SAMPLE_RATE) this.flush();
      }
    }
    flush() {
      if (this.active && this.length >= .2 * SAMPLE_RATE) {
        const pcm = new Float32Array(this.length);
        let offset = 0;
        for (const frame of this.frames) { pcm.set(frame, offset); offset += frame.length; }
        this.onSegment({ pcm, start: this.offset + this.start / SAMPLE_RATE, duration: this.length / SAMPLE_RATE });
      }
      this.frames = []; this.length = 0; this.silence = 0; this.active = false;
      this.preRoll = new Float32Array(0);
    }
    get time() { return this.offset + this.cursor / SAMPLE_RATE; }
  }

  class SoundcheckBuffer {
    constructor() { this.limit = 10 * SAMPLE_RATE; this.length = 0; this.frames = []; this.power = 0; this.peak = 0; }
    accept(samples) {
      const part = samples.subarray(0, Math.max(0, this.limit - this.length));
      if (!part.length) return;
      this.frames.push(part); this.length += part.length;
      for (const value of part) { this.power += value * value; this.peak = Math.max(this.peak, Math.abs(value)); }
    }
    get done() { return this.length >= this.limit; }
    get rms() { return Math.sqrt(this.power / Math.max(1, this.length)); }
    get duration() { return this.length / SAMPLE_RATE; }
    pcm() { const all = new Float32Array(this.length); let position = 0; for (const part of this.frames) { all.set(part, position); position += part.length; } return all; }
  }

  function wavBytes(pcm) {
    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const write = (offset, text) => { for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); };
    write(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); write(8, 'WAVE');
    write(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, SAMPLE_RATE, true); view.setUint32(28, SAMPLE_RATE * 2, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true); write(36, 'data'); view.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) { const value = Math.max(-1, Math.min(1, pcm[i])); view.setInt16(44 + i * 2, Math.round(value < 0 ? value * 32768 : value * 32767), true); }
    return buffer;
  }

  async function startRecording() {
    return beginCapture(false);
  }

  async function beginCapture(soundcheck = false) {
    if (state.captureMode !== 'idle') return;
    if (!navigator.mediaDevices?.getUserMedia || !window.AudioWorkletNode) {
      toast('当前浏览器不支持实时录音，请用最新版 Edge / Chrome 打开 localhost 地址，或手动补充原文。', true, 9000);
      return;
    }
    if (!soundcheck && state.uploadQueue.length >= MAX_QUEUE - 1) { toast('待发送音频接近上限，请先重试发送。', true); return; }
    state.captureKind = soundcheck ? 'soundcheck' : 'classroom';
    state.captureMode = 'starting';
    renderCapture();
    try {
      await ensureRealSession();
      await saveSettings();
      const deviceId = $('#microphone').value;
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      const capture = { stream, context: null, source: null, worklet: null, segmenter: null, sessionId: state.session.id, kind: state.captureKind, testBuffer: soundcheck ? new SoundcheckBuffer() : null, timer: null, startedAt: Date.now(), observedSegments: (state.session.segments || []).length, lastTextAt: Date.now(), quietSince: 0, lastRMS: 0, testSamples: 0 };
      state.capture = capture;
      const AudioContextClass = window.AudioContext || window.webkitAudioContext;
      const context = new AudioContextClass({ latencyHint: 'interactive' });
      capture.context = context;
      await context.audioWorklet.addModule('/static/pcm-worklet.js');
      capture.source = context.createMediaStreamSource(stream);
      capture.worklet = new AudioWorkletNode(context, 'classroom-pcm', { numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1] });
      const existingEnd = Math.max(0, ...(state.session.segments || []).map((segment) => Number(segment.end) || 0), ...(state.session.audio || []).map((item) => (Number(item.start) || 0) + (Number(item.duration) || 0)), ...state.uploadQueue.filter((item) => item.sessionId === capture.sessionId).map((item) => item.start + item.duration));
      if (!soundcheck) capture.segmenter = new AudioSegmenter(Math.max(state.clockSeconds, existingEnd), (chunk) => enqueueAudio(capture.sessionId, chunk));
      else {
        $('#soundcheck-player').pause();
        if (state.soundcheck?.audioUrl) URL.revokeObjectURL(state.soundcheck.audioUrl);
        state.soundcheck = { status: 'capturing' };
        $('#soundcheck-result').open = true;
        renderSoundcheck();
      }
      capture.worklet.port.onmessage = ({ data }) => {
        if (data.type === 'pcm') {
          let power = 0;
          for (const value of data.samples) power += value * value;
          capture.lastRMS = Math.sqrt(power / Math.max(1, data.samples.length));
          capture.quietSince = capture.lastRMS < .003 ? capture.quietSince || Date.now() : 0;
          if (soundcheck) {
            capture.testBuffer.accept(data.samples);
            capture.testSamples = capture.testBuffer.length;
            setVolume(capture.lastRMS);
            $('#recording-clock').textContent = timecode(capture.testBuffer.duration);
            if (capture.testBuffer.done && state.captureMode === 'recording') stopSoundcheck().catch((error) => toast(error.message, true));
          } else {
            capture.segmenter.accept(data.samples);
            state.clockSeconds = capture.segmenter.time;
            $('#recording-clock').textContent = timecode(state.clockSeconds);
          }
        } else if (data.type === 'flushed') {
          const resolver = state.flushResolvers.get(data.token);
          if (resolver) { state.flushResolvers.delete(data.token); resolver(); }
        }
      };
      capture.source.connect(capture.worklet);
      capture.worklet.connect(context.destination);
      await context.resume();
      for (const track of stream.getAudioTracks()) track.addEventListener('ended', () => { if (state.captureMode === 'recording') stopRecording('麦克风连接已断开，已保留收到的音频。').catch((error) => toast(error.message, true)); }, { once: true });
      state.captureMode = 'recording';
      if (soundcheck) capture.timer = setTimeout(() => stopSoundcheck().catch((error) => toast(error.message, true)), 10000);
      renderCapture();
      enumerateMicrophones().catch(() => {});
    } catch (error) {
      await releaseCapture();
      state.captureMode = 'idle';
      state.captureKind = 'classroom';
      renderCapture();
      const explanations = { NotAllowedError: '麦克风权限未获允许。请在地址栏的权限设置中开启麦克风，也可以手动补充原文。', NotFoundError: '没有找到麦克风，请检查设备连接。', NotReadableError: '无法读取麦克风，它可能被其他程序独占。', OverconstrainedError: '所选麦克风不可用，请改选系统默认麦克风。' };
      toast(explanations[error.name] || `录音未启动：${error.message}`, true, 10000);
    }
  }

  function flushWorklet() {
    const capture = state.capture;
    if (!capture?.worklet) return Promise.resolve();
    const token = crypto.randomUUID();
    return new Promise((resolve) => {
      const timer = setTimeout(() => { state.flushResolvers.delete(token); resolve(); }, 1200);
      state.flushResolvers.set(token, () => { clearTimeout(timer); resolve(); });
      capture.worklet.port.postMessage({ type: 'flush', token });
    });
  }

  async function releaseCapture() {
    const capture = state.capture;
    if (!capture) return;
    clearTimeout(capture.timer);
    for (const track of capture.stream.getTracks()) track.stop();
    try { capture.source?.disconnect(); } catch (_) {}
    try { capture.worklet?.disconnect(); } catch (_) {}
    if (capture.worklet) { capture.worklet.port.onmessage = null; capture.worklet.port.close(); }
    if (capture.context && capture.context.state !== 'closed') await capture.context.close().catch(() => {});
    state.capture = null;
  }

  async function stopRecording(reason) {
    if (state.captureMode !== 'recording') return;
    if (state.capture?.kind === 'soundcheck') return stopSoundcheck(reason);
    state.captureMode = 'stopping';
    renderCapture();
    const capture = state.capture;
    try {
      // First stop input, then request the last worklet block, then flush the
      // segmenter. Network uploads continue independently in the background.
      capture.source.disconnect();
      for (const track of capture.stream.getTracks()) track.stop();
      await flushWorklet();
      capture.segmenter.flush();
      state.clockSeconds = capture.segmenter.time;
      localStorage.setItem(`classroom-time-${capture.sessionId}`, String(state.clockSeconds));
      markFinalize(capture.sessionId);
    } finally {
      await releaseCapture();
      state.captureMode = 'idle';
      renderCapture();
      updateCaptureHealth();
    }
    if (reason) toast(reason, true, 8000);
    if (state.uploadPaused) toast('录音已暂停。待发送片段已保留，点击「重试发送」后会继续整理。', true, 8000);
    await drainUploads();
    await finalizeReadySessions();
    await poll(true);
  }

  async function stopSoundcheck(reason) {
    if (state.captureMode !== 'recording' || state.capture?.kind !== 'soundcheck') return;
    state.captureMode = 'stopping';
    renderCapture();
    const capture = state.capture;
    try {
      clearTimeout(capture.timer);
      capture.source.disconnect();
      for (const track of capture.stream.getTracks()) track.stop();
      await flushWorklet();
      const test = capture.testBuffer;
      if (test.length < .2 * SAMPLE_RATE) {
        state.soundcheck = { status: 'error', error: '试音过短，未获得足够音频。可以再次试音。' };
      } else {
        const buffer = wavBytes(test.pcm());
        state.soundcheck = { status: 'uploading', buffer, audioUrl: URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' })), duration: test.duration, volumeWarning: test.rms < .003 || test.peak < .012 ? '试音音量偏低，可以调整麦克风、距离或朝向。音量大小不等于语音清晰度。' : '音量检测仅供参考；请回听确认老师的话是否清晰。' };
      }
    } finally {
      await releaseCapture();
      state.captureMode = 'idle';
      state.captureKind = 'classroom';
      renderCapture();
      renderSoundcheck();
    }
    if (reason) toast(reason, true);
    if (state.soundcheck?.buffer) await uploadSoundcheck();
  }

  async function uploadSoundcheck() {
    const local = state.soundcheck;
    if (!local?.buffer) return;
    local.status = 'uploading'; local.error = '';
    renderSoundcheck(); renderCapture();
    try {
      const result = await api('/api/soundcheck', { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: local.buffer });
      local.jobId = result.job_id || result.id;
      local.status = 'pending';
      await poll(true);
    } catch (error) { local.status = 'upload_error'; local.error = error.message; }
    finally { renderSoundcheck(); renderCapture(); }
  }

  // IndexedDB retains WAV files across a normal refresh; no audio leaves this
  // browser except for the local same-origin /api/audio endpoint.
  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open('classroom-assistant-audio', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('pending', { keyPath: 'id' });
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error('音频暂存数据库被其他页面占用。'));
    });
  }

  function databaseAction(action, payload) {
    if (!state.db) return Promise.resolve(action === 'getAll' ? [] : undefined);
    return new Promise((resolve, reject) => {
      const transaction = state.db.transaction('pending', action === 'getAll' ? 'readonly' : 'readwrite');
      const store = transaction.objectStore('pending');
      const request = action === 'getAll' ? store.getAll() : store[action](payload);
      let result;
      request.onsuccess = () => { result = request.result; };
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error || request.error);
      transaction.onabort = () => reject(transaction.error || new Error('音频暂存已中断。'));
    });
  }

  function nextSequence(sessionId) {
    const key = `classroom-seq-${sessionId}`;
    const value = Math.max(Date.now(), (Number(localStorage.getItem(key)) || 0) + 1);
    localStorage.setItem(key, String(value));
    return value;
  }

  function enqueueAudio(sessionId, chunk) {
    const item = { id: crypto.randomUUID(), sessionId, seq: nextSequence(sessionId), start: chunk.start, duration: chunk.duration, buffer: wavBytes(chunk.pcm), created: Date.now(), status: 'saving', error: '' };
    state.uploadQueue.push(item);
    renderQueue();
    databaseAction('put', { ...item, status: 'queued' }).catch((error) => { toast(`音频暂存失败，离开页面可能丢失未发送片段：${error.message}`, true, 10000); }).finally(() => {
      item.status = 'queued';
      drainUploads().catch((error) => toast(error.message, true));
    });
    if (state.uploadQueue.length >= MAX_QUEUE - 1 && state.captureMode === 'recording') stopRecording('待发送音频接近 60 段上限，已自动暂停录音。请先恢复本地服务并重试发送。').catch((error) => toast(error.message, true));
  }

  function markFinalize(sessionId) {
    state.finalizing.add(sessionId);
    localStorage.setItem('classroom-finalize', JSON.stringify([...state.finalizing]));
    renderQueue();
  }

  async function finalizeReadySessions() {
    for (const sessionId of [...state.finalizing]) {
      if (state.uploadQueue.some((item) => item.sessionId === sessionId)) continue;
      try {
        await api('/api/flush', { method: 'POST', body: { session_id: sessionId } });
        state.finalizing.delete(sessionId);
        localStorage.setItem('classroom-finalize', JSON.stringify([...state.finalizing]));
      } catch (error) {
        // Keep the finalize marker until connectivity returns. The audio itself
        // is already on the local server and must not be uploaded again.
        $('#connection-banner').textContent = `最后一个意群尚未整理：${error.message} 恢复连接后会自动重试。`;
        $('#connection-banner').hidden = false;
      }
    }
    renderQueue();
  }

  async function drainUploads() {
    if (state.uploadPromise) return state.uploadPromise;
    if (state.uploadPaused || !state.uploadQueue.length) return;
    state.uploadPromise = (async () => {
      while (state.uploadQueue.length && !state.uploadPaused) {
        const item = state.uploadQueue[0];
        if (item.status === 'saving') break;
        item.status = 'uploading';
        renderQueue();
        try {
          const params = new URLSearchParams({ session_id: item.sessionId, seq: String(item.seq), start: String(item.start), duration: String(item.duration) });
          await api(`/api/audio?${params}`, { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: item.buffer });
          await databaseAction('delete', item.id).catch(() => {});
          state.uploadQueue.shift();
          renderQueue();
        } catch (error) {
          item.status = 'queued';
          item.error = error.status === 429 ? `服务处理队列已满：${error.message}` : error.message;
          state.uploadPaused = true;
          toast('音频发送暂停，片段已保留。可在录音区点击「重试发送」。', true, 7000);
          renderQueue();
        }
      }
    })();
    try { await state.uploadPromise; }
    finally {
      state.uploadPromise = null;
      renderQueue();
      if (!state.uploadQueue.length) await finalizeReadySessions();
    }
  }

  async function initializeQueue() {
    try {
      state.db = await openDatabase();
      const stored = await databaseAction('getAll');
      state.uploadQueue = stored.sort((a, b) => a.created - b.created || a.seq - b.seq).map((item) => ({ ...item, status: 'queued', error: '' }));
      const markers = JSON.parse(localStorage.getItem('classroom-finalize') || '[]');
      if (Array.isArray(markers)) for (const id of markers) state.finalizing.add(id);
      if (state.uploadQueue.length) {
        state.uploadPaused = true;
        toast(`已恢复 ${state.uploadQueue.length} 段待发送音频。点击「重试发送」继续。`, false, 9000);
      }
    } catch (error) {
      toast('浏览器音频暂存不可用。未发送音频仅保留在当前页面，请勿刷新。', true, 10000);
    } finally {
      state.queueLoaded = true;
      renderCapture();
    }
  }

  $('#record-button').addEventListener('click', () => {
    const action = state.captureMode === 'recording' ? stopRecording() : startRecording();
    action.catch((error) => toast(error.message, true));
  });
  $('#refresh-mics').addEventListener('click', () => enumerateMicrophones().catch((error) => toast(error.message, true)));
  $('#retry-audio').addEventListener('click', async () => {
    state.uploadPaused = false;
    for (const item of state.uploadQueue) item.error = '';
    await drainUploads();
    await poll(true);
  });
  for (const selector of ['#course-title', '#course-terms']) $(selector).addEventListener('input', markSettingsDirty);
  $('#auto-analyze').addEventListener('change', () => { markSettingsDirty(); renderAnalysisMode(); if ($('#auto-analyze').checked) toast('逐段解释已开启。新的意群文字将发送至 Codex。'); });

  $('#runtime-mode').addEventListener('change', async () => {
    state.modeSaving = true;
    $('#runtime-mode').disabled = true;
    try {
      await api('/api/mode', { method: 'POST', body: { mode: $('#runtime-mode').value } });
      await poll(true);
      toast('模式请求已保存。模型会在音频片段边界切换，收音可以继续。');
    } catch (error) { toast(error.message, true); }
    finally { state.modeSaving = false; $('#runtime-mode').disabled = false; renderEngineStatus(); }
  });

  $('#course-preset').addEventListener('change', async () => {
    const courseId = $('#course-preset').value;
    state.selectedCourseBusy = true; renderQueue();
    try {
      await saveSettings();
      await api('/api/course/select', { method: 'POST', body: { course_id: courseId || null } });
      await poll(true);
      toast(courseId ? '课程预设已加载，术语可在上方继续补充。' : '已取消课程预设。');
    } catch (error) { toast(error.message, true); }
    finally { state.selectedCourseBusy = false; renderCourseTools(); renderQueue(); }
  });

  function openCourseDialog(action) {
    const course = currentCourse();
    if (action === 'rename' && !course) return;
    state.courseAction = action;
    $('#course-dialog-title').textContent = action === 'rename' ? '重命名课程预设' : '新建课程预设';
    $('#course-name').value = action === 'rename' ? course.name : '';
    $('#course-dialog').showModal();
    $('#course-name').focus();
  }
  $('#course-new').addEventListener('click', () => openCourseDialog('create'));
  $('#course-rename').addEventListener('click', () => openCourseDialog('rename'));
  $('#course-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = $('#course-name').value.trim();
    if (!name) return;
    const button = event.submitter;
    button.disabled = true;
    try {
      const course = currentCourse();
      const creating = state.courseAction === 'create';
      const result = await api(creating ? '/api/courses' : `/api/courses/${encodeURIComponent(course.id)}`, { method: creating ? 'POST' : 'PUT', body: { name } });
      const id = result.id || result.course?.id;
      if (creating && id) { await saveSettings(); await api('/api/course/select', { method: 'POST', body: { course_id: id } }); }
      await poll(true);
      $('#course-dialog').close();
      toast(creating ? '课程预设已创建，可以生成术语或补充大纲。' : '课程预设已改名。');
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
  $('#course-manage').addEventListener('click', () => {
    const course = currentCourse();
    if (!course) return;
    state.glossaryCourseId = course.id;
    $('#course-syllabus').value = course.syllabus || '';
    state.renderKeys.glossary = null;
    $('#glossary-dialog').showModal();
    renderCourseTools();
  });
  $('#glossary-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.glossaryCourseId || $('#generate-glossary').disabled) return;
    if (isDemo()) { toast('演示课堂不调用 AI。请先新建一节课，再生成术语。'); return; }
    state.glossarySubmitting = true;
    $('#generate-glossary').disabled = true;
    try {
      await api(`/api/courses/${encodeURIComponent(state.glossaryCourseId)}/glossary`, { method: 'POST', body: { syllabus: $('#course-syllabus').value.trim() } });
      await poll(true);
      toast('术语生成已加入 Codex 队列，完成后会保存到课程预设。');
    } catch (error) { toast(error.message, true); }
    finally { state.glossarySubmitting = false; renderCourseTools(); }
  });

  document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => document.getElementById(button.dataset.closeDialog).close()));
  $('#transcript-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-edit-segment]');
    if (!button) return;
    const segment = (state.session?.segments || []).find((item) => item.id === button.dataset.editSegment);
    if (!segment) return;
    state.editingSegment = { id: segment.id, sessionId: state.session.id };
    $('#edit-segment-text').value = segment.text || '';
    $('#edit-original-text').textContent = segment.machine_text ?? segment.text ?? '';
    $('#edit-segment-dialog').showModal();
    $('#edit-segment-text').focus();
  });
  $('#edit-segment-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const target = state.editingSegment;
    const text = $('#edit-segment-text').value.trim();
    if (!target || !text) return;
    const button = event.submitter; button.disabled = true;
    try {
      await api(`/api/segments/${encodeURIComponent(target.id)}/edit`, { method: 'POST', body: { text, session_id: target.sessionId } });
      await poll(true);
      $('#edit-segment-dialog').close();
      toast('原文已修改，机器原稿保留。相关解释会标为待更新。');
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });

  function returnToLive() {
    state.followLive = true;
    state.scrollGuardUntil = performance.now() + 250;
    for (const selector of ['#transcript-list', '#groups-list', '#questions-list']) $(selector).scrollTop = $(selector).scrollHeight;
    $('#return-live').hidden = true;
  }
  function setFocusMode(enabled) {
    state.focusMode = enabled;
    $('.app-shell').classList.toggle('focus-mode', enabled);
    $('#focus-button').setAttribute('aria-pressed', String(enabled));
    $('#focus-button').textContent = enabled ? '退出专注' : '专注模式';
    state.followLive = true;
    render();
    returnToLive();
  }
  $('#focus-button').addEventListener('click', () => setFocusMode(!state.focusMode));
  $('#return-live').addEventListener('click', returnToLive);
  for (const selector of ['#transcript-list', '#groups-list', '#questions-list']) $(selector).addEventListener('scroll', () => {
    if (performance.now() < (state.scrollGuardUntil || 0)) return;
    const element = $(selector);
    if (element.scrollHeight - element.scrollTop - element.clientHeight > 80) { state.followLive = false; $('#return-live').hidden = false; }
  }, { passive: true });

  $('#soundcheck-button').addEventListener('click', () => {
    const action = state.capture?.kind === 'soundcheck' ? stopSoundcheck() : beginCapture(true);
    action.catch((error) => toast(error.message, true));
  });
  $('#soundcheck-retry').addEventListener('click', () => uploadSoundcheck().catch((error) => toast(error.message, true)));

  $('#retranscribe-button').addEventListener('click', async () => {
    if ($('#retranscribe-button').disabled) return;
    state.retranscribeSubmitting = true;
    $('#retranscribe-button').disabled = true;
    try {
      await api('/api/retranscribe', { method: 'POST', body: { session_id: state.session.id } });
      await poll(true);
      toast('高质量重转写已开始排队。修订稿会单独保存，课堂原版保留。');
    } catch (error) { toast(error.message, true); }
    finally { state.retranscribeSubmitting = false; renderAfterClass(); }
  });
  $('#delete-audio-button').addEventListener('click', () => { if (!$('#delete-audio-button').disabled) $('#delete-audio-dialog').showModal(); });
  $('#delete-audio-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter; button.disabled = true;
    try {
      if (state.captureMode !== 'idle' || state.uploadQueue.length) throw new Error('请先结束录音并发送完待处理音频。');
      await api('/api/audio/delete', { method: 'POST', body: { session_id: state.session.id } });
      document.querySelectorAll('#transcript-list audio').forEach((player) => player.pause());
      await poll(true);
      $('#delete-audio-dialog').close();
      toast('本节课录音已删除，转写、解释、问答和已有修订仍保留。');
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
  document.querySelectorAll('[data-ai-resume]').forEach((button) => button.addEventListener('click', async () => {
    const buttons = [...document.querySelectorAll('[data-ai-resume]')];
    buttons.forEach((item) => { item.disabled = true; });
    try {
      await api('/api/ai/resume', { method: 'POST', body: { strategy: button.dataset.aiResume } });
      await poll(true);
      toast(button.dataset.aiResume === 'backfill' ? '已请求补分析未完成的内容。' : '已请求从最新内容继续分析。历史原文仍然保留。');
    } catch (error) { toast(error.message, true); }
    finally { buttons.forEach((item) => { item.disabled = false; }); }
  }));

  $('#session-select').addEventListener('change', async (event) => {
    const id = event.target.value;
    if (!canSwitch()) { event.target.value = state.session?.id || ''; return; }
    try {
      await saveSettings();
      state.switching = true; renderQueue();
      await api(`/api/sessions/${encodeURIComponent(id)}/select`, { method: 'POST' });
      await poll(true);
    } catch (error) { toast(error.message, true); event.target.value = state.session?.id || ''; }
    finally { state.switching = false; renderQueue(); }
  });
  $('#new-session').addEventListener('click', () => {
    if (!canSwitch()) return;
    $('#new-title').value = '';
    $('#new-session-dialog').showModal();
    $('#new-title').focus();
  });
  $('#cancel-new-session').addEventListener('click', () => $('#new-session-dialog').close());
  $('#new-session-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try { await newSession($('#new-title').value.trim()); $('#new-session-dialog').close(); }
    catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });
  $('#demo-button').addEventListener('click', async () => {
    if (!canSwitch()) return;
    try {
      await saveSettings();
      state.switching = true; renderQueue();
      await api('/api/demo', { method: 'POST' });
      await poll(true);
      toast('已载入固定演示：没有开启麦克风，也没有调用 AI。');
    } catch (error) { toast(error.message, true); }
    finally { state.switching = false; renderQueue(); }
  });

  $('#text-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const text = $('#manual-text').value.trim();
    if (!text) return;
    $('#add-text').disabled = true;
    try {
      await ensureRealSession(); await saveSettings();
      await api('/api/text', { method: 'POST', body: { text, session_id: state.session.id } });
      $('#manual-text').value = '';
      await poll(true);
    } catch (error) { toast(error.message, true); }
    finally { $('#add-text').disabled = false; }
  });

  $('#flush-button').addEventListener('click', async () => {
    if (!state.session) return;
    if (isDemo()) { toast('这是固定演示。新建课程后即可整理真实课堂内容。'); return; }
    if (state.flushing || state.capture?.kind === 'soundcheck') return;
    state.flushing = true;
    $('#flush-button').disabled = true;
    try {
      await saveSettings();
      if (state.captureMode === 'recording' && state.capture?.kind === 'classroom') { await flushWorklet(); state.capture?.segmenter.flush(); }
      markFinalize(state.session.id);
      await drainUploads();
      await finalizeReadySessions();
      toast(state.uploadQueue.length ? '已记录整理请求，待音频发送完成后继续。' : '已请求整理。尚未完成转写的音频会处理完后一起归入意群。');
      await poll(true);
    } catch (error) { toast(error.message, true); }
    finally { state.flushing = false; renderCapture(); }
  });

  $('#groups-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-group]');
    if (!button) return;
    state.selectedGroup = button.dataset.group;
    if (state.focusMode) setFocusMode(false);
    $('#context-label').textContent = `聚焦意群 ${state.selectedGroup}`;
    $('#question-context').hidden = false;
    render();
    $('#question-input').focus();
    if (window.innerWidth < 1150) $('#question-form').scrollIntoView({ behavior: 'smooth', block: 'center' });
  });
  $('#clear-context').addEventListener('click', () => { state.selectedGroup = null; $('#question-context').hidden = true; render(); });
  $('#questions-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-question]');
    if (button) { $('#question-input').value = button.dataset.question; $('#question-input').focus(); }
  });
  $('#question-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('#question-form').requestSubmit(); }
  });
  $('#question-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('#ask-button').disabled) return;
    const question = $('#question-input').value.trim();
    if (!question) return;
    if (isDemo()) { toast('演示模式不调用 AI。请新建课程并加入真实课堂原文后提问。'); return; }
    $('#ask-button').disabled = true;
    try {
      await saveSettings();
      await api('/api/ask', { method: 'POST', body: { question, ...(state.selectedGroup ? { group_id: state.selectedGroup } : {}) } });
      $('#question-input').value = '';
      await poll(true);
      $('#questions-list').scrollTop = $('#questions-list').scrollHeight;
    } catch (error) { toast(error.message, true, 8000); }
    finally { $('#ask-button').disabled = false; }
  });

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-retry-group], [data-retry-question], [data-retry-audio]');
    if (!button || isDemo()) return;
    const type = button.dataset.retryGroup ? 'groups' : button.dataset.retryQuestion ? 'questions' : 'audio';
    const id = button.dataset.retryGroup || button.dataset.retryQuestion || button.dataset.retryAudio;
    button.disabled = true;
    try {
      await saveSettings();
      await api(`/api/${type}/${encodeURIComponent(id)}/retry`, { method: 'POST', body: { session_id: state.session.id } });
      await poll(true);
      toast(type === 'audio' ? '已加入本机转写队列。' : '已加入 Codex 处理队列。');
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  });

  document.querySelectorAll('[data-export]').forEach((button) => button.addEventListener('click', async () => {
    try {
      await saveSettings();
      const format = button.dataset.export;
      const response = await api(`/api/export?format=${format}`, { raw: true });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${(state.session?.title || '课堂笔记').replace(/[\\/:*?"<>|]/g, '_')}.${format === 'json' ? 'json' : 'md'}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      $('.export-menu').open = false;
      toast('已导出当前记录。仍在队列中的转写或分析尚未包含在内。');
    } catch (error) { toast(error.message, true); }
  }));

  $('#copy-codex').addEventListener('click', async () => {
    if (!state.session) return;
    const session = state.session;
    const lines = [`请作为我的随堂学习助手，基于下列课堂原文用中文解释，保留关键英文术语。区分老师的原意、你的补充和不确定之处。引用原文时注明段落编号；如果问题超出材料，请明确说明。`, '', `课程：${session.title}`, isDemo() ? '注意：以下是固定演示材料，并非真实录音。' : '', '', ...((session.segments || []).map((segment) => `[${segment.id} ${timecode(segment.start)}–${timecode(segment.end)}]\n${segment.text}`)), '', '我的问题：请先整理这部分内容的核心逻辑与需要理解的术语。'];
    const content = lines.join('\n');
    try { await navigator.clipboard.writeText(content); toast('已复制课堂原文与提示词，可粘贴到当前 Codex / ChatGPT 对话。'); }
    catch (_) {
      const textarea = document.createElement('textarea'); textarea.value = content; document.body.appendChild(textarea); textarea.select();
      const copied = document.execCommand('copy'); textarea.remove();
      toast(copied ? '已复制，可粘贴到 Codex / ChatGPT。' : '浏览器无法写入剪贴板，请导出 Markdown 后复制。', !copied);
    }
    $('.export-menu').open = false;
  });

  document.addEventListener('click', (event) => { if (!event.target.closest('.export-menu')) $('.export-menu').open = false; });
  window.addEventListener('beforeunload', (event) => {
    if (state.captureMode !== 'idle' || state.uploadQueue.length || state.settingsDirty || state.finalizing.size) {
      event.preventDefault(); event.returnValue = '';
    }
  });
  navigator.mediaDevices?.addEventListener?.('devicechange', () => { if (state.captureMode === 'idle') enumerateMicrophones().catch(() => {}); });

  async function initialize() {
    await Promise.allSettled([poll(), initializeQueue(), enumerateMicrophones()]);
    if (!state.uploadQueue.length) await finalizeReadySessions();
    setInterval(async () => {
      await poll();
      if (state.connected && !state.uploadQueue.length && state.finalizing.size) await finalizeReadySessions();
    }, 2000);
    setInterval(() => {
      if (state.capture?.kind === 'classroom') localStorage.setItem(`classroom-time-${state.capture.sessionId}`, String(state.clockSeconds));
      updateCaptureHealth();
    }, 5000);
  }
  initialize().catch((error) => toast(error.message, true));
})();
