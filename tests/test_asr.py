"""Offline tests for audio validation and the serialized ASR boundary."""

import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asr import APP_ROOT, ASREngine, ASRError, resolve_app_path


class FakeModel:
    def __init__(self):
        self.calls = []
        self.active = 0
        self.maximum_active = 0
        self.segments = [SimpleNamespace(start=0, end=0.2, text="Opportunity cost 机会成本。",
                                        avg_logprob=-0.2, no_speech_prob=0.01, compression_ratio=1.0)]

    def detect_language(self, samples, **kwargs):
        return "en", 0.95, [("en", 0.95)]

    def transcribe(self, samples, **kwargs):
        self.calls.append(kwargs)
        def generate():
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                time.sleep(0.02)
                yield from self.segments
            finally:
                self.active -= 1
        return generate(), SimpleNamespace(language="en")


class ASRBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sample.wav"
        self.model = FakeModel()
        self.engine = ASREngine({"device": "cpu"})
        self.engine._model = self.model
        self.engine._device = "cpu"
        self.engine._compute_type = "int8"
        self.engine._active_model_name = "small"
        self.engine._loaded_signature = ("cpu", str(self.engine.cpu_model_path))
        self.engine._probe = lambda: []
        self.engine._model_exists = lambda path: True
        self.engine._cuda_count = 0

    def tearDown(self):
        self.temp.cleanup()

    def wav(self, seconds=0.25, *, silent=False, channels=1, rate=16000):
        count = int(seconds * rate)
        pcm = b"".join(struct.pack("<h", 0 if silent else int(5000 * math.sin(2 * math.pi * 440 * i / rate)))
                       for i in range(count))
        with wave.open(str(self.path), "wb") as audio:
            audio.setnchannels(channels)
            audio.setsampwidth(2)
            audio.setframerate(rate)
            audio.writeframes(pcm)
        return self.path

    def test_silence_never_reaches_decoder(self):
        result = self.engine.transcribe(self.wav(1, silent=True))
        self.assertEqual(result["reason"], "no_signal")
        self.assertEqual(result["text"], "")
        self.assertEqual(self.model.calls, [])

    def test_short_voiced_tail_keeps_mixed_language_and_limits_prompt(self):
        result = self.engine.transcribe(self.wav(0.25), prompt="术语" * 150)
        self.assertIn("机会成本", result["text"])
        options = self.model.calls[0]
        self.assertEqual(len(options["initial_prompt"]), 200)
        self.assertIsNone(options["language"])
        self.assertFalse(options["multilingual"])
        self.assertEqual(options["task"], "transcribe")

    def test_tiny_tail_is_explicit(self):
        result = self.engine.transcribe(self.wav(0.05))
        self.assertEqual(result["reason"], "too_short")
        self.assertEqual(self.model.calls, [])

    def test_rejects_non_wav_and_truncated_audio(self):
        self.path.write_bytes(b"not an audio file")
        with self.assertRaisesRegex(ValueError, "WAV"):
            self.engine.transcribe(self.path)
        self.wav()
        self.path.write_bytes(self.path.read_bytes()[:-80])
        with self.assertRaisesRegex(ValueError, "不完整"):
            self.engine.transcribe(self.path)

    def test_rejects_wrong_audio_format_and_oversized_duration(self):
        with self.assertRaisesRegex(ValueError, "16 kHz"):
            self.engine.transcribe(self.wav(rate=8000))
        with self.assertRaisesRegex(ValueError, "单声道"):
            self.engine.transcribe(self.wav(channels=2))
        self.engine.max_audio_seconds = 0.1
        with self.assertRaisesRegex(ValueError, "过长"):
            self.engine.transcribe(self.wav())

    def test_low_confidence_silence_hallucination_is_filtered(self):
        self.model.segments[0].avg_logprob = -1.3
        self.model.segments[0].no_speech_prob = 0.95
        result = self.engine.transcribe(self.wav())
        self.assertEqual(result["text"], "")
        self.assertEqual(result["reason"], "no_speech")

    def test_high_confidence_repetition_is_not_accepted(self):
        self.model.segments[0].text = "条件" * 30
        self.model.segments[0].avg_logprob = -0.05
        result = self.engine.transcribe(self.wav())
        self.assertEqual(result["text"], "")
        self.assertEqual(result["reason"], "suspected_hallucination")
        self.assertIn("回听", result["warning"])

    def test_prompt_hallucination_gets_one_guarded_prompt_free_retry(self):
        bad = SimpleNamespace(start=0, end=0.2, text="机会成本" * 20,
                              avg_logprob=-0.04, no_speech_prob=0, compression_ratio=6.4)
        good = self.model.segments[0]
        info = SimpleNamespace(language="en")
        with patch.object(self.engine, "_decode", side_effect=[([bad], info), ([good], info)]) as decode:
            result = self.engine.transcribe(self.wav(), "course vocabulary")
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(decode.call_args_list[1].args[1], "")
        self.assertEqual(result["text"], good.text)
        self.assertTrue(result["retry_without_prompt"])
        self.assertTrue(result["recovered_from_hallucination"])
        self.assertTrue(result["segments"][0]["needs_review"])
        self.assertEqual(result["discarded_suspected_hallucinations"], 1)
        self.assertIn("重试一次", result["warning"])

    def test_retry_never_admits_repeated_output_or_retries_again(self):
        bad = SimpleNamespace(start=0, end=0.2, text="机会成本" * 20,
                              avg_logprob=-0.04, no_speech_prob=0, compression_ratio=6.4)
        with patch.object(self.engine, "_decode", return_value=([bad], SimpleNamespace(language="en"))) as decode:
            result = self.engine.transcribe(self.wav(), "course vocabulary")
        self.assertEqual(decode.call_count, 2)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["reason"], "suspected_hallucination")
        self.assertFalse(result["recovered_from_hallucination"])
        self.assertEqual(result["discarded_suspected_hallucinations"], 2)

    def test_no_prompt_hallucination_does_not_retry_identically(self):
        self.model.segments[0].text = "条件" * 30
        result = self.engine.transcribe(self.wav())
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(result["retry_without_prompt"])

    def test_normal_hinted_result_has_no_extra_inference(self):
        result = self.engine.transcribe(self.wav(), "opportunity cost")
        self.assertEqual(len(self.model.calls), 1)
        self.assertFalse(result["retry_without_prompt"])
        self.assertFalse(result["recovered_from_hallucination"])

    def test_language_is_detected_from_opening_audio_not_fixed_english(self):
        self.model.detect_language = lambda samples, **kwargs: ("zh", 0.98, [("zh", 0.98)])
        self.engine.transcribe(self.wav(1))
        self.assertEqual(self.model.calls[0]["language"], "zh")

    def test_generator_inference_is_serialized(self):
        self.wav()
        errors = []
        def work():
            try:
                self.engine.transcribe(self.path)
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=work) for _ in range(3)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(len(self.model.calls), 3)
        self.assertEqual(self.model.maximum_active, 1)

    def test_mode_change_during_audio_waits_until_next_audio_boundary(self):
        self.wav(1)
        began = threading.Event()
        finish = threading.Event()
        result = []
        creates = []
        def blocking_decode(samples, prompt):
            began.set()
            if not finish.wait(3):
                raise RuntimeError("test timed out")
            return [], SimpleNamespace(language="en")
        def create(selection):
            creates.append(selection["device"])
            self.engine._model = FakeModel()
            self.engine._device = selection["device"]
            self.engine._compute_type = selection["compute_type"]
            self.engine._active_model_name = selection["model_name"]
            self.engine._loaded_signature = (selection["device"], selection["model_path"])
        self.engine._decode = blocking_decode
        self.engine._create_model = create
        self.engine._cuda_count = 1
        with patch("asr.read_power_state", return_value={"on_ac_power": True, "battery_percent": 90}):
            worker = threading.Thread(target=lambda: result.append(self.engine.transcribe(self.path)))
            worker.start()
            self.assertTrue(began.wait(2))
            state = self.engine.set_mode("quality")
            self.assertTrue(state["pending_switch"])
            self.assertEqual(creates, [])
            finish.set()
            worker.join(timeout=3)
            self.assertEqual(result[0]["device"], "cpu")
            next_result = self.engine.transcribe(self.path)
            self.assertEqual(next_result["device"], "cuda")
            self.assertEqual(creates, ["cuda"])


