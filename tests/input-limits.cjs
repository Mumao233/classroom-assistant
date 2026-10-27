/* Synthetic DOM and API only: no browser, microphone, real classroom, or AI. */
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
  constructor(tag = 'DIV') { Object.assign(this, { tagName: tag.toUpperCase(), value: '', textContent: '', innerHTML: '', hidden: false, disabled: false, open: false, checked: false, dataset: {}, attributes: {}, listeners: {}, style: {}, scrollTop: 0, scrollHeight: 400, clientHeight: 400, paused: true, ended: false }); this.classList = { toggle: (key, value) => { this.attributes[key] = value; } }; }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; }
  focus() { document.activeElement = this; }
  reportValidity() { return true; }
  pause() {}
  appendChild() {}
  remove() {}
  click() {}
  select() {}
  scrollIntoView() {}
}
for (const match of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)) elements.set(`#${match[2]}`, new Element(match[1]));
for (const match of source.matchAll(/\$\('(#[-\w]+)'\)/g)) assert.ok(elements.has(match[1]), `missing DOM node ${match[1]}`);
const document = {
  activeElement: null, body: new Element(),
  querySelector(selector) { if (selector === 'dialog[open]') return [...elements.values()].find((element) => element.tagName === 'DIALOG' && element.open) || null; if (!elements.has(selector)) elements.set(selector, new Element()); return elements.get(selector); },
  querySelectorAll() { return []; }, getElementById(id) { return elements.get(`#${id}`); },
  addEventListener() {}, createElement(tag) { return new Element(tag); },
};
const fields = [
  { input: '#manual-text', form: '#text-form', endpoint: '/api/text', property: 'text', key: 'text', name: '手动原文', limit: 12000 },
  { input: '#edit-segment-text', form: '#edit-segment-form', endpoint: '/api/segments/S1/edit', property: 'text', key: 'text', name: '修改原文', limit: 12000 },
  { input: '#question-input', form: '#question-form', endpoint: '/api/ask', property: 'question', key: 'question', name: '提问', limit: 4000 },
  { input: '#course-syllabus', form: '#glossary-form', endpoint: '/api/courses/course/glossary', property: 'syllabus', key: 'syllabus', name: '课程大纲', limit: 12000, optional: true },
];
const limits = { text: 12000, question: 4000, syllabus: 12000 };
const server = {
  session: { id: 'synthetic-session', title: 'Synthetic class', settings: { terms: '', auto_analyze: false }, course_id: 'course', segments: [{ id: 'S1', start: 0, end: 1, text: 'Synthetic source.' }], groups: [], questions: [], audio: [], revisions: [] },
  sessions: [], courses: [{ id: 'course', name: 'Synthetic course', glossary: [], status: 'saved' }], jobs: [], queue: { audio: 0, ai: 0 }, ai: { paused: false }, soundcheck: { status: 'idle' }, status: { asr: {}, codex: {} }, input_limits: limits,
};
const requests = [];
let offline = false, failurePath = null, microphoneCalls = 0;
let deferredSubmit = null;
const clone = (value) => JSON.parse(JSON.stringify(value));
const context = vm.createContext({
  document, console, Float32Array, ArrayBuffer, DataView, Blob, URL, URLSearchParams, AbortController, performance,
  location: { href: 'http://127.0.0.1:8766/', origin: 'http://127.0.0.1:8766' }, window: { innerWidth: 1400, addEventListener() {} },
  navigator: { mediaDevices: { addEventListener() {}, async getUserMedia() { microphoneCalls++; throw new Error('Microphone forbidden'); } } },
  localStorage: { getItem() { return null; }, setItem() {} }, setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  fetch: async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url, body, method: options.method || 'GET' });
    if (deferredSubmit?.path === url) { deferredSubmit.entered(); await deferredSubmit.wait; }
    if ((offline && url === '/api/state') || url === failurePath) return { ok: false, status: 422, json: async () => ({ detail: 'Synthetic request rejected.' }) };
    if (url === '/api/state') return { ok: true, status: 200, json: async () => clone(server) };
    if (url.startsWith('/api/requests/')) return { ok: true, status: 200, json: async () => ({ found: false }) };
    assert.ok(fields.some((field) => field.endpoint === url), `unexpected mock API request ${url}`);
    return { ok: true, status: 200, json: async () => ({}) };
  },
});
const testSource = source.replace('  initialize().catch((error) => toast(error.message, true));', '  globalThis.test = { state, poll, render };');
assert.notEqual(testSource, source, 'real app startup is disabled');
vm.runInContext(testSource, context);
const $ = (selector) => document.querySelector(selector);
const writes = (field) => requests.filter((request) => request.url === field.endpoint);
async function trigger(element, type) { for (const handler of element.listeners[type] || []) await handler({ preventDefault() {}, target: element, submitter: new Element('BUTTON') }); }
async function prepare(field, value) {
  // Each boundary case represents a fresh form; request recovery is covered
  // separately in study-ui.cjs with persistent request IDs and lost responses.
  context.test.state.legacyRequests.clear();
  context.test.state.queueLoaded = true;
  context.test.state.editingSegment = { id: 'S1', sessionId: server.session.id, epoch: context.test.state.sessionEpoch };
  context.test.state.glossaryCourseId = 'course';
  $('#glossary-dialog').open = true;
  $('#generate-glossary').disabled = false;
  $(field.input).value = value;
  await trigger($(field.input), 'input');
}
const results = [];
let finished = false;
process.on('beforeExit', () => {
  if (!finished) { console.error('Input-limit suite did not finish (unresolved mock request).'); process.exitCode = 1; }
});
async function check(name, callback) {
  try { await callback(); results.push({ name, passed: true }); }
  catch (error) { results.push({ name, passed: false, error: String(error.message).slice(0, 400) }); }
}
function mixedText(length) { return '中a😀'.repeat(Math.floor(length / 3)) + '文'.repeat(length % 3); }

