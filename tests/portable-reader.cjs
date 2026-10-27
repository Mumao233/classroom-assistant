/* Offline reader DOM simulation; no browser, network, media decoding or AI. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const base = path.join(__dirname, '..');
const renderer = fs.readFileSync(path.join(base, 'static/review-render.js'), 'utf8');
const reader = fs.readFileSync(path.join(base, 'static/portable-reader.js'), 'utf8');
const hash = 'a'.repeat(64);
function read(session, media = []) {
  const events = {}, nodes = new Map(), buttons = [], notices = [];
  const root = { innerHTML: '', addEventListener(type, fn) { events[type] = fn; }, querySelectorAll(selector) { return selector.includes('data-seek') ? buttons : []; } };
  const elements = { reader: root, title: {}, summary: {}, 'classroom-data': { textContent: JSON.stringify({ session, manifest: { media } }) } };
  const sandbox = { window: {}, document: { getElementById: (id) => elements[id] || nodes.get(id) || null, createElement: () => ({}) } };
  sandbox.window.katex = require(path.join(base, 'static/vendor/katex/katex.min.js'));
  vm.createContext(sandbox); vm.runInContext(renderer, sandbox); vm.runInContext(reader, sandbox);
  for (const match of root.innerHTML.matchAll(/<audio id="([^"]+)"/g)) {
    const id = match[1]; nodes.set(id, { id, tagName: 'AUDIO', replaceWith(note) { nodes.delete(id); notices.push(note.textContent); } });
  }
  for (const match of root.innerHTML.matchAll(/<button[^>]+data-seek="([^"]+)"[^>]+data-offset="([^"]+)"/g)) buttons.push({ dataset: { seek: match[1], offset: match[2] }, disabled: false, hidden: false, remove() { this.removed = true; }, setAttribute(k, v) { this[k] = v; } });
  return { html: root.innerHTML, events, nodes, buttons, notices };
}
const segment = { id: 'S1', text: 'Current source 中文 😀', start: 10, end: 20, version: 2, audio_seq: 0 };
const historical = { ...segment, text: 'Frozen source $P=MC$', version: 1 };
const slide = { id: 'P1', title: 'Supply', start: 10, end: 20, version: 1, path: 'slides/p1.png', sha256: hash };
const evidence = { segments: [historical], slides: [slide] };
const changed = { changed: true, new_material: true, unavailable: false, messages: ['SOURCE_CHANGED_SENTINEL'] };
const session = { id: 'A', title: 'Class </script>', segments: [segment], slides: [slide], audio: [], review: {
  marks: [{ id: 'M1', kind: 'important', active: true, target_id: 'S1', evidence, source_status: changed }],
  cards: [{ id: 'C1', ai_content: { title: 'Card', front: 'Question', back: 'Answer', source_ids: ['S1', 'P1'] }, evidence, source_status: changed }],
  notes: [{ id: 'N1', ai_content: { sections: [{ title: 'Notes', body: 'Notes text', source_ids: ['S1'] }] }, evidence, source_status: changed }], selected_note_id: 'N1',
  runs: [{ id: 'R1', status: 'partial', error: 'BATCH_FAILED_SENTINEL', coverage: { range: [10, 20], segment_count: 5, slide_count: 1, completed_batches: 1, batch_count: 3, excluded: ['MISSING_SOURCE_SENTINEL'] }, batches: [{ status: 'done' }, { status: 'error', error: 'SECOND_BATCH_FAILED' }, { status: 'prepared' }], source_status: changed }],
} };
const media = [{ kind: 'image', original_path: slide.path, sha256: hash, availability: 'available', archive_path: `assets/${hash}.png` }, { kind: 'audio', id: 0, start: 8, duration: 20, availability: 'available', archive_path: `assets/${hash}.wav` }];
let count = 0; const check = (fn) => { fn(); count++; };
const output = read(session, media);
check(() => assert.match(output.html, /SOURCE_CHANGED_SENTINEL/, 'frozen sources must disclose changes'));
check(() => assert.match(output.html, /新增内容尚未纳入/, 'new material differs from edited evidence'));
check(() => assert.match(output.html, /BATCH_FAILED_SENTINEL/, 'partial review failure must remain visible offline'));
check(() => assert.match(output.html, /MISSING_SOURCE_SENTINEL/, 'excluded source coverage must remain visible'));
check(() => assert.match(output.html, /1\s*\/\s*3/, 'completed and planned batch count must be visible'));
check(() => assert.match(output.html, /id="source-segment-53-31"/));
check(() => assert.match(output.html, /href="#source-segment-53-31"/));
check(() => assert.match(output.html, /id="source-slide-50-31"/));
check(() => assert.match(output.html, /href="#source-slide-50-31"/));
check(() => assert.equal([...output.html.matchAll(/id="source-segment-53-31"/g)].length, 1, 'snapshots cannot duplicate live source anchors'));
check(() => assert.match(output.html, /class="katex"/, 'local formula renderer works in snapshot text'));
const first = output.buttons[0]; output.events.error({ target: output.nodes.get(first.dataset.seek) });
check(() => assert.ok(first.disabled || first.hidden || first.removed, 'broken audio seek control must become unavailable'));
check(() => assert.doesNotThrow(() => output.events.click({ target: { closest: () => first } }), 'stale playback clicks must be safe'));
const maliciousMedia = media.map((x) => ({ ...x, archive_path: x.kind === 'image' ? 'javascript:alert(1)' : 'https://evil.invalid/audio.wav' }));
const hostile = read({ ...session, segments: [{ ...segment, text: '</script><img src=x onerror=alert(1)> $\\href{https://evil.invalid}{x}$' }] }, maliciousMedia).html;
check(() => assert.doesNotMatch(hostile, /<script|<img[^>]+onerror|(?:src|href)="(?:javascript|https?):/));
check(() => assert.match(hostile, /公式无法渲染/));
check(() => assert.match(hostile, /&lt;img/));
const omitted = read(session, media.map((x) => ({ ...x, availability: 'not_included', reason: 'NOT_INCLUDED_SENTINEL' })));
check(() => assert.doesNotMatch(omitted.html, /<audio|<img/));
check(() => assert.match(omitted.html, /NOT_INCLUDED_SENTINEL/));
console.log(`portable-reader: ${count} checks passed (synthetic DOM; real local KaTeX; no browser/network/media)`);
