"""Portable classroom packages: synthetic files, no real ASR/AI or accounts."""
import copy
import importlib
import io
import hashlib
import json
import zipfile

import pytest
from fastapi import HTTPException

from test_app import room_factory, synthetic_wav, local_client
from test_study_workflow import png, seed, prepare


def store(room):
    return importlib.import_module('portable_package').PortableStore(room)


def export(room, *, audio=False, allow_missing=False, request_id='export-request-001'):
    manager = store(room)
    preview = manager.preview_export(room.current, include_audio=audio)
    result = manager.build_export(preview['preview_id'], request_id=request_id, allow_missing=allow_missing)
    return manager, result, manager.download_path(result['id'])


def add_audio(room):
    room.accept_audio(room.current, 1, 0, synthetic_wav())
    room.get()['audio'][0]['status'] = 'done'
    room.get()['segments'][0]['audio_seq'] = 1
    room.save(room.get())


def test_export_offline_bundle_defaults_to_no_audio_and_preserves_evidence(room_factory):
    room = room_factory()
    seed(room, 1)
    add_audio(room)
    question = prepare(room)
    before = copy.deepcopy(room.get())
    manager, result, path = export(room)
    assert path.is_file() and result['status'] == 'ready'
    with zipfile.ZipFile(path) as bundle:
        manifest = json.loads(bundle.read('manifest.json'))
        record = json.loads(bundle.read('session.json'))
        assert manifest['schema_version'] == 1 and not manifest['include_audio']
        assert record['questions'][0]['evidence'] == question['evidence']
        assert any(item['availability'] == 'not_included' for item in manifest['media'] if item['kind'] == 'audio')
        assert not any(name.endswith('.wav') for name in bundle.namelist())
        assert {'index.html', 'notes.md', 'reader/portable-reader.js', 'reader/portable-reader.css'} <= set(bundle.namelist())
        assert '原文' in bundle.read('index.html').decode('utf-8')
    assert room.get() == before
    assert manager.lookup_export('export-request-001', room.current)['id'] == result['id']


def test_roundtrip_new_copy_keeps_ids_versions_audio_and_does_not_activate_tasks(room_factory):
    room = room_factory()
    seed(room, 1)
    add_audio(room)
    q = prepare(room)
    original = copy.deepcopy(room.get())
    manager, _, path = export(room, audio=True)
    inspected = manager.preview_import(path, request_id='import-preview-001')
    restored = manager.restore(inspected['preview_id'], request_id='import-commit-001')
    duplicate = manager.restore(inspected['preview_id'], request_id='import-commit-001')
    assert restored['session_id'] == duplicate['session_id'] != original['id']
    imported = room.get(restored['session_id'])
    assert imported['segments'] == original['segments']
    assert imported['questions'][0]['evidence'] == q['evidence']
    assert imported['import_origin']['source_session_id'] == original['id']
    assert imported['source_classroom_map'][original['id']] == imported['id']
    assert manager.lookup_import('import-commit-001')['session_id'] == imported['id']
    assert room.get(original['id']) == original
    assert room.bridge.calls == []
    assert all(x['status'] not in ('pending', 'running', 'paused') for x in imported['audio'])
    old_path = room.data / original['id'] / original['slides'][0]['path']
    new_path = room.data / imported['id'] / imported['slides'][0]['path']
    assert old_path.read_bytes() == new_path.read_bytes()
    new_path.write_bytes(b'import-only mutation')
    assert old_path.read_bytes() != new_path.read_bytes()


def test_missing_media_requires_explicit_partial_export_and_restore(room_factory):
    room = room_factory()
    seed(room, 1)
    original = room.get()['slides'][0]
    (room.data / room.current / original['path']).unlink()
    manager = store(room)
    preview = manager.preview_export(room.current)
    assert preview['missing']
    with pytest.raises(HTTPException) as error:
        manager.build_export(preview['preview_id'], request_id='missing-export-001')
    assert error.value.status_code == 409
    result = manager.build_export(preview['preview_id'], request_id='missing-export-001', allow_missing=True)
    inspected = manager.preview_import(manager.download_path(result['id']), request_id='missing-inspect-001')
    with pytest.raises(HTTPException):
        manager.restore(inspected['preview_id'], request_id='missing-restore-001')
    copied = manager.restore(inspected['preview_id'], request_id='missing-restore-001', allow_missing=True)
    assert room.get(copied['session_id'])['slides'][0]['sha256'] == original['sha256']


