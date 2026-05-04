# Changelog

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