class ModeTests(unittest.TestCase):
    def setUp(self):
        self.engine = ASREngine({"mode": "auto"})
        self.engine._probe = lambda: []
        self.engine._model_exists = lambda path: True
        self.engine._cuda_count = 1
        self.power = patch("asr.read_power_state", return_value={"on_ac_power": True, "battery_percent": 80})
        self.mock_power = self.power.start()
        self.addCleanup(self.power.stop)

    def test_auto_tracks_actual_power_and_cuda(self):
        self.assertEqual(self.engine.status()["model_name"], "large-v3-turbo")
        self.mock_power.return_value["on_ac_power"] = False
        self.assertEqual(self.engine.status()["effective_mode"], "battery")
        self.mock_power.return_value["on_ac_power"] = True
        self.engine._cuda_count = 0
        state = self.engine.status()
        self.assertEqual(state["model_name"], "small")
        self.assertFalse(state["high_quality_available"])
        self.assertIn("独显", state["warning"])

    def test_manual_battery_and_quality_fallback_are_explicit(self):
        self.assertEqual(self.engine.set_mode("battery")["device"], "cpu")
        self.mock_power.return_value["on_ac_power"] = False
        state = self.engine.set_mode("quality")
        self.assertEqual(state["requested_mode"], "quality")
        self.assertEqual(state["effective_mode"], "battery")
        self.assertIn("电池", state["warning"])
        with self.assertRaises(ValueError):
            self.engine.set_mode("turbo")

    def test_set_mode_does_not_wait_for_inference_lock(self):
        self.engine._lock.acquire()
        try:
            began = time.monotonic()
            self.engine.set_mode("battery")
            self.assertLess(time.monotonic() - began, 0.25)
        finally:
            self.engine._lock.release()

    def test_switch_releases_old_model_before_loading_new_model(self):
        old = SimpleNamespace(model=SimpleNamespace(unload_model=lambda: events.append("unload")))
        events = []
        self.engine._model = old
        self.engine._device = "cpu"
        self.engine._active_model_name = "small"
        self.engine._loaded_signature = ("cpu", str(self.engine.cpu_model_path))
        self.engine._create_model = lambda selection: events.append("load:" + selection["model_name"])
        self.assertTrue(self.engine.status()["pending_switch"])
        self.engine._ensure_model()
        self.assertEqual(events, ["unload", "load:large-v3-turbo"])

    def test_gpu_load_failure_falls_back_and_avoids_retry_each_chunk(self):
        calls = []
        def create(selection):
            calls.append(selection["device"])
            if selection["device"] == "cuda":
                raise RuntimeError("test GPU load failure")
            self.engine._model = FakeModel()
            self.engine._device = "cpu"
            self.engine._loaded_signature = ("cpu", str(self.engine.cpu_model_path))
        self.engine._create_model = create
        self.engine._ensure_model()
        self.engine._ensure_model()
        self.assertEqual(calls, ["cuda", "cpu"])
        self.assertIn("GPU 运行失败", self.engine.status()["warning"])

    def test_high_quality_uses_shared_lock_and_preserves_requested_mode(self):
        self.engine.set_mode("battery")
        with patch.object(self.engine, "_transcribe", return_value={"device": "cuda"}) as call:
            self.assertEqual(self.engine.transcribe_high_quality("sample.wav"), {"device": "cuda"})
            call.assert_called_once_with("sample.wav", "", mode="quality", require_quality=True)
        self.assertEqual(self.engine.requested_mode, "battery")
        self.mock_power.return_value["on_ac_power"] = False
        with self.assertRaises(ASRError):
            self.engine.transcribe_high_quality("sample.wav")

    def test_quality_review_never_silently_falls_back(self):
        self.engine._create_model = lambda selection: (_ for _ in ()).throw(RuntimeError("test GPU error"))
        with self.assertRaisesRegex(ASRError, "高质量 GPU"):
            self.engine._ensure_model("quality", require_quality=True)
        self.assertIsNone(self.engine._model)

    def test_model_files_require_a_ctranslate_vocabulary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["model.bin", "config.json", "tokenizer.json"]:
                (root / name).touch()
            self.assertFalse(ASREngine._model_exists(root))
            (root / "vocabulary.txt").touch()
            self.assertTrue(ASREngine._model_exists(root))