def test_legacy_json_restores_text_without_searching_external_media(room_factory, tmp_path):
    room = room_factory()
    seed(room, 1)
    original = copy.deepcopy(room.get())
    source = tmp_path / 'legacy.json'
    source.write_text(json.dumps(original, ensure_ascii=False), encoding='utf-8')
    manager = store(room)
    inspected = manager.preview_import(source, request_id='legacy-preview-001')
    assert inspected['missing']
    restored = manager.restore(inspected['preview_id'], request_id='legacy-restore-001', allow_missing=True)
    copied = room.get(restored['session_id'])
    assert copied['segments'] == original['segments']
    assert not (room.data / copied['id'] / original['slides'][0]['path']).exists()


@pytest.mark.parametrize('name', ['../escape.txt', '/absolute.txt', 'C:/escape.txt', 'a\\..\\escape', 'CON', 'assets/foo:bar', 'assets/../escape'])
def test_import_rejects_unsafe_zip_paths(room_factory, tmp_path, name):
    source = tmp_path / 'evil.zip'
    with zipfile.ZipFile(source, 'w') as bundle:
        bundle.writestr(name, b'unsafe')
    with pytest.raises(HTTPException):
        store(room_factory()).preview_import(source, request_id='unsafe-preview-001')
    assert not (tmp_path / 'escape.txt').exists()


def test_import_rejects_case_collisions_and_symlinks(room_factory, tmp_path):
    for kind in ('case', 'link'):
        source = tmp_path / f'{kind}.zip'
        with zipfile.ZipFile(source, 'w') as bundle:
            if kind == 'case':
                bundle.writestr('asset.txt', b'1')
                bundle.writestr('ASSET.TXT', b'2')
            else:
                entry = zipfile.ZipInfo('assets/link')
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
                bundle.writestr(entry, b'../../secret')
        with pytest.raises(HTTPException):
            store(room_factory()).preview_import(source, request_id=f'unsafe-{kind}-preview')


def test_import_rejects_tampering_and_future_format(room_factory, tmp_path):
    room = room_factory()
    seed(room, 1)
    manager, _, path = export(room)
    for kind in ('tamper', 'future'):
        source = tmp_path / f'{kind}.zip'
        with zipfile.ZipFile(path) as original, zipfile.ZipFile(source, 'w') as output:
            for name in original.namelist():
                value = original.read(name)
                if name == 'session.json' and kind == 'tamper':
                    value = value.replace(b'S0001', b'S9999')
                if name == 'manifest.json' and kind == 'future':
                    manifest = json.loads(value)
                    manifest['schema_version'] = 999
                    value = json.dumps(manifest).encode()
                output.writestr(name, value)
        with pytest.raises(HTTPException):
            manager.preview_import(source, request_id=f'bad-{kind}-preview')


def test_export_data_script_escape_and_adopted_user_versions(room_factory):
    room = room_factory()
    seed(room, 1)
    attack = '</script><script>window.PWNED=1</script> 中文😀 $x^2$'
    room.get()['review'] = {'schema_version': 1, 'marks': [], 'runs': [], 'receipts': {},
        'selected_note_id': 'N1', 'cards': [{'id': 'C1', 'ai_content': {'title': 'AI title', 'front': 'AI front', 'back': 'AI back'},
          'evidence': {}, 'user_draft': {'revision': 1, 'content': {'title': 'User title', 'front': attack, 'back': 'User back'}, 'personal_note': '个人备注'}}],
        'notes': [{'id': 'N1', 'ai_content': {'sections': [{'title': 'AI old note', 'body': 'Old body'}]},
          'evidence': {}, 'user_draft': {'revision': 1, 'content': {'sections': [{'title': 'Chosen note', 'body': attack}]}, 'personal_note': 'Note'}}]}
    _, _, path = export(room)
    with zipfile.ZipFile(path) as bundle:
        html = bundle.read('index.html').decode()
        md = bundle.read('notes.md').decode()
        assert '</script><script>window.PWNED' not in html
        assert '\\u003c/script\\u003e' in html
        assert 'User title' in md and 'Chosen note' in md and '个人备注' in md


