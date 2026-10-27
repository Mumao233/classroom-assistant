"""Synthetic v1.2.1 immutable versions, removal and source projections."""
import copy
import importlib
import io
import os

import pytest
from PIL import Image

import slide_context
from slide_context import SlideContextError, resolve_slide_path, save_slide, snapshot_context, update_slide_interval
from test_slide_context import image_bytes, session


def history():
    return importlib.import_module('slide_history')


def asymmetric_image():
    image = Image.new('RGBA', (3, 2))
    image.putdata([(255, 0, 0, 100), (0, 255, 0, 110), (0, 0, 255, 120),
                   (12, 34, 56, 130), (34, 56, 78, 140), (56, 78, 90, 150)])
    output = io.BytesIO()
    image.save(output, 'PNG')
    return output.getvalue()


@pytest.mark.parametrize('direction,transpose', [('left', Image.Transpose.ROTATE_90),
                                               ('right', Image.Transpose.ROTATE_270),
                                               (180, Image.Transpose.ROTATE_180),
                                               (270, Image.Transpose.ROTATE_90)])
def test_rotation_keeps_original_bytes_and_exact_oriented_pixels(tmp_path, session, direction, transpose):
    original = save_slide(tmp_path, session, asymmetric_image(), start=2, title='Synthetic chart')
    original_path = resolve_slide_path(tmp_path, session['id'], original)
    original_bytes = original_path.read_bytes()
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    rotated = history().rotate_slide(tmp_path, session, original['id'], direction)
    assert rotated['id'] == original['id'] and rotated['version'] == 2
    assert rotated['path'] != original['path']
    assert original_path.read_bytes() == original_bytes
    assert frozen['slides'][0] == original
    with Image.open(original_path) as old, Image.open(resolve_slide_path(tmp_path, session['id'], rotated)) as new:
        expected = old.transpose(transpose)
        assert new.size == expected.size and new.tobytes() == expected.tobytes()
    previous = history().find_slide_version(session, original['id'], version=1, sha256=original['sha256'])
    assert previous['path'] == original['path']


def test_removed_slide_cannot_be_selected_but_history_stays_viewable(tmp_path, session):
    original = save_slide(tmp_path, session, image_bytes(), start=0)
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    history().remove_slide(session, original['id'])
    assert snapshot_context(session, mode='recent30', anchor=30)['slides'] == []
    with pytest.raises(SlideContextError):
        snapshot_context(session, mode='recent30', anchor=30, slide_ids=[original['id']])
    status = history().source_status(tmp_path, session, frozen)
    assert status['changed'] and not status['unavailable']
    assert status['slides'][0]['removed'] and status['slides'][0]['available']
    assert resolve_slide_path(tmp_path, session['id'], frozen['slides'][0]).is_file()


def test_removed_flag_is_respected_by_existing_selection(tmp_path, session):
    save_slide(tmp_path, session, image_bytes(), start=0)
    session['slides'][0]['removed'] = True
    assert snapshot_context(session, mode='recent30', anchor=30)['slides'] == []


def test_source_changes_are_separate_and_do_not_rewrite_evidence(tmp_path, session):
    original = save_slide(tmp_path, session, asymmetric_image(), start=0)
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    untouched = copy.deepcopy(frozen)
    first = history().source_status(tmp_path, session, frozen)
    assert not first['changed'] and not first['unavailable']
    history().rotate_slide(tmp_path, session, original['id'], 'right')
    update_slide_interval(session, original['id'], 2, 20)
    session['segments'][0].update(text='Changed synthetic statement.', version=1)
    status = history().source_status(tmp_path, session, frozen)
    assert {change['kind'] for change in status['changes']} >= {'rotation', 'time', 'text'}
    assert status['changed'] and status['token'] != first['token']
    assert status['slides'][0]['available'] and status['slides'][0]['rotated']
    assert frozen == untouched
    assert history().source_status(tmp_path, session, frozen) == status


def test_normal_new_slide_and_late_transcript_do_not_change_frozen_sources(tmp_path, session):
    save_slide(tmp_path, session, image_bytes(), start=0)
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    before = history().source_status(tmp_path, session, frozen)
    save_slide(tmp_path, session, image_bytes(size=(4, 8)), start=35)
    session['segments'].append({'id': 'S0002', 'start': 10, 'end': 15, 'text': 'Late synthetic text.'})
    after = history().source_status(tmp_path, session, frozen)
    assert not after['changed'] and after['token'] == before['token']


@pytest.mark.parametrize('failure', ['missing', 'corrupt'])
def test_old_asset_unavailable_never_falls_back_to_current(tmp_path, session, failure):
    original = save_slide(tmp_path, session, asymmetric_image(), start=0)
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    path = resolve_slide_path(tmp_path, session['id'], original)
    new = history().rotate_slide(tmp_path, session, original['id'], 'right')
    if failure == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'synthetic corruption')
    status = history().source_status(tmp_path, session, frozen)
    assert status['unavailable'] and not status['slides'][0]['available']
    assert status['slides'][0]['availability'] == failure
    assert resolve_slide_path(tmp_path, session['id'], new).is_file()
    selected = history().find_slide_version(session, original['id'], version=1)
    assert selected['path'] == original['path']
    with pytest.raises(SlideContextError):
        resolve_slide_path(tmp_path, session['id'], selected)


