"""Local, serialized Whisper transcription for 16 kHz mono PCM WAV chunks.

The module does not open microphones or download models.  Model loading is lazy,
and all heavy dependencies remain optional until a transcription is requested.
"""

from __future__ import annotations

import importlib.util
import ctypes
import gc
import logging
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import wave
from collections.abc import Mapping


APP_ROOT = Path(__file__).resolve().parent
DEFAULT_MODEL_PATH = str(APP_ROOT / "models/faster-whisper-large-v3-turbo")
DEFAULT_CPU_MODEL_PATH = str(APP_ROOT / "models/faster-whisper-small")
LOGGER = logging.getLogger(__name__)


def resolve_app_path(value):
    """Resolve configured relative paths from the app, never the launch cwd."""
    path = Path(value).expanduser()
    return (path if path.is_absolute() else APP_ROOT / path).resolve()


def read_power_state():
    """Read Windows power status without changing any system setting."""
    result = {"on_ac_power": None, "battery_percent": None}
    if os.name != "nt":
        return result
    class SystemPowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_uint32), ("BatteryFullLifeTime", ctypes.c_uint32)]
    try:
        state = SystemPowerStatus()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(state)):
            if state.ACLineStatus in (0, 1):
                result["on_ac_power"] = state.ACLineStatus == 1
            if state.BatteryLifePercent <= 100:
                result["battery_percent"] = int(state.BatteryLifePercent)
    except (AttributeError, OSError):
        LOGGER.debug("Power status unavailable", exc_info=True)
    return result


class ASRError(RuntimeError):
    """A readable local-ASR setup or inference failure."""


