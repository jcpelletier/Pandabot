# Changelog

## v155
- Fix DAVE decryption: debounce reinit_dave_session at the VoiceConnectionState class level with a 1s window; two concurrent WebSocket connections were calling reinit within the same millisecond, sending two MLS key packages, causing the second welcome to overwrite the first and leave the remote user with no sender cryptor (NoValidCryptorFound manager_count=1)

## v154
- Add TTS_TRIGGER_BOT_IDS env var: comma-separated bot user IDs that are allowed to trigger auto-join/leave, same as human users (enables PandaBot-QA voice tests to drive Pandabot into the channel)

## v153
- DAVE diagnostics: log get_user_ids() on decrypt failure; monkey-patch reinit_dave_session to count/log each call; add 30s periodic session monitor after voice join to track when user_ids is populated

## v151
- Temporary: expose MLS/DAVE debug log via filtered handler to diagnose key exchange failure (remove after diagnosis)

## v150
- Stop TTS-speaking bot replies to text messages — voice replies are for STT-triggered responses only
- Fix SSRC race: when voice_recv passes user=None (SSRC not yet mapped), fall back to _ssrc_to_id lookup so first-burst audio isn't silently dropped
- Upgrade DAVE decrypt failure log from debug to warning so failures are visible

## v149
- Re-add DAVE decryption with corrected logic: drop packets when session not ready (instead of passing ciphertext to Opus decoder), handle can_passthrough for CNG/silence, drop on decrypt failure

## v148
- Remove manual DAVE decryption code and all DAVE investigation diagnostics (packet file dumps, per-packet PCM stats, first-packet hex logs, CRC32 tracking, packet manifests); voice control now relies on DAVE being disabled in Discord server settings instead

## v147
- Wait for MLS key exchange before attempting DAVE decrypt; drop packets silently until user key is in group

## v146
- Strip DAVE E2E layer before Opus decode — was the root cause of garbage audio reaching Whisper

## v145
- Enable guild_members privileged intent to fix voice SSRC→user mapping delay and auto-leave logic

## v144
- Remove dead code: scheduler.py, llm_usage.py, llm_provider.py, tests/test_scheduler.py (all superseded by pandabot_core equivalents since v141)

## v143
- Migrate test fixtures to pandabot_core.testing: conftest.py now uses stub_discord() and tmp_db via pandabot_core; test_fire_task.py mock_claude fixture uses FakeProvider from core instead of local _MockProvider

## v142
- Add CLAUDE.md documenting architecture, pandabot-core dependency, and dead code

## v141
- Migrate bot.py and tools.py to pandabot_core shared infrastructure: scheduler, llm loop, discord_comms, identity, telemetry, llm usage/provider now all sourced from pandabot_core package instead of local copies

## v140
- Add parent_wp_id to update_op_work_package: pass a work package ID to reparent, or -1 to clear the parent entirely

## v139
- Fix send_with_retry: also retry on aiohttp.ClientConnectorError and OSError (network blips), not just Discord 5xx — previously a momentary connection failure silently dropped the reply

## v138
- Add update_op_work_package tool: update subject, type, description, assignee, status, dates on existing work packages via PATCH with lockVersion

## v137
- Add OpenProject tools: set_op_project_parent (hierarchy via PATCH), create_op_work_package, list_op_types; create_op_project now accepts optional parent arg

## v136
- Switch OpenProject auth from OPENPROJECT_USER/OPENPROJECT_PASSWORD to OPENPROJECT_API_KEY; use Basic Auth with "apikey" username per OpenProject API v3 spec

## v135
- Fix notify-discord.sh execute bit: was tracked as 100644 in git so every pull landed it as non-executable, causing Jenkins post-build steps to fail with Permission denied

## v134
- Add SCHEDULED_TASK_PING_USER_ID env var: when set, @pings that user on all terminal scheduled task results (condition met, gave up, success, error) but not on intermediate condition-check pending messages

## v133
- Fix typing indicator: use channel._state.http.send_typing() directly instead of channel.typing() which leaks background tasks and causes 429 rate limits

## v132
- Fix typing indicator crash: catch all exceptions in _keep_typing() loop so Discord errors don't silently kill the indicator
- Add OPERATOR_SSH_CMD env var: when set, system prompt includes WSL SSH connection context so bot tailors command examples to the operator's machine

## v131
- Fix silent empty replies: add fallback message when LLM returns blank response, and skip empty chunks before sending

## v130
- Add send_with_retry() helper: all channel.send calls now retry up to 3 times with exponential backoff (1s, 2s, 4s) on transient Discord 5xx errors

## v129
- Fix get_performance_history timing out when PCP/pmcd is unavailable: reduce timeout from 30s to 10s and add sar (sysstat) fallback for CPU queries that includes average idle %

