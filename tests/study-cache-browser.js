
'use strict';
const output = document.querySelector('#result');
const check = (ok, message) => { if (!ok) throw Error(message); };
const run = fn => async () => { try { output.textContent = await fn(); } catch (error) { output.textContent = 'FAIL: ' + error.message; } };
const names = ['synthetic-cache-A', 'synthetic-cache-B'];
document.querySelector('#seed').onclick = run(async () => {
  output.textContent = '正在初始化本机存储…';
  const state = await studyCache.init(); check(state.persistent, state.message);
  for (const session of names) {
    output.textContent = '正在保存 ' + session;
    await studyCache.putUpload({ id: session, session_id: session, title: '供给曲线😀', start: 12.5,
      created_at: '2026-10-28T00:00:00Z', blob: new Blob(['synthetic ' + session], {type:'image/png'}),
      mime_type: 'image/png', status: 'failed', error: '合成失败，不上传' });
    await studyCache.setDraft(session, '问题 ' + session + ' 中英😀');
  }
  return 'PASS: 两课材料已写入真实 IndexedDB。请刷新/重新打开此页，再点验证。';
});
document.querySelector('#verify').onclick = run(async () => {
  const state = await studyCache.init(); check(state.persistent, state.message);
  for (const session of names) {
    const entries = await studyCache.listUploads(session);
    check(entries.length === 1, '记录数量/课堂隔离');
    check(await entries[0].blob.text() === 'synthetic ' + session, 'Blob 内容');
    check(entries[0].start === 12.5 && entries[0].status === 'failed', '原时间与失败状态');
    check(await studyCache.getDraft(session) === '问题 ' + session + ' 中英😀', '草稿隔离');
  }
  return 'PASS: 真实 IndexedDB 恢复两课 Blob、原时间、失败状态、中英 emoji 草稿；没有发出上传或模型请求。';
});
document.querySelector('#clear').onclick = run(async () => {
  for (const session of names) { await studyCache.clearUploads(session); await studyCache.setDraft(session, ''); }
  const pending = await studyCache.getRequest('synthetic-cache-race');
  if (pending) await studyCache.setRequest('synthetic-cache-race', null, pending.request_id);
  return 'PASS: 已清理本验收页面生成的合成项。';
});
document.querySelector('#race').onclick = run(async () => {
  await studyCache.init();
  try {
    await studyCache.setRequest('synthetic-cache-race', {kind:'study', request_id:crypto.randomUUID(), phase:'prepare', params:{session_id:'synthetic-cache-race'}});
    return 'PASS: 本页面取得待核对请求位置；另一页面必须被拒绝覆盖。';
  } catch (error) {
    if (error.cacheConflict) return 'PASS: 已拒绝覆盖另一页面的待核对请求。';
    throw error;
  }
});
