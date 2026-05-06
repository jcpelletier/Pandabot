# Proposal: OpenAI-Format Provider Support for Pandabot

## Background

Pandabot is a Discord bot that acts as a voice/text assistant for a home Ubuntu server. It monitors Docker containers, systemd services, Jenkins pipelines, Jellyfin media, and more. Each user query runs through an agentic loop: the LLM decides which tools to call (up to 10 rounds), executes them, and synthesizes a final answer.

**Current LLM setup:**
- Primary model: `claude-haiku-4-5` (Anthropic SDK)
- Automatic upgrade to `claude-sonnet-4-5` when the `manage_schedule` tool is invoked (complex schema, Haiku fills parameters unreliably)
- Hard dependency on the `anthropic` Python SDK throughout

**Motivation for this change:**
- Explore cost reduction and/or capability improvement by supporting alternative providers
- DeepSeek (OpenAI-compatible API) is the initial target for testing
- Framing it as generic OpenAI-format support keeps the door open for any compatible provider (Groq, Together, local Ollama, etc.)

The bot's "brain" is logic-heavy and provider-agnostic; the problem is that its "nervous system" is currently hardwired to Anthropic's specific JSON structures across three distinct translation layers: request format, tool schema format, and response parsing.

---

## Architecture: Provider Module per Backend

The provider is implemented as a plain module (not an ABC class hierarchy) containing three functions: `complete`, `format_tool_definitions`, and `format_tool_result`. A factory in `llm_provider.py` returns the right module at startup based on env vars.

```python
# llm_provider.py
def get_provider():
    if os.environ.get("LLM_PROVIDER") == "openai_compat":
        return openai_compat_provider   # module with complete/format_tools/format_tool_result
    return anthropic_provider           # same interface
```

The agentic loop in `bot.py` calls `provider.complete(...)` and receives a `NormalizedResponse` dataclass — it never touches provider-specific types. Adding a third provider means adding one new module and one new branch in the factory.

An ABC class hierarchy would also work here, but adds Python abstract class machinery and an extra layer of indirection for what is currently a two-way choice on a single-user bot. The functional approach is simpler to read and debug at this scale; refactoring to a class is straightforward later if the number of providers grows.

---

## Env Vars (no code changes to add a provider)

| Var | Purpose |
|---|---|
| `LLM_PROVIDER` | `anthropic` (default) or `openai_compat` |
| `OPENAI_COMPAT_BASE_URL` | Base URL, e.g. `https://api.deepseek.com` |
| `OPENAI_COMPAT_API_KEY` | API key for the provider |
| `OPENAI_COMPAT_PRIMARY_MODEL` | Model name for primary calls |
| `OPENAI_COMPAT_UPGRADE_MODEL` | Optional: higher-capability model for complex tools (see below) |

---

## Tool Schema: Canonical Anthropic Format

Tool definitions stay in Anthropic format as the single source of truth. Anthropic's `input_schema` is essentially pure JSON Schema and is the stricter of the two. If a definition satisfies Claude, it will work for DeepSeek/OpenAI.

`OpenAICompatProvider.format_tool_definitions()` wraps each definition at call time — computationally trivial:

```python
# Anthropic canonical (source of truth in tools.py)
{"name": "get_service_status", "description": "...", "input_schema": {"type": "object", ...}}

# OpenAI wrapper applied dynamically
{"type": "function", "function": {"name": "get_service_status", "description": "...", "parameters": {"type": "object", ...}}}
```

The `input_schema` → `parameters` rename is the only structural change; the inner JSON Schema is identical.

---

## Message History: Strict Role Ordering

OpenAI-compatible APIs are sensitive to message role ordering in ways Anthropic's API is not. The provider abstraction must enforce:

```
[system] → [user] → [assistant (tool_calls)] → [tool] → [tool] → [assistant] → ...
```

Three specific failure modes to guard against:

1. **Alternate-turn trap.** Every `assistant` message containing `tool_calls` must be immediately followed by `tool` messages for *every* call ID before another `assistant` message appears. The current Anthropic loop already does this correctly; the translation layer must preserve it.

2. **Content gap.** Some OpenAI-compatible providers (including certain DeepSeek versions) fail when an assistant message has `tool_calls` but the `content` field is absent. The provider must normalize this to `content: null` or `content: ""` explicitly rather than omitting the key.

3. **System prompt position.** OpenAI format takes the system message as `{"role": "system", "content": "..."}` at index 0 of the messages list (not as a top-level API parameter like Anthropic). Some compatible providers reject it if it appears elsewhere. The `OpenAICompatProvider` injects it at position 0 before each call.

---

## Model Upgrade Pattern: Keep Reactive Detection

The current reactive pattern — let the primary model call `manage_schedule`, detect the tool use in the response, re-issue to the upgrade model — is retained. Pre-flight keyword routing is explicitly rejected.

