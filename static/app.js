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
    aiSettings: null, aiSettingsBusy: false, aiResuming: false,
    slideSelection: null, includeImagesPreference: null, slideBusy: false, editingSlide: null,
    studyBusy: false, studyReview: null, studyFailedParams: null, studyDrafts: new Map(),
    sessionEpoch: 0, draftTexts: new Map(), draftWrites: new Map(), draftRevision: new Map(),
    cachedSlides: [], transientSlides: new Map(), cacheReady: false, cachePromise: null,
    requestIds: new Map(), slideURLs: new Map(), slideRecoveryBusy: false,
    legacyRequests: new Map(), pendingLegacyRetry: null,
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
  const textFields = [
    { selector: '#manual-text', label: '手动原文', key: 'text', required: true },
    { selector: '#edit-segment-text', label: '修改原文', key: 'text', required: true },
    { selector: '#question-input', label: '提问', key: 'question', required: true },
    { selector: '#course-syllabus', label: '课程大纲', key: 'syllabus', required: false },
  ];
  // Match Python str.isspace(), including NEL/control separators and excluding FEFF.
  const blankText = (value) => /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]*$/u.test(value);

  function textFieldInfo(field) {
    const value = $(field.selector).value;
    const count = Array.from(value).length;
    const configured = state.snapshot?.input_limits?.[field.key];
    const limit = Number.isSafeInteger(configured) && configured > 0 ? configured : null;
    let error = '';
    if (!state.connected) error = '本地服务未连接，暂不能提交。请等待重新连接，草稿已保留。';
    else if (limit === null) error = '尚未取得有效的输入上限，暂不能提交。请等待本地服务返回配置，草稿已保留。';
    else if (count > limit) error = `上限 ${limit} 个 Unicode 码点，当前 ${count} 个；请缩短后提交，草稿已保留。`;
    else if (field.required && blankText(value)) error = `不能只含空白；上限 ${limit} 个 Unicode 码点，当前 ${count} 个。`;
    return { value, count, limit, error };
  }

  function renderInputLimits() {
    for (const field of textFields) {
      const { count, limit } = textFieldInfo(field);
      const unavailable = !state.connected || limit === null;
      const over = !unavailable && count > limit;
      const bounds = unavailable ? `输入上限尚未就绪，当前 ${count} 个；连接本地服务后才能提交。` : `上限 ${limit} 个，当前 ${count} 个${over ? '；已超出上限，请缩短后提交' : ''}。`;
      const note = `${field.label}：${bounds}按 Unicode 码点计数：中英文字一般 1 个，普通 emoji 1 个，组合 emoji 可能多个；空白计入。`;
      const hint = $(`${field.selector}-limit`);
      if (hint.textContent !== note) hint.textContent = note;
      hint.classList.toggle('error', over);
      $(field.selector).setAttribute('aria-invalid', String(over));
    }
  }

  function validatedText(selector) {
    const field = textFields.find((item) => item.selector === selector);
    const { value, error } = textFieldInfo(field);
    renderInputLimits();
    if (error) { toast(`${field.label}：${error}`, true, 8000); $(selector).focus(); return null; }
    return value;
  }

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
      if (error.name === 'AbortError') { const timeout = new Error('请求超时、无法确定服务商是否已处理'); timeout.resultUnknown = true; throw timeout; }
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
    const expanded = new Set([...element.querySelectorAll('details[data-evidence]')].filter((item) => item.open).map((item) => item.dataset.evidence));
    element.innerHTML = html;
    for (const item of element.querySelectorAll('details[data-evidence]')) item.open = expanded.has(item.dataset.evidence);
    state.renderKeys[selector] = key;
    const typing = ['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName) || Boolean(document.querySelector('dialog[open]'));
    if (nearBottom && state.followLive && !typing) element.scrollTop = element.scrollHeight;
    else element.scrollTop = scroll;
  }

  const emptyTranscript = $('#transcript-list').innerHTML;
  const emptyGroups = $('#groups-list').innerHTML;
  const emptyQuestions = $('#questions-list').innerHTML;

  const providerLabel = (provider) => ({ codex: 'Codex', api: 'API' })[provider] || 'AI';
  const tokenCount = (value) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;

  function usageText(usage) {
    if (!usage || typeof usage !== 'object') return '令牌用量未返回';
    const input = tokenCount(usage.prompt_tokens ?? usage.input_tokens);
    const output = tokenCount(usage.completion_tokens ?? usage.output_tokens);
    const total = tokenCount(usage.total_tokens) ?? (input !== null && output !== null ? input + output : null);
    if (total === null) return '令牌用量未返回';
    return `${total.toLocaleString('zh-CN')} tokens${input !== null && output !== null ? `（输入 ${input.toLocaleString('zh-CN')} / 输出 ${output.toLocaleString('zh-CN')}）` : ''}`;
  }

  function responseAttribution(item, draft = false) {
    const provider = draft ? item.stream_provider || item.ai_provider : item.ai_provider;
    // An explicit stream provider owns its model, including an unknown/default
    // model. A retry must never inherit the previous answer's different model.
    const model = draft ? item.stream_provider ? item.stream_model : item.stream_model || item.ai_model : item.ai_model;
    return [providerLabel(provider), model, !draft && provider === 'api' ? usageText(item.usage) : ''].filter(Boolean).join(' · ');
  }

  function renderAIResponse(item, answer, question = false) {
    const drafting = ['streaming', 'interrupted'].includes(item.stream_status);
    const pending = isPending(item.status);
    let html = answer ? `<div class="response-attribution">${isDemo() ? '固定示例' : `正式${question ? '回答' : '解释'} · ${escapeHTML(responseAttribution(item))}`}</div><div class="analysis-content">${markdown(answer)}</div>` : '';
    if (drafting) {
      const interrupted = item.stream_status === 'interrupted';
      html += `<section class="stream-draft${interrupted ? ' interrupted' : ''}" aria-label="${interrupted ? '已中断的临时内容' : '正在生成的临时内容'}"><div class="stream-label">${interrupted ? '生成中断 · 以下未形成正式回答' : '正在生成 · 临时内容'}${answer ? ' · 原回答保留在上方' : ''}</div><div class="response-attribution">${escapeHTML(responseAttribution(item, true))}</div><div class="analysis-content">${item.streaming_text ? markdown(item.streaming_text) : `<p>${interrupted ? '未收到可用文字。' : '等待返回内容…'}</p>`}</div></section>`;
    } else if (pending) {
      html += `<p class="analysis-pending">${answer ? '正在重新生成，原回答保留供对照。' : question ? '正在等待 AI 解答，你可以继续提问…' : '已加入处理队列，完成后会显示在这里。你可以继续听课。'}</p>`;
    }
    if (item.error) html += `<p class="analysis-error">${escapeHTML(item.error)}</p>`;
    if (!html) html = `<p class="analysis-pending">${question ? '暂未获得回答。' : '原文已按意群保存。可以针对这一段提问，或复制课堂上下文。'}</p>`;
    return html;
  }

  function renderAISettingsStatus(settings = state.snapshot?.ai_settings || state.aiSettings) {
    if (!settings) return;
    state.aiSettings = settings;
    const active = [providerLabel(settings.active_provider || settings.provider), settings.active_model].filter(Boolean).join(' / ');
    $('#ai-active-settings').textContent = `当前生效：${active}${settings.pending ? '。新配置已保存，等待当前请求结束后切换。' : ''}`;
    $('#ai-active-settings').classList.toggle('pending', Boolean(settings.pending));
    $('#ai-key-status').textContent = settings.key_configured ? settings.key_persisted ? '已有密钥，已在本机保存。同一接口地址下留空不会覆盖。' : '已有密钥，仅本次服务运行有效。同一接口地址下留空不会覆盖。' : '尚未配置密钥。密钥不会回显，也不会保存到浏览器。';
    $('#ai-storage-help').textContent = settings.storage_available === false ? '本机安全存储不可用，密钥只能在本次服务运行期间使用。' : '不勾选时仅本次服务运行使用，关闭服务后需要重新输入；勾选后由本机服务安全保存。';
  }

  function renderAIUsage() {
    const usage = state.snapshot?.ai_usage;
    const requests = tokenCount(usage?.requests) || 0;
    const unknown = tokenCount(usage?.unknown) || 0;
    const reported = tokenCount(usage?.usage_reported_requests) || 0;
    const parts = [`本课 API：${requests ? `${requests} 次请求` : '尚无请求'}`];
    if (requests) {
      parts.push(`完成 ${tokenCount(usage.completed) || 0} / 失败 ${tokenCount(usage.failed) || 0}`);
      if (unknown) parts.push(`结果未知 ${unknown}（可能已计费）`);
      parts.push(reported ? `${usageText(usage)} · ${reported} 次已返回用量` : '令牌用量未返回');
    }
    $('#ai-usage').textContent = parts.join(' · ');
    $('#ai-usage').title = '仅统计当前课堂的 API 请求，不含 Codex 和测试连接。令牌数由服务商返回，不代表实际费用；未返回的用量不计为零。';
  }

  function updateAIFormControls() {
    const apiMode = $('#ai-provider').value === 'api';
    const busy = state.aiSettingsBusy;
    const loading = aiIsLoading();
    $('#ai-api-fields').hidden = !apiMode;
    $('#ai-codex-help').hidden = apiMode;
    for (const selector of ['#ai-provider', '#ai-test-button', '#ai-save-button']) $(selector).disabled = busy;
    for (const selector of ['#ai-base-url', '#ai-model', '#ai-clear-key']) $(selector).disabled = busy || !apiMode;
    $('#ai-remember-key').disabled = busy || !apiMode || state.aiSettings?.storage_available === false;
    $('#ai-key').disabled = busy || !apiMode || $('#ai-clear-key').checked;
    $('#ai-base-url').required = apiMode;
    $('#ai-model').required = apiMode;
    $('#ai-save-button').disabled = busy || loading;
    $('#ai-save-button').textContent = busy ? '请稍候…' : loading ? '正在加载，暂不能更换' : '保存设置';
    $('#ai-force-button').hidden = !loading;
    $('#ai-force-button').disabled = busy;
    $('#ai-switch-protection').hidden = !loading;
    $('#ai-test-button').textContent = apiMode ? '测试连接' : '检查登录';
    $('#ai-settings-form').setAttribute('aria-busy', String(busy));
  }

  function fillAIForm(settings) {
    // Only non-sensitive fields may be restored from the service. Never fill a password.
    renderAISettingsStatus(settings);
    $('#ai-provider').value = settings.provider === 'api' ? 'api' : 'codex';
    $('#ai-base-url').value = settings.base_url || '';
    $('#ai-model').value = settings.model || '';
    $('#ai-key').value = '';
    $('#ai-remember-key').checked = settings.storage_available !== false && settings.remember_key === true;
    $('#ai-clear-key').checked = false;
    updateAIFormControls();
  }

  function readAIForm() {
    const apiMode = $('#ai-provider').value === 'api';
    if (!apiMode) return { provider: 'codex' };
    const body = { provider: 'api', base_url: $('#ai-base-url').value.trim(), model: $('#ai-model').value.trim(), remember_key: $('#ai-remember-key').checked && state.aiSettings?.storage_available !== false, clear_key: $('#ai-clear-key').checked };
    if (apiMode && !body.clear_key && $('#ai-key').value.trim()) body.api_key = $('#ai-key').value.trim();
    return body;
  }

  function showAISettingsResult(message, error = false) {
    $('#ai-settings-result').textContent = message;
    $('#ai-settings-result').hidden = !message;
    $('#ai-settings-result').classList.toggle('error', error);
  }

  async function openAISettings() {
    if (state.aiSettingsBusy) return;
    $('#ai-key').value = '';
    showAISettingsResult('正在读取配置…');
    $('#ai-settings-dialog').showModal();
    state.aiSettingsBusy = true; updateAIFormControls();
    try {
      const settings = await api('/api/ai/settings');
      if (state.snapshot) state.snapshot.ai_settings = settings;
      fillAIForm(settings);
      showAISettingsResult('');
    } catch (error) { showAISettingsResult(error.message, true); }
    finally { state.aiSettingsBusy = false; updateAIFormControls(); }
  }

  function aiIsLoading() {
    if (typeof state.snapshot?.ai_settings?.busy === 'boolean') return state.snapshot.ai_settings.busy;
    return Boolean(state.snapshot?.status?.ai?.busy || Number(state.snapshot?.queue?.ai) > 0 || [...(state.session?.groups || []), ...(state.session?.questions || [])].some((item) => isPending(item.status)));
  }

  async function submitAISettings(testOnly = false, force = false) {
    if (state.aiSettingsBusy) return;
    if (!testOnly && !force && aiIsLoading()) { showAISettingsResult('正在加载，暂不能更换。可等待完成，或点击「强制更换」。', true); return; }
    if (!$('#ai-settings-form').reportValidity()) return;
    const body = readAIForm();
    if (force && !testOnly) body.force = true;
    if (testOnly && body.clear_key) { showAISettingsResult('已选择清除密钥，无法测试。取消勾选后可使用已有或新密钥测试。', true); return; }
    state.aiSettingsBusy = true; updateAIFormControls();
    showAISettingsResult(testOnly ? body.provider === 'api' ? '正在测试当前配置；配置尚未保存…' : '正在检查本机 Codex 登录…' : '正在保存设置…');
    const safeMessage = (value) => body.api_key ? String(value).split(body.api_key).join('[密钥已隐藏]') : String(value);
    try {
      // Flush a previously edited auto-analysis toggle before the backend applies
      // API defaults, so a delayed course save cannot silently turn it back on.
      if (!testOnly) await saveSettings();
      const result = await api(testOnly ? '/api/ai/test' : '/api/ai/settings', { method: testOnly ? 'POST' : 'PUT', body, timeout: testOnly ? 180000 : 30000 });
      if (testOnly) {
        const label = `${providerLabel(result.provider || body.provider)}${result.model ? ` / ${result.model}` : ''}`;
        const apiMode = (result.provider || body.provider) === 'api';
        showAISettingsResult(safeMessage(`${result.ok ? apiMode ? '连接成功' : '登录检查通过' : apiMode ? '连接未通过' : '登录检查未通过'} · ${label}${apiMode ? ` · 测试用量：${usageText(result.usage)}` : ''}。${result.message || ''} 配置尚未保存。`), !result.ok);
      } else {
        const settings = result.settings || result;
        if (state.snapshot) state.snapshot.ai_settings = settings;
        fillAIForm(settings);
        showAISettingsResult(settings.pending ? '设置已保存，当前请求完成后切换。课堂收音和转写可以继续。' : '设置已保存。');
        await poll(true);
      }
    } catch (error) { showAISettingsResult(safeMessage(error.message), true); }
    finally { state.aiSettingsBusy = false; updateAIFormControls(); }
  }

  const viewScope = (sessionId = state.session?.id) => ({ sessionId, epoch: state.sessionEpoch });
  const inView = (scope) => Boolean(scope && state.session?.id === scope.sessionId && state.sessionEpoch === scope.epoch);
  const resultUnknown = (question) => Boolean(question?.result_unknown || question?.duplicate_billing_risk || question?.repeat_billing_required || question?.status === 'result_unknown');
  function newRequestId() { return globalThis.crypto?.randomUUID?.() || `ui-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`; }
  function operationId(sessionId, action, id) {
    const key = `classroom-operation:${sessionId}:${action}:${id}`;
    if (state.requestIds.has(key)) return state.requestIds.get(key);
    let value;
    try { value = localStorage.getItem(key); } catch (_) {}
    value ||= newRequestId(); state.requestIds.set(key, value);
    try { localStorage.setItem(key, value); } catch (_) {}
    return value;
  }
  function finishOperation(sessionId, action, id) {
    const key = `classroom-operation:${sessionId}:${action}:${id}`;
    state.requestIds.delete(key); try { localStorage.removeItem?.(key); } catch (_) {}
  }
  function cacheNotice(status) {
    const current = status || window.studyCache?.status?.();
    if (!current) return;
    $('#study-cache-status').textContent = current.persistent ? '图片暂存和问题草稿保存在此浏览器本机；恢复后请手动重试。' : current.message || '图片和草稿仅当前页保留，刷新或关闭后将丢失。';
    $('#study-cache-status').hidden = false;
    $('#study-cache-status').classList.toggle('error', !current.persistent);
  }
  async function ensureStudyCache() {
    if (state.cachePromise) return state.cachePromise;
    state.cachePromise = (async () => {
      if (window.studyCache) {
        cacheNotice(await window.studyCache.init());
        state.cachedSlides = await window.studyCache.listUploads();
      }
      state.cacheReady = true;
      renderPendingSlides();
    })();
    try { await state.cachePromise; }
    catch (error) { cacheNotice({ persistent: false, message: error.message }); state.cachePromise = null; }
  }
  function rememberDraft(sessionId, text) {
    if (!sessionId) return Promise.resolve();
    state.draftTexts.set(sessionId, text);
    state.draftRevision.set(sessionId, (state.draftRevision.get(sessionId) || 0) + 1);
    const prior = state.draftWrites.get(sessionId) || Promise.resolve();
    const saving = prior.catch(() => {}).then(async () => {
      if (window.studyCache) cacheNotice(await window.studyCache.setDraft(sessionId, text));
    }).catch((error) => cacheNotice({ persistent: false, message: `草稿仅当前页保留：${error.message}` }));
    state.draftWrites.set(sessionId, saving);
    return saving;
  }
  async function restoreDraft(scope) {
    const revision = state.draftRevision.get(scope.sessionId) || 0;
    let draft = state.draftTexts.get(scope.sessionId) || '';
    if (window.studyCache) {
      await state.draftWrites.get(scope.sessionId);
      draft = await window.studyCache.getDraft(scope.sessionId);
      const request = await window.studyCache.getRequest(scope.sessionId);
      if (request) state.legacyRequests.set(scope.sessionId, request);
      else state.legacyRequests.delete(scope.sessionId);
    }
    if (inView(scope) && revision === (state.draftRevision.get(scope.sessionId) || 0)) {
      state.draftTexts.set(scope.sessionId, draft); $('#question-input').value = draft; renderInputLimits();
      renderLegacyRecovery();
    }
  }
  function clearSentDraft(sessionId, text, scope) {
    const saved = state.draftTexts.get(sessionId);
    if (saved === text || (inView(scope) && $('#question-input').value === text)) rememberDraft(sessionId, '');
    if (inView(scope) && $('#question-input').value === text) $('#question-input').value = '';
  }
  function renderLegacyRecovery() {
    const record = state.legacyRequests.get(state.session?.id);
    $('#legacy-request-recovery').hidden = !record;
    $('#legacy-request-check').disabled = state.studyBusy || state.switching;
    $('#legacy-request-check').textContent = record?.kind === 'study' ? '核对原图文请求' : '核对并重试原问题';
    if (record) $('#legacy-request-status').textContent = record.error || (record.kind === 'study' ? '此课堂有一个图文请求结果待核对。先核对原任务，不会自动重新调用；需要发送时会再次确认。' : '此课堂有一个提交结果待核对的问题；不会自动再次调用。可手动核对，若后台尚无任务则使用同一标识提交原问题。');
  }
  async function rememberLegacyRequest(sessionId, record, expectedRequestId = state.legacyRequests.get(sessionId)?.request_id) {
    try {
      if (window.studyCache) cacheNotice(await window.studyCache.setRequest(sessionId, record, expectedRequestId));
      else if (record && expectedRequestId && expectedRequestId !== record.request_id) throw new Error('本课已有未核对的请求，请先恢复原请求。');
    } catch (error) {
      if (window.studyCache) {
        const current = await window.studyCache.getRequest(sessionId);
        if (current) state.legacyRequests.set(sessionId, current); else state.legacyRequests.delete(sessionId);
      }
      renderLegacyRecovery(); throw error;
    }
    if (record) state.legacyRequests.set(sessionId, record); else state.legacyRequests.delete(sessionId);
    renderLegacyRecovery();
  }
  async function submitLegacyQuestion(record, initial = false) {
    if (state.studyBusy || state.switching) return;
    const scope = viewScope(record.body.session_id);
    if (!inView(scope)) return;
    state.studyBusy = true; renderQueue(); renderSlides(); renderLegacyRecovery();
    try {
      await rememberLegacyRequest(scope.sessionId, record);
      const existing = await api(`/api/requests/${encodeURIComponent(record.request_id)}?session_id=${encodeURIComponent(scope.sessionId)}`);
      if (!inView(scope)) return;
      if (!existing.found) {
        await api('/api/ask', { method: 'POST', body: { ...record.body, request_id: record.request_id } });
        clearSentDraft(scope.sessionId, record.body.question, scope);
      }
      await rememberLegacyRequest(scope.sessionId, null, record.request_id);
      if (inView(scope) && existing.found) toast('已找到后台的同一任务，未重新调用；当前草稿保留。');
      await poll(true);
    } catch (error) {
      if (state.legacyRequests.get(scope.sessionId)?.request_id === record.request_id) {
        try { await rememberLegacyRequest(scope.sessionId, { ...record, error: `${error.message}。原请求仍保留，请先核对结果。` }); } catch (_) {}
      }
      if (inView(scope)) toast(error.message, true, 8000);
    } finally { state.studyBusy = false; renderQueue(); renderSlides(); renderLegacyRecovery(); }
  }
  async function recoverStudyRequest(record) {
    if (state.studyBusy || state.switching) return;
    const scope = viewScope(record.session_id || record.params?.session_id);
    if (!inView(scope)) return;
    state.studyBusy = true; renderQueue(); renderSlides(); renderLegacyRecovery();
    let retryParams = null;
    try {
      const found = record.question_id ? { found: true, question: await api(`/api/study/${encodeURIComponent(record.question_id)}?session_id=${encodeURIComponent(scope.sessionId)}`) } : await api(`/api/study/requests/${encodeURIComponent(record.request_id)}?session_id=${encodeURIComponent(scope.sessionId)}`);
      if (found.found) {
        await rememberLegacyRequest(scope.sessionId, null, record.request_id);
        if (inView(scope)) {
          if (found.question.status === 'prepared') openStudyReview(found.question, scope.sessionId);
          showStudyStatus(found.question.status === 'prepared' ? '已找到原材料，请确认后发送。' : '已找到后台的同一任务，未重复调用；当前草稿保留。');
        }
        await poll(true);
      } else if (record.params && record.phase === 'prepare') retryParams = record.params;
      else throw new Error('尚未找到原任务，结果仍待核对；未创建新请求。');
    } catch (error) { if (inView(scope)) showStudyStatus(error.message, true); }
    finally { state.studyBusy = false; renderQueue(); renderSlides(); renderLegacyRecovery(); }
    if (retryParams && inView(scope)) return prepareStudy(retryParams, true);
  }
  async function executeLegacyRetry(operation, confirmDuplicate = false) {
    const { scope, type, id } = operation;
    if (!inView(scope) || state.studyBusy) return;
    state.studyBusy = true; renderQueue(); renderSlides();
    try {
      await saveSettings();
      const requestId = operationId(scope.sessionId, `retry-${type}`, id);
      const found = type === 'audio' ? { found: false } : await api(`/api/requests/${encodeURIComponent(requestId)}?session_id=${encodeURIComponent(scope.sessionId)}`);
      if (!inView(scope)) return;
      if (!found.found) await api(`/api/${type}/${encodeURIComponent(id)}/retry`, { method: 'POST', body: { session_id: scope.sessionId, request_id: requestId, confirm_duplicate: confirmDuplicate } });
      finishOperation(scope.sessionId, `retry-${type}`, id);
      if (inView(scope)) { $('#duplicate-retry-dialog').close(); state.pendingLegacyRetry = null; }
      await poll(true);
      if (inView(scope)) toast(found.found ? '已找到原任务，未重复调用。' : type === 'audio' ? '已加入本机转写队列。' : '已加入 AI 处理队列。');
    } catch (error) { if (inView(scope)) toast(error.message, true); }
    finally { state.studyBusy = false; renderQueue(); renderSlides(); }
  }
  function pendingSlides() { return [...state.cachedSlides, ...state.transientSlides.values()]; }
  function renderPendingSlides() {
    const records = pendingSlides().filter((item) => item.session_id === state.session?.id);
    $('#pending-slides-panel').hidden = records.length === 0;
    $('#clear-pending-slides').disabled = state.slideBusy || state.slideRecoveryBusy;
    $('#pending-slides-list').innerHTML = records.map((item) => {
      if (!state.slideURLs.has(item.id) && typeof URL.createObjectURL === 'function') state.slideURLs.set(item.id, URL.createObjectURL(item.blob));
      const preview = state.slideURLs.get(item.id);
      return `<article class="pending-slide"><div>${preview ? `<img src="${escapeHTML(preview)}" alt="待恢复课件">` : ''}</div><div><strong>${escapeHTML(item.title)}</strong><p class="small">原课堂时间 ${timecode(item.start)} · ${(item.size / 1024 / 1024).toFixed(2)} MiB${item.transient ? ' · 仅当前页保留，未进入本机暂存' : ''}</p><p class="small">${escapeHTML(item.error || '后台结果待核对，未自动重传。')}</p><button type="button" class="text-button" data-upload-retry="${escapeHTML(item.id)}" ${state.slideBusy || state.slideRecoveryBusy ? 'disabled' : ''}>核对并重试</button><button type="button" class="text-button" data-upload-clear="${escapeHTML(item.id)}" ${state.slideBusy || state.slideRecoveryBusy ? 'disabled' : ''}>清除暂存</button></div></article>`;
    }).join('');
  }
  async function updatePendingSlide(record) {
    if (record.transient) state.transientSlides.set(record.id, record);
    else {
      cacheNotice(await window.studyCache.putUpload(record));
      state.cachedSlides = await window.studyCache.listUploads();
    }
    renderPendingSlides();
  }
  async function clearPendingSlide(id) {
    if (state.transientSlides.has(id)) state.transientSlides.delete(id);
    else { cacheNotice(await window.studyCache.deleteUpload(id)); state.cachedSlides = await window.studyCache.listUploads(); }
    if (state.slideURLs.has(id)) { URL.revokeObjectURL?.(state.slideURLs.get(id)); state.slideURLs.delete(id); }
    renderPendingSlides();
  }
  async function recoverSlide(record, firstAttempt = false) {
    if (!record || state.slideBusy || state.switching || state.slideRecoveryBusy) return;
    const scope = viewScope(record.session_id);
    if (!inView(scope)) return;
    state.slideBusy = true; renderSlides(); renderQueue(); renderPendingSlides();
    if (inView(scope)) { $('#slide-status').hidden = false; $('#slide-status').textContent = '正在核对后台保存状态…'; }
    try {
      if (record.transient && window.studyCache) {
        const persistentCandidate = { ...record }; delete persistentCandidate.transient;
        try {
          cacheNotice(await window.studyCache.putUpload(persistentCandidate));
          state.transientSlides.delete(record.id); state.cachedSlides = await window.studyCache.listUploads(); record = persistentCandidate;
        } catch (error) { cacheNotice({ persistent: false, message: `${error.message}。正在手动重试的图片仍仅当前页保留。` }); }
      }
      const result = await api(`/api/slides/requests/${encodeURIComponent(record.id)}?session_id=${encodeURIComponent(record.session_id)}`);
      if (result.found) {
        if (result.available !== true) throw new Error('后台记录已存在，但图片缺失或损坏；原图片暂存仍保留，请先核对，未重复上传。');
      } else {
        await updatePendingSlide({ ...record, status: 'pending', error: '' });
        const query = new URLSearchParams({ session_id: record.session_id, start: String(record.start), title: record.title, request_id: record.id });
        await api(`/api/slides?${query}`, { method: 'POST', headers: { 'Content-Type': record.mime_type }, body: record.blob });
        const receipt = await api(`/api/slides/requests/${encodeURIComponent(record.id)}?session_id=${encodeURIComponent(record.session_id)}`);
        if (!receipt.found || receipt.available !== true) throw new Error('尚未确认后台图片已保存且可查看；暂存保留，重试会先核对结果。');
      }
      await clearPendingSlide(record.id);
      if (inView(scope)) { $('#slide-file').value = ''; $('#slide-status').textContent = `后台已确认保存，原起点 ${timecode(record.start)}；可编辑对应课堂范围。`; }
      await poll(true);
    } catch (error) {
      const message = error.resultUnknown ? '请求超时、无法确定服务商是否已处理' : error.message;
      try { await updatePendingSlide({ ...record, status: error.resultUnknown ? 'unknown' : 'failed', error: message }); } catch (cacheError) { cacheNotice({ persistent: false, message: cacheError.message }); }
      if (inView(scope)) { $('#slide-status').textContent = `${message}。图片保留在待恢复列表，请手动核对并重试。`; $('#slide-status').hidden = false; }
    } finally { state.slideBusy = false; renderSlides(); renderQueue(); renderPendingSlides(); }
  }

  // Classroom time follows captured samples, never wall time. Capture the value
  // synchronously at the user's click, before any settings/network await.
  function classroomAnchor() {
    if (state.capture?.kind === 'classroom' && Number.isFinite(state.capture.segmenter?.time)) return state.capture.segmenter.time;
    return Math.max(0, state.clockSeconds || 0, ...(state.session?.segments || []).map((item) => Number(item.end) || 0), ...(state.session?.audio || []).map((item) => (Number(item.start) || 0) + (Number(item.duration) || 0)));
  }

  const slideURL = (id, sessionId, slide = null) => `/api/slides/${encodeURIComponent(id)}?session_id=${encodeURIComponent(sessionId)}${slide ? `&version=${encodeURIComponent(slide.version || 1)}${slide.sha256 ? `&sha256=${encodeURIComponent(slide.sha256)}` : ''}` : ''}`;
  const rangeText = (range) => range?.start != null ? `${timecode(range.start)} — ${range.end == null ? '持续中' : timecode(range.end)}` : '暂无已转写原文';

  function evidenceHTML(question, sessionId) {
    const evidence = question.evidence || {};
    const slides = evidence.slides || [];
    const warnings = evidence.warnings || [];
    const statuses = question.source_status?.slides || [];
    const changes = (question.source_status?.messages || []).map((message) => `<p class="review-note">${escapeHTML(message)}</p>`).join('');
    const pictures = slides.map((slide) => {
      const info = statuses.find((item) => item.id === slide.id && (item.version || 1) === (slide.version || 1));
      const missing = info?.available === false || ['missing', 'corrupt', 'invalid'].includes(info?.availability);
      const url = slideURL(slide.id, sessionId, slide);
      return `<figure>${missing ? '<div class="unavailable-slide">历史图片缺失或损坏，无法查看；不会用新图替代。</div>' : `<a href="${url}" target="_blank" rel="noopener"><img src="${url}" alt="${escapeHTML(slide.title || slide.id)}" loading="lazy"></a>`}<figcaption>${escapeHTML(slide.title || slide.id)} · v${escapeHTML(slide.version || 1)} · ${escapeHTML(rangeText(slide))}${info?.removed ? ' · 已从课堂选择移除，历史副本保留' : ''}${info?.rotated ? ' · 之后已调整方向，此处为原图' : ''}${info?.time_changed ? ' · 之后已修改时间，此处为原范围' : ''}${info?.message ? ` · ${escapeHTML(info.message)}` : ''}</figcaption></figure>`;
    }).join('');
    return `<div class="study-evidence"><p><strong>请求范围</strong> ${escapeHTML(rangeText(evidence.requested_range))}<br><strong>实际原文范围</strong> ${escapeHTML(rangeText(evidence.actual_range))}</p>${changes}${evidence.incomplete ? '<p class="review-note">尚不能确认原文完整，回答只能依据当前已收到的材料。之后可选择「补齐 / 用新材料解释」。</p>' : ''}${warnings.map((warning) => `<p class="review-note">${escapeHTML(warning)}</p>`).join('')}<div class="evidence-slides">${pictures}</div>${(evidence.segments || []).map((segment) => `<div class="evidence-segment"><span class="timecode">${escapeHTML(rangeText(segment))} · ${escapeHTML(segment.id || '')}</span><p>${escapeHTML(segment.text)}</p></div>`).join('') || '<p class="small muted">此快照没有可用的课堂原文。</p>'}</div>`;
  }

  function studyActions(question) {
    if (!question.study || isDemo()) return '';
    const id = escapeHTML(question.id);
    const pending = isPending(question.status);
    return `${resultUnknown(question) ? '<p class="analysis-error">请求超时、无法确定服务商是否已处理</p><p class="small muted">不会自动再次调用。主动重新请求可能重复计费。</p>' : ''}<div class="study-actions">${question.status === 'prepared' ? `<button class="button subtle" data-study-review="${id}">确认发送</button>` : ''}${!pending && (question.error || question.stream_status === 'interrupted' || resultUnknown(question)) ? `<button class="text-button" data-study-retry="${id}">重试原材料</button>` : ''}${!pending && question.status !== 'prepared' ? `<button class="text-button" data-study-refresh="${id}">补齐 / 用新材料解释</button>` : ''}</div><details class="evidence-details" data-evidence="${id}"><summary>查看本次依据 · ${(question.evidence?.slides || []).length} 张课件</summary>${evidenceHTML(question, state.session.id)}</details>`;
  }

  function showStudyStatus(message, error = false) {
    $('#study-status').textContent = message;
    $('#study-status').hidden = !message;
    $('#study-status').classList.toggle('error', error);
    $('#study-reprepare').hidden = !state.studyFailedParams;
  }

  function renderSlides() {
    const slides = (state.session?.slides || []).filter((item) => !item.removed && !item.removed_at);
    $('#slides-count').textContent = slides.length;
    $('#slides-auto').checked = state.slideSelection === null;
    $('#slides-selection-note').textContent = state.slideSelection === null ? '自动按范围选择' : `已手选 ${state.slideSelection.size} 张（最多 4 张）`;
    $('#include-slides').checked = state.includeImagesPreference ?? slides.length > 0;
    $('#study-range-label').hidden = !$('#include-slides').checked;
    $('#slide-file').disabled = state.slideBusy || state.slideRecoveryBusy || state.switching || isDemo() || !state.connected;
    $('#slides-auto').disabled = state.studyBusy;
    const key = JSON.stringify([state.session?.id, slides, state.slideSelection === null ? null : [...state.slideSelection], state.studyBusy]);
    const html = slides.map((slide) => `<article class="slide-card"><label><input type="checkbox" data-slide-select="${escapeHTML(slide.id)}" ${state.slideSelection?.has(slide.id) ? 'checked' : ''} ${state.slideSelection === null || state.studyBusy ? 'disabled' : ''}><span>${escapeHTML(slide.title || slide.id)}</span></label><button class="slide-thumbnail" data-slide-edit="${escapeHTML(slide.id)}" aria-label="查看 ${escapeHTML(slide.title || slide.id)}"><img src="${slideURL(slide.id, state.session.id)}" alt="${escapeHTML(slide.title || slide.id)}" loading="lazy"></button><div class="slide-caption"><span class="timecode">${escapeHTML(rangeText(slide))}</span><button class="text-button" data-slide-edit="${escapeHTML(slide.id)}">编辑范围</button></div></article>`).join('');
    replaceList('#slides-gallery', html || '<p class="small muted">还没有课件。可添加老师的幻灯片、板书照片或截图。</p>', key);
    for (const button of document.querySelectorAll('[data-study-mode]')) button.disabled = state.studyBusy || state.switching || isDemo() || !state.connected;
    $('#study-reprepare').disabled = state.studyBusy;
    renderPendingSlides();
  }

  async function uploadSlide(file) {
    if (!file || state.slideBusy || state.slideRecoveryBusy || state.switching || !state.session) return;
    if (isDemo()) { toast('请新建真实课程后添加课件。', true); return; }
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) { toast('请选择 PNG、JPEG 或 WebP 图片，一次一张。', true); return; }
    const scope = viewScope();
    const record = { id: newRequestId(), session_id: scope.sessionId, start: classroomAnchor(), title: file.name || '粘贴的课件', blob: file, mime_type: file.type, size: file.size, created_at: new Date().toISOString(), status: 'pending', error: '' };
    state.slideRecoveryBusy = true; renderSlides(); renderQueue();
    try {
      await ensureStudyCache();
      if (!window.studyCache) throw new Error('本机图片暂存组件未加载，请刷新后重试。');
      try { await updatePendingSlide(record); }
      catch (error) {
        if (state.transientSlides.size) throw new Error(`${error.message}。当前页已有一张尚未入库的图片，请先处理；旧项未被覆盖。`);
        record.transient = true; record.error = `${error.message}。此图仅当前页保留，未自动上传；请清理暂存后手动重试。`;
        state.transientSlides.set(record.id, record); renderPendingSlides();
        if (inView(scope)) cacheNotice({ persistent: false, message: record.error });
        return;
      }
      if (inView(scope)) $('#slide-file').value = '';
    } catch (error) { if (inView(scope)) toast(error.message, true); return; }
    finally { state.slideRecoveryBusy = false; renderSlides(); renderQueue(); }
    if (inView(scope)) return recoverSlide(record, true);
  }

  function editSlide(id) {
    const slide = state.session?.slides?.find((item) => item.id === id);
    if (!slide || state.slideBusy) return;
    state.editingSlide = { id, sessionId: state.session.id, epoch: state.sessionEpoch, slide: { ...slide }, rotation: 0, rotationRequest: null };
    $('#slide-preview').src = slideURL(id, state.session.id, slide);
    $('#slide-preview').style.transform = '';
    $('#slide-save-rotation').disabled = true;
    $('#slide-title').value = slide.title || '';
    $('#slide-start').value = String(slide.start || 0);
    $('#slide-end').value = slide.end == null ? '' : String(slide.end);
    $('#slide-edit-status').hidden = true;
    $('#slide-dialog').showModal();
  }

  function previewRotation(delta) {
    const editing = state.editingSlide;
    if (!editing || state.slideBusy || !inView(editing)) return;
    editing.rotation = (editing.rotation + delta + 360) % 360;
    $('#slide-preview').style.transform = `rotate(${editing.rotation}deg)`;
    $('#slide-save-rotation').disabled = editing.rotation === 0;
  }
  async function saveRotation() {
    const editing = state.editingSlide;
    if (!editing || state.slideBusy || !editing.rotation || !inView(editing)) return;
    const requestId = operationId(editing.sessionId, 'rotate', `${editing.id}:${editing.slide.version || 1}:${editing.rotation}`);
    state.slideBusy = true; renderSlides(); renderQueue();
    $('#slide-save-rotation').disabled = true;
    try {
      const existing = await api(`/api/slides/requests/${encodeURIComponent(requestId)}?session_id=${encodeURIComponent(editing.sessionId)}`);
      if (existing.found && !existing.available) throw new Error('此方向版本已有记录，但文件不可查看，请先核对，未重复旋转。');
      if (!existing.found) await api(`/api/slides/${encodeURIComponent(editing.id)}/rotate`, { method: 'POST', body: { session_id: editing.sessionId, degrees: editing.rotation, request_id: requestId } });
      if (inView(editing) && state.editingSlide === editing) { $('#slide-dialog').close(); $('#slide-status').hidden = false; $('#slide-status').textContent = '方向已保存为新版本，历史回答继续使用原图。'; }
      await poll(true);
    } catch (error) { if (inView(editing) && state.editingSlide === editing) { $('#slide-edit-status').textContent = `${error.message}。再次保存会先核对原请求。`; $('#slide-edit-status').hidden = false; } }
    finally { state.slideBusy = false; if (state.editingSlide === editing) $('#slide-save-rotation').disabled = !editing.rotation; renderSlides(); renderQueue(); }
  }
  async function removeSlide() {
    const editing = state.editingSlide;
    if (!editing || state.slideBusy || !inView(editing)) return;
    state.slideBusy = true; renderSlides(); renderQueue();
    try {
      await api(`/api/slides/${encodeURIComponent(editing.id)}?session_id=${encodeURIComponent(editing.sessionId)}`, { method: 'DELETE' });
      if (inView(editing) && state.editingSlide === editing) { state.slideSelection?.delete(editing.id); $('#slide-dialog').close(); $('#slide-status').hidden = false; $('#slide-status').textContent = '课件已从本课的新问题选择中移除，历史图片副本仍保留。'; }
      await poll(true);
    } catch (error) { if (inView(editing) && state.editingSlide === editing) { $('#slide-edit-status').textContent = error.message; $('#slide-edit-status').hidden = false; } }
    finally { state.slideBusy = false; renderSlides(); renderQueue(); }
  }

  function frozenStudyParams(mode, question = '', explain = true) {
    const params = { session_id: state.session.id, mode, anchor: classroomAnchor(), question, explain, include_images: $('#include-slides').checked };
    if (mode === 'group' && state.selectedGroup) params.group_id = state.selectedGroup;
    if (state.slideSelection !== null) params.slide_ids = [...state.slideSelection];
    return params;
  }

  function captureStudySelection(params) {
    const segments = state.session?.segments || [];
    // Only identities/versions leave the browser. The server reconstructs and
    // validates its own stored source, rather than trusting client text.
    if (!Array.isArray(params.seen_segments)) params.seen_segments = segments.map((item) => ({ id: item.id, version: item.version || 0 }));
    if (!Array.isArray(params.seen_slides)) params.seen_slides = (state.session?.slides || []).filter((item) => !item.removed && !item.removed_at).map((item) => ({ id: item.id, version: item.version || 1, interval_version: item.interval_version || 0, sha256: item.sha256, start: item.start, end: item.end ?? null, title: item.title || '' }));
    if (params.mode !== 'group' || Array.isArray(params.seen_group_segment_ids)) return true;
    const groups = state.session?.groups || [];
    let group = params.group_id ? groups.find((item) => item.id === params.group_id) : null;
    if (!params.group_id) {
      const candidates = groups.map((item, index) => {
        const members = segments.filter((segment) => (item.segment_ids || []).includes(segment.id));
        const start = item.start ?? (members.length ? Math.min(...members.map((segment) => segment.start)) : null);
        const end = item.end ?? (members.length ? Math.max(...members.map((segment) => segment.end)) : null);
        return { item, index, start, end };
      }).filter((item) => Number.isFinite(item.start) && Number.isFinite(item.end) && item.start <= params.anchor);
      candidates.sort((left, right) => right.end - left.end || right.start - left.start || right.index - left.index);
      group = candidates[0]?.item;
    }
    if (!group) return false;
    params.group_id = group.id;
    params.seen_group_segment_ids = [...(group.segment_ids || [])];
    return true;
  }

  function renderStudyReview() {
    const review = state.studyReview;
    if (!review) return;
    const question = review.question;
    const destination = question.destination || {};
    const hasImages = Boolean(question.evidence?.slides?.length);
    const unsupported = hasImages && question.vision?.state === 'unsupported';
    const changed = Boolean(question.source_status?.changed);
    const unavailable = Boolean(question.source_status?.unavailable);
    const duplicate = resultUnknown(question);
    const visionText = !hasImages ? '本次仅发送原文。' : unsupported ? '此配置已明确不支持图片，不能带图发送。可选择「仅用原文」，保留同一份原文快照。' : question.vision?.state === 'supported' ? question.vision.message || '此配置曾完成同数量图片的请求，不代表本次一定成功或答案正确。' : '图片能力未知：此配置尚未确认支持图片，发送可能失败。';
    $('#study-review').innerHTML = `<dl class="study-destination"><dt>接入</dt><dd>${escapeHTML(destination.label || providerLabel(destination.provider))} (${escapeHTML(destination.provider || '')})</dd><dt>地址</dt><dd>${escapeHTML(destination.base_url || '本机 Codex · 当前登录账号')}</dd><dt>模型</dt><dd>${escapeHTML(destination.model || 'Codex 当前配置模型')}</dd></dl><p class="${unsupported || question.vision?.state === 'unknown' ? 'review-note' : 'dialog-help'}">${escapeHTML(visionText)}</p><p class="study-review-question"><strong>问题</strong><br>${escapeHTML(question.question)}</p>${evidenceHTML(question, review.sessionId)}`;
    $('#study-original-label').hidden = !changed;
    $('#study-duplicate-label').hidden = !duplicate;
    $('#study-refresh-materials').hidden = !changed;
    $('#study-refresh-materials').disabled = state.studyBusy;
    const blocked = unavailable || unsupported;
    $('#study-send').disabled = state.studyBusy || (!review.action && blocked) || (!review.action && question.status !== 'prepared') || (changed && !$('#study-confirm-original').checked && !review.action) || (duplicate && !$('#study-confirm-duplicate').checked);
    $('#study-send').textContent = review.action ? '确认重新请求' : changed ? '确认使用原快照' : '确认发送';
    $('#study-text-only').disabled = state.studyBusy;
    $('#study-text-only').hidden = !hasImages || Boolean(review.action);
    $('#study-remember-label').hidden = !hasImages || unsupported;
    $('#study-remember').disabled = state.studyBusy || unsupported;
  }

  function openStudyReview(question, sessionId = state.session.id, options = {}) {
    if (state.session?.id !== sessionId) return;
    state.studyReview = { question, sessionId, scope: viewScope(sessionId), ...options };
    $('#study-remember').checked = false;
    $('#study-confirm-original').checked = false;
    $('#study-confirm-duplicate').checked = options.duplicateAccepted === true;
    $('#study-send-status').hidden = true;
    renderStudyReview();
    if (!$('#study-dialog').open) $('#study-dialog').showModal();
  }

  async function sendPrepared(question, sessionId, confirmImages = false, remember = false, flags = {}, scope = viewScope(sessionId)) {
    if (question.evidence?.slides?.length && question.vision?.state === 'unsupported') throw new Error('此配置不支持图片，请选择「仅用原文」。');
    if (question.source_status?.unavailable) throw new Error('历史材料缺失或损坏，不能带图发送；可仅用原文或重新取材。');
    if (question.source_status?.changed && !flags.confirm_original) throw new Error('材料已有修改，请再次确认原快照或重新取材。');
    if (resultUnknown(question) && !flags.confirm_duplicate) throw new Error('请求超时、无法确定服务商是否已处理。重新请求前请确认可能重复计费。');
    const prior = state.legacyRequests.get(sessionId);
    const requestId = question.request_id || (prior?.kind === 'study' && prior.question_id === question.id ? prior.request_id : null) || operationId(sessionId, 'study-send', question.id);
    if (prior && prior.request_id !== requestId) throw new Error('本课另有未核对的请求，请先核对原请求，当前材料保留。');
    const receipt = { ...(prior || {}), kind: 'study', request_id: requestId, session_id: sessionId, question_id: question.id, phase: 'send' };
    await rememberLegacyRequest(sessionId, receipt);
    try {
      await api(`/api/study/${encodeURIComponent(question.id)}/send`, { method: 'POST', body: { session_id: sessionId, destination_token: question.destination?.token, confirm_images: confirmImages, remember, source_token: question.source_status?.token, ...flags } });
      await rememberLegacyRequest(sessionId, null, requestId);
    } catch (error) {
      if ([400, 403, 409, 422].includes(error.status)) await rememberLegacyRequest(sessionId, null, requestId);
      else { try { await rememberLegacyRequest(sessionId, { ...receipt, error: `${error.message}。请先核对原任务，不要直接创建新请求。` }); } catch (_) {} }
      throw error;
    }
    const draftKey = `${sessionId}:${question.id}`;
    const draft = state.studyDrafts.get(draftKey);
    if (draft != null) clearSentDraft(sessionId, draft, scope);
    state.studyDrafts.delete(draftKey);
    if (inView(scope) && state.studyReview?.sessionId === sessionId && state.studyReview.question.id === question.id) { $('#study-dialog').close(); state.studyReview = null; }
    if (inView(scope)) showStudyStatus('已加入 AI 队列，提问时的材料已固定。');
  }

  async function prepareStudy(params, reviewOnly = false) {
    if (state.studyBusy || state.switching) return;
    const scope = viewScope(params.session_id);
    if (!inView(scope)) return;
    params.request_id ||= newRequestId();
    const pending = state.legacyRequests.get(params.session_id);
    if (pending && pending.request_id !== params.request_id) { showStudyStatus('本课还有结果待核对的请求，请先核对原任务。', true); return; }
    if (isDemo()) { toast('演示课程不调用 AI，请先新建真实课程。'); return; }
    // Freeze before saveSettings or prepare can yield. On a selection retry the
    // original metadata remains intact; only an explicit refresh captures anew.
    if (!captureStudySelection(params)) { state.studyFailedParams = null; showStudyStatus('还没有可选的意群，请先整理原文或选择最近一段时间。', true); return; }
    if (params.slide_ids?.some((id) => !params.seen_slides.some((item) => item.id === id))) { state.studyFailedParams = params; showStudyStatus('所选课件在上次点击时尚不存在。要加入新图片，请重新点击快捷解释或发送问题。', true); return; }
    if (params.slide_ids?.length > 4) { state.studyFailedParams = params; showStudyStatus('手选超过 4 张，请在课件中减少选择，再重试同一时间段。', true); $('#slides-panel').open = true; return; }
    state.studyBusy = true; renderSlides(); renderQueue();
    state.studyFailedParams = null; showStudyStatus('正在固定课堂原文与课件…');
    let prepared = null;
    try {
      await rememberLegacyRequest(params.session_id, { kind: 'study', request_id: params.request_id, session_id: params.session_id, params, phase: 'prepare' });
      await saveSettings();
      const existing = await api(`/api/study/requests/${encodeURIComponent(params.request_id)}?session_id=${encodeURIComponent(params.session_id)}`);
      prepared = existing.found ? existing.question : await api('/api/study/prepare', { method: 'POST', body: params });
      await rememberLegacyRequest(params.session_id, { kind: 'study', request_id: params.request_id, session_id: params.session_id, params, phase: 'prepare', question_id: prepared.id });
      if (!params.explain) state.studyDrafts.set(`${params.session_id}:${prepared.id}`, params.question);
      if (!inView(scope)) { await rememberLegacyRequest(params.session_id, null, params.request_id); await poll(true); return; }
      if (prepared.status !== 'prepared') { await rememberLegacyRequest(params.session_id, null, params.request_id); showStudyStatus('已找到此前的同一任务，未重复调用。'); await poll(true); return; }
      const hasImages = Boolean(prepared.evidence?.slides?.length);
      if (reviewOnly || hasImages && (prepared.requires_confirmation || prepared.vision?.state === 'unsupported') || prepared.source_status?.changed || prepared.source_status?.unavailable || resultUnknown(prepared)) {
        await rememberLegacyRequest(params.session_id, null, params.request_id);
        openStudyReview(prepared, params.session_id);
        showStudyStatus('材料已固定，确认发送后开始解释。');
      } else await sendPrepared(prepared, params.session_id, false, false, {}, scope);
      await poll(true);
    } catch (error) {
      if (!prepared && [400, 403, 422].includes(error.status) && state.legacyRequests.get(params.session_id)?.request_id === params.request_id) await rememberLegacyRequest(params.session_id, null, params.request_id);
      if (inView(scope)) {
        if (prepared) openStudyReview(prepared, params.session_id);
        else state.studyFailedParams = params;
        showStudyStatus(error.message, true);
        if (error.status === 422 && /4.*张|超过\s*4|最多\s*4|至多\s*4/.test(error.message)) { $('#slides-panel').open = true; toast(`${error.message} 请在课件中关闭自动选择并手动勾选，然后重试同一时间段。`, true, 9000); }
      }
    } finally { state.studyBusy = false; renderSlides(); renderQueue(); renderStudyReview(); }
  }

  async function confirmStudy(textOnly = false) {
    if (state.studyBusy || !state.studyReview) return;
    const review = state.studyReview;
    if (!inView(review.scope)) return;
    if (textOnly && state.legacyRequests.has(review.sessionId)) { $('#study-send-status').textContent = '前一次发送结果仍待核对，请先核对原请求，再选择仅用原文。'; $('#study-send-status').hidden = false; return; }
    let question = review.question;
    const duplicateAccepted = $('#study-confirm-duplicate').checked;
    if (resultUnknown(question) && !duplicateAccepted) { $('#study-send-status').textContent = '请求超时、无法确定服务商是否已处理。请确认可能重复计费后再请求。'; $('#study-send-status').hidden = false; return; }
    if (review.action === 'retry') return retryStudy(question, duplicateAccepted);
    if (review.action === 'refresh') return refreshStudy(question, duplicateAccepted);
    const originalAccepted = $('#study-confirm-original').checked;
    state.studyBusy = true; renderStudyReview(); renderQueue();
    $('#study-send-status').hidden = false; $('#study-send-status').textContent = '正在提交…';
    try {
      if (textOnly) {
        const originalId = question.id;
        const requestId = operationId(review.sessionId, 'text-only', originalId);
        const found = await api(`/api/study/requests/${encodeURIComponent(requestId)}?session_id=${encodeURIComponent(review.sessionId)}`);
        question = found.found ? found.question : await api(`/api/study/${encodeURIComponent(originalId)}/text-only`, { method: 'POST', body: { session_id: review.sessionId, request_id: requestId, confirm_duplicate: duplicateAccepted } });
        const draftKey = `${review.sessionId}:${originalId}`;
        if (state.studyDrafts.has(draftKey)) state.studyDrafts.set(`${review.sessionId}:${question.id}`, state.studyDrafts.get(draftKey));
        if (!inView(review.scope)) { await poll(true); return; }
        state.studyReview = { question, sessionId: review.sessionId, scope: review.scope };
        if (question.status !== 'prepared') { $('#study-dialog').close(); state.studyReview = null; await poll(true); return; }
        if (question.source_status?.changed) { openStudyReview(question, review.sessionId, { duplicateAccepted }); return; }
      }
      await sendPrepared(question, review.sessionId, !textOnly, !textOnly && $('#study-remember').checked, { confirm_original: originalAccepted, confirm_duplicate: duplicateAccepted }, review.scope);
      await poll(true);
    } catch (error) {
      if (inView(review.scope)) { $('#study-send-status').textContent = error.message; $('#study-send-status').hidden = false; }
      await poll(true); // Refresh a changed destination; never resend automatically.
    } finally { state.studyBusy = false; renderStudyReview(); renderSlides(); renderQueue(); }
  }

  async function retryStudy(question, confirmDuplicate = false) {
    if (state.studyBusy) return;
    const sessionId = state.session.id;
    if (state.legacyRequests.has(sessionId)) { showStudyStatus('本课还有结果待核对的请求，请先核对原任务，再主动重试。', true); return; }
    const scope = viewScope(sessionId);
    if (resultUnknown(question) && !confirmDuplicate) { openStudyReview(question, sessionId, { action: 'retry' }); return; }
    state.studyBusy = true; renderSlides(); renderQueue();
    try {
      const requestId = operationId(sessionId, 'retry', question.id);
      const found = await api(`/api/study/requests/${encodeURIComponent(requestId)}?session_id=${encodeURIComponent(sessionId)}`);
      const prepared = found.found ? found.question : await api(`/api/study/${encodeURIComponent(question.id)}/retry`, { method: 'POST', body: { session_id: sessionId, request_id: requestId, confirm_duplicate: confirmDuplicate } });
      if (inView(scope)) {
        if (prepared.status === 'prepared') openStudyReview(prepared, sessionId, { duplicateAccepted: confirmDuplicate });
        showStudyStatus(prepared.status === 'prepared' ? '已保留原材料快照，请确认后重试。' : '已找到此前的同一任务，未重复调用。');
      }
      await poll(true);
    } catch (error) { if (inView(scope)) showStudyStatus(error.message, true); }
    finally { state.studyBusy = false; renderStudyReview(); renderSlides(); renderQueue(); }
  }

  function refreshStudy(question, confirmDuplicate = false) {
    if (state.legacyRequests.has(state.session?.id)) { showStudyStatus('本课还有结果待核对的请求，请先核对原任务，再重新取材。', true); return; }
    if (resultUnknown(question) && !confirmDuplicate) { openStudyReview(question, state.session.id, { action: 'refresh' }); return; }
    const params = { ...question.request_params, session_id: state.session.id, anchor: question.evidence?.requested_range?.end, question: question.question, explain: question.explain !== false };
    delete params.request_id;
    for (const key of ['seen_segments', 'seen_slides', 'seen_group_segment_ids']) delete params[key];
    if (!Number.isFinite(params.anchor)) { showStudyStatus('这条记录没有有效的原始时间范围，无法补齐。', true); return; }
    if (!params.mode) params.mode = question.group_id ? 'group' : 'recent120';
    return prepareStudy(params);
  }

  function render() {
    renderInputLimits();
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
      let content = renderAIResponse(group, analysis);
      if (group.partial) content = '<p class="review-note">阶段整理：老师可能仍在继续讲解。</p>' + content;
      const retry = (group.stale && !pending || ['saved', 'error', 'failed', 'disabled', 'skipped'].includes(group.status)) && !isDemo() ? `<button class="text-button" data-retry-group="${escapeHTML(group.id)}">${group.stale ? '重新分析' : error ? '重试分析' : '分析本段'}</button>` : '';
      return `<article class="group-card${state.selectedGroup === group.id ? ' selected' : ''}" id="group-${escapeHTML(group.id)}"><div class="group-topline"><strong>意群 ${String(index + 1).padStart(2, '0')}</strong><div class="group-meta"><span class="timecode">${timecode(group.start)}</span><span class="group-state ${escapeHTML(group.status || '')}">${escapeHTML(statusLabel(group.status))}</span></div></div>${group.stale ? `<p class="stale-note">${escapeHTML(group.stale_reason || '原文已修改，以下解释可能需要更新。')} 可点击「重新分析」。</p>` : ''}<blockquote class="group-source">${escapeHTML(source)}</blockquote>${content}<div class="group-bottom"><span>${escapeHTML(group.id)} · ${(group.segment_ids || []).length} 段原文</span><span class="group-actions">${retry}<button class="text-button" data-group="${escapeHTML(group.id)}">基于本段提问 ↗</button></span></div></article>`;
    }).join('');
    replaceList('#groups-list', groupsHTML || emptyGroups, `${session.id}:${state.focusMode}:${state.selectedGroup}:${JSON.stringify(groups)}:${JSON.stringify(segments.map((segment) => [segment.id, segment.text]))}`);
    const questionsHTML = questions.map((question) => `<article class="question-block"><p class="user-question">${escapeHTML(question.question)}</p><div class="answer-label">${isDemo() ? '示例回答 · 非 AI 实时生成' : 'AI · 随堂解答'}${question.group_id ? ` · ${escapeHTML(question.group_id)}` : ''}</div>${question.stale ? `<p class="stale-note">本次问答基于提问时的原文，之后相关内容已有修改。${question.answer ? '原回答保留供对照。' : ''}${question.stale_reason ? ` ${escapeHTML(question.stale_reason)}` : ''}</p>` : ''}<div class="question-answer">${question.study && question.status === 'prepared' ? '<p class="analysis-pending">材料已固定，等待确认发送。</p>' : renderAIResponse(question, question.answer, true)}${!question.study && !isPending(question.status) && (question.error || question.stream_status === 'interrupted') && !isDemo() ? `<button class="text-button" data-retry-question="${escapeHTML(question.id)}">重试回答</button>` : ''}${studyActions(question)}</div></article>`).join('');
    replaceList('#questions-list', questionsHTML || emptyQuestions, `${session.id}:${JSON.stringify(questions)}`);
    renderEngineStatus();
    renderAISettingsStatus();
    renderAIUsage();
    renderLegacyRecovery();
    updateAIFormControls();
    renderSlides();
    if (state.studyReview?.sessionId === session.id) {
      const current = questions.find((item) => item.id === state.studyReview.question.id);
      if (current) {
        if (current.destination?.token !== state.studyReview.question.destination?.token) {
          $('#study-remember').checked = false;
          $('#study-send-status').textContent = 'AI 目的地已变化，请核对新地址和模型后重新确认。';
          $('#study-send-status').hidden = false;
        }
        if (current.source_status?.token !== state.studyReview.question.source_status?.token) {
          $('#study-confirm-original').checked = false;
          if (current.source_status?.changed) { $('#study-send-status').textContent = '材料已有修改，请核对原快照，或选择重新取材。'; $('#study-send-status').hidden = false; }
        }
        state.studyReview.question = current;
        renderStudyReview();
      }
    }
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
    const aiSettings = state.snapshot?.ai_settings || state.aiSettings || {};
    for (const [key, label] of [['asr', '本机转写'], ['ai', providerLabel(statuses.ai?.provider || aiSettings.active_provider || aiSettings.provider || 'codex')]]) {
      const info = statuses[key] || (key === 'ai' ? statuses.codex : null) || {};
      const apiMode = key === 'ai' && (info.provider || aiSettings.active_provider || aiSettings.provider) === 'api';
      const ready = info.ready === true || info.available === true || info.authenticated === true || info.logged_in === true || info.configured === true || ['ready', 'connected', 'authenticated', 'ok'].includes(info.status);
      const error = info.error || info.last_error || info.status === 'error' || info.status === 'unavailable';
      const element = $(`#${key === 'ai' ? 'codex' : key}-status`);
      const detail = info.message || info.detail || info.error || info.last_error || info.warning || '';
      const model = key === 'asr' ? info.active_model_name || info.model_name || info.model : info.model || aiSettings.active_model;
      const device = info.active_device || info.device;
      const stateText = key === 'ai' ? info.busy ? '正在生成' : error ? '需检查' : info.inference_verified ? '调用已验证' : apiMode ? info.configured ? '已配置 · 待验证调用' : '待配置' : info.logged_in ? '已登录 · 待验证调用' : info.installed === false ? '未安装' : '待登录' : ready ? `可用 · ${(device || '本机').toUpperCase()}` : error ? '需检查' : info.loading || info.status === 'loading' ? '准备中' : '待就绪';
      element.textContent = `${label} · ${stateText}${model ? ` / ${model}` : ''}`;
      element.title = [detail, device && `运行设备：${device}`, info.pending_switch && '模式请求已保存，将在下一段音频切换。', key === 'ai' && aiSettings.pending && '新配置已保存，等待当前请求结束后切换。', key === 'ai' && !apiMode && info.logged_in === false && '请在启动终端运行 codex login', info.last_elapsed != null && `最近一段转写耗时：${Number(info.last_elapsed).toFixed(1)} 秒`, info.last_inference_seconds != null && `最近一次 AI 耗时：${Number(info.last_inference_seconds).toFixed(1)} 秒`].filter(Boolean).join('\n');
      element.className = `status-pill ${error ? 'error' : ready ? 'ready' : 'warning'}`;
    }
    const jobs = state.snapshot?.jobs || [];
    const audioJobs = state.snapshot?.queue?.audio ?? jobs.filter((job) => ['audio', 'asr', 'transcribe'].includes(job.kind) && isPending(job.status)).length;
    const aiJobs = state.snapshot?.queue?.ai ?? jobs.filter((job) => !['audio', 'asr', 'transcribe'].includes(job.kind) && isPending(job.status)).length;
    $('#ai-queue').textContent = aiJobs ? `AI 待处理 ${aiJobs}` : '';
    const pending = [];
    if (audioJobs) pending.push(`本机转写 ${audioJobs} 项`);
    if (aiJobs) pending.push(`AI ${aiJobs} 项`);
    const lag = Number(state.snapshot?.queue?.lag_seconds) || 0;
    if (lag > 0) pending.push(`转写落后约 ${Math.ceil(lag)} 秒`);
    $('#processing-status').textContent = pending.length ? `${pending.join(' · ')}，完成后自动显示` : '音频 → 本机转写 → 意群 → AI';
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
    $('#ai-pause-title').textContent = ({ network: '网络不可用，AI 已暂停', quota: '额度不足，AI 已暂停', auth: 'AI 认证需要检查', timeout: 'AI 响应超时，已暂停' })[ai.kind] || 'AI 已暂停';
    $('#ai-pause-reason').textContent = `${ai.reason || '尚未分析的课堂内容和问题已保留。'}${aiSettings.pending ? ' 请等待当前请求结束、接入切换完成后再恢复。' : ''}`;
    document.querySelectorAll('[data-ai-resume]').forEach((button) => { button.disabled = state.aiResuming || Boolean(aiSettings.pending); });
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
      $('#glossary-status').textContent = selected.error ? `生成未完成：${selected.error}` : pending ? 'AI 正在生成术语，可以关闭窗口继续听课。' : selected.glossary?.length ? `已保存 ${selected.glossary.length} 个术语${selected.generated_at ? ` · ${new Date(selected.generated_at).toLocaleString('zh-CN')}` : ''}` : '尚无术语表。点击生成后会使用当前 AI 接入。';
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
    const busy = state.captureMode !== 'idle' || queue.length > 0 || state.finalizing.size > 0 || state.switching || state.slideBusy || state.slideRecoveryBusy || state.studyBusy;
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
      if (previousSession && previousSession !== snapshot.session?.id) rememberDraft(previousSession, $('#question-input').value);
      state.snapshot = snapshot;
      state.session = snapshot.session;
      state.connected = true;
      $('#connection-banner').hidden = true;
      $('#connection-dot').className = 'connection-dot ready';
      $('#connection-label').textContent = '已连接本地服务';
      if (previousSession !== state.session?.id) {
        state.sessionEpoch++;
        state.selectedGroup = null;
        state.slideSelection = null;
        state.includeImagesPreference = null;
        state.studyFailedParams = null;
        state.studyReview = null;
        state.editingSlide = null;
        state.editingSegment = null;
        state.pendingLegacyRetry = null;
        $('#study-dialog').close();
        $('#slide-dialog').close();
        $('#edit-segment-dialog').close();
        $('#duplicate-retry-dialog').close();
        $('#slide-file').value = '';
        $('#slide-status').textContent = ''; $('#slide-status').hidden = true;
        $('#question-input').value = state.draftTexts.get(state.session?.id) || '';
        showStudyStatus('');
        state.renderKeys = {};
        state.settingsDirty = false;
        const persistedTime = Number(localStorage.getItem(`classroom-time-${state.session?.id}`)) || 0;
        state.clockSeconds = Math.max(persistedTime, ...(state.session?.segments || []).map((segment) => Number(segment.end) || 0), ...(state.session?.audio || []).map((item) => (Number(item.start) || 0) + (Number(item.duration) || 0)));
        $('#question-context').hidden = true;
        await restoreDraft(viewScope());
      }
      render();
    } catch (error) {
      state.connected = false;
      $('#connection-banner').textContent = error.message + ' 录音中的片段会暂存在当前浏览器。';
      $('#connection-banner').hidden = false;
      $('#connection-dot').className = 'connection-dot error';
      $('#connection-label').textContent = '本地服务未连接';
      renderInputLimits();
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
    const scope = viewScope();
    if (state.settingsPromise) await state.settingsPromise;
    if (!inView(scope)) return;
    if (!state.settingsDirty || !state.session) return;
    const generation = state.saveGeneration;
    const payload = { session_id: scope.sessionId, title: $('#course-title').value.trim() || '未命名课程', terms: $('#course-terms').value.trim(), auto_analyze: $('#auto-analyze').checked };
    state.settingsPromise = api('/api/settings', { method: 'PUT', body: payload });
    try {
      await state.settingsPromise;
      if (!inView(scope)) return;
      if (generation === state.saveGeneration) state.settingsDirty = false;
      state.session.title = payload.title;
      state.session.settings = { ...state.session.settings, ...payload };
      $('#save-status').textContent = state.settingsDirty ? '正在保存…' : '已保存';
    } catch (error) {
      if (inView(scope)) $('#save-status').textContent = '保存失败';
      throw error;
    } finally {
      state.settingsPromise = null;
    }
  }

  function canSwitch() {
    if (state.captureMode !== 'idle' || state.uploadQueue.length || state.finalizing.size || state.switching || state.slideBusy || state.slideRecoveryBusy || state.studyBusy) {
      toast('请先暂停录音，并等待音频、课件和问题提交完成后再切换课程。', true);
      return false;
    }
    return true;
  }

  async function newSession(title) {
    state.switching = true;
    renderQueue(); renderSlides();
    try {
      await saveSettings();
      await api('/api/sessions', { method: 'POST', body: { title: title || `课堂笔记 ${new Date().toLocaleDateString('zh-CN')}` } });
      state.renderKeys = {};
      await poll(true);
    } finally {
      state.switching = false;
      renderQueue(); renderSlides();
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
  $('#auto-analyze').addEventListener('change', () => { markSettingsDirty(); renderAnalysisMode(); if ($('#auto-analyze').checked) toast('逐段解释已开启。新的意群文字将发送至所选 AI 服务；API 模式会产生用量。'); });

  $('#ai-settings-button').addEventListener('click', openAISettings);
  $('#ai-provider').addEventListener('change', updateAIFormControls);
  $('#ai-clear-key').addEventListener('change', () => { if ($('#ai-clear-key').checked) $('#ai-key').value = ''; updateAIFormControls(); });
  $('#ai-settings-form').addEventListener('submit', (event) => { event.preventDefault(); return submitAISettings(); });
  $('#ai-test-button').addEventListener('click', () => submitAISettings(true));
  $('#ai-force-button').addEventListener('click', () => submitAISettings(false, true));
  $('#ai-settings-dialog').addEventListener('close', () => { $('#ai-key').value = ''; $('#ai-clear-key').checked = false; });
  $('#ai-settings-dialog').addEventListener('cancel', () => { $('#ai-key').value = ''; $('#ai-clear-key').checked = false; });

  $('#slide-file').addEventListener('change', () => uploadSlide($('#slide-file').files?.[0]));
  $('#pending-slides-list').addEventListener('click', async (event) => {
    const button = event.target.closest('[data-upload-retry], [data-upload-clear]');
    if (!button || state.slideBusy || state.slideRecoveryBusy || state.switching) return;
    const id = button.dataset.uploadRetry || button.dataset.uploadClear;
    const record = pendingSlides().find((item) => item.id === id && item.session_id === state.session?.id);
    if (!record) return;
    if (button.dataset.uploadRetry) return recoverSlide(record);
    const scope = viewScope(); state.slideRecoveryBusy = true; renderPendingSlides(); renderQueue();
    try { await clearPendingSlide(id); }
    catch (error) { if (inView(scope)) toast(error.message, true); }
    finally { state.slideRecoveryBusy = false; renderPendingSlides(); renderQueue(); }
  });
  $('#clear-pending-slides').addEventListener('click', async () => {
    if (!state.session || state.slideBusy || state.slideRecoveryBusy) return;
    const scope = viewScope(); state.slideRecoveryBusy = true; renderPendingSlides(); renderQueue();
    const ids = pendingSlides().filter((item) => item.session_id === scope.sessionId).map((item) => item.id);
    try {
      if (window.studyCache) { cacheNotice(await window.studyCache.clearUploads(scope.sessionId)); state.cachedSlides = await window.studyCache.listUploads(); }
      for (const [id, item] of state.transientSlides) if (item.session_id === scope.sessionId) state.transientSlides.delete(id);
      for (const id of ids) if (state.slideURLs.has(id)) { URL.revokeObjectURL?.(state.slideURLs.get(id)); state.slideURLs.delete(id); }
      if (inView(scope)) { $('#slide-status').textContent = '已清理本课浏览器暂存，后台已保存的课件和历史回答不受影响。'; $('#slide-status').hidden = false; }
    } catch (error) { if (inView(scope)) toast(error.message, true); }
    finally { state.slideRecoveryBusy = false; renderPendingSlides(); renderQueue(); }
  });
  $('#slide-paste').addEventListener('paste', (event) => {
    const images = [...(event.clipboardData?.items || [])].filter((item) => item.type.startsWith('image/'));
    if (!images.length) return;
    event.preventDefault();
    if (images.length > 1) { toast('请一次粘贴一张图片。', true); return; }
    return uploadSlide(images[0].getAsFile());
  });
  $('#slides-auto').addEventListener('change', () => { state.slideSelection = $('#slides-auto').checked ? null : new Set(); renderSlides(); });
  $('#include-slides').addEventListener('change', () => { state.includeImagesPreference = $('#include-slides').checked; renderSlides(); });
  $('#slides-gallery').addEventListener('change', (event) => {
    const checkbox = event.target.closest('[data-slide-select]');
    if (!checkbox || state.slideSelection === null || state.studyBusy) return;
    if (checkbox.checked && state.slideSelection.size >= 4) { checkbox.checked = false; toast('最多选择 4 张，请先取消一张再选择。', true); return; }
    if (checkbox.checked) state.slideSelection.add(checkbox.dataset.slideSelect);
    else state.slideSelection.delete(checkbox.dataset.slideSelect);
    renderSlides();
  });
  $('#slides-gallery').addEventListener('click', (event) => { const button = event.target.closest('[data-slide-edit]'); if (button) editSlide(button.dataset.slideEdit); });
  $('#slide-rotate-left').addEventListener('click', () => previewRotation(-90));
  $('#slide-rotate-right').addEventListener('click', () => previewRotation(90));
  $('#slide-save-rotation').addEventListener('click', saveRotation);
  $('#slide-remove').addEventListener('click', removeSlide);
  $('#slide-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (state.slideBusy || !state.editingSlide || !$('#slide-form').reportValidity()) return;
    const start = Number($('#slide-start').value);
    const end = $('#slide-end').value.trim() === '' ? null : Number($('#slide-end').value);
    if (!Number.isFinite(start) || start < 0 || (end !== null && (!Number.isFinite(end) || end <= start))) { $('#slide-edit-status').textContent = '结束必须晚于开始，或留空表示持续。'; $('#slide-edit-status').hidden = false; return; }
    const editing = state.editingSlide;
    state.slideBusy = true; $('#slide-save').disabled = true; renderQueue();
    try {
      await api(`/api/slides/${encodeURIComponent(editing.id)}`, { method: 'PATCH', body: { session_id: editing.sessionId, start, end, title: $('#slide-title').value } });
      if (inView(editing) && state.editingSlide === editing) {
        $('#slide-status').hidden = false;
        $('#slide-status').textContent = `课件范围已更新：${rangeText({ start, end })}。已有问题仍保留当时的课件范围。`;
        $('#slide-dialog').close();
      }
      await poll(true);
    } catch (error) { if (inView(editing) && state.editingSlide === editing) { $('#slide-edit-status').textContent = error.message; $('#slide-edit-status').hidden = false; } }
    finally { state.slideBusy = false; $('#slide-save').disabled = false; renderSlides(); renderQueue(); }
  });
  for (const button of document.querySelectorAll('[data-study-mode]')) button.addEventListener('click', () => {
    if (state.studyBusy || !state.session || !state.connected) return;
    const params = frozenStudyParams(button.dataset.studyMode);
    return prepareStudy(params);
  });
  $('#study-reprepare').addEventListener('click', () => {
    if (!state.studyFailedParams) return;
    const params = { ...state.studyFailedParams };
    delete params.slide_ids;
    if (state.slideSelection !== null) params.slide_ids = [...state.slideSelection];
    return prepareStudy(params);
  });
  $('#study-send').addEventListener('click', () => confirmStudy());
  $('#study-text-only').addEventListener('click', () => confirmStudy(true));
  $('#study-confirm-original').addEventListener('change', renderStudyReview);
  $('#study-confirm-duplicate').addEventListener('change', renderStudyReview);
  $('#study-refresh-materials').addEventListener('click', () => { if (state.studyReview && !state.studyBusy) return refreshStudy(state.studyReview.question, $('#study-confirm-duplicate').checked); });
  $('#legacy-request-check').addEventListener('click', () => {
    const record = state.legacyRequests.get(state.session?.id);
    if (record) return record.kind === 'study' ? recoverStudyRequest(record) : submitLegacyQuestion(record);
  });
  $('#duplicate-retry-send').addEventListener('click', () => {
    if (!state.pendingLegacyRetry || !$('#duplicate-retry-consent').checked) { $('#duplicate-retry-status').textContent = '请先确认可能重复计费。'; $('#duplicate-retry-status').hidden = false; return; }
    return executeLegacyRetry(state.pendingLegacyRetry, true);
  });
  $('#questions-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-study-review], [data-study-retry], [data-study-refresh]');
    if (!button || state.studyBusy) return;
    const id = button.dataset.studyReview || button.dataset.studyRetry || button.dataset.studyRefresh;
    const question = state.session?.questions?.find((item) => item.id === id && item.study);
    if (!question) return;
    if (button.dataset.studyReview) openStudyReview(question);
    else if (button.dataset.studyRetry) return retryStudy(question);
    else return refreshStudy(question);
  });

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
    renderInputLimits();
    state.renderKeys.glossary = null;
    $('#glossary-dialog').showModal();
    renderCourseTools();
  });
  $('#glossary-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!state.glossaryCourseId || $('#generate-glossary').disabled) return;
    const syllabus = validatedText('#course-syllabus');
    if (syllabus === null) return;
    if (isDemo()) { toast('演示课堂不调用 AI。请先新建一节课，再生成术语。'); return; }
    state.glossarySubmitting = true;
    $('#generate-glossary').disabled = true;
    try {
      await api(`/api/courses/${encodeURIComponent(state.glossaryCourseId)}/glossary`, { method: 'POST', body: { syllabus } });
      await poll(true);
      toast('术语生成已加入 AI 队列，完成后会保存到课程预设。');
    } catch (error) { toast(error.message, true); }
    finally { state.glossarySubmitting = false; renderCourseTools(); }
  });

  document.querySelectorAll('[data-close-dialog]').forEach((button) => button.addEventListener('click', () => document.getElementById(button.dataset.closeDialog).close()));
  $('#transcript-list').addEventListener('click', (event) => {
    const button = event.target.closest('[data-edit-segment]');
    if (!button) return;
    const segment = (state.session?.segments || []).find((item) => item.id === button.dataset.editSegment);
    if (!segment) return;
    state.editingSegment = { id: segment.id, sessionId: state.session.id, epoch: state.sessionEpoch };
    $('#edit-segment-text').value = segment.text || '';
    renderInputLimits();
    $('#edit-original-text').textContent = segment.machine_text ?? segment.text ?? '';
    $('#edit-segment-dialog').showModal();
    $('#edit-segment-text').focus();
  });
  $('#edit-segment-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const target = state.editingSegment;
    if (!target || !inView(target)) return;
    const text = validatedText('#edit-segment-text');
    if (text === null) return;
    const button = event.submitter; button.disabled = true;
    try {
      await api(`/api/segments/${encodeURIComponent(target.id)}/edit`, { method: 'POST', body: { text, session_id: target.sessionId } });
      await poll(true);
      if (inView(target) && state.editingSegment === target) { $('#edit-segment-dialog').close(); toast('原文已修改，机器原稿保留。相关解释会标为待更新。'); }
    } catch (error) { if (inView(target)) toast(error.message, true); }
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
    if (state.aiResuming || (state.snapshot?.ai_settings || state.aiSettings)?.pending) return;
    state.aiResuming = true;
    renderEngineStatus();
    try {
      await api('/api/ai/resume', { method: 'POST', body: { strategy: button.dataset.aiResume } });
      await poll(true);
      toast(button.dataset.aiResume === 'backfill' ? '已请求补分析未完成的内容。' : '已请求从最新内容继续分析。历史原文仍然保留。');
    } catch (error) { toast(error.message, true); }
    finally { state.aiResuming = false; renderEngineStatus(); }
  }));

  $('#session-select').addEventListener('change', async (event) => {
    const id = event.target.value;
    if (!canSwitch()) { event.target.value = state.session?.id || ''; return; }
    state.switching = true; renderQueue(); renderSlides();
    try {
      await saveSettings();
      await api(`/api/sessions/${encodeURIComponent(id)}/select`, { method: 'POST' });
      await poll(true);
    } catch (error) { toast(error.message, true); event.target.value = state.session?.id || ''; }
    finally { state.switching = false; renderQueue(); renderSlides(); }
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
    if ($('#add-text').disabled || state.switching) return;
    const text = validatedText('#manual-text');
    if (text === null) return;
    let scope = viewScope();
    const fromDemo = isDemo();
    $('#add-text').disabled = true;
    try {
      await ensureRealSession();
      if (fromDemo) scope = viewScope();
      if (!inView(scope)) return;
      await saveSettings();
      if (!inView(scope)) return;
      await api('/api/text', { method: 'POST', body: { text, session_id: scope.sessionId } });
      if (inView(scope) && $('#manual-text').value === text) $('#manual-text').value = '';
      await poll(true);
    } catch (error) { if (inView(scope)) toast(error.message, true); }
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
    if (button) { $('#question-input').value = button.dataset.question; rememberDraft(state.session?.id, $('#question-input').value); renderInputLimits(); $('#question-input').focus(); }
  });
  $('#question-input').addEventListener('input', () => rememberDraft(state.session?.id, $('#question-input').value));
  $('#question-input').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('#question-form').requestSubmit(); }
  });
  $('#question-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    if ($('#ask-button').disabled || state.studyBusy || state.switching) return;
    if (state.legacyRequests.has(state.session?.id)) { renderLegacyRecovery(); toast('请先核对本课上一个待确认的问题，当前草稿保留。', true); return; }
    const question = validatedText('#question-input');
    if (question === null) return;
    if (isDemo()) { toast('演示模式不调用 AI。请新建课程并加入真实课堂原文后提问。'); return; }
    const scope = viewScope();
    const groupId = state.selectedGroup;
    rememberDraft(scope.sessionId, question);
    if ($('#include-slides').checked) {
      const params = frozenStudyParams(state.selectedGroup ? 'group' : $('#study-range').value || 'recent120', question, false);
      return prepareStudy(params);
    }
    $('#ask-button').disabled = true;
    try {
      await saveSettings();
      if (!inView(scope)) return;
      await submitLegacyQuestion({ kind: 'legacy-ask', request_id: newRequestId(), body: { session_id: scope.sessionId, question, ...(groupId ? { group_id: groupId } : {}) } }, true);
      if (inView(scope)) $('#questions-list').scrollTop = $('#questions-list').scrollHeight;
    } catch (error) { if (inView(scope)) toast(error.message, true, 8000); }
    finally { $('#ask-button').disabled = false; }
  });

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-retry-group], [data-retry-question], [data-retry-audio]');
    if (!button || isDemo() || state.switching || state.studyBusy) return;
    const type = button.dataset.retryGroup ? 'groups' : button.dataset.retryQuestion ? 'questions' : 'audio';
    const id = button.dataset.retryGroup || button.dataset.retryQuestion || button.dataset.retryAudio;
    const item = (state.session?.[type] || []).find((entry) => String(entry.id ?? entry.seq) === id);
    const operation = { scope: viewScope(), type, id };
    if (type !== 'audio' && resultUnknown(item)) {
      state.pendingLegacyRetry = operation; $('#duplicate-retry-consent').checked = false;
      $('#duplicate-retry-status').hidden = true;
      $('#duplicate-retry-description').textContent = `${item.question || '这段课堂分析'}。前次调用可能已经处理或计费，再次请求可能重复计费。`;
      $('#duplicate-retry-dialog').showModal(); return;
    }
    return executeLegacyRetry(operation);
  });

  document.querySelectorAll('[data-export]').forEach((button) => button.addEventListener('click', async () => {
    const scope = viewScope();
    const title = state.session?.title || '课堂笔记';
    try {
      await saveSettings();
      const format = button.dataset.export;
      const response = await api(`/api/export?format=${format}&session_id=${encodeURIComponent(scope.sessionId)}`, { raw: true });
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${title.replace(/[\\/:*?"<>|]/g, '_')}.${format === 'json' ? 'json' : 'md'}`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      $('.export-menu').open = false;
      if (inView(scope)) toast('已导出当前记录。仍在队列中的转写或分析尚未包含在内。');
    } catch (error) { if (inView(scope)) toast(error.message, true); }
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
    if (state.captureMode !== 'idle' || state.uploadQueue.length || state.settingsDirty || state.finalizing.size || state.slideBusy || state.slideRecoveryBusy || state.studyBusy || state.transientSlides.size) {
      event.preventDefault(); event.returnValue = '';
    }
  });
  navigator.mediaDevices?.addEventListener?.('devicechange', () => { if (state.captureMode === 'idle') enumerateMicrophones().catch(() => {}); });

  async function initialize() {
    renderInputLimits();
    await Promise.allSettled([poll(), initializeQueue(), ensureStudyCache(), enumerateMicrophones()]);
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
  for (const field of textFields) $(field.selector).addEventListener('input', renderInputLimits);
  initialize().catch((error) => toast(error.message, true));
})();
