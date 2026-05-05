# Changelog

## v104
- Diagnose decrypted_data vs data.opus: save BOTH `packet.decrypted_data` and `data.opus` as separate `.bin` files for each packet (_decrypted.bin and _raw_opus.bin suffixes). Info.txt now includes comparison showing whether the two sources are identical or different — critical for determining if `decrypted_data` exposes the actual decrypted Opus payload or the pre-decryption ciphertext.

## v103
- Instrumentation: simultaneous packet+WAV capture — STTSink now tracks which saved `.bin` Opus packets contribute to each utterance via `_utt_packets` dict. On silence/flush, saves `stt_utterance_packets.json` manifest mapping packet filenames to the PCM WAV. Enables offline correlation: decode the same packets 4 ways (discord.opus.Decoder fresh, direct libopus 48k stereo, direct libopus 16k mono, bot pipeline) and compare pitch autocorrelation/spectral metrics to determine root cause of robotic audio.
- New analysis script: `analyze_correlated_capture.py` — server-side tool that reads the manifest, loads the live WAV, decodes packets 4 ways, computes pitch/spectral/RMS/ZCR/LR-corr metrics, transcribes all with Whisper medium, and prints a comparison table answering 4 key questions about decode-path integrity.

## v102
- Fix STT hallucination: remove tanh soft-clip from `_normalize_audio()` — testing with large-v3 showed that pure linear normalization changed transcription from "Thanks for watching!" (hallucination, no_speech=0.696) to "Thank you." (no_speech=0.759), and at RMS=0.3 gave no_speech=0.676. The tanh soft-clip was distorting the audio in a way that pushed Whisper toward its hallucination mode.
- Fix STT normalization: increase RMS target from 0.12 to 0.25 — bring quiet Opus-decoded speech further into Whisper's effective input range. Combined with linear-only normalization, higher RMS gives Whisper more signal to work with.
- Fix STT threshold: revert `no_speech_threshold` from 0.1 back to 0.6 (Whisper default) — the aggressive 0.1 setting was discarding valid speech segments. The original rationale (CELT NB narrowband audio scores low on speech detection) was correct, but the aggressive threshold was making things worse, not better.

## v101
- Fix v100 regression: the 15-tap triangular FIR low-pass filter (-3dB at ~3200Hz) destroyed the 2000-4000Hz frequency band (35%→9.2%), removing the consonant/sibilant information that Whisper needs for phoneme discrimination on CELT NB Opus audio. Replaced with simple decimation `mono[::3]` — since CELT NB has negligible energy above 8kHz (0.2% in 4-8kHz range), no anti-aliasing filter is needed. This replicates the same algorithm as `audioop.ratecv` but avoids the Python 3.12 C implementation bugs.

## v100
- Fix STT Whisper hallucination on clean audio: replace `audioop.ratecv` (linear interpolation without anti-aliasing) with numpy FIR low-pass filter + 3:1 decimation. `audioop.ratecv` on Python 3.12+ produces spike artifacts and stair-step distortion, causing all speech to be misclassified as noise/whisper by the Whisper model. The new numpy-based pipeline applies a 15-tap triangular anti-alias filter (~7kHz cutoff) before decimation, producing clean 16kHz output that Whisper can transcribe correctly.
- Replace `audioop.tomono` with numpy `mean()` channel mixing for consistency (both now use numpy instead of mixing audioop and numpy).

## v99
- Fix STT spectral distortion: remove pre-emphasis filter (α=0.97) — it was added in v97 to compensate for gain-induced spectral distortion, but with gain removed in v98 it now actively destroys the already-limited fundamental frequency (0-500Hz) content of CELT NB audio and further amplifies the 2-4kHz region, causing Whisper to see 83% of energy in 2-4kHz (vs ~20% for natural speech). Without pre-emphasis the raw spectral distribution is already unusual due to CELT NB narrowband encoding; adding pre-emphasis made it worse.
- Fix STT Whisper thresholds for CELT NB: raise `compression_ratio_threshold` 2.0→2.4 (CELT NB spectrally narrow audio can look "over-compressed" to Whisper's internal metrics); lower `no_speech_threshold` 0.3→0.1 (CELT NB audio scores low on Whisper's speech probability detector due to missing high-frequency content above 4kHz)

## v98
- Fix STT audio distortion: remove all hardware Opus decoder gain (set_gain calls) — set_gain() takes dB not amplitude multiplier (set_gain(8) = 2.51x not 8x), and packets with natural RMS=31485 (96% of max) were being hard-clipped even at low gain values. Software RMS normalization already targets the correct Whisper input level — hardware gain only added irreversible clipping distortion.

