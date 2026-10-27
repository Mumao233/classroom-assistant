/* Browser-cache contract tests with an asynchronous IndexedDB test double.
 * Real IndexedDB/reload acceptance is also performed separately in a browser. */
'use strict';
const assert = require('node:assert/strict');
const { createStudyCache } = require('../static/study-cache.js');

function fakeIndexedDB() {
  const stores = new Map();
  const control = { failWrites: false, failOpen: false };
  const db = {
    objectStoreNames: { contains: name => stores.has(name) },
    createObjectStore(name) { stores.set(name, new Map()); },
    close() {},
    transaction(names, mode) {
      const tx = { error: null }; let pending = 0; let aborted = false;
      const finish = () => setImmediate(() => {
        if (!pending && !aborted) tx.oncomplete?.();
      });
      tx.objectStore = name => {
        const map = stores.get(name);
        const op = fn => {
          const request = {}; pending++;
          setImmediate(() => {
            if (mode === 'readwrite' && control.failWrites) {
              aborted = true; tx.error = Error('QuotaExceededError'); tx.onabort?.();
            } else {
              request.result = fn(); request.onsuccess?.({ target: request });
            }
            pending--; finish();
          }); return request;
        };
        return {
          getAll: () => op(() => [...map.values()].map(value => structuredClone(value))),
          get: key => op(() => map.has(key) ? structuredClone(map.get(key)) : undefined),
          put: record => op(() => { map.set(record.id, structuredClone(record)); return record.id; }),
          delete: key => op(() => map.delete(key)),
        };
      };
      tx.abort = () => { aborted = true; tx.error = Error('Aborted'); setImmediate(() => tx.onabort?.()); };
      finish(); return tx;
    },
  };
  control.open = () => {
    const request = {};
    setImmediate(() => {
      if (control.failOpen) { request.error = Error('SecurityError'); request.onerror?.(); return; }
      request.result = db;
      if (!stores.size) request.onupgradeneeded?.();
      request.onsuccess?.();
    }); return request;
  };
  return control;
}
const record = (id, session = 'A', bytes = 3) => ({ id, session_id: session, start: 12.5,
  title: '课件😀', blob: new Blob([new Uint8Array(bytes)], { type: 'image/png' }),
  mime_type: 'image/png', size: bytes, created_at: '2026-10-28T00:00:00Z', status: 'failed', error: 'synthetic failure' });

async function multiTabRegressions() {
  const db = fakeIndexedDB();
  const a = createStudyCache({ indexedDB: db }), b = createStudyCache({ indexedDB: db });
  await a.init(); await b.init();
  await a.putUpload(record('from-a'));
  assert.deepEqual((await b.listUploads('A')).map(item => item.id), ['from-a'], 'list sees another tab\'s durable upload');
  await a.putUpload(record('from-a-later'));
  await b.clearUploads('A');
  const fresh = createStudyCache({ indexedDB: db }); await fresh.init();
  assert.equal((await fresh.listUploads('A')).length, 0, 'clear enumerates current DB records, not a stale tab map');

  const request = id => ({ kind: 'legacy-ask', request_id: id, body: { session_id: 'A', question: 'Original?' } });
  await a.setRequest('A', request('first-unresolved'));
  await assert.rejects(b.setRequest('A', request('second-unresolved')), /待核对|未解决|其他页面|冲突/);
  assert.equal((await b.getRequest('A')).request_id, 'first-unresolved');
  await b.setRequest('A', { ...request('first-unresolved'), error: 'Updated delivery status' });
  assert.equal((await a.getRequest('A')).error, 'Updated delivery status');
  await assert.rejects(a.setRequest('A', { ...request('first-unresolved'), body: { session_id: 'A', question: 'Different?' } }), /原问题|原材料|身份/);
  await a.setRequest('A', null, 'first-unresolved');
  await a.setRequest('A', request('new-after-completion'));
  await assert.rejects(b.setRequest('A', null), /待核对|未解决|其他页面|冲突/);
  assert.equal((await fresh.getRequest('A')).request_id, 'new-after-completion', 'stale tab cannot delete a newer request');
  await a.setRequest('A', null, 'new-after-completion');

  const study = { kind: 'study', request_id: 'study-one', session_id: 'A', params: { session_id: 'A', question: 'Why?', anchor: 20 }, phase: 'prepare' };
  await a.setRequest('A', study);
  await a.setRequest('A', { ...study, phase: 'send', question_id: 'Q0001', error: 'Response lost' });
  await assert.rejects(a.setRequest('A', { ...study, params: { ...study.params, anchor: 30 } }), /原问题|原材料|身份/);
  assert.equal((await b.getRequest('A')).phase, 'send');
  db.failWrites = true;
  assert.equal((await a.setRequest('A', { ...study, phase: 'send', question_id: 'Q0001', error: 'Only page-local now' })).persistent, false);
  await assert.rejects(a.setRequest('A', null, 'study-one'), /未能清除|仍保留/);
  assert.equal((await a.getRequest('A')).request_id, 'study-one', 'failed durable deletion must keep the local recovery entry too');
  db.failWrites = false;
  await a.setRequest('A', null, 'study-one');
  assert.equal(await fresh.getRequest('A'), null);
  await b.putUpload(record('durable-other', 'B'));
  db.failWrites = true;
  await a.putUpload(record('page-only'));
  db.failWrites = false;
  await b.putUpload(record('new-other', 'B'));
  assert.deepEqual((await a.listUploads()).map(item => item.id), ['durable-other', 'new-other', 'page-only']);
  await b.clearUploads('B');
  assert.deepEqual((await a.listUploads()).map(item => item.id), ['page-only'], 'DB refresh retains only page-local fallback, not stale durable items');
  await a.clearUploads('A');
  assert.equal((await a.listUploads()).length, 0);

  const capped = fakeIndexedDB();
  const capA = createStudyCache({ indexedDB: capped, maxItems: 1 }), capB = createStudyCache({ indexedDB: capped, maxItems: 1 });
  await capA.init(); await capB.init();
  await capA.putUpload(record('old-cap'));
  await capB.listUploads();
  await capA.clearUploads('A');
  await capB.putUpload(record('new-cap'));
  assert.deepEqual((await capB.listUploads()).map(item => item.id), ['new-cap'], 'stale local quota cannot reject after another tab cleared storage');
  console.log('study-cache multi-tab regressions passed');
}

