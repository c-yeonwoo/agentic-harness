"""ApiRunner — 기존 ReAct + plan apply 흐름의 래퍼.

agents.run_developer 의 가운데 부분 (LLM 호출 → plan 정규화 → apply_plan_and_push)
을 그대로 감쌈. HARNESS_MODE=hermes 일 때 사용.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import structlog

from orchestrator import code_tools, llm
from orchestrator.git_apply import EditApplyError, apply_plan_and_push
from orchestrator.runners import ExecutionContext, ExecutionResult

log = structlog.get_logger()


def _coerce(value, expected_type):
    if isinstance(value, expected_type):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return value
        if isinstance(parsed, expected_type):
            return parsed
    return value


def _normalize_plan(plan: dict) -> dict:
    """submit_plan 결과 정규화 — Haiku 가 nested 를 string 으로 넣는 case 복구."""
    if not isinstance(plan, dict):
        return plan
    commits = _coerce(plan.get("commits"), list)
    if isinstance(commits, list):
        norm_commits = []
        for c in commits:
            c = _coerce(c, dict)
            if not isinstance(c, dict):
                continue
            files = _coerce(c.get("files"), list)
            if isinstance(files, list):
                norm_files = []
                for f in files:
                    f = _coerce(f, dict)
                    if not isinstance(f, dict):
                        continue
                    if "edits" in f:
                        edits = _coerce(f.get("edits"), list)
                        if isinstance(edits, list):
                            edits = [_coerce(e, dict) for e in edits if e is not None]
                        f["edits"] = edits
                    norm_files.append(f)
                c["files"] = norm_files
            norm_commits.append(c)
        plan["commits"] = norm_commits
    return plan


def _load_agent_prompt(name: str) -> str:
    agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
    return (agents_dir / f"{name}.md").read_text(encoding="utf-8")


class ApiRunner:
    """기존 동작 보존 — LLM 으로 plan 만들고 plan 을 worktree 에 apply."""

    async def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        # DEVELOPER_MODEL / EXECUTOR_MODEL 둘 다 인식 (ADR-012 rename back-compat)
        model = (
            ctx.model
            or os.environ.get("DEVELOPER_MODEL")
            or os.environ.get("EXECUTOR_MODEL", "gpt-5.3-codex")
        )
        # prompt 파일: developer.md (없으면 code-executor.md fallback)
        try:
            role_prompt = _load_agent_prompt("developer")
        except FileNotFoundError:
            role_prompt = _load_agent_prompt("code-executor")
        system_prompt = role_prompt + "\n\n" + ctx.sot_prompt

        async def _tool_exec(name: str, args: dict) -> str:
            return await code_tools.execute_tool(ctx.repo_cwd, name, args)

        try:
            result, call_info, tool_trace = await llm.call_with_tools(
                model=model,
                system=system_prompt,
                user=ctx.user_prompt,
                tools=code_tools.TOOL_SCHEMAS,
                tool_executor=_tool_exec,
                stop_tool="submit_plan",
                max_iterations=30,
                max_tokens=16000,
                cost_cap_usd=float(
                    os.environ.get("DEVELOPER_COST_CAP")
                    or os.environ.get("EXECUTOR_COST_CAP", "0.80")
                ),
            )
        except Exception as exc:
            log.exception("api_runner.llm_failed", error=str(exc))
            return ExecutionResult(
                ok=False,
                error=f"LLM 호출 실패: {exc}",
                error_kind="crashed",
                model=model,
            )

        if not isinstance(result, dict):
            return ExecutionResult(
                ok=False,
                error=f"submit_plan 호출 없이 종료 (last text: {str(result)[:300]})",
                error_kind="no_plan",
                model=call_info.model,
                input_tokens=call_info.input_tokens,
                output_tokens=call_info.output_tokens,
                cost_usd=call_info.cost_usd,
                tool_trace=tool_trace,
            )

        plan = _normalize_plan(result)

        try:
            apply_info = await apply_plan_and_push(
                repo_cwd=ctx.repo_cwd,
                plan=plan,
                issue_number=ctx.issue_or_pr_number,
                existing_branch=ctx.existing_branch,
            )
        except EditApplyError as exc:
            log.warning("api_runner.edit_apply_failed",
                        path=exc.path, edit_idx=exc.edit_idx)
            return ExecutionResult(
                ok=False,
                error=exc.message,
                error_kind="edit_apply",
                edit_apply_info={
                    "path": exc.path,
                    "edit_idx": exc.edit_idx,
                    "old_str_head": exc.old_str_head,
                    "message": exc.message,
                },
                model=call_info.model,
                input_tokens=call_info.input_tokens,
                output_tokens=call_info.output_tokens,
                cost_usd=call_info.cost_usd,
                tool_trace=tool_trace,
            )
        except Exception as exc:
            log.exception("api_runner.apply_failed", error=str(exc))
            return ExecutionResult(
                ok=False,
                error=f"apply 실패: {exc}",
                error_kind="crashed",
                model=call_info.model,
                input_tokens=call_info.input_tokens,
                output_tokens=call_info.output_tokens,
                cost_usd=call_info.cost_usd,
                tool_trace=tool_trace,
            )

        return ExecutionResult(
            ok=True,
            summary=plan.get("summary") or "",
            branch=apply_info["branch"],
            base=apply_info["base"],
            files_changed=apply_info["files_changed"],
            commits_applied=apply_info["commits_applied"],
            pr_title=plan.get("pr_title"),
            pr_body=plan.get("pr_body"),
            verification=plan.get("verification"),
            model=call_info.model,
            input_tokens=call_info.input_tokens,
            output_tokens=call_info.output_tokens,
            cost_usd=call_info.cost_usd,
            tool_trace=tool_trace,
            extra={"plan": plan},
        )