## v128
- Add OpenProject integration behind ENABLE_OPENPROJECT flag: list/get projects, list/search/get work packages, list versions and version tickets, list project members, create projects, add/remove project members

## v127
- Fix condition_check tasks with generative_prompt firing the LLM immediately on first check regardless of whether the condition was met (caused premature "disc finished ripping" notifications)

## v126
- Add TRUSTED_BOT_IDS env var: comma-separated bot user IDs whose messages bypass the bot filter in on_message, allowing PandaQA to send test prompts to #pandabot and receive replies

## v125
- Restrict Pandabot to its configured channel only — was accidentally responding in all channels including #pandabot-qa

## v124
- Fix scheduled tasks being wiped on every deploy — `scheduler.db` (and `llm_usage.db`) are now in `.gitignore` and untracked from git. Previously `git pull` overwrote the live database with the empty placeholder committed in v109.

## v123
- Fix "no such table: scheduled_tasks" error when asking the bot about scheduled tasks on a fresh DB — `manage_schedule` now calls `init_db()` before querying, so the table always exists.

## v122
- Fix misleading "Error talking to Claude" messages — changed to "Error processing request" so the error text doesn't falsely blame the LLM provider (Claude/DeepSeek/etc.)
- Add parameter validation in `execute_tool()` — missing required parameters now produce a clear error message instead of a raw `KeyError` that appears as "Error talking to Claude: 'action'"
- Increase tool-call limit from 10 to 25 — smaller/faster models (DeepSeek Flash) tend to make more sequential tool calls and were hitting the limit mid-query

## v120
- Fix 400 errors on tool-call queries: DeepSeek reasoning models return `reasoning_content` that must be echoed back each turn; the OpenAI-compat provider now captures and replays it. Same passthrough added for Anthropic thinking blocks.
- Inject live LLM provider and model name into the system prompt so the bot accurately answers "what model are you running on?" instead of hallucinating.

## v119
- Increase channel history context from 10 to 15 messages.

## v118
- Respond to all messages in a channel without requiring an @mention. Mention text is still stripped if present.

## v117
- Fix DeepSeek model name: `OPENAI_COMPAT_PRIMARY_MODEL` corrected to `deepseek-v4-flash` (was `deepseek-chat`).
- Fix DeepSeek pricing in `llm_usage`: `deepseek-v4-flash` $0.14/$0.28 per M tokens, `deepseek-v4-pro` $0.435/$0.87 per M tokens (cache-miss rates from platform.deepseek.com).

## v116
- Add `llm_provider.py`: pluggable LLM backend abstraction supporting Anthropic and any OpenAI-compatible API (DeepSeek, Groq, Ollama, etc.). Providers expose `format_tool_definitions`, `complete`, and `complete_simple`; the agentic loop and scheduler use `get_provider()` throughout.
- Switch active backend to DeepSeek V4 Flash (`deepseek-v4-flash`) via `LLM_PROVIDER=openai_compat`. No upgrade model set; DeepSeek handles all operations including `manage_schedule`.
- Retain model-upgrade path for `manage_schedule`: when `OPENAI_COMPAT_UPGRADE_MODEL` is set, the primary model's tool-call choice triggers a re-issue to the upgrade model. Currently unused (upgrade_model empty).
- Add `provider` column to `llm_usage` table (additive migration, existing rows default to `anthropic`). `query_llm_usage(by_model)` now shows provider alongside model for cross-provider cost comparison.
- Add DeepSeek pricing to `llm_usage`: `deepseek-v4-flash` at $0.14/M input, $0.28/M output; `deepseek-v4-pro` at $0.435/M input, $0.87/M output.
- Message history is now stored as canonical plain dicts (Anthropic format); the OpenAI-compat provider translates on the way out, enforcing strict role ordering and content-gap rules.
- Add `openai>=1.0.0` to `requirements.txt`.

## v115
- Fix movie hallucination bug: `query_media_library(find_files)` now defaults to `file_type="video"`, filtering results to known video extensions (`.mkv`, `.mp4`, `.avi`, etc.) and tagging each result as `[VIDEO]` or `[OTHER]`. The tool schema exposes the new `file_type` parameter (`"video"` | `"all"`). The system prompt now includes a cross-verification rule: `query_jellyfin(search_movies)` is the authority on what movies exist — filesystem matches must be verified via `file_info` before reporting as movies.
- New tests: `TestQueryMediaLibraryFileType` (8 tests) and `TestQueryMediaLibrarySchema` (3 tests) covering video filtering, all-files mode, header labels, and schema validation.