def test_actual_http_export_import_restart_and_lost_response_lookup(tmp_path):
    from test_http_regression import serve
    data = tmp_path / 'http-portable'
    with serve(data) as (client, room):
        original = seed(room, 1)
        frozen = prepare(room)['evidence']
        sid = original['id']
        preview = client.post('/api/packages/preview', json={'session_id': sid}).json()
        response = client.post('/api/packages/export', json={'preview_id': preview['preview_id'], 'request_id': 'http-export-001'})
        assert response.status_code == 200, response.text
        result = response.json()
        downloaded = client.get(result['download_url'])
        assert downloaded.status_code == 200 and 'attachment;' in downloaded.headers['content-disposition']
        assert client.get('/api/packages/exports/http-export-001', params={'session_id': sid}).json()['id'] == result['id']
        inspected = client.post('/api/packages/import/preview?request_id=http-preview-001', content=downloaded.content)
        assert inspected.status_code == 200, inspected.text
        restored = client.post('/api/packages/import', json={'preview_id': inspected.json()['preview_id'], 'request_id': 'http-import-001'})
        assert restored.status_code == 200, restored.text
        imported_id = restored.json()['session_id']
        assert imported_id != sid
        assert room.get(imported_id)['questions'][0]['evidence'] == frozen
        assert not room.bridge.calls
    with serve(data) as (client, room):
        found = client.get('/api/packages/imports/http-import-001').json()
        assert found['found'] and found['session_id'] == imported_id
        duplicate = client.post('/api/packages/import', json={'preview_id': inspected.json()['preview_id'], 'request_id': 'http-import-001'}).json()
        assert duplicate['session_id'] == imported_id and not duplicate['created']
        assert len(room.sessions) == 2
        assert client.get('/api/state').status_code == 200
        assert not room.bridge.calls and not room.engine.calls


@pytest.mark.parametrize('field,value', [
    ('ai_usage', []), ('settings', []), ('questions', [{'id': 'Q1', 'question': 42}]),
    ('slides', [{'id': 'P0001'}]), ('audio', [{'seq': 1, 'path': '../record.wav'}]),
    ('source_classroom_map', {'../outside': 'abc'}), ('review', {'schema_version': 999}),
    ('created_at', 'not a timestamp'), ('api_key', 'SYNTHETIC-NOT-A-KEY'),
    ('segments', [{'id': 'S0001', 'text': 'x', 'start': 3, 'end': 1}]),
])
def test_malformed_metadata_rejected_without_partial_classroom(room_factory, tmp_path, field, value):
    room = room_factory()
    seed(room, 0)
    original = copy.deepcopy(room.get())
    source = copy.deepcopy(original)
    source[field] = value
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    manager = store(room)
    with pytest.raises(HTTPException):
        manager.preview_import(path, request_id='bad-metadata-001')
    assert len(room.sessions) == 1 and room.get() == original
    assert list((manager.root / 'previews').iterdir()) == []


def test_same_path_conflicting_image_hash_and_frozen_corruption_rejected(room_factory, tmp_path):
    room = room_factory()
    seed(room, 1)
    q = prepare(room)
    source = copy.deepcopy(room.get())
    source['questions'][0]['evidence']['slides'][0]['sha256'] = 'a' * 64
    path = tmp_path / 'conflict.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    with pytest.raises(HTTPException):
        store(room).preview_import(path, request_id='path-conflict-001')
    source = copy.deepcopy(room.get())
    source['questions'][0]['evidence']['segments'][0]['start'] = 'bad'
    path.write_text(json.dumps(source), encoding='utf-8')
    with pytest.raises(HTTPException):
        store(room).preview_import(path, request_id='bad-evidence-001')
    assert room.get()['questions'][0]['evidence'] == q['evidence']


