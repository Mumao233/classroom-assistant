"""Synthetic slide decoding, local file integrity and frozen evidence ranges."""
import copy
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, PngImagePlugin

import slide_context as slides_module
from slide_context import (
    MAX_IMAGE_BYTES, MAX_IMAGE_PIXELS, SlideContextError, resolve_slide_path,
    save_slide, snapshot_context, update_slide_interval,
)


def image_bytes(fmt="PNG", size=(12, 8), **save_options):
    image = Image.new("RGB", size, "#24775b")
    output = io.BytesIO()
    image.save(output, format=fmt, **save_options)
    return output.getvalue()


def segment(sid, start, end, text="Synthetic classroom statement.", **extra):
    return {"id": sid, "start": start, "end": end, "text": text, "source": "audio", **extra}


def picture(pid, start, end=None, **extra):
    return {"id": pid, "start": start, "end": end, "title": "Synthetic page", **extra}


@pytest.fixture
def session():
    return {"id": "a" * 32, "segments": [segment("S0001", 0, 30)], "groups": [], "audio": []}


@pytest.mark.parametrize("fmt,extension,mime", [("PNG", ".png", "image/png"),
                                              ("JPEG", ".jpg", "image/jpeg"),
                                              ("WEBP", ".png", "image/png")])
def test_actual_image_formats_are_decoded_reencoded_and_hashed(tmp_path, session, fmt, extension, mime):
    slide = save_slide(tmp_path, session, image_bytes(fmt), start=2, title="Synthetic page")
    path = resolve_slide_path(tmp_path, session["id"], slide)
    assert path.suffix == extension and path.parent == tmp_path / session["id"] / "slides"
    assert slide["id"] == "P0001" and slide["mime_type"] == mime
    assert (slide["width"], slide["height"]) == (12, 8)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == slide["sha256"]
    with Image.open(path) as decoded:
        decoded.load()
        assert decoded.size == (12, 8)
        assert not decoded.getexif()
    slide["title"] = "Caller changed returned copy"
    assert session["slides"][0]["title"] == "Synthetic page"


def test_exif_orientation_is_applied_and_private_metadata_removed(tmp_path, session):
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "Synthetic private metadata"
    slide = save_slide(tmp_path, session, image_bytes("JPEG", (12, 8), exif=exif), start=0)
    assert (slide["width"], slide["height"]) == (8, 12)
    data = resolve_slide_path(tmp_path, session["id"], slide).read_bytes()
    assert b"Synthetic private metadata" not in data
    with Image.open(io.BytesIO(data)) as decoded:
        assert not decoded.getexif()


def test_png_text_metadata_is_removed_and_alpha_is_preserved(tmp_path, session):
    image = Image.new("RGBA", (3, 2), (1, 2, 3, 80))
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "Synthetic private PNG text")
    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=metadata)
    slide = save_slide(tmp_path, session, output.getvalue(), start=0)
    with Image.open(resolve_slide_path(tmp_path, session["id"], slide)) as decoded:
        assert decoded.getpixel((0, 0)) == (1, 2, 3, 80)
        assert "Comment" not in decoded.info


@pytest.mark.parametrize("content", [b"", b"not an image", image_bytes("GIF"), image_bytes()[:35]])
def test_invalid_images_do_not_change_session_or_create_files(tmp_path, session, content):
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError):
        save_slide(tmp_path, session, content, start=0)
    assert session == before and list(tmp_path.iterdir()) == []


def test_upload_byte_limit_is_inclusive(tmp_path, session):
    small = image_bytes()
    exact_limit = small + b"\0" * (MAX_IMAGE_BYTES - len(small))
    save_slide(tmp_path, session, exact_limit, start=0)
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError, match="10 MiB"):
        save_slide(tmp_path, session, exact_limit + b"\0", start=1)
    assert session == before


def test_pixel_limit_is_inclusive_and_checked_before_full_decode(tmp_path, session, monkeypatch):
    assert MAX_IMAGE_PIXELS == 24_000_000
    monkeypatch.setattr(slides_module, "MAX_IMAGE_PIXELS", 24)
    save_slide(tmp_path, session, image_bytes(size=(6, 4)), start=0)
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError, match="2400 万"):
        save_slide(tmp_path, session, image_bytes(size=(5, 5)), start=1)
    assert session == before


def test_reencoded_size_limit_rejects_expansion_before_any_write(tmp_path, session, monkeypatch):
    compressed = image_bytes("WEBP", size=(40, 40))
    encoded, *_ = slides_module._image_bytes(compressed)
    assert len(encoded) > len(compressed)
    # Exercise real WebP -> PNG expansion without generating a huge fixture.
    monkeypatch.setattr(slides_module, "MAX_IMAGE_BYTES", len(compressed))
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError, match="重新编码后超过"):
        save_slide(tmp_path, session, compressed, start=0)
    assert session == before and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("fmt", ["PNG", "WEBP"])