## v97
- Fix STT audio clipping: reduce Opus decoder gain 8→4; gain=8 was clipping on loud frames (peak=32768, 400 clipped samples/chunk) adding harmonic distortion that degrades Whisper recognition
- Fix STT spectral imbalance: add pre-emphasis filter (α=0.97) after 16kHz resampling; Discord audio arrives with ~12% sub-100Hz energy and only ~1% sibilance — pre-emphasis boosts above 300Hz to match Whisper's training distribution
- Fix STT hallucination filter: switch from exact-match to substring-match so variants like "I'll see you next time" are caught; expand list with common Whisper hallucination phrases

## v96
- Fix STT audio volume: call `decoder.set_gain(8)` on every Opus decoder (initial and replacement) — the default gain=1 produces PCM RMS ~2400, far below Whisper's effective range; gain=8 raises it to ~4700 without clipping
- Fix STT normalization: replace peak-based normalization with RMS-based normalization (`target_rms=0.12`) using soft-clip (tanh) — preserves speech-to-noise ratio better than peak norm when audio has occasional loud transients
- Fix STT Whisper thresholds: lower `no_speech_threshold` 0.6→0.3 and `log_prob_threshold` -1.0→-2.0 to accept quieter speech segments that the previous thresholds discarded as silence

## v95
- Fix STT speech detection: replace static RMS threshold with adaptive noise-floor tracking — dynamically adjusts based on observed background noise, preventing quiet speech from being gated out
- Fix STT audio levels: add `_normalize_audio()` to normalize peak amplitude to 0.80 before passing to Whisper — ensures consistent volume regardless of mic distance or speaking volume
- Fix STT transcription determinism: set `temperature=0.0` (greedy decoding) to reduce hallucinations and produce more consistent results
- Fix STT utterance loss on disconnect: add `flush()` method to `STTSink` — transcribes any buffered audio when the bot leaves the voice channel instead of discarding it
- Fix STT debugging: log per-utterance stats (duration, frame count, peak RMS, average RMS, noise floor) for diagnosing detection issues
- Fix STT minimum utterance length: raise `MIN_SECS` from 0.4 to 0.6 to avoid transcribing short clicks/pops
- **FIX STT ROOT CAUSE**: Remove `audioop.ratecv` 48kHz→16kHz resampling — its linear interpolation creates stair-step artifacts that confuse Whisper's VAD model, causing it to discard all audio as noise. Pass native 48kHz audio directly and let Whisper's internal band-limited resampler handle downsampling.
- **FIX STT ROOT CAUSE**: Disable Whisper VAD filter (`vad_filter=False`) — the VAD model was removing 100% of audio segments because the resampling artifacts made speech sound like noise to the VAD classifier. Whisper's built-in `no_speech_threshold` and `log_prob_threshold` provide sufficient silence rejection without VAD.
- **DIAGNOSTIC**: Add per-packet debug logging to `STTSink.write()` — logs packet type (RTP/Silence/Fake), sequence number, timestamp, Opus byte size, and decoded PCM stats (RMS, min, max, zero count, max flat run) for every packet in the first 50, then every 100th packet thereafter. Helps diagnose clicking-sound issue by revealing which packets produce garbled audio.
- **DIAGNOSTIC**: Save raw decoded PCM as WAV (`/opt/discord-bot/stt_raw_pcm.wav`) on silence detection and flush — captures the exact stereo 48kHz PCM output of `discord.opus.Decoder.decode()` so we can hear what the Opus decoder actually produces before any resampling or Whisper processing.
- **FIX OPUS DECODER CRASH**: Stop destroying the Opus decoder on "corrupted stream" errors — ~60% of Opus packets from Discord's jitter buffer are corrupt (opus_len=255, duplicate timestamps). Previously, `self._decoders.pop(uid, None)` destroyed the decoder state on every error, preventing Opus's internal error concealment from recovering. Now we skip bad packets and let the decoder recover naturally.

## v94
- Fix `query_system(aspect='hardware')` — replace `sudo dmidecode` (requires root, unavailable to `discord-bot` user) with world-readable sysfs DMI files for motherboard info and `/proc/meminfo` for RAM capacity

## v93
- Fix `query_system(aspect='hardware')` — replace `sudo dmidecode` (requires root, unavailable to `discord-bot` user) with world-readable sysfs DMI files for motherboard info and `/proc/meminfo` for RAM capacity

## v91
- Fix STT aliasing: replace naive [::3] decimation with audioop.ratecv (stdlib linear-interp resampler) for 48kHz→16kHz — eliminates aliasing that made speech unintelligible to Whisper

## v90
- Fix STT transcription: replace WAV temp-file path (av/ffmpeg conversion produced empty segments) with direct numpy float32 mono 16kHz array passed to model.transcribe()
- PCM conversion: 16-bit LE stereo 48kHz → float32 → mono (L+R average) → 16kHz (decimate ×3)
- Add numpy>=1.24.0 to requirements.txt