def test_media_changed_after_export_preview_rejects_and_keeps_source(room_factory):
    room = room_factory()
    seed(room, 1)
    manager = store(room)
    preview = manager.preview_export(room.current)
    image = room.data / room.current / room.get()['slides'][0]['path']
    changed = png(113)
    image.write_bytes(changed)
    with pytest.raises(HTTPException):
        manager.build_export(preview['preview_id'], request_id='change-export-001')
    assert not manager.lookup_export('change-export-001', room.current)['found']
    assert not list((manager.root / 'exports').glob('*.zip'))
    assert image.read_bytes() == changed


def test_restore_write_failure_has_no_half_classroom_and_retry_is_single(room_factory, monkeypatch):
    import portable_package
    room = room_factory()
    seed(room, 1)
    manager, _, path = export(room)
    preview = manager.preview_import(path, request_id='failure-preview-001')
    original_copy = portable_package.shutil.copyfile
    def fail_write(*args, **kwargs):
        raise OSError('synthetic disk full')
    monkeypatch.setattr(portable_package.shutil, 'copyfile', fail_write)
    with pytest.raises(HTTPException) as error:
        manager.restore(preview['preview_id'], request_id='failure-restore-001')
    assert error.value.status_code == 507 and len(room.sessions) == 1
    assert not list((manager.root / 'imports').glob('*/session.json'))
    assert not manager.lookup_import('failure-restore-001')['found']
    monkeypatch.setattr(portable_package.shutil, 'copyfile', original_copy)
    restored = manager.restore(preview['preview_id'], request_id='failure-restore-001')
    assert manager.restore(preview['preview_id'], request_id='failure-restore-001')['session_id'] == restored['session_id']
    assert len(room.sessions) == 2


def test_completed_review_and_edited_string_roundtrip_preserve_frozen_original(room_factory):
    from test_review_workflow import ReviewBridge, seed as review_seed, prepare as review_prepare, send as review_send
    from test_app import launch, wait_for
    bridge = ReviewBridge()
    room = room_factory(bridge=bridge)
    s = review_seed(room)
    run = review_prepare(room)
    review_send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    card = s['review']['cards'][0]
    room.edit_review_artifact('cards', card['id'], {'session_id': s['id'], 'request_id': 'portable-edit-001',
        'revision': 0, 'content': 'My adopted explanation 中文😀', 'personal_note': '个人笔记'})
    original = copy.deepcopy(s)
    manager, _, path = export(room)
    preview = manager.preview_import(path, request_id='review-preview-001')
    imported = room.get(manager.restore(preview['preview_id'], request_id='review-import-001')['session_id'])
    for collection in ('cards', 'notes'):
        assert len(imported['review'][collection]) == len(original['review'][collection])
        for restored, old in zip(imported['review'][collection], original['review'][collection]):
            # The export adds a read-only status projection; every original
            # stored field, including evidence and AI/user content, is exact.
            assert {key: restored[key] for key in old} == old
            assert set(restored) - set(old) == {'source_status'}
    assert imported['review']['receipts'] == {}
    assert room.review_public(imported)['runs'][0]['status'] == 'done'
    with zipfile.ZipFile(path) as bundle:
        assert 'My adopted explanation 中文😀' in bundle.read('notes.md').decode()
    assert len(bridge.calls) == 1