def test_legacy_version_lookup_is_read_only_and_rejects_unknown(tmp_path, session):
    original = save_slide(tmp_path, session, image_bytes(), start=0)
    session['slides'][0].pop('version', None)
    before = copy.deepcopy(session)
    assert history().find_slide_version(session, original['id'], version=1)['sha256'] == original['sha256']
    with pytest.raises(SlideContextError):
        history().find_slide_version(session, original['id'], version=2)
    with pytest.raises(SlideContextError):
        history().find_slide_version(session, original['id'], sha256='a' * 64)
    assert session == before


def test_oversize_input_message_reports_actual_bytes_and_dimensions(tmp_path, session):
    content = image_bytes(size=(12, 8))
    content += b'\0' * (slide_context.MAX_IMAGE_BYTES + 1 - len(content))
    with pytest.raises(SlideContextError) as error:
        save_slide(tmp_path, session, content, start=0)
    assert str(len(content)) in str(error.value) and '12' in str(error.value) and '8' in str(error.value)
    assert 'MiB' in str(error.value)


def test_oversize_pixel_message_reports_actual_dimensions(tmp_path, session, monkeypatch):
    monkeypatch.setattr(slide_context, 'MAX_IMAGE_PIXELS', 24)
    with pytest.raises(SlideContextError) as error:
        save_slide(tmp_path, session, image_bytes(size=(7, 4)), start=0)
    assert '7' in str(error.value) and '4' in str(error.value) and '28' in str(error.value)


def test_manual_end_change_is_visible_even_when_observed_metadata_has_latest_revision(tmp_path, session):
    save_slide(tmp_path, session, image_bytes(), start=0)
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    update_slide_interval(session, 'P0001', 0, 20)
    # Recovering an old browser range must still compare its actual end value.
    frozen['slides'][0]['interval_version'] = session['slides'][0]['interval_version']
    status = history().source_status(tmp_path, session, frozen)
    assert status['changed'] and status['slides'][0]['time_changed']


def test_failed_rotation_write_keeps_version_and_original(tmp_path, session, monkeypatch):
    original = save_slide(tmp_path, session, asymmetric_image(), start=0)
    before = copy.deepcopy(session)
    monkeypatch.setattr(slide_context.os, 'fsync', lambda *_: (_ for _ in ()).throw(OSError('synthetic failure')))
    with pytest.raises(SlideContextError):
        history().rotate_slide(tmp_path, session, original['id'], 90)
    assert session == before
    assert len(list((tmp_path / session['id'] / 'slides').iterdir())) == 1
    assert resolve_slide_path(tmp_path, session['id'], original).is_file()


def test_multiple_versions_and_noops_preserve_asset_identity(tmp_path, session):
    original = save_slide(tmp_path, session, asymmetric_image(), start=0)
    history().rotate_slide(tmp_path, session, original['id'], 90)
    current = history().rotate_slide(tmp_path, session, original['id'], 270)
    assert current['version'] == 3 and current['sha256'] == original['sha256']
    before = copy.deepcopy(session)
    assert history().rotate_slide(tmp_path, session, original['id'], 0)['version'] == 3
    assert session == before
    assert len({history().find_slide_version(session, original['id'], version=n)['path'] for n in (1, 2, 3)}) == 3
    with pytest.raises(SlideContextError):
        history().find_slide_version(session, original['id'], version=2, sha256=original['sha256'])
    history().remove_slide(session, original['id'])
    removed = copy.deepcopy(session)
    history().remove_slide(session, original['id'])
    assert session == removed
    with pytest.raises(SlideContextError):
        history().rotate_slide(tmp_path, session, original['id'], 90)


def test_rotated_snapshots_do_not_recursively_embed_all_past_versions(tmp_path, session):
    save_slide(tmp_path, session, asymmetric_image(), start=0)
    history().rotate_slide(tmp_path, session, 'P0001', 90)
    frozen = snapshot_context(session, mode='recent30', anchor=30)
    assert 'versions' not in frozen['slides'][0]
    assert frozen['slides'][0]['version'] == 2


def test_retry_upload_restores_original_time_between_existing_pages(tmp_path, session):
    first = save_slide(tmp_path, session, image_bytes(), start=0)
    later = save_slide(tmp_path, session, image_bytes(size=(4, 8)), start=20)
    before_later = copy.deepcopy(session['slides'][1])
    frozen = snapshot_context(session, mode='recent30', anchor=20)
    before_status = history().source_status(tmp_path, session, frozen)
    restored = save_slide(tmp_path, session, image_bytes(size=(8, 4)), start=10, allow_backdated=True)
    assert (restored['start'], restored['end']) == (10, 20)
    assert session['slides'][0]['end'] == 10
    assert session['slides'][1] == before_later
    assert frozen['slides'][0]['end'] == 20
    assert history().source_status(tmp_path, session, frozen) == before_status
    selected = snapshot_context(session, mode='recent30', anchor=30)['slides']
    assert [item['id'] for item in selected] == [first['id'], restored['id'], later['id']]


