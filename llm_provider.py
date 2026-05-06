"""LLM provider abstraction — Anthropic and OpenAI-compatible backends.

Providers expose three methods:
  format_tool_definitions(tool_defs)       -> formatted tool list (call once, cache result)
  complete(system, messages, tools, model) -> NormalizedResponse
  complete_simple(messages, model)         -> (text, input_tokens, output_tokens)

Tool definitions stay in canonical Anthropic format (input_schema) as the single
source of truth. The OpenAI-compat provider wraps them at format_tool_definitions time.
Messages are always in canonical Anthropic format; providers translate internally.
"""

import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("panda-bot")


@dataclass
class ContentBlock:
    type: str           # "text", "tool_use", or "thinking"
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict | None = None
    thinking: str | None = None
    signature: str | None = None


@dataclass
class NormalizedResponse:
    stop_reason: str        # "end_turn" or "tool_use"
    content: list[ContentBlock]
    model: str
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# Anthropic provider
# ---------------------------------------------------------------------------

class AnthropicProvider:
    def __init__(self) -> None:
        import anthropic
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.client = anthropic.Anthropic(api_key=api_key)
        self.primary_model = os.environ.get("ANTHROPIC_PRIMARY_MODEL", "claude-haiku-4-5")
        self.upgrade_model = os.environ.get("ANTHROPIC_UPGRADE_MODEL", "claude-sonnet-4-5")

    def format_tool_definitions(self, tool_defs: list[dict]) -> list[dict]:
        return tool_defs  # Anthropic format is the canonical source of truth

    def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        formatted_tools: list[dict],
        model: str,
        max_tokens: int = 4096,
    ) -> NormalizedResponse:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            tools=formatted_tools,
            messages=messages,
        )
        content: list[ContentBlock] = []
        for block in response.content:
            if block.type == "text":
                content.append(ContentBlock(type="text", text=block.text))
            elif block.type == "tool_use":
                content.append(ContentBlock(
                    type="tool_use",
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))
            elif block.type == "thinking":
                content.append(ContentBlock(
                    type="thinking",
                    thinking=block.thinking,
                    signature=block.signature,
                ))
        return NormalizedResponse(
            stop_reason="end_turn" if response.stop_reason == "end_turn" else "tool_use",
            content=content,
            model=model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def complete_simple(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 800,
    ) -> tuple[str, int, int]:
        response = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return (
            response.content[0].text,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# OpenAI-compatible provider
# ---------------------------------------------------------------------------

class OpenAICompatProvider:
    def __init__(self) -> None:
        from openai import OpenAI
        api_key = os.environ["OPENAI_COMPAT_API_KEY"]
        base_url = os.environ["OPENAI_COMPAT_BASE_URL"]
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=60.0,
        )
        self.primary_model = os.environ["OPENAI_COMPAT_PRIMARY_MODEL"]
        self.upgrade_model = os.environ.get("OPENAI_COMPAT_UPGRADE_MODEL", "")

    def format_tool_definitions(self, tool_defs: list[dict]) -> list[dict]:
        """Wrap Anthropic-canonical tool defs into OpenAI function-call format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": td["name"],
                    "description": td.get("description", ""),
                    "parameters": td["input_schema"],
                },
            }
            for td in tool_defs
        ]

    def _to_openai_messages(self, system_prompt: str, messages: list[dict]) -> list[dict]:
        """Translate canonical Anthropic-format message history to OpenAI format.

        Enforces strict ordering:
          [system] -> [user] -> [assistant(tool_calls)] -> [tool, tool, ...] -> [assistant] -> ...

        Three failure modes guarded against:
        1. Every tool_calls assistant turn is immediately followed by tool messages.
        2. Assistant messages with tool_calls always have content set (even if null).
        3. System prompt injected at index 0, not as a top-level API parameter.
        """
        result: list[dict] = [{"role": "system", "content": system_prompt}]

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            if role == "user":
                if isinstance(content, str):
                    result.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    # May contain tool_result blocks (from prior tool calls) and/or text.
                    # tool_results become individual "tool" role messages (one per result).
                    tool_results = [
                        b for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_result"
                    ]
                    text_parts = [
                        b for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                    ]
                    for tr in tool_results:
                        tr_content = tr["content"]
                        if not isinstance(tr_content, str):
                            tr_content = json.dumps(tr_content)
                        result.append({
                            "role": "tool",
                            "tool_call_id": tr["tool_use_id"],
                            "content": tr_content,
                        })
                    if text_parts:
                        combined = "\n".join(p.get("text", "") for p in text_parts)
                        result.append({"role": "user", "content": combined})

            elif role == "assistant":
                if isinstance(content, str):
                    result.append({"role": "assistant", "content": content})
                elif isinstance(content, list):
                    tool_calls = []
                    text_parts = []
                    reasoning_parts = []
                    for block in content:
                        # Support both plain dicts (canonical) and SDK objects (legacy)
                        bt = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
                        if bt == "tool_use":
                            tid = block["id"] if isinstance(block, dict) else block.id
                            tname = block["name"] if isinstance(block, dict) else block.name
                            tinput = block["input"] if isinstance(block, dict) else block.input
                            tool_calls.append({
                                "id": tid,
                                "type": "function",
                                "function": {
                                    "name": tname,
                                    "arguments": json.dumps(tinput),
                                },
                            })
                        elif bt == "text":
                            txt = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                            text_parts.append(txt)
                        elif bt == "reasoning_content":
                            txt = block.get("text", "") if isinstance(block, dict) else getattr(block, "text", "")
                            reasoning_parts.append(txt)

                    combined_text: str | None = "\n".join(text_parts) if text_parts else None
                    oai_msg: dict = {
                        "role": "assistant",
                        "content": combined_text,  # null is valid and required for providers that check it
                    }
                    if tool_calls:
                        oai_msg["tool_calls"] = tool_calls
                    if reasoning_parts:
                        oai_msg["reasoning_content"] = reasoning_parts[0]
                    result.append(oai_msg)

        return result

    def complete(
        self,
        system_prompt: str,
        messages: list[dict],
        formatted_tools: list[dict],
        model: str,
        max_tokens: int = 4096,
    ) -> NormalizedResponse:
        oai_messages = self._to_openai_messages(system_prompt, messages)
        response = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=oai_messages,
            tools=formatted_tools,
            tool_choice="auto",
        )

        choice = response.choices[0]
        msg = choice.message
        content: list[ContentBlock] = []

        # DeepSeek reasoning models return reasoning_content alongside content.
        # Must be echoed back in subsequent turns or the API returns 400.
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning:
            content.append(ContentBlock(type="reasoning_content", text=reasoning))

        if msg.content:
            content.append(ContentBlock(type="text", text=msg.content))

        if msg.tool_calls:
            for tc in msg.tool_calls:
                content.append(ContentBlock(
                    type="tool_use",
                    id=tc.id,
                    name=tc.function.name,
                    input=json.loads(tc.function.arguments),
                ))

        stop_reason = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"

        return NormalizedResponse(
            stop_reason=stop_reason,
            content=content,
            model=model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

    def complete_simple(
        self,
        messages: list[dict],
        model: str,
        max_tokens: int = 800,
    ) -> tuple[str, int, int]:
        response = self.client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
        )
        return (
            response.choices[0].message.content or "",
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
        )


# ---------------------------------------------------------------------------
# Factory — call get_provider() everywhere; instance is cached at module level
# ---------------------------------------------------------------------------

_provider: AnthropicProvider | OpenAICompatProvider | None = None


def get_provider() -> AnthropicProvider | OpenAICompatProvider:
    global _provider
    if _provider is None:
        provider_name = os.environ.get("LLM_PROVIDER", "anthropic")
        if provider_name == "openai_compat":
            _provider = OpenAICompatProvider()
            log.info(
                "LLM provider: openai_compat  base_url=%s  primary=%s  upgrade=%s",
                os.environ.get("OPENAI_COMPAT_BASE_URL", "?"),
                os.environ.get("OPENAI_COMPAT_PRIMARY_MODEL", "?"),
                os.environ.get("OPENAI_COMPAT_UPGRADE_MODEL") or "none",
            )
        else:
            _provider = AnthropicProvider()
            log.info(
                "LLM provider: anthropic  primary=%s  upgrade=%s",
                _provider.primary_model,
                _provider.upgrade_model,
            )
    return _provider


def get_provider_name() -> str:
    """Return the LLM_PROVIDER env var value (default 'anthropic')."""
    return os.environ.get("LLM_PROVIDER", "anthropic")
