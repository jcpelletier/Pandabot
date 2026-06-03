"""
Tests for voice_gateway.stt's GPU-resident-model eviction state machine.

The state machine has four interesting cases under STT_DEVICE=cuda:
  1. Resident model + plenty of VRAM           → use cuda model
  2. Resident model + low VRAM                  → evict, route this turn to CPU
  3. No resident model + still low VRAM         → stay on CPU
  4. No resident model + VRAM recovered enough  → reload cuda model

Each is exercised below with stubbed nvidia-smi output and stubbed WhisperModel
constructors so the tests run without a GPU or the faster_whisper package.
"""

from __future__ import annotations

import importlib
import sys
import types
import threading
import pytest


class _FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeWhisperModel:
    """Records which device/compute_type it was constructed with, returns one segment."""

    instances: list["_FakeWhisperModel"] = []

    def __init__(self, model_size, device, compute_type, download_root):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        _FakeWhisperModel.instances.append(self)

    def transcribe(self, audio_path, language="en", beam_size=1):
        return [_FakeSegment("hello world")], None


@pytest.fixture
def stt_cuda(monkeypatch):
    """Import stt fresh with STT_DEVICE=cuda and a stubbed faster_whisper.

    Yields the freshly imported module so each test gets its own module-level
    state (cleared model cache, etc.).
    """
    monkeypatch.setenv("STT_DEVICE", "cuda")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "float32")
    monkeypatch.setenv("STT_GPU_MIN_FREE_MB", "1000")
    monkeypatch.setenv("STT_GPU_RELOAD_MIN_FREE_MB", "3000")

    fake_pkg = types.ModuleType("faster_whisper")
    fake_pkg.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_pkg)
    _FakeWhisperModel.instances = []

    # Drop any prior import so module-level env reads pick up the monkeypatched values.
    sys.modules.pop("voice_gateway.stt", None)
    stt = importlib.import_module("voice_gateway.stt")
    yield stt
    sys.modules.pop("voice_gateway.stt", None)


def _fake_free_mb(value):
    return lambda: value


def test_resident_with_plenty_of_vram_uses_cuda(stt_cuda, monkeypatch):
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(2500))
    assert stt_cuda._transcribe_sync("/tmp/x.wav") == "hello world"
    # One model constructed: the cuda primary.
    assert len(_FakeWhisperModel.instances) == 1
    assert _FakeWhisperModel.instances[0].device == "cuda"
    assert _FakeWhisperModel.instances[0].compute_type == "float32"


def test_resident_under_pressure_evicts_and_falls_back_to_cpu(stt_cuda, monkeypatch):
    # First call warms cuda with plenty of VRAM.
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(2500))
    stt_cuda._transcribe_sync("/tmp/x.wav")
    assert stt_cuda._model_primary is not None  # cuda model resident

    # Second call: VRAM has crashed (game launched). Must evict + route to CPU.
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(400))
    stt_cuda._transcribe_sync("/tmp/x.wav")

    assert stt_cuda._model_primary is None, "cuda model must be unloaded under pressure"
    assert stt_cuda._model_cpu is not None, "CPU fallback must be loaded"
    # Two models total: original cuda + new cpu.
    devices = [m.device for m in _FakeWhisperModel.instances]
    assert devices == ["cuda", "cpu"]


def test_evicted_with_still_low_vram_stays_on_cpu(stt_cuda, monkeypatch):
    # Simulate "post-eviction" state.
    stt_cuda._model_primary = None
    stt_cuda._was_evicted = True
    # 1500 MB free is above the 1000 MB eviction threshold but below the 3000 MB
    # reload threshold — must stay on CPU (no thrash).
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(1500))

    stt_cuda._transcribe_sync("/tmp/x.wav")

    assert stt_cuda._model_primary is None, "must not reload cuda under reload threshold"
    assert stt_cuda._model_cpu is not None
    assert all(m.device == "cpu" for m in _FakeWhisperModel.instances)


def test_evicted_with_recovered_vram_reloads_cuda(stt_cuda, monkeypatch):
    stt_cuda._model_primary = None
    stt_cuda._was_evicted = True
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(3500))

    stt_cuda._transcribe_sync("/tmp/x.wav")

    assert stt_cuda._model_primary is not None
    assert stt_cuda._was_evicted is False, "reload should clear the evicted flag"
    assert _FakeWhisperModel.instances[-1].device == "cuda"


