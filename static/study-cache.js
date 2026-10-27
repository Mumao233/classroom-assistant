/* Local pending media and per-class drafts. Nothing here sends network requests. */
(function (root) {
  'use strict';
  function createStudyCache(options = {}) {
    let factory;
    try { factory = Object.prototype.hasOwnProperty.call(options, 'indexedDB') ? options.indexedDB : root.indexedDB; }
    catch (_) { factory = null; }
    const maxItems = options.maxItems ?? 10;
    const maxBytes = options.maxBytes ?? 100 * 1024 * 1024;
    let database = null, ready = null, serial = Promise.resolve();
    let persistent = false, message = '图片和草稿仅当前页保留，刷新或关闭后将丢失。';
    const uploads = new Map(), drafts = new Map(), requests = new Map(), durableIds = new Set();
    const pageUploadIds = new Set(), pageRequests = new Map(), durableRequests = new Map();
    const status = () => ({ persistent, message });
    const fallback = () => { persistent = false; message = '浏览器本机存储不可用或已满；新增图片和草稿仅当前页保留，刷新或关闭后将丢失。'; };
    const enqueue = fn => {
      const next = serial.then(async () => { await init(); return fn(); });
      serial = next.catch(() => {}); return next;
    };
    function transact(names, mode, action) {
      return new Promise((resolve, reject) => {
        let tx;
        try { tx = database.transaction(names, mode); } catch (error) { reject(error); return; }
        let value, failure;
        tx.oncomplete = () => resolve(value);
        tx.onabort = tx.onerror = () => reject(failure || tx.error || Error('本机存储失败'));
        const fail = error => { failure = error; tx.abort(); };
        try { action(tx, result => { value = result; }, fail); } catch (error) { fail(error); }
      });
    }
    async function init() {
      if (ready) { await ready; return status(); }
      ready = (async () => {
        if (!factory) return status();
        try {
          database = await new Promise((resolve, reject) => {
            const request = factory.open('classroom-study-cache', 1);
            let abandoned = false;
            request.onupgradeneeded = () => {
              const db = request.result;
              for (const name of ['uploads', 'drafts']) if (!db.objectStoreNames.contains(name)) db.createObjectStore(name, { keyPath: 'id' });
            };
            request.onsuccess = () => { if (abandoned) request.result.close(); else resolve(request.result); };
            request.onerror = () => reject(request.error);
            request.onblocked = () => { abandoned = true; reject(Error('本机存储被其他页面占用')); };
          });
          await transact(['uploads', 'drafts'], 'readonly', tx => {
            tx.objectStore('uploads').getAll().onsuccess = event => {
              for (const record of event.target.result) { uploads.set(record.id, record); durableIds.add(record.id); }
            };
            tx.objectStore('drafts').getAll().onsuccess = event => {
              for (const record of event.target.result) {
                if (record.kind === 'request') { requests.set(record.session_id, record.value); durableRequests.set(record.session_id, record.value.request_id); }
                else drafts.set(record.id, record.text);
              }
            };
          });
          persistent = true; message = '';
          database.onversionchange = () => { database.close(); fallback(); };
        } catch (_) { fallback(); }
        return status();
      })();
      return ready;
    }
    function quota(records, candidate) {
      const others = records.filter(record => record.id !== candidate.id);
      if (others.length + 1 > maxItems) throw Error('待处理图片最多 10 张，请先重试或按课堂清理；旧图片未删除。');
      if (others.reduce((sum, record) => sum + record.blob.size, candidate.blob.size) > maxBytes) {
        throw Error('待处理图片容量上限为 100 MiB，请先重试或按课堂清理；旧图片未删除。');
      }
    }
    function mergeUploads(records) {
      const local = [...pageUploadIds].map(id => uploads.get(id)).filter(Boolean);
      uploads.clear(); durableIds.clear();
      for (const record of records) { uploads.set(record.id, record); durableIds.add(record.id); }
      for (const record of local) uploads.set(record.id, record);
    }
    async function refreshUploads() {
      if (!database) return true;
      try {
        const records = await transact(['uploads'], 'readonly', (tx, done) => {
          tx.objectStore('uploads').getAll().onsuccess = event => done(event.target.result);
        });
        mergeUploads(records);
        return true;
      } catch (_) { fallback(); return false; }
    }
    function canonical(value) {
      if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
      if (value && typeof value === 'object') return '{' + Object.keys(value).sort()
        .map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
      return JSON.stringify(value);
    }
    function conflict(message) {
      const error = Error(message); error.cacheConflict = true; return error;
    }
    function sameRequest(old, value, expected) {
      if (!old) return;
      if (value === null) {
        if (old.request_id !== expected) throw conflict('其他页面已有新的待核对请求，未清除；请先核对最新请求。');
        return;
      }
      if (old.request_id !== value.request_id) throw conflict('此课堂已有未解决的待核对请求，未覆盖；请先核对原请求。');
      for (const key of ['kind', 'session_id', 'body', 'params', 'question_id']) {
        if (old[key] !== undefined && canonical(old[key]) !== canonical(value[key])) {
          throw conflict('待核对请求的原问题、原材料与身份不可修改；请保留原请求。');
        }
      }
    }
    async function latestRequest(sessionId) {
      if (database) {
        try {
          const stored = await transact(['drafts'], 'readonly', (tx, done) => {
            tx.objectStore('drafts').get('request:' + sessionId).onsuccess = event => done(event.target.result?.value || null);
          });
          if (stored) durableRequests.set(sessionId, stored.request_id); else durableRequests.delete(sessionId);
          const local = pageRequests.get(sessionId);
          if (local && stored && local.request_id !== stored.request_id) {
            throw conflict('其他页面已有不同的待核对请求；当前页原请求仍保留，请先核对。');
          }
          const current = local || stored;
          if (current) requests.set(sessionId, current); else requests.delete(sessionId);
        } catch (error) {
          if (error.cacheConflict) throw error;
          fallback();
        }
      }
      return requests.has(sessionId) ? JSON.parse(JSON.stringify(requests.get(sessionId))) : null;
    }
    async function sameIdentity(old, record) {
      if (!old) return;
      if (['session_id', 'start', 'title', 'created_at', 'mime_type'].some(key => old[key] !== record[key])) {
        throw Error('待处理图片身份不可修改，必须保留原课堂、原时间和原图。');
      }
      if (old.blob === record.blob) return;
      if (old.blob.size !== record.blob.size) throw Error('待处理图片必须保留原图。');
      const [left, right] = await Promise.all([old.blob.arrayBuffer(), record.blob.arrayBuffer()]);
      const a = new Uint8Array(left), b = new Uint8Array(right);
      if (a.some((value, index) => value !== b[index])) throw Error('待处理图片必须保留原图。');
    }
    async function putUpload(record) {
      return enqueue(async () => {
        if (!record || typeof record.id !== 'string' || !record.id || !record.session_id || !record.blob ||
            typeof record.blob.arrayBuffer !== 'function' || !Number.isFinite(record.start) || record.start < 0) {
          throw Error('待处理图片缺少原课堂、时间、请求标识或图片数据。');
        }
        await refreshUploads();
        const value = { ...record, size: record.blob.size };
        if (root.crypto?.subtle) {
          const digest = await root.crypto.subtle.digest('SHA-256', await value.blob.arrayBuffer());
          value.content_hash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
        }
        await sameIdentity(uploads.get(value.id), value);
        quota([...uploads.values()], value);
        if (persistent) {
          try {
            await transact(['uploads'], 'readwrite', (tx, done, fail) => {
              const store = tx.objectStore('uploads');
              store.getAll().onsuccess = event => {
                try {
                  const all = event.target.result;
                  const combined = new Map(all.map(record => [record.id, record]));
                  for (const id of pageUploadIds) if (uploads.has(id)) combined.set(id, uploads.get(id));
                  quota([...combined.values()], value);
                  const old = all.find(item => item.id === value.id);
                  if (old && (old.session_id !== value.session_id || old.start !== value.start || old.title !== value.title ||
                    old.created_at !== value.created_at || old.blob.size !== value.blob.size ||
                    (old.content_hash && old.content_hash !== value.content_hash))) throw Error('待处理图片身份不可修改。');
                  // Existing durable blobs are immutable; only delivery state changes.
                  store.put(old ? { ...value, blob: old.blob } : value); done(all);
                } catch (error) { fail(error); }
              };
            });
            durableIds.add(value.id);
            pageUploadIds.delete(value.id);
          } catch (error) {
            if (/最多 10|容量上限|身份|原图/.test(error.message)) throw error;
            fallback();
            pageUploadIds.add(value.id);
          }
        } else pageUploadIds.add(value.id);
        uploads.set(value.id, value); return status();
      });
    }
    async function remove(ids) {
      // A deletion cannot be claimed successful while its durable copy survives.
      await refreshUploads();
      const storedIds = ids.filter(id => durableIds.has(id));
      if (database && storedIds.length) {
        await transact(['uploads'], 'readwrite', tx => {
          for (const id of storedIds) tx.objectStore('uploads').delete(id);
        }).catch(() => { throw Error('浏览器未能清除本机暂存，请重试；刷新后该项可能仍在。'); });
      }
      for (const id of ids) { uploads.delete(id); durableIds.delete(id); pageUploadIds.delete(id); }
      return status();
    }
    async function clearUploads(sessionId) {
      if (!sessionId) throw Error('请指定要清理的课堂。');
      if (!await refreshUploads()) throw Error('浏览器暂时无法核对本机暂存，未清理；原图片仍保留，请重试。');
      let remaining;
      // After degradation, read-only reconciliation may prove that this class
      // has only page-local items. Those can be cleared without a failing write.
      if (database && (persistent || [...durableIds].some(id => uploads.get(id)?.session_id === sessionId))) {
        try {
          remaining = await transact(['uploads'], 'readwrite', (tx, done, fail) => {
            const store = tx.objectStore('uploads');
            store.getAll().onsuccess = event => {
              try {
                const all = event.target.result;
                for (const record of all) if (record.session_id === sessionId) store.delete(record.id);
                done(all.filter(record => record.session_id !== sessionId));
              } catch (error) { fail(error); }
            };
          });
        } catch (_) { throw Error('浏览器未能清除本机暂存，请重试；原图片仍保留。'); }
      }
      for (const [id, record] of uploads) if (record.session_id === sessionId) {
        uploads.delete(id); durableIds.delete(id); pageUploadIds.delete(id);
      }
      if (remaining) mergeUploads(remaining);
      return status();
    }
    function setRequest(sessionId, record, expectedRequestId) {
      const expected = expectedRequestId === undefined ? requests.get(sessionId)?.request_id || null : expectedRequestId;
      return enqueue(async () => {
        if (!sessionId) throw Error('待核对请求缺少课堂。');
        const value = record === null ? null : JSON.parse(JSON.stringify(record));
        if (value && (typeof value.request_id !== 'string' || !value.request_id)) throw Error('待核对请求缺少稳定请求标识。');
        for (const identity of [value?.session_id, value?.body?.session_id, value?.params?.session_id]) {
          if (identity !== undefined && identity !== sessionId) throw conflict('待核对请求必须保留原课堂身份。');
        }
        const old = await latestRequest(sessionId);
        sameRequest(old, value, expected);
        if (database && (persistent || value === null && durableRequests.has(sessionId))) {
          try {
            await transact(['drafts'], 'readwrite', (tx, done, fail) => {
              const store = tx.objectStore('drafts');
              store.get('request:' + sessionId).onsuccess = event => {
                try {
                  sameRequest(event.target.result?.value, value, expected);
                  if (value === null) store.delete('request:' + sessionId);
                  else store.put({ id: 'request:' + sessionId, kind: 'request', session_id: sessionId, value });
                } catch (error) { fail(error); }
              };
            });
            pageRequests.delete(sessionId);
            if (value) durableRequests.set(sessionId, value.request_id); else durableRequests.delete(sessionId);
          } catch (error) {
            if (error.cacheConflict) throw error;
            fallback();
            if (value === null) throw Error('浏览器未能清除待核对请求，原恢复标识仍保留；请重试。');
            pageRequests.set(sessionId, value);
          }
        } else if (value) pageRequests.set(sessionId, value);
        else pageRequests.delete(sessionId);
        if (value === null) requests.delete(sessionId); else requests.set(sessionId, value);
        return status();
      });
    }
    return {
      init, status,
      listUploads: sessionId => enqueue(async () => {
        await refreshUploads();
        return [...uploads.values()].filter(record => !sessionId || record.session_id === sessionId)
          .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)) || a.id.localeCompare(b.id)).map(record => ({ ...record }));
      }),
      putUpload,
      deleteUpload: id => enqueue(() => remove([id])),
      clearUploads: sessionId => enqueue(() => clearUploads(sessionId)),
      getDraft: sessionId => enqueue(() => drafts.get(sessionId) || ''),
      getRequest: sessionId => enqueue(() => latestRequest(sessionId)),
      setRequest,
      setDraft: (sessionId, text) => enqueue(async () => {
        if (!sessionId || typeof text !== 'string') throw Error('问题草稿缺少课堂。');
        if (persistent) {
          try { await transact(['drafts'], 'readwrite', tx => tx.objectStore('drafts').put({ id: sessionId, text })); }
          catch (_) { fallback(); }
        }
        drafts.set(sessionId, text); return status();
      }),
    };
  }
  if (typeof module === 'object' && module.exports) module.exports = { createStudyCache };
  else root.studyCache = createStudyCache();
})(typeof window === 'undefined' ? globalThis : window);
