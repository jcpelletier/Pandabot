#!/usr/bin/env python3
"""
test_decoder_state.py — Persistent vs Per-Packet Decoder Comparison (Option H)

Tests whether the decoder internal state corruption hypothesis explains the 
garbage PCM output. Decodes saved .bin Opus packets with:

  Method A (PERSISTENT):  One discord.opus.Decoder instance for all packets
  Method B (PER-PACKET):  Fresh discord.opus.Decoder per packet (current bot behavior)
  Method C (PERSISTENT_CTYPES): One libopus ctypes decoder instance (bypasses discord.py)

If Method A or C produces significantly cleaner audio (higher RMS, lower flat runs,
Whisper transcribes something), the decoder-internal state reset is confirmed as
the root cause.

Usage:
    cd /opt/discord-bot
    python3 test_decoder_state.py [--pkt-dir /opt/discord-bot/stt_packets]
"""

import os, sys, struct, json, time, ctypes, glob
import numpy as np

# ─── Configuration ────────────────────────────────────────────────────
PKT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stt_packets")
if not os.path.isdir(PKT_DIR):
    # Fall back to CWD
    PKT_DIR = "."
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_decoder_state_output")

# ─── Load libopus via ctypes (bypasses discord.py) ────────────────────
def _load_libopus():
    """Locate and load libopus shared library."""
    search_paths = [
        "libopus.so.0", "libopus.so",
        "/usr/lib/x86_64-linux-gnu/libopus.so.0",
        "/usr/lib/libopus.so.0",
        "/usr/local/lib/libopus.so.0",
    ]
    for lib in search_paths:
        try:
            return ctypes.cdll.LoadLibrary(lib)
        except OSError:
            continue
    # Try finding via ldconfig
    try:
        import subprocess
        out = subprocess.check_output(["ldconfig", "-p"], text=True)
        for line in out.splitlines():
            if "libopus.so" in line:
                path = line.split()[-1]
                return ctypes.cdll.LoadLibrary(path)
    except Exception:
        pass
    raise RuntimeError("Cannot find libopus. Install: sudo apt install libopus0")

_opus = _load_libopus()

OPUS_APPLICATION_AUDIO = 2049
OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_LOWDELAY = 2051

_opus.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
_opus.opus_decoder_create.restype = ctypes.c_void_p
_opus.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
_opus.opus_decoder_destroy.restype = None
_opus.opus_decode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_int32,
                                ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
_opus.opus_decode.restype = ctypes.c_int
_opus.opus_encoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
_opus.opus_encoder_create.restype = ctypes.c_void_p
_opus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
_opus.opus_encoder_destroy.restype = None
_opus.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                ctypes.POINTER(ctypes.c_uint8), ctypes.c_int32]
_opus.opus_encode.restype = ctypes.c_int32

def ctypes_decoder_create(rate=48000, channels=2):
    err = ctypes.c_int()
    st = _opus.opus_decoder_create(rate, channels, ctypes.byref(err))
    if err.value != 0:
        raise RuntimeError(f"opus_decoder_create failed: error={err.value}")
    return st

def ctypes_decoder_destroy(st):
    _opus.opus_decoder_destroy(st)

def ctypes_decode(st, data, rate=48000, channels=2):
    """Decode a single Opus packet via ctypes libopus. Returns PCM bytes."""
    frame_size = rate * 120 // 1000  # max 120ms frames
    out_size = frame_size * channels
    out_buf = (ctypes.c_int16 * out_size)()
    data_arr = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    n = _opus.opus_decode(st, data_arr, len(data), out_buf, frame_size, 0)
    if n < 0:
        raise RuntimeError(f"opus_decode failed: {n}")
    return bytes(out_buf[:n * channels * 2])

# ─── Load packets ─────────────────────────────────────────────────────
def load_packets(pkt_dir: str) -> list[tuple[str, bytes]]:
    """Return sorted list of (filename, raw_opus_bytes) from pkt_dir."""
    bins = sorted(glob.glob(os.path.join(pkt_dir, "pkt_*.bin")))
    if not bins:
        # Also check local directory for pre-downloaded packets
        bins = sorted(glob.glob("pkt_*.bin"))
    if not bins:
        raise FileNotFoundError(f"No pkt_*.bin files found in {pkt_dir} or .")
    
    packets = []
    for path in bins:
        with open(path, 'rb') as f:
            data = f.read()
        fname = os.path.basename(path)
        packets.append((fname, data))
        print(f"  Loaded {fname}: {len(data)} bytes")
    
    print(f"\n  Total: {len(packets)} packets loaded")
    return packets