def test_import_disarms_inflight_review_but_preserves_attempt_and_evidence(room_factory):
    from test_review_workflow import seed as review_seed, prepare as review_prepare
    room = room_factory()
    s = review_seed(room)
    run = review_prepare(room)
    raw = room.review_run(s, run['id'])
    raw.update(status='running', confirmed_at='synthetic', confirmed_source_token='synthetic')
    batch = raw['batches'][0]
    attempt = {'id': 'A1', 'status': 'running', 'dispatch_started_at': '2026-01-01',
               'raw_ai_output': 'partial raw output', 'destination': copy.deepcopy(raw['destination'])}
    batch.update(status='running', job_id='synthetic-job', attempts=[copy.deepcopy(attempt)])
    frozen = copy.deepcopy(raw['evidence'])
    manager, _, path = export(room)
    preview = manager.preview_import(path, request_id='inflight-preview-001')
    imported = room.get(manager.restore(preview['preview_id'], request_id='inflight-import-001')['session_id'])
    restored_run = imported['review']['runs'][0]
    assert restored_run['status'] == 'paused' and 'confirmed_at' not in restored_run
    assert restored_run['batches'][0]['status'] == 'unknown' and 'job_id' not in restored_run['batches'][0]
    assert restored_run['batches'][0]['attempts'] == [attempt]
    assert restored_run['evidence'] == frozen
    assert room.review_public(imported)['runs'][0]['repeat_billing_required']
    assert room.ai_queue.empty() and not room.bridge.calls


def test_limits_enforced_on_real_entry_count_and_streamed_upload(local_client, monkeypatch, tmp_path):
    import portable_package, portable_routes
    client, room, _, _ = local_client
    monkeypatch.setattr(portable_routes, 'MAX_PACKAGE_BYTES', 16)
    response = client.post('/api/packages/import/preview?request_id=upload-limits-001', content=b'x' * 17)
    assert response.status_code == 413
    response = client.post('/api/packages/import/preview?request_id=upload-limits-002', content=iter([b'x' * 9, b'x' * 8]))
    assert response.status_code == 413
    assert not list((room.data / '.portable' / 'uploads').iterdir())
    monkeypatch.setattr(portable_package, 'MAX_FILES', 2)
    path = tmp_path / 'many.zip'
    with zipfile.ZipFile(path, 'w') as bundle:
        for i in range(3):
            bundle.writestr(f'{i}.txt', b'x')
    with pytest.raises(HTTPException) as error:
        store(room).preview_import(path, request_id='file-limits-001')
    assert error.value.status_code == 413


def test_legacy_absent_versions_are_not_written_into_frozen_evidence(room_factory, tmp_path):
    room = room_factory()
    seed(room, 1)
    prepare(room)
    source = copy.deepcopy(room.get())
    for slide in [*source['slides'], *source['questions'][0]['evidence']['slides']]:
        slide.pop('version', None)
    source['questions'][0]['evidence']['segments'][0].pop('version', None)
    frozen = copy.deepcopy(source['questions'][0]['evidence'])
    path = tmp_path / 'old.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    manager = store(room)
    preview = manager.preview_import(path, request_id='old-preview-001')
    sid = manager.restore(preview['preview_id'], request_id='old-restore-001', allow_missing=True)['session_id']
    assert room.get(sid)['questions'][0]['evidence'] == frozen


def test_historical_images_roundtrip_preserves_exact_versions(room_factory):
    room = room_factory()
    seed(room, 1)
    prepared = prepare(room)
    original = copy.deepcopy(prepared['evidence']['slides'][0])
    room.rotate_study_slide('P0001', {'session_id': room.current, 'degrees': 90, 'request_id': 'rotate-export-001'})
    manager, _, path = export(room)
    preview = manager.preview_import(path, request_id='historical-preview-001')
    sid = manager.restore(preview['preview_id'], request_id='historical-import-001')['session_id']
    imported = room.get(sid)
    frozen = imported['questions'][0]['evidence']['slides'][0]
    assert frozen == original
    assert (room.data / sid / frozen['path']).is_file()
    assert (room.data / sid / imported['slides'][0]['path']).is_file()
    assert imported['slides'][0]['version'] != frozen.get('version', 1)