class ASREngine:
    """One serialized engine for CPU small and plugged-in CUDA large.

    set_mode() is nonblocking; model changes happen at the next audio boundary.
    transcribe_high_quality() shares the same model and lock, so callers must
    reuse this instance instead of creating a second GPU engine for review.
    """

    def __init__(self, config=None):
        if config is None:
            config = {}
        elif not isinstance(config, Mapping):
            config = vars(config)
        self.config = dict(config)
        self.gpu_model_path = resolve_app_path(
            self.config.get("gpu_model_path") or self.config.get("model_path")
            or os.environ.get("CLASSROOM_MODEL_PATH") or DEFAULT_MODEL_PATH
        )
        self.cpu_model_path = resolve_app_path(self.config.get("cpu_model_path") or DEFAULT_CPU_MODEL_PATH)
        self.model_path = self.cpu_model_path
        self.requested_device = str(self.config.get("device", "auto")).lower()
        if self.requested_device not in {"auto", "cpu", "cuda"}:
            raise ValueError("ASR device must be auto, cpu, or cuda.")
        old_mode = {"auto": "auto", "cpu": "battery", "cuda": "quality"}[self.requested_device]
        self.requested_mode = str(self.config.get("mode", old_mode)).lower()
        if self.requested_mode not in {"auto", "battery", "quality"}:
            raise ValueError("ASR mode must be auto, battery, or quality.")
        self.cpu_threads = max(1, int(self.config.get("cpu_threads", min(6, os.cpu_count() or 2))))
        self.beam_size = max(1, min(5, int(self.config.get("beam_size", 1))))
        self.max_audio_seconds = float(self.config.get("max_audio_seconds", 90))
        self._lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._model = None
        self._loaded_signature = None
        self._active_model_name = None
        self._device = None
        self._compute_type = None
        self._cuda_count = None
        self._last_cuda_probe = 0.0
        self._gpu_failure_until = 0.0
        self._gpu_failure_reason = None
        self._gpu_verified = False
        self._last_error = None
        self._warning = None
        self._last_elapsed = None
        self._dll_handles = []
        self._dlls_added = False

    def _setup_dlls(self):
        if self._dlls_added:
            return
        self._dlls_added = True
        if os.name != "nt" or not hasattr(os, "add_dll_directory"):
            return
        # Reuse installed local libraries in this process only. No installation
        # or system PATH / power / GPU setting is changed.
        roots = [Path(sys.prefix) / "Lib/site-packages/nvidia"]
        directories = [p for root in roots for p in root.glob("*/bin")]
        directories += [resolve_app_path(p) for p in self.config.get("cuda_library_dirs", [])]
        for directory in dict.fromkeys(directories):
            if not directory.is_dir():
                continue
            try:
                self._dll_handles.append(os.add_dll_directory(str(directory)))
                os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
            except OSError:
                LOGGER.debug("Could not add CUDA DLL directory %s", directory, exc_info=True)

    @staticmethod
    def _model_exists(path):
        return (all((path / name).is_file() for name in ("model.bin", "config.json", "tokenizer.json"))
                and any((path / name).is_file() for name in ("vocabulary.json", "vocabulary.txt")))

    def _probe(self):
        missing = [name for name in ("numpy", "faster_whisper", "ctranslate2") if importlib.util.find_spec(name) is None]
        if not missing and (self._cuda_count is None or time.monotonic() - self._last_cuda_probe >= 10):
            self._setup_dlls()
            try:
                import ctranslate2
                self._cuda_count = ctranslate2.get_cuda_device_count()
                self._warning = None
            except Exception as exc:
                self._cuda_count = 0
                self._warning = f"CUDA 检测失败，自动模式使用 CPU：{exc}"
            self._last_cuda_probe = time.monotonic()
        return missing

    def _selection(self, mode=None):
        requested = mode or self.requested_mode
        power = read_power_state()
        cuda_ready = bool(self._cuda_count) and time.monotonic() >= self._gpu_failure_until
        large_exists = self._model_exists(self.gpu_model_path)
        high_quality = power["on_ac_power"] is True and cuda_ready and large_exists
        quality = requested != "battery" and high_quality
        warning = None
        if requested != "battery" and not quality:
            if self._gpu_failure_until > time.monotonic():
                warning = "GPU 运行失败，已回退 CPU small：" + str(self._gpu_failure_reason)
            elif power["on_ac_power"] is False:
                warning = "当前使用电池，已选择 CPU small 省电模式。"
            elif power["on_ac_power"] is None:
                warning = "无法确认电源状态，已选择 CPU small 省电模式。"
            elif not self._cuda_count:
                warning = "已接通电源，但独显当前不可用，已回退 CPU small。"
            elif not large_exists:
                warning = "未找到高质量大模型，已回退 CPU small。"
        return {
            **power, "requested_mode": requested,
            "effective_mode": "quality" if quality else "battery",
            "device": "cuda" if quality else "cpu",
            "compute_type": "float16" if quality else "int8",
            "model_name": "large-v3-turbo" if quality else "small",
            "model_path": str(self.gpu_model_path if quality else self.cpu_model_path),
            "high_quality_available": high_quality,
            "warning": warning,
        }

    def status(self):
        missing = self._probe()
        with self._state_lock:
            selected = self._selection()
            signature = (selected["device"], selected["model_path"])
            model_exists = self._model_exists(Path(selected["model_path"]))
            usable = not missing and model_exists
        return {
            **selected,
            "available": usable,
            "ready": usable,
            "model_loaded": self._model is not None,
            "busy": self._lock.locked(),
            "model_exists": model_exists,
            "cpu_model_exists": self._model_exists(self.cpu_model_path),
            "gpu_model_exists": self._model_exists(self.gpu_model_path),
            "cpu_model_path": str(self.cpu_model_path),
            "gpu_model_path": str(self.gpu_model_path),
            "missing_packages": missing,
            "requested_device": self.requested_device,
            "cuda_available": bool(self._cuda_count),
            "cuda_device_count": self._cuda_count,
            "gpu_verified": self._gpu_verified,
            "high_quality_available": selected["high_quality_available"] and not missing,
            "active_model_name": self._active_model_name,
            "active_device": self._device,
            "pending_switch": self._model is not None and signature != self._loaded_signature,
            "warning": selected["warning"] or self._warning,
            "last_error": self._last_error,
            "last_elapsed": self._last_elapsed,
            "python": sys.executable,
        }

    def set_mode(self, mode):
        mode = str(mode).lower()
        if mode not in {"auto", "battery", "quality"}:
            raise ValueError("ASR mode must be auto, battery, or quality.")
        with self._state_lock:
            self.requested_mode = mode
            self.config["mode"] = mode
            if mode == "quality":
                self._gpu_failure_until = 0.0  # Explicit retry requested.
                self._last_cuda_probe = 0.0
        return self.status()

    def _release_model(self):
        model, self._model = self._model, None
        if model is not None:
            try:
                native = getattr(model, "model", None)
                if native is not None and hasattr(native, "unload_model"):
                    native.unload_model()
            except Exception:
                LOGGER.debug("Explicit model unload failed; releasing references", exc_info=True)
            del model
            gc.collect()
        self._loaded_signature = None
        self._active_model_name = None
        self._device = None
        self._compute_type = None

    def unload(self):
        """Release model after current inference; caller must not hold its own ASR lock."""
        with self._lock:
            self._release_model()

    def _create_model(self, selection):
        from faster_whisper import WhisperModel
        device, compute_type = selection["device"], selection["compute_type"]
        model = WhisperModel(
            selection["model_path"], device=device, compute_type=compute_type,
            cpu_threads=self.cpu_threads, num_workers=1, local_files_only=True,
        )
        self._model = model
        self._device = device
        self._compute_type = compute_type
        self.model_path = Path(selection["model_path"])
        self._active_model_name = selection["model_name"]
        self._loaded_signature = (device, selection["model_path"])

    def _mark_gpu_failure(self, exc):
        self._gpu_failure_reason = str(exc)
        self._gpu_failure_until = time.monotonic() + 60

    def _ensure_model(self, mode=None, require_quality=False):
        missing = self._probe()
        if missing:
            raise ASRError("缺少本地转写依赖：" + ", ".join(missing))
        current = self._selection(mode)
        if require_quality and not current["high_quality_available"]:
            raise ASRError("高质量复核需要接通电源、启用独显并备有大模型。" + (current["warning"] or ""))
        if not self._model_exists(Path(current["model_path"])):
            raise ASRError(f"未找到完整本地 Whisper 模型：{current['model_path']}")
        if self._model is not None and self._loaded_signature == (current["device"], current["model_path"]):
            return
        self._release_model()
        try:
            self._create_model(current)
        except Exception as exc:
            if current["device"] != "cuda":
                raise ASRError(f"本地模型加载失败：{exc}") from exc
            self._mark_gpu_failure(exc)
            self._release_model()
            if require_quality:
                raise ASRError(f"高质量 GPU 模型加载失败：{exc}") from exc
            self._create_model(self._selection("battery"))

    def _read_wav(self, wav_path):
        path = Path(wav_path)
        if not path.is_file():
            raise ValueError("音频文件不存在。")
        if path.stat().st_size > self.max_audio_seconds * 32000 + 1024 * 1024:
            raise ValueError("音频分段过大，请缩短每段时长。")
        try:
            with wave.open(str(path), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 16000, "NONE"):
                    raise ValueError("音频必须是 16 kHz、单声道、16-bit PCM WAV。")
                count = audio.getnframes()
                duration = count / 16000
                if duration > self.max_audio_seconds:
                    raise ValueError("音频分段过长，请缩短每段时长。")
                raw = audio.readframes(count)
                if len(raw) != count * 2:
                    raise ValueError("WAV 音频数据不完整。")
        except (wave.Error, EOFError) as exc:
            raise ValueError("不是有效的 PCM WAV 音频。") from exc
        return raw, duration

    def _decode(self, samples, prompt):
        # Full-chunk language voting can select Chinese for an English ->
        # Chinese chunk and make Whisper translate or skip the English. Detect
        # from its opening speech instead, without hard-coding a language.
        language = None
        if len(samples) >= 8000:
            try:
                from faster_whisper.vad import VadOptions
                detected, probability, _ = self._model.detect_language(
                    samples[:48000], vad_filter=True,
                    vad_parameters=VadOptions(min_speech_duration_ms=100, speech_pad_ms=250),
                )
                if probability >= 0.5:
                    language = detected
            except (ValueError, RuntimeError):
                # A short opening silence can provide no language evidence.
                pass
        segments, info = self._model.transcribe(
            samples, task="transcribe", language=language, multilingual=False,
            initial_prompt=prompt or None, condition_on_previous_text=False,
            beam_size=self.beam_size, temperature=0,
            vad_filter=True,
            vad_parameters={"threshold": 0.35, "min_speech_duration_ms": 100,
                            "min_silence_duration_ms": 500, "speech_pad_ms": 250},
            no_speech_threshold=0.6, log_prob_threshold=-1.0,
            compression_ratio_threshold=2.4, word_timestamps=False,
        )
        # The generator executes inference here, inside the engine lock.
        return list(segments), info

    def _decode_safely(self, samples, prompt, require_quality):
        try:
            decoded, info = self._decode(samples, prompt)
        except Exception as exc:
            if self._device != "cuda":
                raise
            self._mark_gpu_failure(exc)
            self._release_model()
            if require_quality:
                raise ASRError(f"高质量 GPU 转写失败：{exc}") from exc
            self._create_model(self._selection("battery"))
            decoded, info = self._decode(samples, prompt)
        if self._device == "cuda":
            self._gpu_verified = True
        return decoded, info

    @staticmethod
    def _filter_segments(decoded, duration):
        accepted = []
        suspected = 0
        for segment in decoded:
            text = segment.text.strip()
            avg_logprob = float(getattr(segment, "avg_logprob", 0.0))
            no_speech = float(getattr(segment, "no_speech_prob", 0.0))
            compression = float(getattr(segment, "compression_ratio", 0.0))
            if not text or not math.isfinite(avg_logprob):
                continue
            # Repetition loops can have very HIGH confidence; confidence alone
            # must never be allowed to admit them, including on a retry.
            repetitive = bool(re.search(r"([\w\u4e00-\u9fff]{1,24})(?:[\s,，、;；.!。!?？]*\1){5,}", text))
            if compression > 3.2 or repetitive:
                suspected += 1
                continue
            if no_speech > 0.6 and avg_logprob < -1.0:
                continue
            start = max(0.0, min(duration, float(segment.start)))
            end = max(start, min(duration, float(segment.end)))
            accepted.append({
                "start": round(start, 3), "end": round(end, 3), "text": text,
                "avg_logprob": round(avg_logprob, 4),
                "needs_review": avg_logprob < -0.8 or no_speech > 0.5,
            })
        return accepted, suspected

    def transcribe(self, wav_path, prompt=""):
        return self._transcribe(wav_path, prompt)

    def transcribe_high_quality(self, wav_path, prompt=""):
        """Manual review through this same engine, never disguised CPU fallback."""
        if not self.status()["high_quality_available"]:
            raise ASRError("高质量复核需要接通电源、启用独显并备有大模型。")
        return self._transcribe(wav_path, prompt, mode="quality", require_quality=True)

    def _transcribe(self, wav_path, prompt="", mode=None, require_quality=False):
        began = time.perf_counter()
        raw, duration = self._read_wav(wav_path)
        import numpy as np
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        if samples.size:
            samples -= samples.mean()  # Ignore a constant microphone DC offset.
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        result = {
            "text": "", "segments": [], "language": None, "duration": round(duration, 4),
            "elapsed": 0.0, "device": self._device or self.status()["device"],
            "rms": round(rms, 6), "warning": self._warning, "reason": None,
        }
        if duration < 0.1 or (rms < 0.00035 and peak < 0.002):
            result["reason"] = "too_short" if duration < 0.1 else "no_signal"
            result["elapsed"] = round(time.perf_counter() - began, 3)
            return result
        prompt = str(prompt or "").replace("\x00", " ").strip()[:200]
        with self._lock:
            self._last_error = None
            try:
                self._ensure_model(mode, require_quality)
                decoded, info = self._decode_safely(samples, prompt, require_quality)
                accepted, suspected_hallucinations = self._filter_segments(decoded, duration)
                retry_without_prompt = bool(prompt and suspected_hallucinations)
                recovered = False
                if retry_without_prompt:
                    # Broad glossary hints can provoke repetition on otherwise
                    # clear audio. Make exactly one prompt-free attempt, still
                    # under the same lock and with the exact same safeguards.
                    decoded, info = self._decode_safely(samples, "", require_quality)
                    accepted, final_suspected = self._filter_segments(decoded, duration)
                    suspected_hallucinations += final_suspected
                    recovered = bool(accepted) and final_suspected == 0
                    for segment in accepted:
                        segment["needs_review"] = True
                result.update(
                    text=" ".join(segment["text"] for segment in accepted), segments=accepted,
                    language=getattr(info, "language", None), device=self._device,
                    compute_type=self._compute_type, warning=self.status()["warning"],
                    requested_mode=self.requested_mode,
                    effective_mode="quality" if self._device == "cuda" else "battery",
                    model_name=self._active_model_name,
                    reason=None if accepted else ("suspected_hallucination" if suspected_hallucinations else "no_speech"),
                    discarded_suspected_hallucinations=suspected_hallucinations,
                    retry_without_prompt=retry_without_prompt,
                    recovered_from_hallucination=recovered,
                )
                if suspected_hallucinations:
                    recovery_warning = (
                        "首次识别出现疑似重复，已移除术语提示重试一次；请回听核对恢复的文字。"
                        if recovered else
                        "检测到疑似重复识别，已排除可疑文字；请回听该段音频核对。"
                    )
                    result["warning"] = " ".join(filter(None, (result["warning"], recovery_warning)))
            except Exception as exc:
                self._last_error = str(exc)
                if isinstance(exc, ASRError):
                    raise
                raise ASRError(f"本地转写失败：{exc}") from exc
            finally:
                self._last_elapsed = round(time.perf_counter() - began, 3)
                result["elapsed"] = self._last_elapsed
        return result
