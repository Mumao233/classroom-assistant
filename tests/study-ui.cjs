/* Synthetic DOM/API only: no service, credentials, microphone, or real AI. */
'use strict';
const assert = require('assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const html = fs.readFileSync(path.resolve(__dirname, '../static/index.html'), 'utf8');
const source = fs.readFileSync(path.resolve(__dirname, '../static/app.js'), 'utf8');
const elements = new Map();
class Element {
  constructor(tag = 'DIV') {
    Object.assign(this, { tagName: tag.toUpperCase(), listeners: {}, value: '', textContent: '', hidden: false, disabled: false, open: false, checked: false, dataset: {}, attributes: {}, style: {}, scrollTop: 0, scrollHeight: 400, clientHeight: 400, paused: true, ended: false });
    this.classList = { toggle: (key, value) => { this.attributes[key] = value; } };
    this.innerHTML = '';
  }
  set innerHTML(value) {
    this._html = value;
    this.details = [...value.matchAll(/<details\b[^>]*data-evidence="([^"]+)"/g)].map((match) => { const node = new Element('DETAILS'); node.dataset.evidence = match[1]; return node; });
  }
  get innerHTML() { return this._html; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  querySelectorAll(selector) { return selector === 'details[data-evidence]' ? this.details : []; }
  showModal() { this.open = true; }
  close() { this.open = false; for (const fn of this.listeners.close || []) fn(); }
  focus() { document.activeElement = this; }
  reportValidity() { return true; }
  closest(selector) {
    return selector.split(',').some((part) => {
      const match = part.trim().match(/^\[data-([\w-]+)\]$/);
      return match && match[1].replace(/-([a-z])/g, (_, char) => char.toUpperCase()) in this.dataset;
    }) ? this : null;
  }
  pause() {} appendChild() {} remove() {} click() {} select() {} scrollIntoView() {}
}
for (const match of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)) {
  assert.ok(!elements.has(`#${match[2]}`), `Duplicate id: ${match[2]}`);
  elements.set(`#${match[2]}`, new Element(match[1]));
}
for (const match of source.matchAll(/\$\('(#[-\w]+)'\)/g)) assert.ok(elements.has(match[1]), `Missing id: ${match[1]}`);
const quickButtons = ['recent30', 'recent120', 'group'].map((mode) => { const node = new Element('BUTTON'); node.dataset.studyMode = mode; return node; });
const document = {
  activeElement: null, body: new Element(), listeners: {},
  querySelector(selector) {
    if (selector === 'dialog[open]') return [...elements.values()].find((item) => item.tagName === 'DIALOG' && item.open) || null;
    if (!elements.has(selector)) elements.set(selector, new Element());
    return elements.get(selector);
  },
  querySelectorAll: (selector) => selector === '[data-study-mode]' ? quickButtons : [],
  getElementById: (id) => elements.get(`#${id}`),
  createElement: (tag) => new Element(tag),
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
};
const $ = (selector) => document.querySelector(selector);
const clone = (value) => JSON.parse(JSON.stringify(value));
const requests = [];
let preparedCounter = 0;
let prepareGate = null;
let settingsGate = null;
let prepareFailure = null;
let sendFailure = null;
let uploadFailure = null;
let knownVision = 'unknown';
let remembered = false;
let microphoneCalls = 0;
const slides = [1, 2].map((index) => ({ id: `P${index}`, title: `Slide ${index}`, start: 0, end: null, path: 'ignored/file.png', mime_type: 'image/png' }));
const server = {
  input_limits: { text: 12000, question: 4000, syllabus: 12000 },
  session: { id: 'synthetic-session', title: 'Synthetic lecture', settings: { terms: '', auto_analyze: false }, slides, segments: [{ id: 'S1', start: 20, end: 50, text: 'Frozen original source.' }], groups: [], questions: [], audio: [], revisions: [] },
  sessions: [{ id: 'synthetic-session', title: 'Synthetic lecture' }], courses: [], jobs: [], soundcheck: { status: 'idle' },
  status: { asr: { available: true }, ai: { provider: 'codex', configured: true, busy: false } },
  ai_settings: { provider: 'codex', active_provider: 'codex', active_model: 'synthetic-model', base_url: 'https://synthetic.invalid/v1', model: 'synthetic-model', busy: false },
  ai_usage: {}, queue: { audio: 0, ai: 0 }, ai: { paused: false },
};
const destination = () => ({ provider: server.ai_settings.provider, label: 'Synthetic AI', base_url: server.ai_settings.base_url, model: server.ai_settings.model, token: `token-${server.ai_settings.model}` });
function makePrepared(params, oldEvidence = null) {
  const selected = params.include_images ? server.session.slides.filter((item) => params.slide_ids ? params.slide_ids.includes(item.id) : true) : [];
  const range = { start: Math.max(0, params.anchor - (params.mode === 'recent30' ? 30 : 120)), end: params.anchor };
  const question = { id: `Q${++preparedCounter}`, study: true, status: 'prepared', question: params.question || 'Explain what I just heard.', explain: params.explain, request_params: clone(params), evidence: oldEvidence ? clone(oldEvidence) : { requested_range: range, actual_range: { start: 20, end: 50 }, segments: clone(server.session.segments), slides: clone(selected), incomplete: true, warnings: [] }, destination: destination(), requires_confirmation: Boolean(selected.length && !remembered), vision: { state: knownVision } };
  server.session.questions.push(question);
  return question;
}
const context = vm.createContext({
  document, console, Blob, ArrayBuffer, Float32Array, DataView, URL, URLSearchParams, AbortController, performance,
  location: { href: 'http://127.0.0.1:8766/', origin: 'http://127.0.0.1:8766' },
  window: { addEventListener() {}, innerWidth: 1400 },
  navigator: { mediaDevices: { addEventListener() {}, async getUserMedia() { microphoneCalls++; throw Error('No microphone allowed'); } } },
  localStorage: { getItem() { return null; }, setItem() {} },
  setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  fetch: async (url, options = {}) => {
    const method = options.method || 'GET';
    const body = typeof options.body === 'string' ? JSON.parse(options.body) : options.body;
    requests.push({ url, method, body });
    let result;
    let failure;
    if (url === '/api/state') result = server;
    else if (url === '/api/settings') { if (settingsGate) await settingsGate; result = {}; }
    else if (url === '/api/ask') { result = { id: 'TEXT' }; }
    else if (url === '/api/ai/settings' && method === 'PUT') { Object.assign(server.ai_settings, body); result = server.ai_settings; }
    else if (url === '/api/study/prepare') {
      if (prepareGate) await prepareGate;
      failure = prepareFailure;
      if (!failure) result = makePrepared(body);
    } else if (/^\/api\/slides\?/.test(url)) {
      failure = uploadFailure;
      if (!failure) { const query = new URL(`http://localhost${url}`).searchParams; result = { id: 'P3', title: query.get('title'), start: Number(query.get('start')), end: null }; server.session.slides.push(result); }
    } else if (/^\/api\/slides\//.test(url) && method === 'PATCH') { Object.assign(server.session.slides.find((item) => url.endsWith(item.id)), body); result = {}; }
    else if (/^\/api\/study\//.test(url)) {
      const [, id, action] = url.match(/^\/api\/study\/([^/]+)\/(send|retry|text-only)$/) || [];
      const question = server.session.questions.find((item) => item.id === id);
      assert.ok(question, `Question ${id} exists`);
      if (action === 'send') { failure = sendFailure; if (!failure) { question.status = 'pending'; result = question; } }
      else { result = makePrepared(question.request_params, question.evidence); if (action === 'text-only') { result.evidence.slides = []; result.requires_confirmation = false; } }
    } else throw Error(`Unexpected mock request ${method} ${url}`);
    return failure ? { ok: false, status: failure.status, json: async () => ({ detail: failure.detail }) } : { ok: true, status: 200, json: async () => clone(result) };
  },
});
const testSource = source.replace('  initialize().catch((error) => toast(error.message, true));', '  globalThis.test = { state, poll, render, prepareStudy, frozenStudyParams, classroomAnchor, renderStudyReview, openStudyReview, confirmStudy, retryStudy, refreshStudy, uploadSlide, editSlide, renderSlides, fillAIForm, submitAISettings };');
assert.notEqual(testSource, source);
vm.runInContext(testSource, context);
const app = context.test;
const calls = (suffix) => requests.filter((request) => request.url.endsWith(suffix));
const sends = () => requests.filter((request) => /\/study\/.*\/send$/.test(request.url));
async function trigger(element, type, extra = {}) { for (const listener of element.listeners[type] || []) await listener({ target: element, preventDefault() {}, ...extra }); }
function button(dataset) { const item = new Element('BUTTON'); item.dataset = dataset; return item; }

(async () => {
  app.state.queueLoaded = true;
  await app.poll();
  assert.equal($('#include-slides').checked, true, 'slides enable image context by default');
  app.state.capture = { kind: 'classroom', segmenter: { time: 88.25 } };
  app.state.clockSeconds = 120;
  let releasePrepare;
  prepareGate = new Promise((resolve) => { releasePrepare = resolve; });
  const click = trigger(quickButtons[0], 'click');
  app.state.capture.segmenter.time = 101.5;
  releasePrepare(); await click; prepareGate = null;
  assert.equal(calls('/prepare').at(-1).body.anchor, 88.25, 'sample clock frozen before awaits');
  assert.equal(calls('/prepare').at(-1).body.mode, 'recent30');
  assert.equal(sends().length, 0, 'prepare with images never sends before confirmation');
  assert.equal($('#study-dialog').open, true);
  assert.match($('#study-review').innerHTML, /图片能力未知/);
  assert.match($('#study-review').innerHTML, /synthetic\.invalid/);
  assert.equal(($('#study-review').innerHTML.match(/<img /g) || []).length, 2, 'all pictures appear in review');
  assert.match($('#study-review').innerHTML, /Frozen original source/);
  const originalRange = clone(app.state.studyReview.question.evidence.requested_range);
  await trigger($('#study-send'), 'click');
  assert.equal(sends().length, 1);
  assert.equal(sends()[0].body.confirm_images, true);
  assert.deepEqual(server.session.questions[0].evidence.requested_range, originalRange);

  app.state.capture = null;
  app.state.clockSeconds = 150;
  $('#include-slides').checked = true;
  $('#study-range').value = 'recent120';
  $('#question-input').value = 'What does this chart mean?';
  await trigger($('#question-form'), 'submit');
  assert.equal(calls('/prepare').at(-1).body.question, 'What does this chart mean?');
  assert.equal($('#question-input').value, 'What does this chart mean?', 'draft retained until send succeeds');
  sendFailure = { status: 502, detail: 'Synthetic sending failure' };
  await trigger($('#study-send'), 'click');
  assert.equal($('#question-input').value, 'What does this chart mean?');
  assert.match($('#study-send-status').textContent, /Synthetic sending failure/);
  sendFailure = null;
  $('#question-input').value = 'New unsent draft';
  await trigger($('#study-send'), 'click');
  assert.equal($('#question-input').value, 'New unsent draft', 'later draft never erased by older send');

  const beforeText = sends().length;
  app.state.includeImagesPreference = false; app.renderSlides();
  $('#question-input').value = 'Plain text question';
  await trigger($('#question-form'), 'submit');
  assert.equal(calls('/api/ask').at(-1).body.question, 'Plain text question');
  assert.equal(sends().length, beforeText, 'ordinary text uses the original ask route');
  app.state.includeImagesPreference = true; app.renderSlides();

  knownVision = 'unsupported';
  await trigger(quickButtons[1], 'click');
  assert.equal($('#study-send').disabled, true);
  assert.match($('#study-review').innerHTML, /不支持图片/);
  const fixedSource = clone(app.state.studyReview.question.evidence.segments);
  server.session.segments[0].text = 'A changed later transcript';
  const sendsBeforeUnsupported = sends().length;
  await trigger($('#study-send'), 'click');
  assert.equal(sends().length, sendsBeforeUnsupported, 'unsupported images blocked even if event is forced');
  await trigger($('#study-text-only'), 'click');
  assert.equal(calls('/text-only').length, 1);
  assert.deepEqual(server.session.questions.at(-1).evidence.segments, fixedSource, 'text-only keeps the exact original snapshot');
  assert.equal(server.session.questions.at(-1).evidence.slides.length, 0);
  assert.equal(sends().at(-1).body.confirm_images, false);

  knownVision = 'unknown';
  prepareFailure = { status: 422, detail: '范围内有 5 张图片，请手选至多 4 张。' };
  await trigger(quickButtons[0], 'click');
  const failedAnchor = app.state.studyFailedParams.anchor;
  assert.equal($('#slides-panel').open, true);
  assert.equal($('#study-reprepare').hidden, false);
  assert.ok(!('slide_ids' in calls('/prepare').at(-1).body), 'auto selection never silently truncates to four');
  $('#slides-auto').checked = false; await trigger($('#slides-auto'), 'change');
  const checkbox = button({ slideSelect: 'P1' }); checkbox.checked = true;
  await trigger($('#slides-gallery'), 'change', { target: checkbox });
  prepareFailure = null; app.state.clockSeconds = 999;
  await trigger($('#study-reprepare'), 'click');
  assert.equal(calls('/prepare').at(-1).body.anchor, failedAnchor, 'manual correction preserves the earlier click time');
  assert.deepEqual(calls('/prepare').at(-1).body.slide_ids, ['P1']);
  assert.equal(($('#study-review').innerHTML.match(/<img /g) || []).length, 1);
  app.state.slideSelection = new Set(['P1', 'P2', 'P3', 'P4']);
  const fifth = button({ slideSelect: 'P5' }); fifth.checked = true;
  await trigger($('#slides-gallery'), 'change', { target: fifth });
  assert.equal(fifth.checked, false);
  assert.equal(app.state.slideSelection.size, 4, 'fifth manual selection refused without dropping earlier images');
  app.state.slideSelection = new Set(['P1']);

  const preparedId = app.state.studyReview.question.id;
  app.state.studyReview = null; $('#study-dialog').close(); await app.poll();
  assert.match($('#questions-list').innerHTML, new RegExp(`data-study-review="${preparedId}"`), 'server prepared records survive browser state loss');
  await trigger($('#questions-list'), 'click', { target: button({ studyReview: preparedId }) });
  assert.equal(app.state.studyReview.question.id, preparedId);
  const prepared = server.session.questions.find((item) => item.id === preparedId);
  prepared.destination = { ...prepared.destination, base_url: 'https://new.invalid/v1', model: 'new-model', token: 'new-token' };
  $('#study-remember').checked = true; await app.poll();
  assert.match($('#study-review').innerHTML, /new\.invalid/);
  assert.equal($('#study-remember').checked, false);
  assert.match($('#study-send-status').textContent, /目的地已变化/);

  prepared.status = 'error'; prepared.error = 'Synthetic prior failure';
  await app.poll();
  const beforeRetry = sends().length;
  await app.retryStudy(clone(prepared));
  assert.equal(sends().length, beforeRetry, 'retry only prepares; never automatically sends');
  assert.deepEqual(clone(app.state.studyReview.question.evidence), prepared.evidence);
  assert.equal($('#study-dialog').open, true);
  prepared.answer = 'Existing answer'; prepared.status = 'done';
  app.state.clockSeconds = 2000;
  await app.refreshStudy(clone(prepared));
  assert.equal(calls('/prepare').at(-1).body.anchor, prepared.evidence.requested_range.end);
  assert.equal(prepared.answer, 'Existing answer', 'refresh preserves previous answer');

  await app.poll();
  const evidenceNode = $('#questions-list').details.find((item) => item.dataset.evidence === prepared.id);
  evidenceNode.open = true;
  prepared.answer = 'Updated test answer triggers a poll render'; await app.poll();
  assert.equal($('#questions-list').details.find((item) => item.dataset.evidence === prepared.id).open, true, 'poll keeps evidence expanded');
  prepared.evidence.slides[0].title = '<script>alert(1)</script>';
  prepared.evidence.segments[0].text = '<img src=x onerror=alert(1)>';
  await app.poll();
  assert.ok(!$('#questions-list').innerHTML.includes('<script>'));
  assert.match($('#questions-list').innerHTML, /&lt;img src=x/);
  assert.ok(!$('#questions-list').innerHTML.includes('ignored/file.png'), 'evidence image URLs come from API ids, never filesystem paths');

  app.fillAIForm(server.ai_settings);
  server.ai_settings.busy = true; await app.poll();
  assert.equal($('#ai-save-button').disabled, true);
  assert.equal($('#ai-save-button').textContent, '正在加载，暂不能更换');
  assert.equal($('#ai-force-button').hidden, false);
  $('#ai-provider').value = 'api'; $('#ai-clear-key').checked = true;
  const settingsBefore = calls('/api/ai/settings').length;
  await trigger($('#ai-settings-form'), 'submit');
  assert.equal(calls('/api/ai/settings').length, settingsBefore, 'ordinary save and key clearing blocked while loading');
  await trigger($('#ai-force-button'), 'click');
  assert.equal(calls('/api/ai/settings').at(-1).body.force, true);
  assert.equal(calls('/api/ai/settings').at(-1).body.clear_key, true);

  app.state.clockSeconds = 77; server.session.segments = []; await app.poll();
  const image = new Blob(['synthetic image'], { type: 'image/png' });
  uploadFailure = { status: 422, detail: 'Synthetic image failure' };
  app.state.slideSelection = new Set(['P1']);
  await app.uploadSlide(image);
  assert.deepEqual([...app.state.slideSelection], ['P1']);
  assert.equal(app.state.slideBusy, false);
  assert.match($('#slide-status').textContent, /Synthetic image failure/);
  uploadFailure = null;
  await app.uploadSlide(image);
  const upload = requests.filter((item) => item.url.startsWith('/api/slides?')).at(-1);
  assert.ok(upload.body instanceof Blob, 'binary upload, no multipart or JSON');
  assert.match(upload.url, /start=77/);
  app.editSlide('P1'); $('#slide-start').value = '10'; $('#slide-end').value = '35';
  await trigger($('#slide-form'), 'submit');
  assert.equal(calls('/api/slides/P1').at(-1).body.end, 35);
  assert.equal($('#slide-dialog').open, false);
  assert.match($('#slide-status').textContent, /00:10 — 00:35/, 'editing a range also updates the persistent upload status');

  remembered = true; knownVision = 'supported';
  const beforeRemembered = sends().length;
  await trigger(quickButtons[0], 'click');
  assert.equal(sends().length, beforeRemembered + 1, 'server remembered same-image authorization permits automatic send');
  app.state.includeImagesPreference = false; app.renderSlides();
  const beforeNoImages = sends().length;
  await trigger(quickButtons[1], 'click');
  assert.equal(sends().length, beforeNoImages + 1, 'shortcut without images sends directly');
  assert.equal(calls('/prepare').at(-1).body.include_images, false);

  // Delay both settings and prepare: arriving/edited source must not enter the
  // selection metadata captured by the original click.
  server.session.segments = [{ id: 'Sfreeze', start: 10, end: 30, version: 2, text: 'Version two' }];
  server.session.slides = [{ id: 'Pfreeze', sha256: 'original-image-hash', start: 10, end: 30, title: 'Original slide' }];
  server.session.groups = [{ id: 'Golder', start: 0, end: 9, segment_ids: [] }, { id: 'Gfreeze', start: 10, end: 30, segment_ids: ['Sfreeze'] }];
  await app.poll();
  app.state.clockSeconds = 35; app.state.slideSelection = null;
  app.state.includeImagesPreference = true; app.renderSlides();
  app.state.settingsDirty = true; app.state.saveGeneration++;
  let releaseSettings; let releaseFrozenPrepare;
  settingsGate = new Promise((resolve) => { releaseSettings = resolve; });
  prepareGate = new Promise((resolve) => { releaseFrozenPrepare = resolve; });
  const frozenClick = trigger(quickButtons[2], 'click');
  app.state.session.segments[0].version = 3;
  app.state.session.segments[0].text = 'Edited while settings save waits';
  app.state.session.segments.push({ id: 'Slate', start: 31, end: 35, version: 0, text: 'Arrived after click' });
  app.state.session.slides[0].start = 4; app.state.session.slides[0].end = 90;
  app.state.session.slides[0].title = 'Changed after click';
  app.state.session.slides.push({ id: 'Plate', sha256: 'later-image-hash', start: 20, end: null, title: 'Later slide' });
  app.state.session.groups[1].segment_ids.push('Slate'); app.state.session.groups[1].end = 35;
  app.state.session.groups.push({ id: 'Glater', start: 31, end: 35, segment_ids: ['Slate'] });
  releaseSettings(); settingsGate = null;
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  releaseFrozenPrepare(); await frozenClick; prepareGate = null;
  const frozenBody = calls('/prepare').at(-1).body;
  assert.deepEqual(frozenBody.seen_segments, [{ id: 'Sfreeze', version: 2 }], 'capture source versions before saving settings');
  assert.deepEqual(frozenBody.seen_slides, [{ id: 'Pfreeze', sha256: 'original-image-hash', start: 10, end: 30, title: 'Original slide' }], 'capture image association before saving settings');
  assert.equal(frozenBody.group_id, 'Gfreeze', 'the latest already-visible group is fixed at click time');
  assert.deepEqual(frozenBody.seen_group_segment_ids, ['Sfreeze'], 'late group members cannot enter the old selection');
  assert.ok(!JSON.stringify(frozenBody).includes('Version two'), 'metadata never supplies client-authored transcript text');
  assert.equal($('#slides-selection-note').textContent, '自动按范围选择', 'summary does not repeat the image count');
  const displayedQuestion = clone(app.state.session.questions.at(-1));
  displayedQuestion.vision = { state: 'supported', message: '仅曾验证同数量图片，结果不保证正确。' };
  app.openStudyReview(displayedQuestion);
  assert.match($('#study-review').innerHTML, /仅曾验证同数量图片，结果不保证正确/);
  assert.match($('#study-review').innerHTML, /尚不能确认原文完整/);

  prepareFailure = { status: 422, detail: '范围内超过 4 张课件，请手选。' };
  await trigger(quickButtons[0], 'click');
  const failedSelection = clone(app.state.studyFailedParams);
  server.session.segments[0].version = 5;
  server.session.segments[0].text = 'New version for explicit refresh';
  server.session.segments.push({ id: 'Snew', start: 31, end: 35, version: 0, text: 'New late source' });
  server.session.slides[0].title = 'Revised title'; server.session.slides[0].end = 80;
  server.session.slides.push({ id: 'Pnew', sha256: 'new-hash', start: 30, end: null, title: 'New slide' });
  await app.poll();
  app.state.slideSelection = new Set(['Pfreeze']);
  prepareFailure = null;
  await trigger($('#study-reprepare'), 'click');
  const retriedSelection = calls('/prepare').at(-1).body;
  assert.deepEqual(retriedSelection.seen_segments, failedSelection.seen_segments, 'manual selection retry keeps source versions from the failed click');
  assert.deepEqual(retriedSelection.seen_slides, failedSelection.seen_slides, 'manual selection retry keeps original image intervals and titles');
  assert.equal(retriedSelection.anchor, failedSelection.anchor);
  app.state.studyFailedParams = clone(failedSelection);
  app.state.slideSelection = new Set(['Pnew']);
  const callsBeforeNewImage = calls('/prepare').length;
  await trigger($('#study-reprepare'), 'click');
  assert.equal(calls('/prepare').length, callsBeforeNewImage, 'newly added image cannot enter an old failed selection');
  assert.match($('#study-status').textContent, /上次点击时尚不存在/);

  await app.refreshStudy(displayedQuestion);
  const refreshedSelection = calls('/prepare').at(-1).body;
  assert.equal(refreshedSelection.anchor, displayedQuestion.evidence.requested_range.end, 'refresh keeps the old requested endpoint');
  assert.deepEqual(refreshedSelection.seen_segments, [{ id: 'Sfreeze', version: 5 }, { id: 'Snew', version: 0 }], 'explicit refresh captures current identities and versions');
  assert.equal(refreshedSelection.seen_slides[0].title, 'Revised title');
  assert.equal(refreshedSelection.seen_slides.length, 2);

  server.session.groups = []; await app.poll();
  const callsBeforeEmptyGroup = calls('/prepare').length;
  await trigger(quickButtons[2], 'click');
  assert.equal(calls('/prepare').length, callsBeforeEmptyGroup, 'no visible group cannot be replaced by one arriving after the click');
  assert.match($('#study-status').textContent, /还没有可选的意群/);
  assert.equal(microphoneCalls, 0);
  console.log('study-ui passed: consent, frozen snapshots, recovery, image selection, input preservation, busy switching; synthetic DOM/API only');
})().catch((error) => { console.error(error); process.exitCode = 1; });