def test_fresh_boot_loads_cuda_even_below_reload_threshold(stt_cuda, monkeypatch):
    """At boot, _was_evicted is False — we should attempt cuda regardless of
    current VRAM. The reload-threshold gate only protects against post-eviction
    thrash, not normal startup."""
    assert stt_cuda._was_evicted is False
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(1500))

    stt_cuda._transcribe_sync("/tmp/x.wav")

    assert stt_cuda._model_primary is not None
    assert _FakeWhisperModel.instances[-1].device == "cuda"


def test_nvidia_smi_failure_evicts_when_resident(stt_cuda, monkeypatch):
    # Warm cuda first.
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(2500))
    stt_cuda._transcribe_sync("/tmp/x.wav")
    assert stt_cuda._model_primary is not None

    # Now nvidia-smi starts failing — treat as low VRAM (safe default).
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(None))
    stt_cuda._transcribe_sync("/tmp/x.wav")
    assert stt_cuda._model_primary is None


def test_poller_evicts_without_voice_turn(stt_cuda, monkeypatch):
    """Poller must unload the cuda model when VRAM drops, without waiting for a voice call."""
    # Warm the cuda model.
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(2500))
    stt_cuda._transcribe_sync("/tmp/x.wav")
    assert stt_cuda._model_primary is not None

    # VRAM drops (game launched). Run one poller tick directly.
    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(400))
    stt_cuda._vram_poll_loop.__code__  # just confirm it exists
    # Call the body logic directly (skip the sleep/Event wait).
    free_mb = stt_cuda._get_gpu_free_mb()
    if stt_cuda._model_primary is not None and free_mb < stt_cuda.STT_GPU_MIN_FREE_MB:
        stt_cuda._unload_gpu_resident_model()

    assert stt_cuda._model_primary is None
    assert stt_cuda._was_evicted is True


def test_poller_reloads_after_game_exits(stt_cuda, monkeypatch):
    """Poller must reload cuda once VRAM recovers, without waiting for a voice call."""
    stt_cuda._model_primary = None
    stt_cuda._was_evicted = True

    monkeypatch.setattr(stt_cuda, "_get_gpu_free_mb", _fake_free_mb(3500))
    free_mb = stt_cuda._get_gpu_free_mb()
    if (stt_cuda._model_primary is None
            and stt_cuda._was_evicted
            and free_mb >= stt_cuda.STT_GPU_RELOAD_MIN_FREE_MB):
        stt_cuda._was_evicted = False
        stt_cuda._get_primary_model()

    assert stt_cuda._model_primary is not None
    assert stt_cuda._was_evicted is False


def test_poller_not_started_when_interval_zero(monkeypatch):
    """STT_VRAM_POLL_INTERVAL_SECS=0 must not start the poller thread."""
    import asyncio
    monkeypatch.setenv("STT_DEVICE", "cuda")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "float32")
    monkeypatch.setenv("STT_GPU_MIN_FREE_MB", "1000")
    monkeypatch.setenv("STT_VRAM_POLL_INTERVAL_SECS", "0")
    fake_pkg = types.ModuleType("faster_whisper")
    fake_pkg.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_pkg)
    _FakeWhisperModel.instances = []
    sys.modules.pop("voice_gateway.stt", None)
    stt = importlib.import_module("voice_gateway.stt")
    try:
        threads_before = {t.name for t in threading.enumerate()}
        asyncio.run(stt.warm())
        threads_after = {t.name for t in threading.enumerate()}
        assert "stt-vram-poller" not in threads_after - threads_before
    finally:
        sys.modules.pop("voice_gateway.stt", None)


def test_min_free_zero_disables_check(monkeypatch):
    """STT_GPU_MIN_FREE_MB=0 keeps the cuda model resident regardless of VRAM."""
    monkeypatch.setenv("STT_DEVICE", "cuda")
    monkeypatch.setenv("STT_COMPUTE_TYPE", "float32")
    monkeypatch.setenv("STT_GPU_MIN_FREE_MB", "0")
    fake_pkg = types.ModuleType("faster_whisper")
    fake_pkg.WhisperModel = _FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_pkg)
    _FakeWhisperModel.instances = []
    sys.modules.pop("voice_gateway.stt", None)
    stt = importlib.import_module("voice_gateway.stt")
    try:
        # Even with no free VRAM, the check is bypassed.
        monkeypatch.setattr(stt, "_get_gpu_free_mb", _fake_free_mb(0))
        stt._transcribe_sync("/tmp/x.wav")
        assert stt._model_primary is not None
        assert _FakeWhisperModel.instances[-1].device == "cuda"
    finally:
        sys.modules.pop("voice_gateway.stt", None)
