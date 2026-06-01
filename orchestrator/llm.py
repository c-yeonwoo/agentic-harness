"""Provider-agnostic LLM 호출 레이어.

- LLM_PROVIDER=openai|anthropic (default: openai)
- 공통 인터페이스: call, call_with_tools
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import structlog

from orchestrator import claude as anthropic_provider

log = structlog.get_logger()


@dataclass
class LlmCall:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    _cost_usd: float = 0.0

    @property
    def cost_usd(self) -> float:
        return self._cost_usd


def _provider() -> str:
    return os.environ.get("LLM_PROVIDER", "openai").strip().lower() or "openai"


def _to_common_call(model: str, in_tok: int, out_tok: int, cache_r: int = 0, cache_w: int = 0) -> LlmCall:
    cost = 0.0
    if model.startswith("claude"):
        a = anthropic_provider.LlmCall(
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cache_read_tokens=cache_r,
            cache_write_tokens=cache_w,
        )
        cost = a.cost_usd
    return LlmCall(
        model=model,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_r,
        cache_write_tokens=cache_w,
        _cost_usd=cost,
    )


def _oa_tools_from_anthropic(tools: list[dict]) -> list[dict]:
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


async def _openai_call(*, model: str, system: str, user: str, max_tokens: int = 4000, tools: Optional[list[dict]] = None) -> tuple[str, LlmCall]:
    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 미설정 — .env 확인")

    client = AsyncOpenAI(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if max_tokens:
        kwargs["max_completion_tokens"] = max_tokens
    if tools:
        kwargs["tools"] = _oa_tools_from_anthropic(tools)
        kwargs["tool_choice"] = "auto"

    resp = await client.chat.completions.create(**kwargs)
    msg = resp.choices[0].message
    text = msg.content or ""
    usage = resp.usage
    call = _to_common_call(
        model=model,
        in_tok=getattr(usage, "prompt_tokens", 0) or 0,
        out_tok=getattr(usage, "completion_tokens", 0) or 0,
    )
    return text, call


async def call(*, model: str, system: str, user: str, max_tokens: int = 4000, tools: Optional[list[dict]] = None) -> tuple[str, LlmCall]:
    provider = _provider()
    if provider == "anthropic":
        text, info = await anthropic_provider.call(
            model=model,
            system=system,
            user=user,
            max_tokens=max_tokens,
            tools=tools,
        )
        return text, _to_common_call(
            model=info.model,
            in_tok=info.input_tokens,
            out_tok=info.output_tokens,
            cache_r=info.cache_read_tokens,
            cache_w=info.cache_write_tokens,
        )
    if provider == "openai":
        return await _openai_call(model=model, system=system, user=user, max_tokens=max_tokens, tools=tools)
    raise RuntimeError(f"지원하지 않는 LLM_PROVIDER: {provider}")


async def call_with_tools(
    *,
    model: str,
    system: str,
    user: str,
    tools: list[dict],
    tool_executor,
    stop_tool: Optional[str] = None,
    max_iterations: int = 10,
    max_tokens: int = 4000,
    cost_cap_usd: float = 0.50,
) -> tuple[Any, LlmCall, list[dict]]:
    provider = _provider()

    if provider == "anthropic":
        result, info, trace = await anthropic_provider.call_with_tools(
            model=model,
            system=system,
            user=user,
            tools=tools,
            tool_executor=tool_executor,
            stop_tool=stop_tool,
            max_iterations=max_iterations,
            max_tokens=max_tokens,
            cost_cap_usd=cost_cap_usd,
        )
        return result, _to_common_call(
            model=info.model,
            in_tok=info.input_tokens,
            out_tok=info.output_tokens,
            cache_r=info.cache_read_tokens,
            cache_w=info.cache_write_tokens,
        ), trace

    if provider != "openai":
        raise RuntimeError(f"지원하지 않는 LLM_PROVIDER: {provider}")

    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 미설정 — .env 확인")

    client = AsyncOpenAI(api_key=api_key)
    oa_tools = _oa_tools_from_anthropic(tools)

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    acc_in = 0
    acc_out = 0
    trace: list[dict] = []

    for i in range(max_iterations):
        resp = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=oa_tools,
            tool_choice="auto",
            max_completion_tokens=max_tokens,
        )
        usage = resp.usage
        acc_in += getattr(usage, "prompt_tokens", 0) or 0
        acc_out += getattr(usage, "completion_tokens", 0) or 0

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            return (msg.content or ""), _to_common_call(model=model, in_tok=acc_in, out_tok=acc_out), trace

        assistant_msg = {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in tool_calls],
        }
        messages.append(assistant_msg)

        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            if stop_tool and name == stop_tool:
                trace.append({"name": name, "input": args, "result_preview": "(stop_tool — plan 확정)"})
                return args, _to_common_call(model=model, in_tok=acc_in, out_tok=acc_out), trace

            try:
                result = await tool_executor(name, args)
            except Exception as exc:
                result = f"(tool error: {exc})"

            trace.append({"name": name, "input": args, "result_preview": result[:200]})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        if _to_common_call(model=model, in_tok=acc_in, out_tok=acc_out).cost_usd >= cost_cap_usd:
            return (
                f"(❌ cost cap ${cost_cap_usd} 도달 — iteration {i}/{max_iterations})",
                _to_common_call(model=model, in_tok=acc_in, out_tok=acc_out),
                trace,
            )

    return (
        f"(❌ max iterations {max_iterations} 도달 — 작업 미완)",
        _to_common_call(model=model, in_tok=acc_in, out_tok=acc_out),
        trace,
    )