def test_multiframe_images_are_rejected(tmp_path, session, fmt):
    output = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(output, format=fmt, save_all=True,
        append_images=[Image.new("RGB", (3, 3), "blue")], duration=100, loop=0)
    with pytest.raises(SlideContextError, match="单帧"):
        save_slide(tmp_path, session, output.getvalue(), start=0)
    assert "slides" not in session


def test_new_page_closes_previous_without_mutating_old_snapshots(tmp_path, session):
    first = save_slide(tmp_path, session, image_bytes(), start=0)
    snapshot = snapshot_context(session, mode="recent30", anchor=30)
    original_bytes = resolve_slide_path(tmp_path, session["id"], first).read_bytes()
    second = save_slide(tmp_path, session, image_bytes(size=(14, 9)), start=20)
    assert [s["id"] for s in session["slides"]] == ["P0001", "P0002"]
    assert session["slides"][0]["end"] == 20 and second["end"] is None
    assert first["end"] is None and snapshot["slides"][0]["end"] is None
    assert resolve_slide_path(tmp_path, session["id"], first).read_bytes() == original_bytes


def test_backdated_upload_is_rejected_and_interval_edit_is_explicit(tmp_path, session):
    first = save_slide(tmp_path, session, image_bytes(), start=20)
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError, match="手动调整"):
        save_slide(tmp_path, session, image_bytes(), start=10)
    assert session == before
    edited = update_slide_interval(session, first["id"], 0, 10)
    assert edited["start"] == 0 and edited["end"] == 10
    assert first["start"] == 20
    assert save_slide(tmp_path, session, image_bytes(), start=10)["id"] == "P0002"


@pytest.mark.parametrize("start,end", [(-1, 2), (3, 2), (float("nan"), 2), (0, float("inf")), (True, 2)])
def test_invalid_intervals_are_rejected_without_mutation(tmp_path, session, start, end):
    first = save_slide(tmp_path, session, image_bytes(), start=0)
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError):
        update_slide_interval(session, first["id"], start, end)
    assert session == before


def test_missing_slide_interval_cannot_update_another_classroom(session):
    with pytest.raises(SlideContextError, match="本课堂"):
        update_slide_interval(session, "P0001", 0, 10)
    assert "slides" not in session


def test_saved_file_collision_does_not_overwrite_or_close_existing_slide(tmp_path, session, monkeypatch):
    monkeypatch.setattr(slides_module.uuid, "uuid4", lambda: SimpleNamespace(hex="c" * 32))
    first = save_slide(tmp_path, session, image_bytes(), start=0)
    path = resolve_slide_path(tmp_path, session["id"], first)
    before = copy.deepcopy(session)
    contents = path.read_bytes()
    with pytest.raises(SlideContextError, match="未能保存"):
        save_slide(tmp_path, session, image_bytes(size=(2, 2)), start=10)
    assert session == before and path.read_bytes() == contents


def test_failed_write_does_not_mutate_session_or_leave_partial_image(tmp_path, session, monkeypatch):
    monkeypatch.setattr(slides_module.os, "fsync", lambda *_: (_ for _ in ()).throw(OSError("synthetic failure")))
    before = copy.deepcopy(session)
    with pytest.raises(SlideContextError, match="未能保存"):
        save_slide(tmp_path, session, image_bytes(), start=0)
    assert session == before
    assert not list(tmp_path.rglob("*.png"))


@pytest.mark.parametrize("bad_path", ["../other.png", "slides/../other.png", "slides\\" + "b" * 32 + ".png",
                                    "/slides/" + "b" * 32 + ".png", "slides/file.png", "slides/" + "b" * 32 + ".exe"])
def test_resolver_rejects_noncanonical_or_escaping_paths(tmp_path, session, bad_path):
    with pytest.raises(SlideContextError, match="路径"):
        resolve_slide_path(tmp_path, session["id"], {"path": bad_path, "sha256": "0" * 64})


@pytest.mark.parametrize("bad_sid", ["../escape", "a/b", "a\\b", ".", "C:escape", ""])
def test_session_directory_cannot_escape_data(tmp_path, bad_sid):
    with pytest.raises(SlideContextError):
        save_slide(tmp_path, {"id": bad_sid}, image_bytes(), start=0)
    assert list(tmp_path.iterdir()) == []


def test_resolver_rejects_tampering_and_other_classroom(tmp_path, session):
    slide = save_slide(tmp_path, session, image_bytes(), start=0)
    path = resolve_slide_path(tmp_path, session["id"], slide)
    with pytest.raises(SlideContextError, match="不可用"):
        resolve_slide_path(tmp_path, "b" * 32, slide)
    path.write_bytes(image_bytes(size=(2, 2)))
    with pytest.raises(SlideContextError, match="校验失败"):
        resolve_slide_path(tmp_path, session["id"], slide)