# ─── PCM analysis ─────────────────────────────────────────────────────
def compute_pitch_autocorr(samples: np.ndarray, rate: int) -> float:
    """Normalized pitch autocorrelation at lag matching ~100-300Hz."""
    if len(samples) < rate // 50:
        return 0.0
    lo = int(rate / 300)
    hi = int(rate / 80)
    if hi >= len(samples):
        hi = len(samples) // 2
    if lo >= hi:
        return 0.0
    s = samples - samples.mean()
    s_std = s.std()
    if s_std < 1e-10:
        return 0.0
    s_norm = s / s_std
    corr = np.correlate(s_norm, s_norm, mode='full')
    mid = len(s_norm) - 1
    segment = corr[mid + lo:mid + hi + 1]
    seg_max = segment.max() / len(s_norm)
    return float(seg_max)

def spectral_pct(samples: np.ndarray, rate: int, bands: list[tuple[int, int]]) -> list[float]:
    """Percentage of spectral energy in each frequency band."""
    n = len(samples)
    if n < 2:
        return [0.0] * len(bands)
    fft_ = np.fft.rfft(samples * np.hanning(n))
    psd = np.abs(fft_)**2
    freqs = np.fft.rfftfreq(n, 1 / rate)
    total = psd.sum()
    if total == 0:
        return [0.0] * len(bands)
    pcts = []
    for lo, hi in bands:
        mask = (freqs >= lo) & (freqs < hi)
        pcts.append(round(float(psd[mask].sum() / total * 100), 1))
    return pcts

def analyze_pcm(pcm: bytes, rate: int, channels: int, label: str) -> dict:
    """Compute metrics for a PCM buffer (same as analyze_correlated_capture.py)."""
    arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float64)
    if channels == 2:
        L = arr[0::2]
        R = arr[1::2]
    else:
        L = arr
        R = arr
    dur = len(L) / rate
    rms = float(np.sqrt(np.mean(L ** 2)))
    peak = float(np.max(np.abs(L)))
    zcr = float(np.sum(np.abs(np.diff(np.sign(L)))) / (2 * len(L))) * rate if len(L) > 1 else 0.0
    pitch = compute_pitch_autocorr(L, rate)
    bands = [(0, 500), (500, 2000), (2000, 4000), (4000, 8000), (8000, 24000)]
    spec = spectral_pct(L, rate, bands)
    flat_run = 0
    max_flat = 0
    prev = None
    for s in arr:
        if prev is not None and s == prev:
            flat_run += 1
        else:
            flat_run = 0
        max_flat = max(max_flat, flat_run)
        prev = s
    return {
        "label": label,
        "duration": round(dur, 3),
        "rms": round(rms, 1),
        "peak": int(peak),
        "pitch_autocorr": round(pitch, 4),
        "zcr": round(zcr, 1),
        "spectral_0_500": round(spec[0], 1),
        "spectral_500_2k": round(spec[1], 1),
        "spectral_2k_4k": round(spec[2], 1),
        "spectral_4k_8k": round(spec[3], 1),
        "spectral_8k_24k": round(spec[4], 1),
        "max_flat_run": int(max_flat),
        "lr_corr": round(float(np.corrcoef(L, R)[0, 1]) if channels == 2 else 1.0, 4),
    }

def save_wav(path: str, pcm: bytes, rate: int, channels: int):
    """Save PCM bytes as WAV file."""
    import wave
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    print(f"  Wrote {path} ({len(pcm)} bytes, {len(pcm)/(rate*channels*2):.2f}s)")