## v114
- Add `!hear` command: plays back the last captured `stt_raw_pcm.wav` (raw 48kHz stereo) and `stt_debug_latest.wav` (16kHz Whisper input) through the voice channel so the user can hear exactly what the bot recorded. This is the key diagnostic to determine whether the STT failure is in audio reception (noise) or in Whisper configuration (degraded but intelligible speech).
- Improve Whisper: add `initial_prompt="Voice chat transcription."` to steer away from YouTube-style hallucinations ("Thanks for watching!" etc.) that appear when audio looks noise-like to the model.
- Improve Whisper logging: log per-segment `no_speech_prob` and `avg_logprob` so we can see at runtime why segments are accepted or discarded; log a clear diagnostic hint when no segments are returned.
- Improve hallucination message: tell the user to run `!hear` to play back what the bot captured.

## v113
- No code changes — pre-commit hook requires v113 entry for the auto-bump from v112.

## v112
- Audio content quality fix: reset Opus decoder at utterance start to clear Comfort Noise Generator (CNG) prediction state accumulated from SILK NB frames between utterances (4.3% of packets). Previously, CNG parameters in the decoder's inter-frame prediction memory (adaptive codebook, LPC coefficients, pitch synthesis filter) destroyed the pitch harmonic structure of the next utterance's CELT NB frames, causing PitchAuto to drop from ~0.54 to ~0.12-0.18. Now the decoder is recreated on each speech→silence→speech transition, giving a clean prediction slate.
- CRC32 checksum tracking: compute `zlib.crc32` of every Opus payload and store per-utterance in `_utt_crcs` dict. CRC32 lists are saved to the utterance manifest (`packet_crc32_first5`, `packet_crc32_count`) for unambiguous correlation between saved `.bin` packet files and debug WAV recordings.
- Fix `test_decoder_state.py` bugs: Methods C/D/E crash from ctypes `LP_c_short` list-slice bug in re-encode loop (use `ctypes.Array` slice instead of Python list slice for `opus_encode` input). Pre-initialize `metrics_c`/`metrics_d`/`pcm_c`/`pcm_d` to avoid `NameError` in summary when all packets fail to decode.

## v111
- Fix typo in `test_decoder_state.py`: `_load_libus()` → `_load_libopus()` (NameError on line 57).

## v110
- Add `test_decoder_state.py` — offline decoder state comparison script: decodes saved Opus packets 4 ways (persistent discord.opus.Decoder, per-packet fresh decoder, persistent ctypes libopus, per-packet ctypes libopus), compares PCM metrics (RMS, peak, flat runs, pitch), and re-encodes decoded PCM back to Opus (Method E) to compare TOC bytes against original packets.

## v109
- Fix TTS silent skip for STT-triggered responses: `speak_response()` now logs when the voice client is not connected (previously returned silently with zero diagnostic trail), and the second mid-stream disconnect check also logs. `_on_stt_transcript()` now pings `_voice_last_play[guild_id]` at the start of the pipeline (before Whisper transcription) to prevent `task_voice_idle_check` from disconnecting the bot during the ~8-11s Whisper+Claude processing window. Prior to this fix, if the last TTS play was more than `TTS_IDLE_TIMEOUT` (300s) ago, the idle checker would disconnect the bot between audio capture and TTS response — the hallucination diagnostic message would reach Claude, Claude would reply, but `speak_response()` would silently return because `_voice_clients.get(guild_id)` was `None`.

## v107
- SSRC mapping audit: extract Synchronization Source (SSRC) from RTP header bytes 8-11 for every incoming audio packet. The `STTSink` now maintains an `_ssrc_map` dict tracking which SSRC belongs to which Discord user. Per-packet logs include `ssrc=N`, first-packet log shows SSRC+seq+ts, and `info.txt` saves the SSRC per packet. On each utterance, an SSRC audit log line reports the user's SSRC and the full mapping table — critical for verifying that the bot is actually receiving audio from the correct sender (SSRC mismatch = subscribed to wrong stream).
- Whisper hallucination handling: instead of returning `None` (which silenced the bot entirely), the hallucination detector now returns a diagnostic message: `"[Audio reception issue: the user said something but the bot's voice receiver only captured noise/silence. This is a known debug issue — audio packets are received but contain no recognizable speech.]"`. This ensures the bot speaks an audible response each time (confirming audio OUTPUT still works) and provides debug context to the LLM.

## v106
- (Placeholder — next version)

## v105
- Loopback audio reception test (`!test_audio`): bot generates a 1-second 440Hz sine wave, plays it into the voice channel while simultaneously listening in loopback mode. The `STTSink` gains `loopback_mode` (bypasses bot-user filter) and `suppress_transcribe` (prevents Whisper transcription of the test tone). New analysis script `analyze_loopback.py` compares original vs captured signal (cross-correlation, dominant frequency, SNR, RMS).
- `_start_listening()` now accepts `**sink_kwargs` forwarded to `STTSink` constructor for per-call configuration.

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