def test_recent_window_uses_time_intersection_not_append_order(session):
    session["segments"] = [segment("S0001", 60, 72), segment("S0002", 72, 84),
                           segment("S0003", 49, 61), segment("S0004", 30, 54),
                           segment("S0005", 84, 96)]
    snapshot = snapshot_context(session, mode="recent30", anchor=84)
    assert snapshot["requested_range"] == {"start": 54, "end": 84}
    assert snapshot["actual_range"] == {"start": 49, "end": 84}
    assert snapshot["source_ids"] == ["S0003", "S0001", "S0002"]
    assert "整块" in snapshot["warnings"][0]


def test_recent120_clamps_to_recording_origin_and_includes_boundary_chunk(session):
    session["segments"] = [segment("S0001", 0, 12), segment("S0002", 12, 24)]
    snapshot = snapshot_context(session, mode="recent120", anchor=20)
    assert snapshot["requested_range"] == {"start": 0, "end": 20}
    assert snapshot["actual_range"] == {"start": 0, "end": 24}
    assert len(snapshot["segments"]) == 2 and snapshot["incomplete"] is False


def test_group_contains_only_member_segments_and_defaults_to_latest_group(session):
    session["segments"] = [segment("S0001", 0, 10), segment("S0002", 10, 20), segment("S0003", 5, 9)]
    session["groups"] = [{"id": "G0001", "segment_ids": ["S0002"], "partial": False},
                         {"id": "G0002", "segment_ids": ["S0001", "S0003"], "partial": False}]
    snapshot = snapshot_context(session, mode="group", anchor=120, group_id="G0002")
    assert snapshot["group_id"] == "G0002" and snapshot["anchor"] == 120
    assert snapshot["source_ids"] == ["S0001", "S0003"]
    assert snapshot["requested_range"] == {"start": 0, "end": 10}
    assert not snapshot["incomplete"]
    assert snapshot_context(session, mode="group", anchor=120, group_id="G0001")["source_ids"] == ["S0002"]


def test_default_group_uses_teaching_time_when_old_audio_arrives_late(session):
    session["segments"] = [segment("S0001", 60, 80), segment("S0002", 10, 20)]
    session["groups"] = [{"id": "G0001", "segment_ids": ["S0001"], "start": 60, "end": 80},
                         {"id": "G0002", "segment_ids": ["S0002"], "start": 10, "end": 20}]
    snapshot = snapshot_context(session, mode="group", anchor=90)
    assert snapshot["group_id"] == "G0001" and snapshot["source_ids"] == ["S0001"]
    assert snapshot_context(session, mode="group", anchor=90, group_id="G0002")["source_ids"] == ["S0002"]


def test_default_group_derives_missing_legacy_times_from_its_members(session):
    session["segments"] = [segment("S0001", 60, 80), segment("S0002", 10, 20)]
    session["groups"] = [{"id": "G0001", "segment_ids": ["S0001"]}, {"id": "G0002", "segment_ids": ["S0002"]}]
    assert snapshot_context(session, mode="group", anchor=90)["group_id"] == "G0001"


def test_default_group_respects_click_anchor_but_keeps_crossing_group_whole(session):
    session["segments"] = [segment("S0001", 10, 30), segment("S0002", 31, 40)]
    session["groups"] = [{"id": "G0001", "segment_ids": ["S0001"], "start": 10, "end": 30},
                         {"id": "G0002", "segment_ids": ["S0002"], "start": 31, "end": 40}]
    snapshot = snapshot_context(session, mode="group", anchor=20)
    assert snapshot["group_id"] == "G0001" and snapshot["actual_range"]["end"] == 30
    with pytest.raises(SlideContextError):
        snapshot_context(session, mode="group", anchor=5)


@pytest.mark.parametrize("groups", [[], [{"id": "G0001", "segment_ids": []}],
                                     [{"id": "G0001", "segment_ids": ["S9999"]}]])
def test_absent_or_incomplete_group_records_are_rejected(session, groups):
    session["groups"] = groups
    with pytest.raises(SlideContextError):
        snapshot_context(session, mode="group", anchor=30)


def test_default_images_follow_time_intersection_and_lecture_order(session):
    session["segments"] = [segment("S0001", 30, 60)]
    session["slides"] = [picture("P0003", 50), picture("P0001", 0, 30), picture("P0002", 30, 50), picture("P0004", 60)]
    snapshot = snapshot_context(session, mode="recent30", anchor=60)
    assert [s["id"] for s in snapshot["slides"]] == ["P0002", "P0003"]
    assert snapshot["source_ids"] == ["S0001", "P0002", "P0003"]
    assert "图片证据" in snapshot["context"]