**Why not pre-flight regex?** The upgrade fires when the LLM actually invokes `manage_schedule` with write parameters. A keyword heuristic fires on guessed user intent, which is strictly less accurate:

- "what's on the schedule?" → regex fires, upgrade wasted; LLM would have done a read-only call that Haiku handles fine
- "set up a weekly recap" → regex may not fire; tool gets called anyway with the complex write schema

The re-issue round-trip is a real cost, but it only occurs when `manage_schedule` is actually called — a rare event on a single-user bot. The ground truth signal (the LLM's own tool choice) is more reliable than any keyword list, which will drift out of sync with how users actually phrase requests.

**One valid optimization from the review:** cache the converted tool definitions so the re-issue call doesn't re-run `format_tool_definitions` a second time. That's a trivial win with no tradeoff.

The env var `OPENAI_COMPAT_UPGRADE_MODEL` controls whether an upgrade model exists on the OpenAI-compat path; if unset, the primary model is always used.

---

## `llm_usage.py` Changes

Two changes needed:

1. **Add a `provider` column** to the `llm_usage` table to avoid model name collisions across providers (e.g., a future provider might also have a model called `"large"`).

2. **Token field mapping.** OpenAI uses `response.usage.prompt_tokens` / `response.usage.completion_tokens`; Anthropic uses `input_tokens` / `output_tokens`. The provider normalizes these to a common shape before `log_call` is invoked.

3. **Pricing table.** Add DeepSeek model entries. The `query_llm_usage` tool already supports `by_model` breakdown — cost comparisons across providers will be immediately available via the bot once the column is added.

---

## litellm Consideration

The third-party review flagged [litellm](https://github.com/BerriAI/litellm) as an option that handles provider translation automatically, including accepting Anthropic-formatted dictionaries at a DeepSeek endpoint. This is worth evaluating as an alternative to writing the provider classes manually.

**Tradeoff:** litellm adds a dependency and its own abstraction overhead, but saves writing and maintaining the translation layer. Given the bot's scope (one Anthropic provider + one OpenAI-compat provider), the class-based approach above is likely simpler to debug and own long-term. litellm becomes more attractive if a third or fourth provider is added.

---

## DeepSeek-Specific Note

Use `deepseek-chat` (DeepSeek V4 Flash) for this integration, **not** `deepseek-reasoner` (R1). R1 is a reasoning model with inconsistent or absent tool-calling support depending on the API version — it is not suited for tool-heavy agentic loops. `deepseek-chat` has native function-calling support and is the correct target for testing.

---

## What Does NOT Change

- `tools.py` tool implementations — all subprocess/API calls are provider-agnostic
- The scheduler, webhook server, Discord event handling
- The pending-confirmation flow (`manage_files`, `set_jenkins_schedule`) — only the block-type detection moves into the provider layer
- `llm_usage.py` database queries — the schema addition is additive, existing rows are unaffected

---

## Client Timeout

The `anthropic` SDK includes retry and connection handling that the standard `openai` client does not apply by default to smaller/third-party providers. Set an explicit timeout on the OpenAI client at construction time:

```python
client = OpenAI(
    api_key=api_key,
    base_url=base_url,
    timeout=60.0,  # prevents zombie connections to smaller providers
)
```

---

## Implementation Order

1. Define `NormalizedResponse` dataclass and provider factory in a new `llm_provider.py`
2. Implement `anthropic_provider` module (wraps existing logic, no behavior change)
3. Implement `openai_compat_provider` module with strict role-ordering enforcement and content-gap handling
4. Replace the `claude.messages.create` call in `_run_claude_loop` with `provider.complete()`; cache converted tool definitions so the `manage_schedule` re-issue doesn't re-run the conversion
5. Add `provider` column to `llm_usage` DB (additive migration, default `"anthropic"` for existing rows)
6. Update pricing table with DeepSeek model costs
7. Implement pre-flight routing for `HIGH_COMPLEXITY_TOOLS`
8. Test against `deepseek-chat` with the full tool suite — priority: `manage_schedule`, `manage_files`, any multi-tool-call sequences

---

## Summary

| Component | Decision |
|---|---|
| Architecture | Functional provider modules + factory; no ABC class hierarchy |
| Tool schema | Canonical Anthropic JSON Schema → dynamic OpenAI wrapper at call time |
| Message history | Strict `[system, user, assistant(tool_calls), tool, ...]` enforced in provider layer |
| Model upgrade | Reactive detection retained (LLM tool choice > keyword heuristic); cache converted tool defs |
| Cost tracking | Add `provider` column to `llm_usage`; normalize token field names in provider |
| DeepSeek target | `deepseek-chat` (V3) — not R1, which lacks reliable tool-calling |
| litellm | Defer unless a third provider is added |
| Client timeout | Explicit 60s timeout on `openai.OpenAI` client construction |