def whisper_transcribe(pcm_48k_stereo: bytes) -> dict:
    """Run Whisper transcription on PCM (same pipeline as bot)."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return {"text": "(faster-whisper not installed)", "no_speech": 1.0, "avg_logprob": -99.0}
    
    raw = np.frombuffer(pcm_48k_stereo, dtype=np.int16).reshape(-1, 2)
    mono = raw.mean(axis=1, dtype=np.float64)
    decimated = mono[::3]
    samples = (decimated / 32768.0).astype(np.float32)
    rms = float(np.sqrt(np.mean(samples ** 2)))
    if rms >= 0.001:
        gain = 0.12 / rms
        scaled = samples * gain
        samples = np.tanh(scaled * 1.5) / 1.5
    
    model = WhisperModel("medium", device="cpu", compute_type="int8")
    segments, info = model.transcribe(
        samples, language="en", beam_size=5, vad_filter=False,
        temperature=0.0, compression_ratio_threshold=2.4,
        log_prob_threshold=-2.0, no_speech_threshold=0.1,
    )
    result = {"text": "", "no_speech": 0.0, "avg_logprob": 0.0, "lang_prob": 0.0}
    texts = []
    for seg in segments:
        texts.append(seg.text)
        result["no_speech"] = seg.no_speech_prob
        result["avg_logprob"] = seg.avg_logprob
    result["text"] = " ".join(texts)
    result["lang_prob"] = info.language_probability
    return result

# ─── TOC byte analysis ────────────────────────────────────────────────
CONFIG_NAMES = {
    0: "SILK NB 20ms", 1: "SILK NB 20ms", 2: "SILK NB 20ms", 3: "SILK NB 20ms",
    4: "SILK MB 2.5ms", 5: "SILK MB 5ms", 6: "SILK MB 10ms", 7: "SILK MB 20ms",
    8: "SILK WB 10ms", 9: "SILK WB 20ms", 10: "SILK WB 20ms", 11: "SILK WB 20ms",
    12: "Hybrid SWB", 13: "Hybrid SWB", 14: "Hybrid FB", 15: "Hybrid FB",
    16: "CELT NB 2.5ms", 17: "CELT NB 5ms", 18: "CELT NB 10ms", 19: "CELT NB 20ms",
    20: "CELT WB 2.5ms", 21: "CELT WB 5ms", 22: "CELT WB 10ms", 23: "CELT WB 20ms",
    24: "CELT SWB 5ms", 25: "CELT SWB 10ms", 26: "CELT SWB 20ms",
    27: "CELT FB 2.5ms", 28: "CELT FB 5ms", 29: "CELT FB 10ms", 30: "CELT FB 10ms",
    31: "CELT FB 20ms",
}
FRAME_VALS = {0: "1 frame", 1: "2 frames", 2: "2 frames diff", 3: "1+redundancy"}

def describe_toc(b: int) -> str:
    config = (b >> 3) & 0x1f
    stereo = (b >> 2) & 0x01
    frames_val = b & 0x03
    mode_name = CONFIG_NAMES.get(config, f"UNKNOWN config={config}")
    frames_str = FRAME_VALS.get(frames_val, f"frames={frames_val}")
    return f"config={config:2d} {mode_name:20s} stereo={stereo} {frames_str}"

# ═══════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Decoder state comparison test")
    parser.add_argument("--pkt-dir", default=PKT_DIR, help="Directory containing pkt_*.bin files")
    parser.add_argument("--no-whisper", action="store_true", help="Skip Whisper transcription")
    args = parser.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)

    # ── Step 1: Load packets ──────────────────────────────────────────
    print("=" * 70)
    print("TEST: Persistent vs Per-Packet Decoder Comparison")
    print("=" * 70)
    print(f"\nLoading packets from: {args.pkt_dir}")
    packets = load_packets(args.pkt_dir)
    
    # ── Step 2: TOC analysis ──────────────────────────────────────────
    print("\n" + "-" * 70)
    print("TOC Byte Analysis")
    print("-" * 70)
    for fname, data in packets:
        if len(data) == 0:
            continue
        toc = data[0]
        desc = describe_toc(toc)
        print(f"  {fname}: 0x{toc:02x} -> {desc} [{len(data)}B]")

    # ── Step 3: Method A — Persistent discord.opus.Decoder ────────────
    print("\n" + "-" * 70)
    print("METHOD A: Persistent discord.opus.Decoder")
    print("-" * 70)
    
    pcm_a = b""
    try:
        import discord
        dec_a = discord.opus.Decoder()
        dec_a_fails = 0
        for fname, data in packets:
            try:
                pcm = dec_a.decode(data, fec=False)
                pcm_a += pcm
            except Exception as e:
                print(f"  DECODE ERROR [{fname}]: {e}")
                dec_a_fails += 1
        print(f"  Decoded: {len(packets)-dec_a_fails}/{len(packets)} packets -> {len(pcm_a)} bytes")
        
        if pcm_a and len(pcm_a) > 100:
            metrics_a = analyze_pcm(pcm_a, 48000, 2, "Method A (persistent discord.opus)")
            fmt_a = (f"  RMS={metrics_a['rms']:.1f}  peak={metrics_a['peak']}  "
                     f"flat_run={metrics_a['max_flat_run']}  pitch={metrics_a['pitch_autocorr']:.4f}")
            print(fmt_a)
            save_wav(os.path.join(OUT_DIR, "method_a_persistent_discord.wav"), pcm_a, 48000, 2)
    except ImportError:
        print("  discord.py not available — skipping Method A")
        metrics_a = None
    except Exception as e:
        print(f"  Method A failed: {e}")
        metrics_a = None

    # ── Step 4: Method B — Per-packet discord.opus.Decoder ────────────
    print("\n" + "-" * 70)
    print("METHOD B: Per-Packet discord.opus.Decoder (fresh decoder each packet)")
    print("-" * 70)
    
    pcm_b = b""
    try:
        import discord
        dec_b_fails = 0
        for fname, data in packets:
            try:
                dec_b = discord.opus.Decoder()
                pcm = dec_b.decode(data, fec=False)
                pcm_b += pcm
            except Exception as e:
                print(f"  DECODE ERROR [{fname}]: {e}")
                dec_b_fails += 1
        print(f"  Decoded: {len(packets)-dec_b_fails}/{len(packets)} packets -> {len(pcm_b)} bytes")
        
        if pcm_b and len(pcm_b) > 100:
            metrics_b = analyze_pcm(pcm_b, 48000, 2, "Method B (per-packet discord.opus)")
            fmt_b = (f"  RMS={metrics_b['rms']:.1f}  peak={metrics_b['peak']}  "
                     f"flat_run={metrics_b['max_flat_run']}  pitch={metrics_b['pitch_autocorr']:.4f}")
            print(fmt_b)
            save_wav(os.path.join(OUT_DIR, "method_b_per_packet_discord.wav"), pcm_b, 48000, 2)
    except ImportError:
        print("  discord.py not available — skipping Method B")
        metrics_b = None
    except Exception as e:
        print(f"  Method B failed: {e}")
        metrics_b = None

    # ── Step 5: Method C — Persistent ctypes libopus decoder ──────────
    print("\n" + "-" * 70)
    print("METHOD C: Persistent ctypes libopus decoder (bypasses discord.py)")
    print("-" * 70)
    
    pcm_c = b""
    try:
        st_c = ctypes_decoder_create(48000, 2)
        dec_c_fails = 0
        for fname, data in packets:
            try:
                pcm = ctypes_decode(st_c, data, 48000, 2)
                pcm_c += pcm
            except Exception as e:
                print(f"  DECODE ERROR [{fname}]: {e}")
                dec_c_fails += 1
        ctypes_decoder_destroy(st_c)
        print(f"  Decoded: {len(packets)-dec_c_fails}/{len(packets)} packets -> {len(pcm_c)} bytes")
        
        if pcm_c and len(pcm_c) > 100:
            metrics_c = analyze_pcm(pcm_c, 48000, 2, "Method C (persistent ctypes)")
            fmt_c = (f"  RMS={metrics_c['rms']:.1f}  peak={metrics_c['peak']}  "
                     f"flat_run={metrics_c['max_flat_run']}  pitch={metrics_c['pitch_autocorr']:.4f}")
            print(fmt_c)
            save_wav(os.path.join(OUT_DIR, "method_c_persistent_ctypes.wav"), pcm_c, 48000, 2)
    except Exception as e:
        print(f"  Method C failed: {e}")
        metrics_c = None

    # ── Step 6: Method D — Per-packet ctypes libopus decoder ──────────
    print("\n" + "-" * 70)
    print("METHOD D: Per-Packet ctypes libopus decoder")
    print("-" * 70)
    
    pcm_d = b""
    try:
        dec_d_fails = 0
        for fname, data in packets:
            try:
                st_d = ctypes_decoder_create(48000, 2)
                pcm = ctypes_decode(st_d, data, 48000, 2)
                ctypes_decoder_destroy(st_d)
                pcm_d += pcm
            except Exception as e:
                print(f"  DECODE ERROR [{fname}]: {e}")
                dec_d_fails += 1
        print(f"  Decoded: {len(packets)-dec_d_fails}/{len(packets)} packets -> {len(pcm_d)} bytes")
        
        if pcm_d and len(pcm_d) > 100:
            metrics_d = analyze_pcm(pcm_d, 48000, 2, "Method D (per-packet ctypes)")
            fmt_d = (f"  RMS={metrics_d['rms']:.1f}  peak={metrics_d['peak']}  "
                     f"flat_run={metrics_d['max_flat_run']}  pitch={metrics_d['pitch_autocorr']:.4f}")
            print(fmt_d)
            save_wav(os.path.join(OUT_DIR, "method_d_per_packet_ctypes.wav"), pcm_d, 48000, 2)
    except Exception as e:
        print(f"  Method D failed: {e}")
        metrics_d = None

    # ── Step 7: Re-encode test (Option I) ─────────────────────────────
    print("\n" + "-" * 70)
    print("METHOD E: Re-encode + TOC compare (Option I)")
    print("-" * 70)
    
    if pcm_a and len(pcm_a) >= 3840:  # 20ms at 48kHz stereo
        try:
            # Re-encode decoded PCM back to Opus
            st_enc = _opus.opus_encoder_create(48000, 2, OPUS_APPLICATION_AUDIO, ctypes.byref(ctypes.c_int()))
            enc_buf = (ctypes.c_uint8 * 4000)()
            pcm_arr = (ctypes.c_int16 * (len(pcm_a) // 2)).from_buffer_copy(pcm_a)
            
            frame_size = 960  # 20ms at 48kHz
            n_frames = (len(pcm_a) // 2) // (frame_size * 2)  # * 2 for stereo
            orig_tocs = [data[0] for _, data in packets if len(data) > 0]
            reenc_tocs = []
            
            for i in range(min(n_frames, len(packets))):
                offset = i * frame_size * 2
                src = pcm_arr[offset:offset + frame_size * 2]
                n_bytes = _opus.opus_encode(st_enc, src, frame_size, enc_buf, 4000)
                if n_bytes > 0:
                    reenc_tocs.append(enc_buf[0])
            
            _opus.opus_encoder_destroy(st_enc)
            
            print(f"  Original TOC bytes:   [{', '.join(f'0x{b:02x}' for b in orig_tocs)}]")
            print(f"  Re-encoded TOC bytes: [{', '.join(f'0x{b:02x}' for b in reenc_tocs)}]")
            
            toc_matches = sum(1 for o, r in zip(orig_tocs, reenc_tocs) if o == r)
            print(f"  TOC match: {toc_matches}/{min(len(orig_tocs), len(reenc_tocs))}")
            
            if toc_matches < len(orig_tocs) and len(reenc_tocs) >= len(orig_tocs):
                print("\n  ⚠️  TOC BYTES DIFFER! Re-encoded packets have different config than originals.")
                print("     This suggests the decoded PCM has different characteristics than")
                print("     what the encoder would produce from the original speech.")
                print("     -> Packet data may be corrupted, OR decoder output is degraded.")
        except Exception as e:
            print(f"  Re-encode test failed: {e}")
    else:
        print("  Skipped (no PCM data from Method A)")

    # ── Step 8: Whisper Transcription ─────────────────────────────────
    if not args.no_whisper:
        print("\n" + "-" * 70)
        print("Whisper Transcription (all methods)")
        print("-" * 70)
        
        for label, pcm_data in [("Method A (persistent discord)", pcm_a),
                                 ("Method B (per-packet discord)", pcm_b),
                                 ("Method C (persistent ctypes)", pcm_c),
                                 ("Method D (per-packet ctypes)", pcm_d)]:
            if pcm_data and len(pcm_data) > 1000:
                try:
                    result = whisper_transcribe(pcm_data)
                    print(f"\n  {label}:")
                    print(f"    Text:      {result['text']!r}")
                    print(f"    No speech: {result['no_speech']:.3f}")
                    print(f"    Avg logp:  {result['avg_logprob']:.3f}")
                except Exception as e:
                    print(f"\n  {label}: Whisper error: {e}")
            else:
                print(f"\n  {label}: No data to transcribe")

    # ── Step 9: Summary comparison ────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY: Method Comparison")
    print("=" * 70)
    
    all_metrics = []
    for m, label in [(metrics_a, "A (persistent discord)"),
                      (metrics_b, "B (per-packet discord)"),
                      (metrics_c, "C (persistent ctypes)"),
                      (metrics_d, "D (per-packet ctypes)")]:
        if m is not None:
            all_metrics.append(m)
            print(f"\n  {label}:")
            print(f"    RMS={m['rms']:.1f}  peak={m['peak']}  flat_run={m['max_flat_run']}")
            print(f"    pitch={m['pitch_autocorr']:.4f}  zcr={m['zcr']:.1f}")
            print(f"    spectral: <500Hz={m['spectral_0_500']}%  500-2k={m['spectral_500_2k']}%  2k-4k={m['spectral_2k_4k']}%")
            print(f"    lr_corr={m['lr_corr']:.4f}  duration={m['duration']:.2f}s")
    
    if all_metrics:
        # Check if persistent decoders produce better metrics
        print("\n  ── Hypothesis Evaluation ──")
        
        a_rms = metrics_a['rms'] if metrics_a else 0
        b_rms = metrics_b['rms'] if metrics_b else 0
        c_rms = metrics_c['rms'] if metrics_c else 0
        d_rms = metrics_d['rms'] if metrics_d else 0
        
        a_pitch = metrics_a['pitch_autocorr'] if metrics_a else 0
        b_pitch = metrics_b['pitch_autocorr'] if metrics_b else 0
        c_pitch = metrics_c['pitch_autocorr'] if metrics_c else 0
        d_pitch = metrics_d['pitch_autocorr'] if metrics_d else 0
        
        a_flat = metrics_a['max_flat_run'] if metrics_a else 999
        b_flat = metrics_b['max_flat_run'] if metrics_b else 999
        c_flat = metrics_c['max_flat_run'] if metrics_c else 999
        d_flat = metrics_d['max_flat_run'] if metrics_d else 999
        
        # Compare persistent vs per-packet for discord.py
        if metrics_a and metrics_b:
            print(f"\n  discord.opus.Decoder: Persistent(RMS={a_rms:.0f}, pitch={a_pitch:.4f}, flat={a_flat})")
            print(f"                         vs Per-packet(RMS={b_rms:.0f}, pitch={b_pitch:.4f}, flat={b_flat})")
            if a_rms > b_rms * 1.2 and a_flat < b_flat * 0.8:
                print("  ✅ PERSISTENT is BETTER — decoder state reset IS the root cause!")
            elif abs(a_rms - b_rms) < a_rms * 0.1 and abs(a_flat - b_flat) < 10:
                print("  ⚠️ Persistent ≈ Per-packet — decoder reset is NOT the cause")
            else:
                print("  ⚠️ Inconclusive — metrics differ but not clearly better")
        
        # Compare persistent vs per-packet for ctypes
        if metrics_c and metrics_d:
            print(f"\n  ctypes libopus:        Persistent(RMS={c_rms:.0f}, pitch={c_pitch:.4f}, flat={c_flat})")
            print(f"                         vs Per-packet(RMS={d_rms:.0f}, pitch={d_pitch:.4f}, flat={d_flat})")
            if c_rms > d_rms * 1.2 and c_flat < d_flat * 0.8:
                print("  ✅ PERSISTENT is BETTER — decoder state reset IS the root cause!")
            elif abs(c_rms - d_rms) < c_rms * 0.1 and abs(c_flat - d_flat) < 10:
                print("  ⚠️ Persistent ≈ Per-packet — decoder reset is NOT the cause")
            else:
                print("  ⚠️ Inconclusive — metrics differ but not clearly better")
        
        # Compare discord.py vs ctypes (both persistent)
        if metrics_a and metrics_c:
            print(f"\n  Persistent: discord.py(RMS={a_rms:.0f}) vs ctypes(RMS={c_rms:.0f})")
            if abs(a_rms - c_rms) < max(a_rms, c_rms) * 0.15:
                print("  ✅ discord.py ≈ ctypes — discord.py bindings work correctly")
            else:
                print("  ⚠️ discord.py ≠ ctypes — possible binding issue!")
    
    print(f"\n  Output WAV files in: {OUT_DIR}/")
    print("=" * 70)
    print("To interpret results:")
    print("  - If persistent decoder produces CLEANER audio -> bot should use persistent decoder")
    print("  - If all methods produce identical garbage -> decoder state is NOT the issue")
    print("  - If ctypes persistent differs from discord persistent -> discord.py binding issue")
    print("=" * 70)