## v89
- Fix STT silence timer: replace asyncio future cancellation with threading.Timer (reliable cancel from voice_recv thread)
- Fix Whisper model download: set download_root=/opt/discord-bot/models; create /home/discord-bot so hf_xet can write its cache
- Remove CUDA attempt for Whisper (bot venv lacks CUDA runtime); use CPU int8 directly
- Clean up STTSink: remove fallback opus extraction path, simplify write()

## v88
- Fix STT: replace fedirz/faster-whisper-server (Gradio UI, no REST API) with in-process faster-whisper
- WhisperModel loads lazily on first speech; uses CUDA float16 with CPU int8 fallback
- Transcription runs in a thread executor; temp WAV written/deleted per utterance

## v87
- Fix STT crash: return wants_opus=True to bypass voice_recv decoder (crashed on first bad packet)
- Decode Opus→PCM ourselves per-user with per-packet error handling; bad packets silently skipped

## v86
- Fix discord-ext-voice-recv version pin (package uses alpha versioning, latest is 0.5.2a179)

## v85
- Fix STT: use discord-ext-voice-recv instead of discord.sinks (not in discord.py stdlib)
- STTSink now subclasses voice_recv.AudioSink with correct write(user, data) signature
- Connect with VoiceRecvClient when ENABLE_STT=true
- RMS gate prevents silent frames from ever reaching Whisper or Claude

## v84
- Add STT voice input via faster-whisper-server (Docker, GPU, medium model)
- Custom STTSink buffers per-user PCM, fires transcription after 1.5s silence
- Voice transcripts fed to Claude; reply posted to text channel and spoken via TTS
- Fix libopus not loading automatically — now explicitly loaded at startup
- ENABLE_STT, STT_URL, STT_MODEL, STT_SILENCE_TIMEOUT_SECS, STT_RMS_THRESHOLD env vars

## v83
- Add local TTS voice pipeline via Kokoro-82M (Docker, GPU, OpenAI-compatible endpoint)
- Add `!join` / `!leave` voice channel commands
- Add `TTS_AUTO_JOIN_CHANNEL_ID` — bot auto-joins watched channel on user entry, leaves when empty
- Sentence splitting with markdown stripping; concurrent TTS fetch overlaps with playback
- 5-minute idle auto-disconnect

## v82
- Add `restart_container` tool: restart any whitelisted Docker container with the standard confirmed-first flow
- Update `DOCKER_LOG_CONTAINERS` default to include `excalidraw` and `excalidraw-room`

## v81
- Add LLM usage logging — every Claude API call is recorded (model, tokens, estimated cost, user message) in SQLite
- New `query_llm_usage` tool: ask the bot "how much did we spend last month?" or "how much did that last question cost?" — supports `recent`, `daily`, `monthly`, and `by_model` breakdowns
- Token counts and per-call cost now appear in bot logs at INFO level

## v80
- Fix Claude API 400 error when channel history contains embed-only messages with no text content

## v79
- Add `query_crawl_analytics` tool — opt-in (`ENABLE_CRAWL_ANALYTICS=true`) HTTP analytics endpoint with `summary` and `export` actions; token stored in `.env` via `CRAWL_ANALYTICS_TOKEN`

## v78
- Fix `launch_steam` sudoers mismatch — remove `setsid` from sudo call so the rule matches, add PATH to env

## v77
- Fix `launch_steam` running as wrong user — now runs as `genesis` via sudoers so Steam can access its own home directory

## v76
- Add `launch_steam` — launch Steam in Big Picture mode on the server's local display

## v75
- Add `query_steam` — list installed games with sizes and last-played dates, or show disk usage sorted by size
- Add `manage_steam` — remove a Steam game with confirmation (deletes folder + ACF manifest)

## v74
- Enforce changelog entry in pre-commit hook — commits are blocked until `## v{N}` exists in CHANGELOG.md

## v73
- Fix missing changelog in startup announcement (v71/v72 entries were never written)

## v72
- Consolidate 17 tools → 13 for cleaner Haiku routing: `query_system` replaces `query_system_health` + `query_storage` + `query_network`; `query_jenkins` replaces three separate Jenkins read tools

## v71
- Add CHANGELOG.md — startup announcement now includes latest changes
- Git tag created automatically on every commit (pushed with `git push`)

## v70
- Add CHANGELOG.md — startup announcement now includes latest changes
- Git tag created automatically on every commit (pushed with `git push`)

## v69
- Add `shutdown_steam` tool — shut down Steam on demand after gaming sessions

## v68
- Add `search_movies` to `query_jellyfin` — genre/mood recommendations now use Jellyfin metadata (genres, ratings, plot summaries) instead of filesystem filenames
