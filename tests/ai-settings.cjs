/* Pure Node DOM/API simulation: no real network, audio devices, or credentials. */
'use strict';
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const root = path.resolve(__dirname, '../static');
const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
const elements = new Map();
class Element {
  constructor(tag = 'DIV') {
    this.tagName = tag.toUpperCase(); this.listeners = {}; this.value = ''; this.textContent = ''; this.innerHTML = '';
    this.hidden = false; this.disabled = false; this.open = false; this.checked = false; this.dataset = {}; this.attributes = {};
    this.style = {}; this.scrollTop = 0; this.scrollHeight = 400; this.clientHeight = 400; this.paused = true; this.ended = false;
    this.classList = { toggle: (key, value) => { this.attributes[key] = value; } };
  }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; for (const listener of this.listeners.close || []) listener(); }
  focus() { document.activeElement = this; }
  reportValidity() { return true; }
  pause() { this.paused = true; }
  appendChild() {}
  remove() {}
  click() {}
  select() {}
  scrollIntoView() {}
}
for (const match of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)) {
  assert.ok(!elements.has(`#${match[2]}`), `duplicate DOM id ${match[2]}`);
  elements.set(`#${match[2]}`, new Element(match[1]));
}
for (const match of source.matchAll(/\$\('(#[-\w]+)'\)/g)) assert.ok(elements.has(match[1]), `missing DOM node ${match[1]}`);
assert.match(html, /id="ai-key" type="password" autocomplete="new-password"/);
assert.match(html, /课间 1\.2\.0/);
const aiButtons = ['backfill', 'latest'].map((strategy) => { const button = new Element('BUTTON'); button.dataset.aiResume = strategy; return button; });
const closeButtons = [...html.matchAll(/data-close-dialog="([^"]+)"/g)].map((match) => { const button = new Element('BUTTON'); button.dataset.closeDialog = match[1]; return button; });
const document = {
  activeElement: null, body: new Element(), listeners: {},
  querySelector(selector) {
    if (selector === 'dialog[open]') return [...elements.values()].find((element) => element.tagName === 'DIALOG' && element.open) || null;
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  querySelectorAll(selector) { return selector === '[data-ai-resume]' ? aiButtons : selector === '[data-close-dialog]' ? closeButtons : []; },
  getElementById(id) { return elements.get(`#${id}`); },
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); },
  createElement(tag) { return new Element(tag); },
};
const store = new Map();
const requests = [];
let microphoneCalls = 0;
let testGate = null;
let testFailure = null;
let resumeGate = null;
const server = {
  input_limits: { text: 12000, question: 4000, syllabus: 12000 },
  session: { id: 'test-session', title: 'Synthetic class', settings: { terms: '', auto_analyze: false }, segments: [], groups: [], questions: [], audio: [], revisions: [] },
  sessions: [{ id: 'test-session', title: 'Synthetic class' }], courses: [], jobs: [], soundcheck: { status: 'idle' },
  status: { asr: { available: true, device: 'cpu', model_name: 'small', requested_mode: 'auto', effective_mode: 'battery', on_ac_power: false }, codex: { logged_in: true }, ai: { provider: 'codex', model: 'account-model', configured: true, logged_in: true, busy: false } },
  ai_settings: { provider: 'codex', base_url: 'https://example.invalid/v1', model: 'synthetic-model', remember_key: false, key_configured: true, key_persisted: false, pending: false, active_provider: 'codex', active_model: 'account-model', storage_available: true },
  ai_usage: { requests: 0, completed: 0, failed: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, usage_reported_requests: 0 },
  queue: { audio: 0, ai: 0 }, ai: { paused: false },
};
const clone = (value) => JSON.parse(JSON.stringify(value));
const context = vm.createContext({
  document, console, Float32Array, ArrayBuffer, DataView, Blob, URL, URLSearchParams, AbortController, performance,
  location: { href: 'http://127.0.0.1:8766/', origin: 'http://127.0.0.1:8766' },
  window: { innerWidth: 1400, addEventListener() {} },
  navigator: { mediaDevices: { addEventListener() {}, async getUserMedia() { microphoneCalls++; throw new Error('Never open a microphone in this test'); } }, clipboard: { async writeText() {} } },
  localStorage: { getItem: (key) => store.get(key) ?? null, setItem: (key, value) => { assert.ok(!String(value).includes('synthetic-secret'), 'keys never enter browser storage'); store.set(key, value); } },
  setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  fetch: async (url, options = {}) => {
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body;
    const method = options.method || 'GET';
    requests.push({ url, method, body });
    let result;
    if (url === '/api/state') result = server;
    else if (url === '/api/settings' && method === 'PUT') { Object.assign(server.session.settings, body); result = {}; }
    else if (url === '/api/ai/resume' && method === 'POST') { if (resumeGate) await resumeGate; server.ai.paused = false; result = {}; }
    else if (url === '/api/ai/settings' && method === 'GET') result = server.ai_settings;
    else if (url === '/api/ai/settings' && method === 'PUT') {
      for (const key of ['provider', 'base_url', 'model', 'remember_key']) if (key in body) server.ai_settings[key] = body[key];
      if (body.clear_key) server.ai_settings.key_configured = false;
      else if (body.api_key) server.ai_settings.key_configured = true;
      server.ai_settings.key_persisted = server.ai_settings.key_configured && server.ai_settings.remember_key;
      if (body.provider === 'api') server.session.settings.auto_analyze = false;
      result = server.ai_settings;
    } else if (url === '/api/ai/test' && method === 'POST') {
      if (testGate) await testGate;
      if (testFailure) return { ok: false, status: 502, json: async () => ({ detail: testFailure }) };
      result = { ok: true, provider: body.provider, model: body.model || 'account-model', usage: body.provider === 'api' ? { prompt_tokens: 7, completion_tokens: 1, total_tokens: 8 } : null, message: 'Synthetic connection verified.' };
    } else throw new Error(`Unexpected mock API request ${method} ${url}`);
    return { ok: true, status: 200, json: async () => clone(result) };
  },
});
const testSource = source.replace('  initialize().catch((error) => toast(error.message, true));', '  globalThis.test = { state, poll, render, usageText, renderAIResponse, fillAIForm };');
assert.notEqual(testSource, source, 'test entry point replaced; app startup never runs');
vm.runInContext(testSource, context);
const $ = (selector) => document.querySelector(selector);
async function trigger(element, type, extra = {}) {
  for (const listener of element.listeners[type] || []) await listener({ preventDefault() {}, target: element, ...extra });
}
const settingsRequests = () => requests.filter((request) => request.url === '/api/ai/settings' && request.method === 'PUT');
const testRequests = () => requests.filter((request) => request.url === '/api/ai/test');

