# Changelog

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
