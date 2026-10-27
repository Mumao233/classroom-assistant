/* Synthetic DOM and API acceptance. No real server, browser, audio, or AI. */
'use strict';
const assert = require('assert/strict');
const fs = require('fs');
const vm = require('vm');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '../static/index.html'), 'utf8');
const source = fs.readFileSync(path.join(__dirname, '../static/review.js'), 'utf8');
const elements = new Map(), downloads = [], calls = [], messages = [];
const clone = (x) => JSON.parse(JSON.stringify(x));
class Element {
  constructor(tag = 'DIV') { Object.assign(this, { tagName: tag.toUpperCase(), listeners: {}, dataset: {}, attributes: {}, value: '', checked: false, hidden: false, disabled: false, open: false, textContent: '', innerHTML: '', files: [] }); this.classList = { toggle: (k, v) => { this.attributes[k] = v; } }; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  setAttribute(k, v) { this.attributes[k] = v; }
  querySelectorAll() { return []; }
  showModal() { this.open = true; }
  close() { this.open = false; for (const fn of this.listeners.close || []) fn(); }
  focus() {} appendChild() {} remove() {}
  click() { if (this.href) downloads.push(this.href); }
  closest(selector) { return selector.split(',').some((p) => { const match = p.trim().match(/^\[data-([\w-]+)\]$/); return match && match[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()) in this.dataset; }) ? this : null; }
}
for (const match of html.matchAll(/<([a-z][\w-]*)\b[^>]*\bid="([^"]+)"[^>]*>/gi)) { assert.ok(!elements.has(match[2]), `duplicate id ${match[2]}`); elements.set(match[2], new Element(match[1])); }
for (const match of source.matchAll(/\$\('([\w-]+)'\)/g)) assert.ok(elements.has(match[1]), `missing ${match[1]}`);
const $ = (id) => elements.get(id);
const types = ['concept', 'derivation', 'example', 'pitfall'].map((value) => Object.assign(new Element('INPUT'), { value, checked: true }));
const docListeners = {};
const shell = new Element();
const document = { getElementById: $, querySelector: () => shell, querySelectorAll: (s) => s === '[data-review-type]' ? types : [], addEventListener: (type, fn) => { (docListeners[type] ||= []).push(fn); }, createElement: (tag) => new Element(tag), body: new Element() };
const makeStorage = () => { const map = new Map(); return { get length() { return map.size; }, key: (i) => [...map.keys()][i] ?? null, getItem: (k) => map.get(k) ?? null, setItem: (k, v) => map.set(k, String(v)), removeItem: (k) => map.delete(k) }; };
const localStorage = makeStorage();
let seq = 0, serviceGate = null, prepareGate = null, nextError = null, reviewUI;
const sandbox = { console, document, localStorage, sessionStorage: makeStorage(), crypto: { randomUUID: () => `synthetic-${++seq}` }, setTimeout, clearTimeout, addEventListener() {} }; sandbox.window = sandbox;
vm.runInNewContext(source, sandbox);
const makeSession = (id) => ({ id, title: 'Microeconomics', course_id: 'micro', segments: [{ id: 'S1', text: 'Price <script>alert(1)</script> $P=MC$', start: 10, end: 20, version: 1, audio_seq: 0 }], slides: [], audio: [{ seq: 0, start: 8, duration: 20 }], review: { marks: [], runs: [], cards: [], notes: [], selection_revision: 0, selected_note_id: null } });
let session = makeSession('A');
const snapshot = { courses: [{ id: 'micro', name: '微观经济学' }] };
const api = async (url, options = {}) => {
  calls.push({ url, body: options.body && clone(options.body) });
  if (serviceGate) await serviceGate;
  if (nextError) { const e = nextError; nextError = null; throw e; }
  if (url === '/api/review/prepare') {
    const run = { id: 'R1', status: 'prepared', evidence: { segments: [clone(session.segments[0])], slides: [] }, options: {}, destination: { token: 'D1', provider: 'codex', model: 'synthetic' }, source_status: { token: 'T1', changed: false }, coverage: { segment_count: 1, slide_count: 0, batch_count: 1, completed_batches: 0, range: [10, 20] }, batches: [{ status: 'prepared' }] };
    session.review.runs.push(run); return clone(run);
  }
  if (url.endsWith('/send') || url.endsWith('/continue')) { const run = session.review.runs[0]; run.status = 'pending'; return clone(run); }
  if (url === '/api/review/marks') { const previous = session.review.marks[0]; if (options.body.revision !== (previous?.revision || 0)) throw Object.assign(new Error('Mark CAS conflict'), { status: 409 }); const mark = { id: 'M1', ...options.body, revision: (previous?.revision || 0) + 1, evidence: { segments: [clone(session.segments[0])], slides: [] }, source_status: { changed: false } }; session.review.marks = [mark]; return clone(mark); }
  if (url.endsWith('/draft')) { const id = url.split('/').at(-2); const item = session.review.notes.find((x) => x.id === id); if (options.body.revision !== item.user_draft.revision) throw Object.assign(new Error('CAS conflict'), { status: 409 }); item.user_draft = { revision: item.user_draft.revision + 1, content: options.body.content, personal_note: options.body.personal_note }; return clone(item); }
  if (url === '/api/packages/preview') return { preview_id: 'preview1', title: 'Synthetic class', estimated_bytes: 1234, files_count: 3, missing: options.body.include_audio ? [{ kind: 'audio', id: '0', reason: 'missing synthetic clip' }] : [], warnings: ['未带录音时无法离线回听'] };
  if (url === '/api/packages/export') return { id: 'package1', status: 'ready', download_url: '/api/packages/download/package1' };
  if (url.startsWith('/api/packages/exports/')) return { id: 'package1', status: 'ready', download_url: '/api/packages/download/package1' };
  if (url.startsWith('/api/packages/import/preview')) return { preview_id: 'preview-import', title: 'Imported synthetic', source_session: 'original', missing: [], estimated_bytes: 1200 };
  if (url === '/api/packages/import') return { session_id: 'copyA', created: true };
  throw new Error(`Unexpected ${url}`);
};
const tick = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const trigger = async (id, type = 'click') => { for (const fn of $(id).listeners[type] || []) await fn({ target: $(id) }); await tick(); };
const delegated = async (dataset) => { const target = Object.assign(new Element('BUTTON'), { dataset }); for (const fn of docListeners.click || []) fn({ target }); await tick(); };
const poll = async () => reviewUI.sync(session);
(async () => {
  reviewUI = sandbox.ReviewUI.mount({ api, getSession: () => session, getSnapshot: () => snapshot, poll, toast: (t) => messages.push(t), jump: (id) => messages.push(`jump:${id}`), showSlide() {}, beforePrepare: async () => { if (prepareGate) await prepareGate; } });
  session.course_id = ''; session.title = 'Synthetic class';
  reviewUI.sync(session);
  assert.equal($('review-english').checked, false);
  session.course_id = 'micro'; reviewUI.sync(session);
  assert.equal($('review-english').checked, true, 'micro preset enables English');
  $('review-english').checked = false; await trigger('review-english', 'change');
  session.course_id = ''; reviewUI.sync(session); session.course_id = 'micro'; reviewUI.sync(session);
  assert.equal($('review-english').checked, false, 'explicit English preference survives preset changes');
  await trigger('review-open'); assert.equal($('review-view').hidden, false);
  $('review-scope').value = 'all'; $('review-images').checked = true;
  let release; prepareGate = new Promise((r) => { release = r; });
  const preparing = trigger('review-generate');
  session.segments.push({ id: 'S-late', version: 0, text: 'arrived late', start: 21, end: 30 });
  release(); await preparing; prepareGate = null;
  assert.deepEqual(calls.find((x) => x.url === '/api/review/prepare').body.seen_segments, [{ id: 'S1', version: 1 }], 'click snapshot precedes async settings');
  assert.equal($('review-plan-dialog').open, true);
  assert.ok(!$('review-plan-content').innerHTML.includes('<script>alert'));
  const before = calls.length; await trigger('review-plan-send'); assert.equal(calls.length, before, 'plan consent mandatory');
  $('review-plan-consent').checked = true; await trigger('review-plan-send'); assert.equal(calls.at(-1).body.destination_token, 'D1');
  session.review.runs[0].status = 'unknown'; session.review.runs[0].repeat_billing_required = true; await poll();
  await delegated({ reviewRun: 'R1' }); $('review-plan-consent').checked = true;
  const writes = calls.length; await trigger('review-plan-send'); assert.equal(calls.length, writes, 'billing acknowledgement mandatory');
  $('review-plan-duplicate').checked = true; await trigger('review-plan-send'); assert.equal(calls.at(-1).body.confirm_duplicate, true);
  await delegated({ reviewMark: 'unclear', reviewKind: 'segment', reviewTarget: 'S1' });
  assert.equal(session.review.marks[0].active, true); assert.match($('review-marks').innerHTML, /没懂/);
  await delegated({ reviewMarkEdit: 'M1' }); $('review-mark-note').value = 'My unsaved mark note';
  session.review.marks[0].revision = 2; session.review.marks[0].note = 'Other page mark note'; await poll();
  await trigger('review-mark-save');
  assert.equal(session.review.marks[0].note, 'Other page mark note', 'poll cannot upgrade an open mark editor CAS revision');
  assert.equal($('review-mark-note').value, 'My unsaved mark note', 'conflict retains mark note input');
  $('review-mark-dialog').close();
  const note = { id: 'N1', ai_content: { sections: [{ section: 'structure', title: '供需', body: 'Price is a signal.', source_ids: ['S1'], missing: [], supplement: '' }] }, user_draft: { revision: 0, content: 'Original note', personal_note: '' }, evidence: { segments: [clone(session.segments[0])], slides: [] }, source_status: { changed: false } };
  session.review.notes.push(note); await poll();
  await delegated({ reviewEdit: 'N1', reviewEditKind: 'note' }); assert.equal($('review-editor-dialog').open, true);
  $('review-editor-content').value = 'My edited answer'; await trigger('review-editor-content', 'input');
  note.user_draft.revision = 1; note.user_draft.content = 'Other tab answer';
  await trigger('review-editor-save'); assert.match($('review-editor-status').textContent, /CAS conflict/); assert.equal($('review-editor-content').value, 'My edited answer');
  await trigger('review-editor-rebase'); await trigger('review-editor-save'); assert.equal(note.user_draft.content, 'My edited answer'); assert.equal(note.user_draft.revision, 2);
  $('review-editor-content').value = 'A draft before switch'; await trigger('review-editor-content', 'input');
  const classA = session; session = makeSession('B'); await poll(); assert.equal($('review-editor-dialog').open, false); assert.equal(reviewUI.controller.drafts().length, 0);
  session = classA; await poll(); assert.equal(reviewUI.controller.drafts().length, 1);
  await delegated({ reviewJump: 'S1' }); assert.equal($('review-view').hidden, true); assert.ok(messages.includes('jump:S1'));
  await trigger('review-package-export'); assert.equal(calls.at(-1).body.include_audio, false, 'audio excluded by default');
  $('review-package-audio').checked = true; await trigger('review-package-audio', 'change');
  const packageWrites = calls.filter((x) => x.url === '/api/packages/export').length;
  await trigger('review-package-confirm'); assert.equal(calls.filter((x) => x.url === '/api/packages/export').length, packageWrites, 'missing media requires consent');
  $('review-package-missing').checked = true; await trigger('review-package-confirm'); assert.equal(downloads.at(-1), '/api/packages/download/package1');
  await trigger('review-package-export'); nextError = new Error('lost export response'); await trigger('review-package-confirm'); assert.ok(Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)).some((k) => k.startsWith('classroom-package-operation:')));
  const posts = calls.filter((x) => x.url === '/api/packages/export').length; await trigger('review-package-recover');
  assert.equal(calls.filter((x) => x.url === '/api/packages/export').length, posts, 'package recovery only queries'); assert.equal(localStorage.getItem('classroom-package-operation'), null);
  localStorage.setItem('classroom-package-operation:other-page', JSON.stringify({ mode: 'export', sid: 'A', request_id: 'other-page' }));
  await trigger('review-package-recover'); assert.equal(localStorage.getItem('classroom-package-operation:other-page'), null, 'each page package operation is independently recoverable');
  await trigger('review-package-import'); $('review-package-file').files = [{ size: 1200, name: 'synthetic.zip' }]; await trigger('review-package-file', 'change'); await trigger('review-package-confirm'); assert.match($('review-package-status').textContent, /独立课堂 copyA/);
  assert.equal(session.id, 'A', 'import does not redirect current classroom');
  const duplicatePage = sandbox.ReviewUI.mount({ api, getSession: () => session, getSnapshot: () => snapshot, poll, toast() {}, jump() {}, showSlide() {} });
  assert.notEqual(duplicatePage.controller.tabId, reviewUI.controller.tabId, 'duplicating a tab must not clone the local draft owner');
  assert.ok(!source.includes('.blob()'), 'large exports use native download, not whole-file Blob');
  console.log('review-mount passed: plan/billing consent, frozen click selection, mark UI, CAS conflict/rebase, class draft recovery, source jump, package preflight/native download/lookup/import (synthetic DOM/API)');
})().catch((e) => { console.error(e); process.exitCode = 1; });