def test_explicit_images_are_owned_unique_and_sorted_by_teaching_time(session):
    session["slides"] = [picture("P0001", 5, 10), picture("P0002", 40, 60)]
    snapshot = snapshot_context(session, mode="recent30", anchor=30, slide_ids=["P0002", "P0001"])
    assert [s["id"] for s in snapshot["slides"]] == ["P0001", "P0002"]
    assert any("时间之外" in warning for warning in snapshot["warnings"])
    for selection in (["P9999"], ["P0001", "P0001"], "P0001"):
        with pytest.raises(SlideContextError):
            snapshot_context(session, mode="recent30", anchor=30, slide_ids=selection)


def test_more_than_four_images_is_never_silently_truncated(session):
    session["slides"] = [picture(f"P{n:04d}", n, n + 1) for n in range(1, 6)]
    with pytest.raises(SlideContextError, match="超过 4"):
        snapshot_context(session, mode="recent30", anchor=30)
    first_four = [slide["id"] for slide in session["slides"][:4]]
    assert len(snapshot_context(session, mode="recent30", anchor=30, slide_ids=first_four)["slides"]) == 4
    with pytest.raises(SlideContextError, match="最多使用 4"):
        snapshot_context(session, mode="recent30", anchor=30, slide_ids=first_four + ["P0005"])
    assert snapshot_context(session, mode="recent30", anchor=30, include_images=False)["slides"] == []


def test_zero_length_slide_is_not_implicitly_selected(session):
    session["slides"] = [picture("P0001", 10, 10)]
    assert snapshot_context(session, mode="recent30", anchor=30)["slides"] == []
    selected = snapshot_context(session, mode="recent30", anchor=30, slide_ids=["P0001"])
    assert selected["slides"][0]["id"] == "P0001" and "P0001" in selected["source_ids"]


def test_snapshot_is_deeply_frozen_without_rewriting_legacy_record(session):
    session["segments"][0]["edits"] = [{"text": "Synthetic original"}]
    session["slides"] = [picture("P0001", 0, 30, sha256="a" * 64)]
    before = json.dumps(session, sort_keys=True)
    snapshot = snapshot_context(session, mode="recent30", anchor=30)
    assert json.dumps(session, sort_keys=True) == before
    session["segments"][0]["text"] = "Later corrected text"
    session["segments"][0]["edits"][0]["text"] = "Later edit history"
    update_slide_interval(session, "P0001", 50, None)
    assert snapshot["segments"][0]["text"] == "Synthetic classroom statement."
    assert snapshot["segments"][0]["edits"][0]["text"] == "Synthetic original"
    assert snapshot["slides"][0]["start"] == 0


@pytest.mark.parametrize("text", ["", " \n\t", None])
def test_no_usable_text_is_rejected_even_with_images(session, text):
    session["segments"][0]["text"] = text
    session["slides"] = [picture("P0001", 0)]
    with pytest.raises(SlideContextError, match="不会仅凭图片"):
        snapshot_context(session, mode="recent30", anchor=30)


@pytest.mark.parametrize("source", ["manual", "text"])
def test_manual_source_is_marked_estimated_only_in_snapshot(session, source):
    session["segments"][0]["source"] = source
    snapshot = snapshot_context(session, mode="recent30", anchor=30)
    assert snapshot["segments"][0]["time_estimated"] and snapshot["incomplete"]
    assert "time_estimated" not in session["segments"][0]
    assert any("估算" in message for message in snapshot["warnings"])


@pytest.mark.parametrize("status", ["pending", "running", "error", "done"])
def test_audio_gaps_use_uncertainty_not_an_assertion_of_missing_words(session, status):
    session["audio"] = [{"seq": 1, "start": 15, "duration": 10, "status": status}]
    snapshot = snapshot_context(session, mode="recent30", anchor=30)
    assert snapshot["incomplete"]
    warnings = " ".join(snapshot["warnings"])
    assert "确认" in warnings and "缺词" not in warnings


def test_audio_outside_selection_does_not_make_it_incomplete(session):
    session["audio"] = [{"seq": 1, "start": 31, "duration": 10, "status": "error"}]
    assert snapshot_context(session, mode="recent30", anchor=30)["incomplete"] is False


def test_transcript_lag_and_partial_group_are_explicit(session):
    session["segments"][0]["end"] = 20
    assert snapshot_context(session, mode="recent30", anchor=30)["incomplete"]
    session["groups"] = [{"id": "G0001", "segment_ids": ["S0001"], "partial": True}]
    snapshot = snapshot_context(session, mode="group", anchor=30)
    assert snapshot["incomplete"] and any("尚未讲完" in w for w in snapshot["warnings"])


def test_context_is_not_silently_truncated(session):
    text = "Synthetic long text. " * 4000
    session["segments"][0]["text"] = text
    assert text in snapshot_context(session, mode="recent30", anchor=30)["context"]