(async () => {
  context.test.state.queueLoaded = true;
  await context.test.poll();
  assert.equal(context.test.state.connected, true);
  assert.equal($('#ai-usage').textContent, '本课 API：尚无请求');
  await trigger($('#ai-settings-button'), 'click');
  assert.equal($('#ai-settings-dialog').open, true);
  assert.equal($('#ai-key').value, '', 'server key status never fills the password');
  assert.equal($('#ai-remember-key').checked, false);
  assert.equal($('#ai-api-fields').hidden, true);
  assert.equal($('#ai-test-button').textContent, '检查登录');
  assert.match($('#ai-key-status').textContent, /仅本次服务运行/);
  const unnamedCodexDraft = context.test.renderAIResponse({ ai_provider: 'api', ai_model: 'old-api-model', stream_provider: 'codex', stream_model: null, stream_status: 'streaming', streaming_text: 'Temporary text', status: 'running' }, 'Old API answer.');
  assert.match(unnamedCodexDraft, /正式解释 · API · old-api-model/);
  assert.ok(!unnamedCodexDraft.slice(unnamedCodexDraft.indexOf('stream-draft')).includes('old-api-model'), 'a default Codex model must not inherit the old API model name');

  $('#ai-provider').value = 'api'; await trigger($('#ai-provider'), 'change');
  assert.equal($('#ai-api-fields').hidden, false);
  $('#ai-model').value = 'unsaved-model';
  $('#ai-base-url').value = 'https://unsaved.invalid/v1';
  $('#ai-key').value = 'synthetic-secret-for-test';
  await context.test.poll();
  assert.equal($('#ai-model').value, 'unsaved-model', 'poll never overwrites edited settings');
  assert.equal($('#ai-base-url').value, 'https://unsaved.invalid/v1');
  assert.equal($('#ai-key').value, 'synthetic-secret-for-test');
  const originalSettings = clone(server.ai_settings);
  let releaseTest;
  testGate = new Promise((resolve) => { releaseTest = resolve; });
  const checking = trigger($('#ai-test-button'), 'click');
  assert.equal($('#ai-save-button').disabled, true);
  assert.equal($('#ai-provider').disabled, true);
  assert.equal($('#ai-settings-form').getAttribute('aria-busy'), 'true');
  const checkingRequests = testRequests().length;
  await trigger($('#ai-test-button'), 'click');
  await trigger($('#ai-settings-form'), 'submit');
  assert.equal(testRequests().length, checkingRequests, 'repeated test events do not submit additional requests');
  assert.equal(settingsRequests().length, 0, 'saving cannot overlap a connection test');
  releaseTest(); await checking; testGate = null;
  assert.equal(settingsRequests().length, 0, 'connection testing does not save');
  assert.deepEqual(server.ai_settings, originalSettings);
  assert.equal(testRequests().at(-1).body.model, 'unsaved-model');
  assert.equal(testRequests().at(-1).body.api_key, 'synthetic-secret-for-test');
  assert.match($('#ai-settings-result').textContent, /8 tokens/);
  assert.match($('#ai-settings-result').textContent, /配置尚未保存/);
  assert.equal(server.ai_usage.requests, 0, 'test usage remains separate from the classroom');
  assert.equal($('#ai-key').value, 'synthetic-secret-for-test', 'unsaved key remains only in the open password field for saving');
  assert.equal($('#ai-save-button').disabled, false);

  testFailure = 'Rejected synthetic-secret-for-test by the synthetic service';
  await trigger($('#ai-test-button'), 'click');
  assert.ok(!$('#ai-settings-result').textContent.includes('synthetic-secret-for-test'), 'errors never echo the submitted key');
  assert.match($('#ai-settings-result').textContent, /密钥已隐藏/);
  testFailure = null;
  $('#ai-settings-dialog').close();
  assert.equal($('#ai-key').value, '', 'close clears the volatile password field');
  assert.ok(!JSON.stringify([...store]).includes('synthetic-secret'));

  await trigger($('#ai-settings-button'), 'click');
  $('#ai-provider').value = 'api'; await trigger($('#ai-provider'), 'change');
  server.ai_settings.pending = true;
  server.ai.paused = true;
  context.test.state.settingsDirty = true; context.test.state.saveGeneration = 1;
  $('#auto-analyze').checked = true;
  await trigger($('#ai-settings-form'), 'submit');
  assert.ok(requests.findIndex((request) => request.url === '/api/settings') < requests.findIndex((request) => request.url === '/api/ai/settings' && request.method === 'PUT'), 'pending classroom settings are saved before applying API defaults');
  assert.equal(server.session.settings.auto_analyze, false, 'API default remains off after earlier classroom setting edits');
  assert.equal($('#auto-analyze').checked, false);
  assert.ok(!('api_key' in settingsRequests().at(-1).body), 'empty key is omitted and preserves the existing key');
  assert.equal(settingsRequests().at(-1).body.clear_key, false);
  assert.equal(server.ai_settings.key_configured, true);
  assert.match($('#ai-active-settings').textContent, /当前生效：Codex \/ account-model/);
  assert.match($('#ai-active-settings').textContent, /等待当前请求结束/);
  assert.ok(aiButtons.every((button) => button.disabled), 'resume unavailable while a provider switch is pending');
  assert.match($('#ai-pause-reason').textContent, /接入切换完成/);

  server.ai_settings.pending = false;
  server.ai_settings.active_provider = 'api'; server.ai_settings.active_model = 'synthetic-model';
  server.status.ai = { provider: 'api', model: 'synthetic-model', configured: true, busy: true };
  await context.test.poll();
  assert.ok(aiButtons.every((button) => !button.disabled));
  assert.match($('#codex-status').textContent, /^API · 正在生成 \/ synthetic-model$/);
  server.status.ai.busy = false; server.status.ai.inference_verified = true;
  await context.test.poll();
  assert.match($('#codex-status').textContent, /^API · 调用已验证 \/ synthetic-model$/);
  let releaseResume;
  resumeGate = new Promise((resolve) => { releaseResume = resolve; });
  const resuming = trigger(aiButtons[0], 'click');
  assert.ok(aiButtons.every((button) => button.disabled));
  await context.test.poll();
  assert.ok(aiButtons.every((button) => button.disabled), 'poll must not enable the recovery actions during an unfinished resume request');
  const resumeCount = requests.filter((request) => request.url === '/api/ai/resume').length;
  await trigger(aiButtons[1], 'click');
  assert.equal(requests.filter((request) => request.url === '/api/ai/resume').length, resumeCount, 'repeated recovery events cannot submit concurrent requests');
  server.ai_settings.pending = true;
  releaseResume(); await resuming; resumeGate = null;
  assert.ok(aiButtons.every((button) => button.disabled), 'finishing recovery must preserve a newly pending provider switch');
  server.ai_settings.pending = false; await context.test.poll();
  assert.ok(aiButtons.every((button) => !button.disabled));
  $('#ai-clear-key').checked = true; await trigger($('#ai-clear-key'), 'change');
  assert.equal($('#ai-key').disabled, true);
  const testsBeforeClear = testRequests().length;
  await trigger($('#ai-test-button'), 'click');
  assert.equal(testRequests().length, testsBeforeClear, 'clear-key intent never sends a test request');
  await trigger($('#ai-settings-form'), 'submit');
  assert.equal(settingsRequests().at(-1).body.clear_key, true);
  assert.ok(!('api_key' in settingsRequests().at(-1).body));
  assert.equal(server.ai_settings.key_configured, false);
  assert.match($('#ai-key-status').textContent, /尚未配置密钥/);
  $('#ai-key').value = 'synthetic-secret-to-save'; $('#ai-remember-key').checked = true;
  await trigger($('#ai-settings-form'), 'submit');
  assert.equal(settingsRequests().at(-1).body.api_key, 'synthetic-secret-to-save');
  assert.equal(server.ai_settings.key_persisted, true);
  assert.equal($('#ai-key').value, '', 'save clears password without revealing the saved key');
  assert.match($('#ai-key-status').textContent, /已在本机保存/);

  $('#ai-settings-dialog').close(); server.ai_settings.storage_available = false; server.ai_settings.remember_key = false;
  await trigger($('#ai-settings-button'), 'click');
  assert.equal($('#ai-remember-key').disabled, true);
  assert.equal($('#ai-remember-key').checked, false);
  assert.match($('#ai-storage-help').textContent, /安全存储不可用/);
  $('#ai-provider').value = 'codex'; await trigger($('#ai-provider'), 'change');
  assert.equal($('#ai-base-url').disabled, true, 'hidden API fields cannot block Codex form validation');
  await trigger($('#ai-settings-form'), 'submit');
  assert.deepEqual(settingsRequests().at(-1).body, { provider: 'codex' }, 'Codex switch preserves stored API configuration');
  $('#ai-settings-dialog').close();

  const oldAnswer = 'Previous complete answer.';
  const draft = '<img src=x onerror=alert(1)>New partial response';
  server.session.questions = [{ id: 'Q1', question: 'Synthetic question', answer: oldAnswer, status: 'running', ai_provider: 'codex', ai_model: 'old-model', stream_provider: 'api', stream_model: 'new-model', streaming_text: draft, stream_status: 'streaming' }];
  server.session.groups = [{ id: 'G1', segment_ids: [], text: 'Synthetic source', analysis: 'Previous complete explanation.', status: 'running', ai_provider: 'codex', ai_model: 'old-model', stream_provider: 'api', stream_model: 'new-model', streaming_text: 'Partial group explanation', stream_status: 'streaming' }];
  await context.test.poll();
  assert.match($('#questions-list').innerHTML, /正式回答 · Codex · old-model/);
  assert.match($('#questions-list').innerHTML, /API · new-model/);
  assert.match($('#questions-list').innerHTML, /正在生成 · 临时内容 · 原回答保留在上方/);
  assert.match($('#questions-list').innerHTML, /Previous complete answer\./);
  assert.ok(!$('#questions-list').innerHTML.includes('<img'), 'streamed model output is escaped');
  assert.match($('#questions-list').innerHTML, /&lt;img/);
  assert.match($('#groups-list').innerHTML, /正式解释 · Codex · old-model/);
  assert.match($('#groups-list').innerHTML, /Partial group explanation/);
  for (const item of [...server.session.questions, ...server.session.groups]) { item.stream_status = 'interrupted'; item.status = 'error'; item.error = 'Synthetic stream failure'; }
  await context.test.poll();
  for (const selector of ['#questions-list', '#groups-list']) {
    assert.match($(selector).innerHTML, /生成中断 · 以下未形成正式回答/);
    assert.match($(selector).innerHTML, /Synthetic stream failure/);
  }
  assert.match($('#questions-list').innerHTML, /data-retry-question="Q1"/);
  assert.equal(server.session.questions[0].answer, oldAnswer, 'interrupted generation never replaces a complete answer');
  server.session.questions[0] = { id: 'Q1', question: 'Synthetic question', answer: 'Completed new answer.', status: 'done', ai_provider: 'api', ai_model: 'new-model', usage: { total_tokens: 20 }, stream_status: 'complete', streaming_text: 'Completed new answer.' };
  await context.test.poll();
  assert.equal($('#questions-list').innerHTML.split('Completed new answer.').length - 1, 1, 'complete stream is not duplicated as a draft');
  assert.ok(!$('#questions-list').innerHTML.includes('stream-draft'));
  assert.match($('#questions-list').innerHTML, /20 tokens/);
  const hostileAnswer = context.test.renderAIResponse({ ai_provider: 'api', ai_model: '<img src=x onerror=alert(1)>', status: 'error', error: '<svg onload=alert(2)>' }, '# <script>alert(3)</script>\n**<iframe src=javascript:alert(4)>**', true);
  assert.ok(!/<(?:img|svg|script|iframe)\b/i.test(hostileAnswer), 'formal answers, errors, and provider metadata remain escaped');
  assert.match(hostileAnswer, /&lt;script&gt;/);

  server.ai_usage = { requests: 2, completed: 1, failed: 1, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, usage_reported_requests: 0 };
  await context.test.poll();
  assert.match($('#ai-usage').textContent, /2 次请求 · 完成 1 \/ 失败 1 · 令牌用量未返回/);
  assert.ok(!$('#ai-usage').textContent.includes('0 tokens'), 'missing usage is not displayed as zero');
  server.ai_usage = { requests: 2, completed: 0, failed: 0, unknown: 2, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0, usage_reported_requests: 0 };
  await context.test.poll();
  assert.match($('#ai-usage').textContent, /完成 0 \/ 失败 0 · 结果未知 2（可能已计费） · 令牌用量未返回/);
  assert.ok(!$('#ai-usage').textContent.includes('0 tokens'), 'unknown request outcomes never imply zero token usage');
  server.ai_usage = { requests: 3, completed: 2, failed: 1, prompt_tokens: 20, completion_tokens: 5, total_tokens: 25, usage_reported_requests: 1 };
  await context.test.poll();
  assert.match($('#ai-usage').textContent, /25 tokens（输入 20 \/ 输出 5） · 1 次已返回用量/);
  assert.ok(!$('#ai-usage').textContent.includes('结果未知'), 'unknown outcomes remain hidden when absent or zero');
  assert.equal(context.test.usageText({ total_tokens: 0 }), '0 tokens', 'reported zero remains distinct from missing usage');
  assert.equal(context.test.usageText(null), '令牌用量未返回');
  assert.equal(context.test.usageText({ prompt_tokens: 2, completion_tokens: 3 }), '5 tokens（输入 2 / 输出 3）');
  assert.equal(microphoneCalls, 0);
  assert.ok(![...elements.values()].some((element) => `${element.textContent}${element.innerHTML}`.includes('synthetic-secret')), 'secret never appears in visible output');
  console.log(JSON.stringify({ test: 'AI settings and streamed-answer UI', result: 'passed', mockRequests: requests.length, realNetwork: false, realMicrophoneOpened: false, checks: ['DOM references', 'password lifecycle and browser storage', 'poll preserves unsaved edits', 'test never saves settings or overlaps save', 'save and clear-key contracts', 'pending switch and resume race', 'Codex switch preserves API configuration', 'formal and temporary answer separation', 'formal/stream/error/model HTML escaping', 'default-model retry attribution', 'API inference verification state', 'reported versus unknown usage'] }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