(async () => {
  for (const field of fields) await check(`${field.name}: submission before first connection is blocked`, async () => {
    context.test.state.snapshot = null; context.test.state.session = null; context.test.state.connected = false;
    await prepare(field, 'Synthetic 中😀 draft'); const before = writes(field).length;
    await trigger($(field.form), 'submit'); assert.equal(writes(field).length, before);
    assert.ok($(field.input).value === 'Synthetic 中😀 draft');
    assert.match($('#toast').textContent, /服务|上限/);
  });
  await context.test.poll();
  for (const field of fields) {
    await check(`${field.name}: native UTF-16 maxlength removed`, () => {
      const tag = html.match(new RegExp(`<textarea\\b[^>]*\\bid="${field.input.slice(1)}"[^>]*>`))[0];
      assert.ok(!/\bmaxlength\s*=/.test(tag), 'native maxlength can truncate valid code-point input');
      assert.match(tag, /aria-describedby=/, 'input limit is accessible from its field');
      assert.ok(elements.has(`${field.input}-limit`), 'live limit hint exists in the actual HTML');
    });
    for (const length of [field.limit - 1, field.limit]) await check(`${field.name}: mixed Unicode length ${length} accepted`, async () => {
      const value = mixedText(length); await prepare(field, value);
      const before = writes(field).length; await trigger($(field.form), 'submit');
      assert.equal(writes(field).length, before + 1, 'valid boundary must be submitted exactly once');
      assert.ok(writes(field).at(-1).body[field.property] === value, 'valid input must reach the service without trimming or truncation');
    });
    await check(`${field.name}: over limit rejected without discarding draft`, async () => {
      const value = mixedText(field.limit + 1); await prepare(field, value);
      const before = writes(field).length; await trigger($(field.form), 'submit');
      assert.equal(writes(field).length, before, 'over-limit draft must not be sent');
      assert.ok($(field.input).value === value, 'rejected draft is preserved');
      const message = $('#toast').textContent;
      assert.ok(message.includes(field.name) && message.includes(String(field.limit)) && message.includes(String(field.limit + 1)), 'error identifies field, limit, and actual code-point count');
      await context.test.poll(); assert.ok($(field.input).value === value, 'poll must preserve rejected drafts');
    });
    await check(`${field.name}: surrounding whitespace is submitted and counted`, async () => {
      const value = ' \n中 a😀\t '; await prepare(field, value);
      const before = writes(field).length; await trigger($(field.form), 'submit');
      assert.equal(writes(field).length, before + 1);
      assert.ok(writes(field).at(-1).body[field.property] === value, 'raw whitespace must not be trimmed');
      const padded = ' ' + 'a'.repeat(field.limit) + '\n'; await prepare(field, padded);
      const paddedBefore = writes(field).length; await trigger($(field.form), 'submit');
      assert.equal(writes(field).length, paddedBefore, 'whitespace contributes to the limit');
      assert.ok($(field.input).value === padded);
    });
    await check(`${field.name}: whitespace-only requirement`, async () => {
      for (const value of ['', ' \t\n ']) {
        await prepare(field, value); const before = writes(field).length;
        await trigger($(field.form), 'submit');
        assert.equal(writes(field).length, before + (field.optional ? 1 : 0));
        if (field.optional) assert.ok(writes(field).at(-1).body[field.property] === value);
        else assert.ok($('#toast').textContent.includes(field.name), 'required blank field must be identified');
      }
    });
    await check(`${field.name}: Python-compatible Unicode whitespace`, async () => {
      for (const value of ['\u0085', '\u001c\u001f']) {
        await prepare(field, value); const before = writes(field).length;
        await trigger($(field.form), 'submit');
        assert.equal(writes(field).length, before + (field.optional ? 1 : 0), 'NEL and control separators count as whitespace');
      }
      await prepare(field, '\ufeff'); const before = writes(field).length;
      await trigger($(field.form), 'submit');
      assert.equal(writes(field).length, before + 1, 'FEFF is a code point, not Python whitespace');
      assert.ok(writes(field).at(-1).body[field.property] === '\ufeff');
    });
    await check(`${field.name}: server rejection preserves raw draft`, async () => {
      const value = ' \nSynthetic 中😀 draft '; await prepare(field, value);
      failurePath = field.endpoint;
      try { await trigger($(field.form), 'submit'); } finally { failurePath = null; }
      assert.ok($(field.input).value === value, 'failed submission preserves full text');
      await context.test.poll(); assert.ok($(field.input).value === value, 'poll must not replace drafts after server failure');
    });
  }
  for (const field of fields.filter((item) => ['#manual-text', '#question-input'].includes(item.input))) await check(`${field.name}: successful deferred submission preserves newer draft`, async () => {
    const submitted = 'Submitted synthetic 中😀 text';
    const newerDraft = 'New draft typed while waiting 新😀';
    await prepare(field, submitted);
    let releaseSubmission, markEntered;
    const entered = new Promise((resolve) => { markEntered = resolve; });
    const wait = new Promise((resolve) => { releaseSubmission = resolve; });
    deferredSubmit = { path: field.endpoint, wait, entered: markEntered };
    const before = writes(field).length;
    const submitting = trigger($(field.form), 'submit');
    await entered;
    try {
      assert.equal(writes(field).length, before + 1);
      assert.ok(writes(field).at(-1).body[field.property] === submitted, 'in-flight request retains the submitted text');
      $(field.input).value = newerDraft;
      await trigger($(field.input), 'input');
      await context.test.poll();
      assert.ok($(field.input).value === newerDraft, 'poll preserves text entered during the request');
      releaseSubmission();
      await submitting;
      assert.ok($(field.input).value === newerDraft, 'successful completion must not erase a newer draft');
    } finally {
      releaseSubmission(); await submitting; deferredSubmit = null;
    }
  });
  for (const field of fields) await check(`${field.name}: limits come from live metadata`, async () => {
    server.input_limits = { ...limits, [field.key]: 3 }; await context.test.poll();
    await prepare(field, '中a😀'); const before = writes(field).length;
    await trigger($(field.form), 'submit'); assert.equal(writes(field).length, before + 1);
    await prepare(field, '👩‍💻'); const emojiBefore = writes(field).length;
    await trigger($(field.form), 'submit'); assert.equal(writes(field).length, emojiBefore + 1, 'joined emoji occupies three code points');
    await prepare(field, '👩‍👩‍👧‍👦'); const familyBefore = writes(field).length;
    await trigger($(field.form), 'submit'); assert.equal(writes(field).length, familyBefore, 'one visual family emoji can exceed a three-code-point bound');
    await prepare(field, '中a😀 '); const bounded = writes(field).length;
    await trigger($(field.form), 'submit'); assert.equal(writes(field).length, bounded, 'metadata controls the bound');
    const hint = $(`${field.input}-limit`).textContent;
    assert.ok(hint.includes('3') && hint.includes('4') && hint.includes('Unicode') && hint.includes('空白'), 'live hint includes code-point counts and whitespace rule');
  });
  for (const metadata of [undefined, { text: 0, question: '4000', syllabus: -1 }]) {
    server.input_limits = metadata; await context.test.poll();
    for (const field of fields) await check(`${field.name}: ${metadata ? 'invalid' : 'missing'} metadata blocks writes`, async () => {
      await prepare(field, '中a😀'); const before = writes(field).length;
      await trigger($(field.form), 'submit'); assert.equal(writes(field).length, before);
      assert.ok($(field.input).value === '中a😀');
      assert.match($('#toast').textContent, /上限/);
    });
  }
  server.input_limits = limits; await context.test.poll(); offline = true; await context.test.poll();
  for (const field of fields) await check(`${field.name}: offline service blocks writes despite cached metadata`, async () => {
    await prepare(field, '中a😀'); const before = writes(field).length;
    await trigger($(field.form), 'submit'); assert.equal(writes(field).length, before);
    assert.ok($(field.input).value === '中a😀'); assert.match($('#toast').textContent, /服务/);
  });
  await check('no microphone access', () => assert.equal(microphoneCalls, 0));
  const failures = results.filter((result) => !result.passed);
  console.log(JSON.stringify({ test: 'Unicode input limits', passed: results.length - failures.length, failed: failures.length, mockRequests: requests.length, realNetwork: false, failures }, null, 2));
  if (failures.length) process.exitCode = 1;
  finished = true;
})().catch((error) => { console.error(error); process.exitCode = 1; });