def test_backfill_preserves_explicit_intervals_and_equal_start_overlap(tmp_path, session):
    first = save_slide(tmp_path, session, image_bytes(), start=0)
    update_slide_interval(session, first['id'], 0, 30)
    later = save_slide(tmp_path, session, image_bytes(), start=20)
    equal = save_slide(tmp_path, session, image_bytes(), start=20, allow_backdated=True)
    assert equal['end'] is None and session['slides'][1]['end'] is None
    restored = save_slide(tmp_path, session, image_bytes(), start=10, allow_backdated=True)
    assert restored['end'] == 20 and session['slides'][0]['end'] == 30


def test_pixel_limit_diagnostic_survives_pillow_bomb_guard(tmp_path, session, monkeypatch):
    monkeypatch.setattr(slide_context, 'MAX_IMAGE_PIXELS', 24)
    monkeypatch.setattr(Image, 'MAX_IMAGE_PIXELS', 12)
    with pytest.raises(SlideContextError) as error:
        save_slide(tmp_path, session, image_bytes(size=(7, 4)), start=0)
    assert '7' in str(error.value) and '4' in str(error.value) and '28' in str(error.value)


def test_readonly_asset_cache_avoids_rehash_and_invalidates_changed_file(tmp_path, session, monkeypatch):
    original = save_slide(tmp_path, session, image_bytes(), start=0)
    path = resolve_slide_path(tmp_path, session['id'], original)
    path_class = type(path)
    original_open = path_class.open
    reads = []

    def counted_open(self, *args, **kwargs):
        if self == path and args and args[0] == 'rb':
            reads.append(str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(path_class, 'open', counted_open)
    resolve_slide_path(tmp_path, session['id'], original, force=False)
    resolve_slide_path(tmp_path, session['id'], original, force=False)
    assert len(reads) <= 1
    path.write_bytes(b'synthetic corruption')
    with pytest.raises(SlideContextError, match='校验失败'):
        resolve_slide_path(tmp_path, session['id'], original, force=False)
    assert len(reads) >= 1


def test_default_resolution_always_verifies_even_after_cached_projection(tmp_path, session, monkeypatch):
    original = save_slide(tmp_path, session, image_bytes(), start=0)
    path = resolve_slide_path(tmp_path, session['id'], original, force=False)
    path_class = type(path)
    original_open = path_class.open
    reads = []

    def counted_open(self, *args, **kwargs):
        if self == path and args and args[0] == 'rb':
            reads.append(str(self))
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(path_class, 'open', counted_open)
    resolve_slide_path(tmp_path, session['id'], original)
    resolve_slide_path(tmp_path, session['id'], original)
    assert len(reads) == 2


@pytest.mark.parametrize('fmt,options', [('PNG', {}), ('JPEG', {}), ('JPEG', {'progressive': True}),
                                       ('WEBP', {}), ('WEBP', {'lossless': True})])
def test_diagnostic_headers_report_dimensions_without_decoding(fmt, options):
    assert slide_context.image_dimensions(image_bytes(fmt, size=(17, 9), **options)) == (17, 9)


def test_cached_asset_same_size_change_and_disappearance_are_not_trusted(tmp_path, session):
    original = save_slide(tmp_path, session, image_bytes(), start=0)
    path = resolve_slide_path(tmp_path, session['id'], original, force=False)
    contents = path.read_bytes()
    previous = path.stat()
    path.write_bytes(bytes([contents[0] ^ 1]) + contents[1:])
    os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    with pytest.raises(SlideContextError, match='校验失败'):
        resolve_slide_path(tmp_path, session['id'], original, force=False)
    path.unlink()
    with pytest.raises(SlideContextError) as error:
        resolve_slide_path(tmp_path, session['id'], original, force=False)
    assert error.value.code == 'missing'
    path.write_bytes(contents)
    assert resolve_slide_path(tmp_path, session['id'], original, force=False) == path


def test_rotation_expansion_above_limit_keeps_all_old_files(tmp_path, session, monkeypatch):
    import random

    image = Image.frombytes('RGB', (40, 40), random.Random(71).randbytes(40 * 40 * 3))
    body = io.BytesIO()
    image.save(body, 'JPEG')
    original = save_slide(tmp_path, session, body.getvalue(), start=0)
    path = resolve_slide_path(tmp_path, session['id'], original)
    output = io.BytesIO()
    with Image.open(path) as loaded:
        loaded.transpose(Image.Transpose.ROTATE_270).save(output, 'PNG')
    assert len(output.getvalue()) > path.stat().st_size
    monkeypatch.setattr(slide_context, 'MAX_IMAGE_BYTES', len(output.getvalue()) - 1)
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError) as error:
        history().rotate_slide(tmp_path, session, original['id'], 'right')
    assert error.value.code == 'size'
    assert session == before
    assert list(path.parent.iterdir()) == [path]