@pytest.mark.parametrize('damage', ['missing', 'length', 'hash', 'invalid-image'])
def test_archive_media_damage_requires_explicit_partial_restore(room_factory, tmp_path, damage):
    room = room_factory()
    seed(room, 1)
    manager, _, original = export(room)
    path = tmp_path / f'{damage}.zip'
    with zipfile.ZipFile(original) as source:
        contents = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(contents['manifest.json'])
    image = next(item for item in manifest['media'] if item['kind'] == 'image')
    name = image['archive_path']
    if damage == 'missing':
        del contents[name]
    elif damage == 'length':
        contents[name] = b'short'
    elif damage == 'hash':
        contents[name] = b'x' * len(contents[name])
    else:
        contents[name] = b'not an image'
        image.update(sha256=hashlib.sha256(contents[name]).hexdigest(), size=len(contents[name]))
        # Keep source metadata and every declared hash internally consistent;
        # invalid image bytes are a media problem, not malformed metadata.
        session = json.loads(contents['session.json'])
        session['slides'][0]['sha256'] = image['sha256']
        for receipt in session.get('request_receipts', {}).values():
            if receipt.get('slide'):
                receipt['slide']['sha256'] = image['sha256']
        contents['session.json'] = json.dumps(session).encode()
        for item in manifest['files']:
            if item['path'] in (name, 'session.json'):
                body = contents[item['path']]
                item.update(sha256=hashlib.sha256(body).hexdigest(), size=len(body))
    contents['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(path, 'w') as target:
        for name, body in contents.items():
            target.writestr(name, body)
    preview = manager.preview_import(path, request_id=f'damage-{damage}-preview')
    assert len(preview['missing']) == 1
    with pytest.raises(HTTPException) as error:
        manager.restore(preview['preview_id'], request_id=f'damage-{damage}-restore')
    assert error.value.status_code == 409
    sid = manager.restore(preview['preview_id'], request_id=f'damage-{damage}-restore', allow_missing=True)['session_id']
    assert room.get(sid)['segments'] == room.get()['segments']
    assert not (room.data / sid / room.get(sid)['slides'][0]['path']).exists()
    assert (room.data / room.current / room.get()['slides'][0]['path']).is_file()


def test_package_and_metadata_limits_reject_before_restore(room_factory, tmp_path, monkeypatch):
    import portable_package
    room = room_factory()
    seed(room, 0)
    manager, _, path = export(room)
    for limit in ('MAX_PACKAGE_BYTES', 'MAX_MANIFEST_BYTES', 'MAX_SESSION_BYTES', 'MAX_DIRECTORY_BYTES'):
        original = getattr(portable_package, limit)
        monkeypatch.setattr(portable_package, limit, 8)
        with pytest.raises(HTTPException) as error:
            manager.preview_import(path, request_id=f'limit-{limit}-001')
        assert error.value.status_code == 413
        monkeypatch.setattr(portable_package, limit, original)
    assert len(room.sessions) == 1


@pytest.mark.parametrize('field,value', [('original_path', []), ('sha256', {}), ('archive_path', []), ('id', 'P9999')])
def test_malformed_media_manifest_is_rejected(room_factory, tmp_path, field, value):
    room = room_factory()
    seed(room, 1)
    manager, _, original = export(room)
    path = tmp_path / 'malformed.zip'
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(path, 'w') as target:
        for name in source.namelist():
            body = source.read(name)
            if name == 'manifest.json':
                manifest = json.loads(body)
                manifest['media'][0][field] = value
                body = json.dumps(manifest).encode()
            target.writestr(name, body)
    with pytest.raises(HTTPException):
        manager.preview_import(path, request_id='manifest-shape-001')
    assert len(room.sessions) == 1


@pytest.mark.parametrize('mime', [None, 'text/html', 'image/jpeg'])
def test_import_rejects_missing_or_wrong_image_mime(room_factory, mime):
    from portable_package import validate_session
    room = room_factory()
    seed(room, 1)
    source = copy.deepcopy(room.get())
    if mime is None:
        del source['slides'][0]['mime_type']
    else:
        source['slides'][0]['mime_type'] = mime
    with pytest.raises(HTTPException) as error:
        validate_session(source)
    assert error.value.status_code == 422


def test_image_bytes_must_match_extension(tmp_path):
    from portable_package import validate_image
    path = tmp_path / 'png-disguised-as-jpeg.jpg'
    path.write_bytes(png())
    with pytest.raises(HTTPException):
        validate_image(path)


def test_all_exports_project_review_source_changes_progress_without_mutation(room_factory):
    from portable_package import portable_session_projection, portable_markdown
    from test_review_workflow import ReviewBridge, seed as review_seed, prepare as review_prepare, send as review_send
    from test_app import launch, wait_for
    room = room_factory(bridge=ReviewBridge())
    s = review_seed(room)
    room.set_review_mark({'session_id': s['id'], 'request_id': 'projection-mark-001',
        'target_kind': 'segment', 'target_id': 'S0001', 'kind': 'important', 'note': 'keep source'})
    run = review_prepare(room)
    review_send(room, run)
    launch(room, '_ai_loop')
    wait_for(lambda: room.review_run(s, run['id'])['status'] == 'done')
    room.edit_segment(s['id'], 'S0001', 'Corrected teaching text.')
    room.append_text(s['id'], 'New teaching after generation.', 24, 36, 'asr')
    raw = room.review_run(s, run['id'])
    failed = copy.deepcopy(raw['batches'][0])
    failed.update(id='failed-batch-001', status='error', error='Synthetic failed batch')
    raw['batches'].append(failed)
    raw['coverage']['batch_count'] = 2
    raw.update(status='partial', error='Synthetic partial failure')
    before = copy.deepcopy(s)
    projected = portable_session_projection(s, room.data)
    for collection in ('marks', 'cards', 'notes', 'runs'):
        assert projected['review'][collection][0]['source_status']['changed']
        assert projected['review'][collection][0]['evidence'] == before['review'][collection][0]['evidence']
    assert projected['review']['runs'][0]['source_status']['new_material']
    assert projected['review']['notes'][0]['source_status']['new_material']
    assert projected['review']['runs'][0]['coverage']['completed_batches'] == 1
    markdown = portable_markdown(s, room.data)
    assert '依据已变化' in markdown and '新增内容尚未纳入本版本' in markdown
    assert '已完成 1 / 2 批' in markdown and 'partial' in markdown
    assert 'Synthetic partial failure' in markdown and 'Synthetic failed batch' in markdown
    manager, _, path = export(room)
    with zipfile.ZipFile(path) as bundle:
        record = json.loads(bundle.read('session.json'))
        assert record['review']['runs'][0]['source_status']['new_material']
        assert 'Synthetic failed batch' in bundle.read('notes.md').decode()
    assert s == before


@pytest.mark.parametrize('collection,damage', [('cards', 'draft'), ('notes', 'draft'), ('cards', 'content'), ('notes', 'content')])
def test_import_rejects_artifact_runtime_contract_gaps(room_factory, collection, damage):
    from portable_package import validate_session
    from test_review_workflow import seed as review_seed, prepare as review_prepare
    room = room_factory()
    s = review_seed(room)
    run = review_prepare(room)
    source = copy.deepcopy(s)
    content = {'title': 'Title', 'front': 'Front', 'back': 'Back'} if collection == 'cards' else {
        'sections': [{'section': 'structure', 'title': 'Title', 'body': 'Body'}]}
    artifact = {'id': 'C1' if collection == 'cards' else 'N1', 'evidence': copy.deepcopy(run['evidence']),
        'ai_content': content, 'user_draft': {'revision': 0, 'content': 'Saved text', 'personal_note': ''}}
    if damage == 'draft':
        del artifact['user_draft']
    else:
        artifact['ai_content'] = 'Wrong structured content type'
    source['review'][collection] = [artifact]
    with pytest.raises(HTTPException) as error:
        validate_session(source)
    assert error.value.status_code == 422
