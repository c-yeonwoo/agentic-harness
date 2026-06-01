"""Anthropic SDK 호출 + cost 추적.

cost 추적은 호출별 (per agent run) — issue/PR comment 에 기록.
누적 cost 는 .last_run.json 에 옵셔널 저장 (월간 추정용).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import anthropic
import structlog

log = structlog.get_logger()


# Anthropic 가격표 (2026-05 기준, per 1M tokens)
# 새 모델 추가 시 여기에. 모르는 모델은 0으로 fallback.
_PRICING = {
    "claude-haiku-4-5":              {"input": 1.00,  "output": 5.00,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-haiku-4-5-20251001":     {"input": 1.00,  "output": 5.00,  "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-5":             {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5-20250929":    {"input": 3.00,  "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-5":     {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-6":     {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
}


@dataclass
class LlmCall:
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def cost_usd(self) -> float:
        rates = _PRICING.get(self.model, {})
        if not rates:
            return 0.0
        return (
            self.input_tokens       * rates["input"]       / 1_000_000
          + self.output_tokens      * rates["output"]      / 1_000_000
          + self.cache_read_tokens  * rates.get("cache_read", 0)  / 1_000_000
          + self.cache_write_tokens * rates.get("cache_write", 0) / 1_000_000
        )


_client: Optional[anthropic.AsyncAnthropic] = None


def get_client() -> anthropic.AsyncAnthropic:
    """Anthropic client — enterprise / 사내 프록시 지원.

    환경변수:
      ANTHROPIC_API_KEY      표준 API key (`sk-ant-...`)
      ANTHROPIC_AUTH_TOKEN   OAuth Bearer 토큰 (key 대신 사용 가능)
      ANTHROPIC_BASE_URL     사내 LLM gateway 등 (예: https://llm.ohouse.com/anthropic)

    우선순위: ANTHROPIC_AUTH_TOKEN > ANTHROPIC_API_KEY.
    Bedrock / Vertex 는 별 SDK class 필요 — 현재 미지원 (필요 시 분기).
    """
    global _client
    if _client is not None:
        return _client

    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()

    if not auth_token and not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY 또는 ANTHROPIC_AUTH_TOKEN 미설정 — .env 확인"
        )

    # OAuth Access Token (sk-ant-oat...) 는 x-api-key 헤더로 보내면 401.
    # SDK 의 auth_token 으로 전달 → Authorization: Bearer 헤더로 변환.
    # claude-code OAuth flow 로 발급된 토큰이 이 형식.
    if api_key.startswith("sk-ant-oat") and not auth_token:
        auth_token = api_key
        api_key = ""

    kwargs: dict[str, str] = {}
    if auth_token:
        kwargs["auth_token"] = auth_token
    else:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url

    _client = anthropic.AsyncAnthropic(**kwargs)
    log.info("claude.client_init",
             auth_mode="bearer" if auth_token else "api_key",
             base_url=base_url or "default")
    return _client


async def call(
    *,
    model: str,
    system: str,
    user: str,
    max_tokens: int = 4000,
    tools: Optional[list[dict]] = None,
) -> tuple[str, LlmCall]:
    """단순 1-shot 호출 (tool use 없이). ReAct 가 필요한 경우 call_with_tools 사용.

    Returns (response_text, LlmCall).
    """
    client = get_client()
    # System prompt 를 cache block 으로 — 반복 호출 시 cache hit (token 0.1x, rate limit 안 셈)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    if tools:
        kwargs["tools"] = tools

    resp = await client.messages.create(**kwargs)

    text_parts = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    text = "".join(text_parts)

    usage = resp.usage
    call_info = LlmCall(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )
    log.info("llm.call",
             model=model,
             in_tok=call_info.input_tokens,
             out_tok=call_info.output_tokens,
             cost=round(call_info.cost_usd, 5))
    return text, call_info


async def call_with_tools(
    *,
    model: str,
    system: str,
    user: str,
    tools: list[dict],
    tool_executor,                                # async (name, args) -> str
    stop_tool: Optional[str] = None,              # 이 도구 호출 시 즉시 종료 + input 반환
    max_iterations: int = 10,
    max_tokens: int = 4000,
    cost_cap_usd: float = 0.50,
) -> tuple[Any, LlmCall, list[dict]]:
    """ReAct loop — LLM 이 tool 호출 → 결과 받아 다시 호출 → 최종 답변.

    stop_tool 이 설정되어 있고 LLM 이 그 도구 호출하면 즉시 종료. 그 도구의
    input dict 가 첫 번째 반환값. (JSON parsing 안정성 — Anthropic SDK 가
    schema 검증 후 dict 로 줌.)

    stop_tool 없으면 end_turn 시 final text 반환.

    Returns:
      (result, accumulated_cost_info, tool_trace)
      result: stop_tool input dict 또는 text string
    """
    client = get_client()
    messages: list[dict] = [{"role": "user", "content": user}]
    accumulated = LlmCall(model=model, input_tokens=0, output_tokens=0)
    trace: list[dict] = []

    for iter_idx in range(max_iterations):
        # 비용 cap 체크
        if accumulated.cost_usd >= cost_cap_usd:
            log.warning("llm.cost_cap_hit",
                        spent=round(accumulated.cost_usd, 4), cap=cost_cap_usd)
            return (
                f"(❌ cost cap ${cost_cap_usd} 도달 — iteration {iter_idx}/{max_iterations})",
                accumulated, trace,
            )

        # System prompt 를 cache block 으로 (ReAct 모든 iter 가 같은 system → cache hit)
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=tools, messages=messages,
        )

        usage = resp.usage
        accumulated.input_tokens += usage.input_tokens
        accumulated.output_tokens += usage.output_tokens
        accumulated.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        accumulated.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0

        log.info("llm.iter",
                 iter=iter_idx, stop=resp.stop_reason,
                 in_tok=usage.input_tokens, out_tok=usage.output_tokens,
                 acc_cost=round(accumulated.cost_usd, 4))

        # end_turn → 최종 답변
        if resp.stop_reason == "end_turn":
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            return text, accumulated, trace

        # tool_use → 모든 tool 실행 후 다음 turn
        if resp.stop_reason == "tool_use":
            # stop_tool 검사 — 호출되었으면 input 반환 + 종료
            if stop_tool:
                for block in resp.content:
                    if getattr(block, "type", "") == "tool_use" and block.name == stop_tool:
                        trace.append({
                            "name": block.name,
                            "input": block.input or {},
                            "result_preview": "(stop_tool — plan 확정)",
                        })
                        log.info("llm.stop_tool_called", tool=stop_tool)
                        return block.input or {}, accumulated, trace

            tool_results = []
            for block in resp.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                tool_name = block.name
                tool_input = block.input or {}
                try:
                    result = await tool_executor(tool_name, tool_input)
                except Exception as exc:
                    result = f"(tool error: {exc})"
                trace.append({
                    "name": tool_name,
                    "input": tool_input,
                    "result_preview": result[:200],
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            # assistant turn (tool_use 포함) + user turn (tool_result)
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": tool_results})
            continue

        # 그 외 stop_reason — max_tokens 초과 등
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        log.warning("llm.unexpected_stop", stop=resp.stop_reason)
        return text, accumulated, trace

    # max_iterations 도달
    log.warning("llm.max_iter_hit", iter=max_iterations)
    return (
        f"(❌ max iterations {max_iterations} 도달 — 작업 미완)",
        accumulated, trace,
    )