(async () => {
  await multiTabRegressions();
  const db = fakeIndexedDB(); let cache = createStudyCache({ indexedDB: db });
  assert.equal((await cache.init()).persistent, true);
  await cache.putUpload(record('one')); await cache.putUpload(record('two', 'B'));
  await cache.setDraft('A', 'Why 供给😀?'); await cache.setDraft('B', 'Another question');
  await cache.setRequest('A', {kind: 'legacy-ask', request_id: 'ask-once', body: {session_id: 'A', question: 'Why?'}});
  cache = createStudyCache({ indexedDB: db }); await cache.init();
  assert.deepEqual((await cache.listUploads('A')).map(r => r.id), ['one']);
  assert.equal(await cache.getDraft('A'), 'Why 供给😀?');
  assert.equal(await cache.getDraft('C'), '');
  assert.equal((await cache.getRequest('A')).request_id, 'ask-once');
  assert.equal(await cache.getRequest('B'), null);
  await cache.setRequest('A', null);
  const reopened = createStudyCache({ indexedDB: db }); await reopened.init();
  assert.equal(await reopened.getRequest('A'), null);
  assert.equal(await reopened.getDraft('A'), 'Why 供给😀?');
  const old = (await cache.listUploads('A'))[0];
  assert.deepEqual([...new Uint8Array(await old.blob.arrayBuffer())], [0, 0, 0]);
  await cache.putUpload({ ...old, status: 'unknown' });
  await assert.rejects(cache.putUpload({ ...old, session_id: 'B' }), /原课堂|身份/);
  await assert.rejects(cache.putUpload({ ...old, blob: new Blob(['abc']) }), /原图|身份/);
  assert.equal((await cache.listUploads()).length, 2);
  for (let i = 0; i < 8; i++) await cache.putUpload(record('extra' + i));
  await assert.rejects(cache.putUpload(record('eleventh')), /10/);
  assert.equal((await cache.listUploads()).length, 10, 'no silent eviction');
  await cache.clearUploads('A');
  assert.deepEqual((await cache.listUploads()).map(r => r.id), ['two']);
  assert.equal(await cache.getDraft('B'), 'Another question');
  await cache.deleteUpload('two'); assert.equal((await cache.listUploads()).length, 0);
  const small = createStudyCache({ indexedDB: fakeIndexedDB(), maxBytes: 5 }); await small.init();
  await small.putUpload(record('three'));
  await assert.rejects(small.putUpload(record('six')), /100 MiB|容量/);
  assert.equal((await small.listUploads()).length, 1);
  const concurrent = createStudyCache({ indexedDB: fakeIndexedDB(), maxItems: 1 }); await concurrent.init();
  const concurrentResults = await Promise.allSettled([concurrent.putUpload(record('x')), concurrent.putUpload(record('y'))]);
  assert.equal(concurrentResults.filter(r => r.status === 'fulfilled').length, 1);
  assert.equal((await concurrent.listUploads()).length, 1);
  db.failWrites = true;
  assert.equal((await cache.putUpload(record('memory'))).persistent, false);
  assert.match(cache.status().message, /仅当前页/);
  assert.equal((await cache.init()).persistent, false, 'init reflects a later quota failure');
  assert.equal((await cache.listUploads('A'))[0].id, 'memory');
  await cache.clearUploads('A'); assert.equal((await cache.listUploads()).length, 0, 'page-only fallback can be cleared');
  const denied = fakeIndexedDB(); denied.failOpen = true;
  const fallback = createStudyCache({ indexedDB: denied });
  assert.equal((await fallback.init()).persistent, false);
  await fallback.putUpload(record('local')); await fallback.setDraft('A', 'draft');
  assert.equal(await fallback.getDraft('A'), 'draft');
  assert.equal((await fallback.listUploads()).length, 1);
  const absent = createStudyCache({ indexedDB: null });
  assert.equal((await absent.init()).persistent, false);
  console.log('study-cache: persistence, isolation, identity, limits, no eviction, quota fallback passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