class PortablePathTests(unittest.TestCase):
    def test_defaults_are_application_local_independent_of_cwd(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            previous = Path.cwd()
            try:
                os.chdir(directory)
                engine = ASREngine()
                self.assertEqual(engine.cpu_model_path, APP_ROOT / "models/faster-whisper-small")
                self.assertEqual(engine.gpu_model_path, APP_ROOT / "models/faster-whisper-large-v3-turbo")
            finally:
                os.chdir(previous)

    def test_relative_overrides_use_app_folder_and_absolute_paths_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            absolute = Path(directory) / "gpu-model"
            engine = ASREngine({"cpu_model_path": "custom/cpu", "gpu_model_path": str(absolute)})
            self.assertEqual(engine.cpu_model_path, APP_ROOT / "custom/cpu")
            self.assertEqual(engine.gpu_model_path, absolute.resolve())
            self.assertEqual(resolve_app_path("cuda/bin"), APP_ROOT / "cuda/bin")

    def test_explicit_legacy_model_path_and_environment_override_still_work(self):
        with patch.dict(os.environ, {"CLASSROOM_MODEL_PATH": "custom/from-env"}):
            self.assertEqual(ASREngine().gpu_model_path, APP_ROOT / "custom/from-env")
            engine = ASREngine({"model_path": "custom/legacy"})
            self.assertEqual(engine.gpu_model_path, APP_ROOT / "custom/legacy")
            engine = ASREngine({"model_path": "custom/legacy", "gpu_model_path": "custom/preferred"})
            self.assertEqual(engine.gpu_model_path, APP_ROOT / "custom/preferred")


if __name__ == "__main__":
    unittest.main()
