"""Agent 실행 — Runner 추상화 위에서 동작.

각 agent 의 공통 흐름:
  1. lock 획득 (assignee 로 bot 부착 — ADR-014)
  2. SoT discover + user prompt 빌드
  3. ExecutionContext 만들어 Runner.execute() 호출
     - HARNESS_MODE=hermes → ApiRunner (기존 ReAct + plan apply)
     - HARNESS_MODE=local  → LocalClaudeRunner (claude -p 헤드리스)
  4. ExecutionResult 의 ok / error_kind 에 따라:
     - ok=True   → PR 생성 + ah:needs-review 라벨 + 성공 코멘트
     - edit_apply → ❌ 코멘트 + ah:needs-execution 재부착 (retry 큐)
     - no_changes → ❌ 코멘트 + awaiting-human
     - crashed/no_plan → ❌ 코멘트 + 호출자가 awaiting-human 처리
  5. lock 해제

reviewer 는 ReAct 가 필요 없는 단일 LLM 호출 — Runner 우회하고
기존 llm.call 직접 사용 (로컬 모드는 향후 별도 작업).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import structlog

from orchestrator import gh, llm, lock
from orchestrator.runners import ExecutionContext, get_runner, resolve_mode
from orchestrator.source_of_truth import SourceOfTruth

log = structlog.get_logger()


AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def _load_agent_prompt(name: str) -> str:
    return (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _extract_json(text: str) -> Optional[dict]:
    """reviewer LLM 응답에서 JSON 추출. ```json ... ``` 감싸기도 처리."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("agent.json_parse_failed", error=str(exc), text=text[:300])
        return None


def _format_tb(exc: BaseException) -> str:
    import traceback
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    return "".join(lines)[-3000:]


def _resolve_repo_cwd(repo: str, repo_cwd: Optional[Path]) -> Path:
    """repo_cwd 가 None 이면 ~/dev-private/<name> 또는 ~/dev/<name> 추론."""
    if repo_cwd is not None:
        return repo_cwd
    name = repo.split("/")[-1]
    for cand in (Path.home() / "dev-private" / name, Path.home() / "dev" / name):
        if (cand / ".git").exists():
            return cand
    # fallback — 첫 후보 (없어도 cwd 없는 lock 호출 시 그냥 메타 안 씀)
    return Path.home() / "dev-private" / name


def _cost_footer(result_or_call) -> str:
    """ExecutionResult / LlmCall 공통 cost 한 줄."""
    cost = getattr(result_or_call, "cost_usd", 0.0) or 0.0
    in_tok = getattr(result_or_call, "input_tokens", 0) or 0
    out_tok = getattr(result_or_call, "output_tokens", 0) or 0
    model = getattr(result_or_call, "model", "") or ""
    return (
        f"_cost ${cost:.4f} · "
        f"{in_tok} in / {out_tok} out · model={model}_"
    )


# ── developer ────────────────────────────────────────────────────────────


def _build_developer_user_prompt(issue: gh.Issue) -> str:
    """신규 모드 user prompt. Runner-agnostic 한 task 기술만 (도구 가이드는 system prompt 영역)."""
    return (
        f"# Issue #{issue.number}: {issue.title}\n\n"
        f"URL: {issue.url}\n"
        f"Labels: {', '.join(issue.labels)}\n\n"
        f"## Body\n{issue.body}\n"
    )


def _build_amend_user_prompt(pr: gh.PullRequest, review_summary: str, retry_hint: str) -> str:
    """amend 모드 user prompt. PR body + 최근 review + retry hint."""
    return (
        f"# PR #{pr.number} (amend mode): {pr.title}\n\n"
        f"URL: {pr.url}\n"
        f"branch: `{pr.head_ref}` (이 branch 에 추가 commit 만들어야 함)\n"
        f"Labels: {', '.join(pr.labels)}\n\n"
        f"## PR body\n{pr.body}\n\n"
        f"## 최근 review 의견 (이걸 반영)\n{review_summary or '(없음)'}\n"
        f"{retry_hint}"
    )


async def run_developer(
    repo: str,
    issue: gh.Issue,
    sot: SourceOfTruth,
    bot_user: str,
    model: Optional[str] = None,
    repo_cwd: Optional[Path] = None,
) -> bool:
    """`ah:needs-execution` issue 1개 → PR + `ah:needs-review`. 락/모드 분기/에러 처리 책임."""
    cwd = _resolve_repo_cwd(repo, repo_cwd)
    if not (cwd / ".git").exists():
        log.error("developer.cwd_not_git", cwd=str(cwd))
        return False
    if not await lock.acquire(repo, "issue", issue.number, bot_user, repo_cwd=cwd):
        log.info("developer.lock_skipped", issue=issue.number)
        return False

    try:

        mode = resolve_mode("developer")
        log.info("developer.start", issue=issue.number, mode=mode)

        ctx = ExecutionContext(
            repo=repo,
            repo_cwd=cwd,
            role="developer",
            sot_prompt=sot.to_prompt(),
            user_prompt=_build_developer_user_prompt(issue),
            issue_or_pr_number=issue.number,
            existing_branch=None,
            title_hint=issue.title,
            model=model,
        )

        runner = get_runner("developer", mode=mode)
        try:
            result = await runner.execute(ctx)
        except Exception as exc:
            log.exception("developer.runner_crashed", issue=issue.number, error=str(exc))
            try:
                await gh.comment_issue(repo, issue.number,
                    f"❌ **developer 예외 발생** (mode=`{mode}`)\n\n"
                    f"```\n{type(exc).__name__}: {exc}\n```\n\n"
                    f"<details><summary>traceback</summary>\n\n"
                    f"```\n{_format_tb(exc)}\n```\n\n</details>"
                )
            except Exception as inner:
                log.warning("developer.error_comment_failed", error=str(inner))
            return False

        log.info("developer.runner_done", issue=issue.number, ok=result.ok,
                 error_kind=result.error_kind, files=result.files_changed,
                 commits=result.commits_applied)

        # ── 성공 경로: PR 생성 + 라벨 전이 ──
        if result.ok:
            pr_body = (result.pr_body or "").rstrip()
            if f"#{issue.number}" not in pr_body:
                pr_body += f"\n\n---\nCloses #{issue.number}"
            # PR body 끝에 generator + cost footer 부착 (developer + /pr-description 합산)
            pr_body += f"\n\n_🤖 Generated by agentic-harness developer ({mode} mode)_"
            pr_body += f"\n{_cost_footer(result)}"

            try:
                pr = await gh.create_pr(
                    repo,
                    title=result.pr_title or f"[#{issue.number}] {issue.title}",
                    body=pr_body,
                    head=result.branch,
                    base=result.base,
                    labels=["ah:needs-review"],
                )
            except Exception as exc:
                log.exception("developer.pr_create_failed", issue=issue.number, error=str(exc))
                await gh.comment_issue(repo, issue.number,
                    f"❌ **PR 생성 실패** (mode=`{mode}`) — push 는 됐는데 gh pr create 실패\n\n"
                    f"```\n{exc}\n```\n\n"
                    f"branch: `{result.branch}` (수동 PR 생성 가능)"
                )
                return False

            await gh.comment_issue(repo, issue.number,
                f"✅ **PR 생성됨** → {pr.url}\n\n"
                f"- mode: `{mode}`\n"
                f"- branch: `{result.branch}`\n"
                f"- commits: {result.commits_applied}, files: {result.files_changed}\n"
                f"- 다음 단계: code-reviewer 가 PR 받아 review (`ah:needs-review`)\n\n"
                f"{_cost_footer(result)}"
            )
            try:
                await gh.remove_label(repo, "issue", issue.number, "ah:needs-execution")
            except Exception:
                pass
            log.info("developer.pr_created", issue=issue.number, pr=pr.number, url=pr.url,
                     mode=mode)
            return True

        # ── 실패 경로: error_kind 별 분기 ──
        return await _handle_developer_failure(
            repo=repo, kind="issue", target_n=issue.number,
            result=result, mode=mode,
        )
    finally:
        await lock.release(repo, "issue", issue.number, bot_user, repo_cwd=cwd)


async def _handle_developer_failure(
    *, repo: str, kind: str, target_n: int, result, mode: str,
) -> bool:
    """ExecutionResult.ok=False 의 error_kind 별 코멘트 + 라벨 처리.

    라벨 배타성 보장 (ADR-014): 실패 시 현재 워크플로우 라벨 → ah:awaiting-human
    으로 전이. edit_apply 만 예외 — retry queue 유지.
    """
    comment_fn = gh.comment_issue if kind == "issue" else gh.comment_pr

    # 현재 라벨 (워크플로우 라벨 1개) — 전이 시 제거
    # issue: ah:needs-execution / PR: ah:in-debate (또는 ah:needs-execution 옛 흐름)
    current_workflow_labels = (
        ["ah:needs-execution"] if kind == "issue"
        else ["ah:in-debate", "ah:needs-execution"]
    )

    async def _transition_to_awaiting_human():
        """워크플로우 라벨 제거 + ah:awaiting-human 부착."""
        for lab in current_workflow_labels:
            try:
                await gh.remove_label(repo, kind, target_n, lab)
            except Exception:
                pass
        try:
            await gh.add_label(repo, kind, target_n, "ah:awaiting-human")
        except Exception as exc:
            log.warning("developer.add_awaiting_label_failed",
                        kind=kind, n=target_n, error=str(exc))

    if result.error_kind == "edit_apply":
        # 자동 retry — 라벨 그대로 유지 (다음 tick 에 다시 시도)
        info = result.edit_apply_info or {}
        try:
            retry_label = "ah:needs-execution" if kind == "issue" else "ah:in-debate"
            await comment_fn(repo, target_n,
                f"❌ **edit 매칭 실패 — 자동 retry** (mode=`{mode}`)\n\n"
                f"- 파일: `{info.get('path', '?')}`\n"
                f"- edit index: `{info.get('edit_idx', '?')}`\n"
                f"- 사유: {info.get('message', result.error or '?')}\n\n"
                f"찾으려던 `old_str` (앞 200자):\n"
                f"```\n{info.get('old_str_head', '')}\n```\n\n"
                f"💡 다음 tick 의 developer 가 이 코멘트를 SoT context 로 보고 "
                f"현재 본문 재확인 후 plan 재생성. 무한 retry 멈추려면 "
                f"`{retry_label}` 라벨 제거.\n\n"
                f"{_cost_footer(result)}"
            )
            # 라벨이 떨어졌으면 재부착 (보호) — edit_apply 는 retry queue 유지
            try:
                await gh.add_label(repo, kind, target_n, retry_label)
            except Exception:
                pass
        except Exception as inner:
            log.warning("developer.edit_apply_comment_failed", error=str(inner))
        # True 반환 — 호출자가 awaiting-human 부여 안 하게
        return True

    if result.error_kind == "no_changes":
        # claude 가 작업 거부 (예: "이건 안 고치는 게 맞다") — 사람 결정으로 escalate
        try:
            await comment_fn(repo, target_n,
                f"🛑 **변경 사항 없음 — 사람 결정 대기** (mode=`{mode}`)\n\n"
                f"developer 가 worktree 변경 0건. 작업 거부 또는 \"이건 안 고치는 게 맞다\" 판단.\n\n"
                f"summary: {result.summary or '(없음)'}\n\n"
                f"라벨 전이: 현재 워크플로우 라벨 → `ah:awaiting-human`. "
                f"사람이 PR/issue 보고 결정 (merge / 라벨 재부착으로 사이클 재개).\n\n"
                f"{_cost_footer(result)}"
            )
        except Exception as inner:
            log.warning("developer.no_changes_comment_failed", error=str(inner))
        await _transition_to_awaiting_human()
        return False

    # no_plan / crashed / 기타 — 사람 escalation
    try:
        await comment_fn(repo, target_n,
            f"🛑 **developer 실패 — 사람 결정 대기** (mode=`{mode}`, kind=`{result.error_kind or '?'}`)\n\n"
            f"```\n{result.error or '(no error message)'}\n```\n\n"
            f"라벨 전이: 현재 워크플로우 라벨 → `ah:awaiting-human`.\n\n"
            f"{_cost_footer(result)}"
        )
    except Exception as inner:
        log.warning("developer.failure_comment_failed", error=str(inner))
    await _transition_to_awaiting_human()
    return False


async def run_developer_amend(
    repo: str,
    pr: gh.PullRequest,
    sot: SourceOfTruth,
    bot_user: str,
    model: Optional[str] = None,
    repo_cwd: Optional[Path] = None,
) -> bool:
    """`ah:needs-execution` PR 1개 → 같은 branch 에 amend commit + `ah:needs-review`.

    이전 흐름 (PR close + 새 PR) 폐기 — review thread / git history / `Closes #N` 보존.
    Returns True if successful.
    """
    cwd = _resolve_repo_cwd(repo, repo_cwd)
    if not (cwd / ".git").exists():
        log.error("developer.amend.cwd_not_git", cwd=str(cwd))
        return False
    if not await lock.acquire(repo, "pr", pr.number, bot_user, repo_cwd=cwd):
        log.info("developer.amend_lock_skipped", pr=pr.number)
        return False

    try:

        # ── PR context 캐시 (ADR-015) ──
        from orchestrator import pr_context as prctx
        ctx = await prctx.discover_pr(
            repo=repo, repo_cwd=cwd, pr_number=pr.number, pr=pr,
        )
        recent_comments = ctx.comments
        EDIT_FAIL_MARK = "❌ **edit 매칭 실패"
        edit_fail_count = sum(1 for c in recent_comments if EDIT_FAIL_MARK in c["body"])
        if edit_fail_count >= 2:
            log.warning("developer.amend.retry_cap_reached",
                        pr=pr.number, count=edit_fail_count)
            try:
                await gh.remove_label(repo, "pr", pr.number, "ah:in-debate")
            except Exception:
                pass
            try:
                await gh.add_label(repo, "pr", pr.number, "ah:awaiting-human")
            except Exception:
                pass
            await gh.comment_pr(repo, pr.number,
                f"🛑 **retry cap (1회) 도달 — 사람 결정 대기**\n\n"
                f"두 차례 amend 시도에서 edit 매칭 실패 ({edit_fail_count}회). "
                f"자동 retry 중단."
            )
            return True

        # review summary — debate summary 우선, 없으면 raw comments
        review_summary = ""
        # 1) debate summary (round 2+ 시 캐시 또는 생성)
        if prctx.needs_summary(ctx):
            log.info("developer.amend.generate_summary", pr=pr.number, round=ctx.debate_round)
            try:
                summary_text = await prctx.generate_debate_summary(
                    repo_cwd=cwd, ctx=ctx,
                )
                if summary_text:
                    try:
                        await gh.comment_pr(repo, pr.number,
                            f"📋 **debate summary (round {ctx.debate_round})**\n\n"
                            f"{summary_text}\n\n"
                            f"---\n{prctx.SUMMARY_MARKER}\n{summary_text}"
                        )
                    except Exception as exc:
                        log.warning("developer.amend.summary_post_failed", error=str(exc))
            except Exception as exc:
                log.warning("developer.amend.summary_failed", error=str(exc))
        # 2) summary 가 있으면 prompt 에 prepend (raw 대신 압축)
        if ctx.debate_summary:
            review_summary += (
                f"\n## 📋 Debate summary (round {ctx.debate_round})\n"
                f"{ctx.debate_summary}\n\n"
                f"---\n## 최근 코멘트 (참고만 — 위 summary 가 우선)\n"
            )
        # 3) 최근 5개 raw (보조)
        for c in recent_comments[-5:]:
            snippet = c["body"][:1500]
            review_summary += f"\n--- {c['author']} @ {c['createdAt']} ---\n{snippet}\n"

        # retry hint (1회 실패한 경우)
        retry_hint = ""
        if edit_fail_count == 1:
            last_fail = None
            for c in reversed(recent_comments):
                if EDIT_FAIL_MARK in c["body"]:
                    last_fail = c["body"]
                    break
            retry_hint = (
                "\n\n---\n\n"
                "⚠️ **이번이 retry #1 / cap 1 — 직전 amend 가 edit 매칭 실패** ⚠️\n\n"
                "직전 시도에서 edit 의 `old_str` 매칭이 안 됐어. 이번엔:\n\n"
                "1. **위 ❌ 코멘트 (직전 실패 정보) 정확히 읽기**\n"
                "2. **현재 PR branch 의 파일 정확한 내용 확인** (직전 plan 이 변경한 후 상태)\n"
                "3. **직전과 같은 `old_str` 절대 금지** — 충분한 unique context (5+ 줄) 포함\n"
                "4. 이번에도 실패하면 자동 retry 없음 — awaiting-human 으로 escalate\n"
                f"\n\n## 직전 실패 코멘트 (원문)\n{last_fail[:2000] if last_fail else '(원문 추출 실패)'}\n"
            )

        mode = resolve_mode("developer-amend")
        log.info("developer.amend.start", pr=pr.number, mode=mode,
                 prev_fails=edit_fail_count)

        ctx = ExecutionContext(
            repo=repo,
            repo_cwd=cwd,
            role="developer-amend",
            sot_prompt=sot.to_prompt(),
            user_prompt=_build_amend_user_prompt(pr, review_summary, retry_hint),
            issue_or_pr_number=pr.number,
            existing_branch=pr.head_ref,
            title_hint=pr.title,
            model=model,
        )

        runner = get_runner("developer-amend", mode=mode)
        try:
            result = await runner.execute(ctx)
        except Exception as exc:
            log.exception("developer.amend.runner_crashed", pr=pr.number, error=str(exc))
            try:
                await gh.comment_pr(repo, pr.number,
                    f"❌ **developer (amend) 예외 발생** (mode=`{mode}`)\n\n"
                    f"```\n{type(exc).__name__}: {exc}\n```\n\n"
                    f"<details><summary>traceback</summary>\n\n"
                    f"```\n{_format_tb(exc)}\n```\n\n</details>"
                )
            except Exception as inner:
                log.warning("developer.amend.error_comment_failed", error=str(inner))
            return False

        log.info("developer.amend.runner_done", pr=pr.number, ok=result.ok,
                 error_kind=result.error_kind)

        if result.ok:
            # PR 은 이미 존재 — 라벨만 swap + 성공 코멘트
            # debate cycle: ah:in-debate 제거 (reviewer 가 다시 평가할 차례)
            try:
                await gh.remove_label(repo, "pr", pr.number, "ah:in-debate")
            except Exception:
                pass
            try:
                await gh.add_label(repo, "pr", pr.number, "ah:needs-review")
            except Exception as exc:
                log.warning("developer.amend.add_label_failed", error=str(exc))

            await gh.comment_pr(repo, pr.number,
                f"✅ **amend commit 추가됨** (mode=`{mode}`, "
                f"{result.commits_applied} commit, {result.files_changed} files)\n\n"
                f"- branch: `{result.branch}`\n"
                f"- summary: {result.summary or '(없음)'}\n"
                f"- 다음: code-reviewer 가 다시 review (`ah:needs-review`)\n\n"
                f"{_cost_footer(result)}"
            )
            log.info("developer.amend.pushed", pr=pr.number, branch=result.branch, mode=mode)
            return True

        return await _handle_developer_failure(
            repo=repo, kind="pr", target_n=pr.number,
            result=result, mode=mode,
        )
    finally:
        await lock.release(repo, "pr", pr.number, bot_user, repo_cwd=cwd)


# ── back-compat aliases (ADR-012 rename — 다음 release 까지 유지) ─────────────
# 기존 caller (poller, palette-executor.sh, 외부 스크립트) 가 옛 이름으로 import
# 하는 경우 깨지지 않게.
run_code_executor = run_developer
run_code_executor_amend = run_developer_amend


# ── SoT Bootstrap (ah init 의 일부) ──────────────────────────────────────────


async def run_sot_bootstrap(
    repo: str,
    repo_cwd: Path,
    force_regenerate: bool = False,
    model: Optional[str] = None,
) -> dict:
    """프로젝트의 SoT 초안 작성 — CLAUDE.md / docs/ARCHITECTURE / GLOSSARY / CONVENTIONS / ADR-000.

    이미 존재하는 파일은 force_regenerate=False 면 skip. claude -p 가 repo_cwd 에서
    Glob/Read 로 코드베이스 스캔 후 Write 로 직접 파일 작성.

    Returns: {ok, summary, detected, files_created, files_skipped, todos, cost_usd, error?}
    """
    if not (repo_cwd / ".git").exists():
        return {"ok": False, "error": f"repo cwd 가 git repo 아님: {repo_cwd}"}

    mode = resolve_mode("po")  # bootstrap 도 PO 모드 사용
    if mode != "local":
        return {"ok": False, "error": f"SoT bootstrap 은 local 모드만 지원 (현재 {mode})"}

    # 기존 SoT 가 있으면 prompt 에 inject — 부분 갱신 모드 가능
    sot_prompt = ""
    try:
        from orchestrator.source_of_truth import discover
        sot = await discover(repo_cwd)
        sot_prompt = sot.to_prompt()
    except Exception as exc:
        log.warning("sot_bootstrap.sot_discover_failed", error=str(exc))

    from orchestrator.runners.local_claude import run_sot_bootstrap_local

    log.info("sot_bootstrap.start", repo=repo, cwd=str(repo_cwd),
             force=force_regenerate)

    res = await run_sot_bootstrap_local(
        repo_cwd=repo_cwd,
        repo=repo,
        force_regenerate=force_regenerate,
        sot_prompt=sot_prompt,
        model=model,
    )

    if res.error:
        return {"ok": False, "error": res.error,
                "cost_usd": res.cost_usd, "model": res.model}

    return {
        "ok": True,
        "summary": res.summary,
        "detected": res.detected,
        "files_created": res.files_created,
        "files_skipped": res.files_skipped,
        "files_updated": res.files_updated,
        "todos": res.todos,
        "cost_usd": res.cost_usd,
        "model": res.model,
    }


# ── PO (mode A — 자연어 → issue 분할) ─────────────────────────────────────────


async def run_po_local(
    repo: str,
    user_agenda: str,
    sot: SourceOfTruth,
    repo_cwd: Optional[Path] = None,
    model: Optional[str] = None,
    dry_run: bool = False,
) -> dict:
    """PO mode A — 자연어 → SoT 대조 → 1~N개 issue 생성.

    Returns: {"ok": bool, "created": [{"number": int, "url": str, "title": str}, ...],
              "summary": str, "split_rationale": str, "cost_usd": float, "error": str?}

    dry_run=True 면 issue 생성 안 하고 LocalPoResult 의 issues 만 반환 (디버깅).
    """
    cwd = repo_cwd
    if cwd is None:
        name = repo.split("/")[-1]
        cwd = Path.home() / "dev-private" / name
        if not (cwd / ".git").exists():
            cwd = Path.home() / "dev" / name
    if not (cwd / ".git").exists():
        return {"ok": False, "created": [], "summary": "",
                "error": f"repo cwd 가 git repo 아님: {cwd}"}

    mode = resolve_mode("po")
    log.info("po.start", repo=repo, mode=mode, agenda_chars=len(user_agenda))

    if mode != "local":
        return {"ok": False, "created": [], "summary": "",
                "error": f"PO mode={mode!r} 미구현 — 현재 local 모드만 지원 (PO_MODE=local 또는 HARNESS_MODE=local)"}

    # 표준 라벨 ensure (멱등)
    try:
        await gh.ensure_standard_labels(repo)
    except Exception as exc:
        log.warning("po.labels_ensure_failed", error=str(exc))

    from orchestrator.runners.local_claude import run_po_mode_a_local
    res = await run_po_mode_a_local(
        repo_cwd=cwd,
        repo=repo,
        user_agenda=user_agenda,
        sot_prompt=sot.to_prompt(),
        model=model,
    )

    if res.error:
        return {"ok": False, "created": [], "summary": res.summary,
                "error": res.error, "cost_usd": res.cost_usd}

    if not res.issues:
        return {
            "ok": False, "created": [], "summary": res.summary,
            "split_rationale": res.split_rationale,
            "error": "PO 가 issue 0건 반환 — 추가 정보 필요. summary 확인: " + (res.summary or "(없음)"),
            "cost_usd": res.cost_usd,
        }

    if dry_run:
        return {
            "ok": True, "created": [], "summary": res.summary,
            "split_rationale": res.split_rationale,
            "issues_preview": res.issues,
            "cost_usd": res.cost_usd, "model": res.model,
        }

    created: list[dict] = []
    for it in res.issues:
        try:
            issue = await gh.create_issue(
                repo,
                title=it["title"],
                body=it["body"] + "\n\n---\n_🤖 Generated by agentic-harness PO (mode A, local)_",
                labels=it["labels"],
            )
            created.append({"number": issue.number, "url": issue.url, "title": issue.title})
            log.info("po.issue_created", repo=repo, number=issue.number, title=issue.title)
        except Exception as exc:
            log.exception("po.issue_create_failed", title=it["title"][:60], error=str(exc))
            created.append({"number": None, "url": None, "title": it["title"], "error": str(exc)})

    return {
        "ok": any(c.get("number") for c in created),
        "created": created,
        "summary": res.summary,
        "split_rationale": res.split_rationale,
        "cost_usd": res.cost_usd,
        "model": res.model,
    }


# ── code-reviewer ────────────────────────────────────────────────────────────


async def run_code_reviewer(
    repo: str,
    pr: gh.PullRequest,
    sot: SourceOfTruth,
    bot_user: str,
    model: Optional[str] = None,
    repo_cwd: Optional[Path] = None,
) -> bool:
    """`ah:needs-review` PR 1개 review → comment + 라벨 전이.

    verdict 별 처리 (ADR-012 debate cycle):
      - approve                                → `ah:awaiting-human` (사람 merge)
      - request_changes / concerns_noted       → `ah:in-debate`
                                                  (developer amend 사이클 — approve 까지 계속)
      - debate round cap (3회) 도달            → `ah:awaiting-human` (사람 escalation)

    HARNESS_MODE / REVIEWER_MODE 로 분기:
      - hermes → llm.call (단일 LLM 호출, 기존 동작)
      - local  → claude -p 헤드리스 + opus (ADR-011)
    """
    mode = resolve_mode("reviewer")
    if mode == "hermes":
        model = model or os.environ.get("REVIEWER_MODEL", "gpt-5.3-codex")
    elif mode == "local":
        from orchestrator.runners import resolve_local_model
        model = model or resolve_local_model("reviewer")  # default: sonnet
    else:
        raise RuntimeError(f"unknown reviewer mode: {mode}")

    cwd_for_lock = _resolve_repo_cwd(repo, repo_cwd)
    if not (cwd_for_lock / ".git").exists():
        log.error("reviewer.cwd_not_git", cwd=str(cwd_for_lock))
        return False
    if not await lock.acquire(repo, "pr", pr.number, bot_user, repo_cwd=cwd_for_lock):
        log.info("reviewer.lock_skipped", pr=pr.number)
        return False

    try:
        try:
            # PR context 캐시 (ADR-015) — 4개 호출 → 1개 (cache hit) 또는 1 set (병렬 fetch)
            from orchestrator import pr_context as prctx
            cwd_for_cache = repo_cwd
            if cwd_for_cache is None:
                name = repo.split("/")[-1]
                cwd_for_cache = Path.home() / "dev-private" / name
                if not (cwd_for_cache / ".git").exists():
                    cwd_for_cache = Path.home() / "dev" / name
            if not (cwd_for_cache / ".git").exists():
                cwd_for_cache = Path.cwd()
            ctx = await prctx.discover_pr(
                repo=repo, repo_cwd=cwd_for_cache, pr_number=pr.number, pr=pr,
            )
            diff = ctx.diff
            files = ctx.files
            linked = ctx.linked_issues
            comments = ctx.comments

            linked_summary = ""
            for n in linked[:3]:
                try:
                    iss = await gh.get_issue(repo, n)
                    linked_summary += f"\n### Linked issue #{n}: {iss.title}\n{iss.body[:1500]}\n"
                except Exception:
                    pass

            # debate summary 우선 (round 2+ — round 1 은 raw 그대로)
            comments_summary = ""
            human_feedback_present = False
            if prctx.needs_summary(ctx):
                log.info("reviewer.generate_summary", pr=pr.number, round=ctx.debate_round)
                try:
                    summary_text = await prctx.generate_debate_summary(
                        repo_cwd=cwd_for_cache, ctx=ctx,
                    )
                    if summary_text:
                        try:
                            await gh.comment_pr(repo, pr.number,
                                f"📋 **debate summary (round {ctx.debate_round})**\n\n"
                                f"{summary_text}\n\n"
                                f"---\n{prctx.SUMMARY_MARKER}\n{summary_text}"
                            )
                        except Exception as exc:
                            log.warning("reviewer.summary_post_failed", error=str(exc))
                except Exception as exc:
                    log.warning("reviewer.summary_failed", error=str(exc))
            if ctx.debate_summary:
                comments_summary += (
                    f"## 📋 Debate summary (round {ctx.debate_round})\n"
                    f"{ctx.debate_summary}\n\n"
                    f"---\n## 최근 raw 코멘트 (참고용)\n"
                )
            if comments:
                lines = []
                for c in comments[-10:]:
                    author = c["author"]
                    is_bot = author.lower().endswith("[bot]") or author == bot_user
                    if not is_bot and c["body"].strip():
                        human_feedback_present = True
                    snippet = c["body"][:1200]
                    lines.append(f"--- {author} @ {c['createdAt']} ---\n{snippet}")
                comments_summary += "\n\n".join(lines)

            # 프롬프트 빌드 — mode 별 prompt 파일 다름
            prompt_name = "code-reviewer-local" if mode == "local" else "code-reviewer"
            system_prompt = _load_agent_prompt(prompt_name) + "\n\n" + sot.to_prompt()
            files_summary = "\n".join(
                f"- `{f['path']}` (+{f['additions']} / -{f['deletions']})"
                for f in files
            )
            reroute_hint = ""
            if human_feedback_present:
                reroute_hint = (
                    "\n\n⚠ **이 PR 은 사람이 직접 `ah:needs-review` 라벨을 다시 붙였거나 "
                    "코멘트로 추가 피드백을 남겼을 가능성이 높음.** 위 PR comments 의 사람 "
                    "피드백을 반드시 반영해서 review.\n"
                )
            user_msg = (
                f"# PR #{pr.number}: {pr.title}\n\n"
                f"URL: {pr.url}\n"
                f"branch: {pr.head_ref} → {pr.base_ref}\n"
                f"labels: {', '.join(pr.labels)}\n\n"
                f"## PR body\n{pr.body}\n\n"
                f"## Files changed ({len(files)})\n{files_summary}\n\n"
                f"## Linked issues\n{linked_summary or '(없음)'}\n\n"
                f"## PR comments ({len(comments)}) — 사람 피드백 / 이전 review\n"
                f"{comments_summary or '(없음)'}\n"
                f"{reroute_hint}"
                f"## Diff\n```diff\n{diff}\n```\n"
            )

            # mode 별 호출 분기
            log.info("reviewer.start", pr=pr.number, mode=mode, model=model)
            if mode == "local":
                from orchestrator.runners.local_claude import run_reviewer_local
                cwd = repo_cwd
                if cwd is None:
                    name = repo.split("/")[-1]
                    cwd = Path.home() / "dev" / name
                if not (cwd / ".git").exists():
                    cwd = Path.cwd()  # fallback — reviewer 는 read-only 라 덜 critical
                local_res = await run_reviewer_local(
                    repo_cwd=cwd,
                    system_prompt=system_prompt,
                    user_prompt=user_msg,
                    model=model,
                )
                if local_res.review is None:
                    await gh.comment_pr(repo, pr.number,
                        f"❌ code-reviewer (mode=local): {local_res.error or 'JSON 파싱 실패'}\n\n"
                        f"<details><summary>raw</summary>\n\n```\n{local_res.raw_text[:2000]}\n```\n\n</details>"
                    )
                    return False
                review = local_res.review
                # llm.LlmCall 호환 객체 (cost_footer / format_review_comment 용)
                call_info = llm.LlmCall(
                    model=local_res.model,
                    input_tokens=local_res.input_tokens,
                    output_tokens=local_res.output_tokens,
                    _cost_usd=local_res.cost_usd,
                )
            else:
                text, call_info = await llm.call(
                    model=model, system=system_prompt, user=user_msg, max_tokens=4000,
                )
                review = _extract_json(text)
                if review is None:
                    await gh.comment_pr(repo, pr.number,
                        f"❌ code-reviewer: LLM 응답 JSON 파싱 실패.\n\n"
                        f"<details><summary>raw</summary>\n\n```\n{text[:2000]}\n```\n\n</details>"
                    )
                    return False

            comment_body = _format_review_comment(review, call_info)
            verdict = (review.get("verdict") or "concerns_noted").lower()
            event_map = {
                "approve": "APPROVE",
                "request_changes": "REQUEST_CHANGES",
                "concerns_noted": "COMMENT",
            }
            try:
                event = event_map.get(verdict, "COMMENT")
                await gh.submit_pr_review(repo, pr.number, body=comment_body, event=event)
            except Exception:
                await gh.comment_pr(repo, pr.number, comment_body)

            try:
                await gh.remove_label(repo, "pr", pr.number, "ah:needs-review")
            except Exception as exc:
                log.warning("reviewer.remove_label_failed", error=str(exc))

            # verdict 별 라벨 전이 (ADR-012)
            # approve         → awaiting-human (사람 merge)
            # 그 외 (concerns_noted / request_changes) → developer 가 approve 받을
            #   때까지 amend. 단 cap (DEBATE_ROUND_CAP=3) 넘으면 escalate.
            DEBATE_ROUND_CAP = int(os.environ.get("DEBATE_ROUND_CAP", "3"))
            DEBATE_MARK = "🔁 **debate round"

            if verdict == "approve":
                # 최종 게이트 통과 — 사람 merge 결정 대기
                # in-debate 라벨이 붙어있었다면 제거
                try:
                    await gh.remove_label(repo, "pr", pr.number, "ah:in-debate")
                except Exception:
                    pass
                try:
                    await gh.add_label(repo, "pr", pr.number, "ah:awaiting-human")
                except Exception as exc:
                    log.warning("reviewer.add_label_failed", error=str(exc))
            else:
                # concerns_noted 또는 request_changes — developer 가 대응해야 함
                # debate round 카운트 — 기존 "🔁 debate round" 코멘트 갯수
                debate_round = sum(1 for c in comments if DEBATE_MARK in c["body"]) + 1

                if debate_round > DEBATE_ROUND_CAP:
                    # cap 도달 — 사람 escalation (critique tie-break 미구현 fallback)
                    log.warning("reviewer.debate_cap_reached",
                                pr=pr.number, round=debate_round, cap=DEBATE_ROUND_CAP)
                    try:
                        await gh.remove_label(repo, "pr", pr.number, "ah:in-debate")
                    except Exception:
                        pass
                    try:
                        await gh.add_label(repo, "pr", pr.number, "ah:awaiting-human")
                    except Exception as exc:
                        log.warning("reviewer.cap_label_failed",
                                    pr=pr.number, error=str(exc))
                    try:
                        await gh.comment_pr(repo, pr.number,
                            f"🛑 **debate round cap ({DEBATE_ROUND_CAP}) 도달 — 사람 결정 대기**\n\n"
                            f"reviewer 가 {debate_round-1}회 연속으로 approve 안 함. "
                            f"자동 amend 중단. 사람이 PR 직접 검토 후 결정.\n\n"
                            f"이어서 자동 진행하려면 `ah:awaiting-human` → `ah:in-debate` 로 라벨 교체."
                        )
                    except Exception as exc:
                        log.warning("reviewer.cap_comment_failed",
                                    pr=pr.number, error=str(exc))
                else:
                    # debate 사이클 진행 — ah:in-debate 단일 라벨이 trigger + 상태 둘 다
                    # (이전엔 needs-execution 도 같이 붙였는데 redundant — needs-execution
                    #  은 issue 전용 의미로 정리)
                    try:
                        await gh.add_label(repo, "pr", pr.number, "ah:in-debate")
                    except Exception as exc:
                        log.warning("reviewer.add_debate_label_failed",
                                    pr=pr.number, error=str(exc))
                    verdict_kor = {
                        "request_changes": "request_changes (🔴)",
                        "concerns_noted": "concerns_noted (🟡)",
                    }.get(verdict, verdict)
                    try:
                        await gh.comment_pr(repo, pr.number,
                            f"{DEBATE_MARK} {debate_round}/{DEBATE_ROUND_CAP}** "
                            f"— reviewer verdict: **{verdict_kor}**\n\n"
                            f"developer 가 amend mode 로 위 review 의견 반영 예정. "
                            f"approve 나올 때까지 이 사이클 반복 (cap {DEBATE_ROUND_CAP}회).\n\n"
                            f"사람 개입 필요하면 PR 의 `ah:in-debate` 라벨 떼서 멈출 수 있음."
                        )
                    except Exception as exc:
                        log.warning("reviewer.debate_comment_failed",
                                    pr=pr.number, error=str(exc))

            log.info("reviewer.done", pr=pr.number, verdict=verdict,
                     needs_adr=bool(review.get("needs_adr")),
                     in_debate=(verdict != "approve"),
                     cost=round(call_info.cost_usd, 4))
            return True
        except Exception as exc:
            log.exception("reviewer.crashed", pr=pr.number, error=str(exc))
            try:
                err_type = type(exc).__name__
                await gh.comment_pr(repo, pr.number,
                    f"❌ **code-reviewer 예외 발생**\n\n"
                    f"```\n{err_type}: {exc}\n```\n\n"
                    f"<details><summary>traceback</summary>\n\n"
                    f"```\n{_format_tb(exc)}\n```\n\n</details>"
                )
            except Exception as inner:
                log.warning("reviewer.error_comment_failed", error=str(inner))
            return False
    finally:
        await lock.release(repo, "pr", pr.number, bot_user, repo_cwd=cwd_for_lock)


def _format_review_comment(review: dict, call_info: llm.LlmCall) -> str:
    verdict = review.get("verdict", "?")
    verdict_emoji = {
        "approve": "✅", "request_changes": "🔴", "concerns_noted": "🟡",
    }.get(verdict, "❓")

    parts = [
        f"## 🤖 code-reviewer — {verdict_emoji} `{verdict}`",
        "",
        f"**Summary**: {review.get('summary', '?')}",
        "",
    ]

    sc = review.get("scope_check") or {}
    if sc:
        match = "✓" if sc.get("match") else "✗"
        parts += [
            f"### Scope check — {match}",
            f"- **Issue intent**: {sc.get('issue_intent', '?')}",
            f"- **PR does**: {sc.get('pr_does', '?')}",
        ]
        if sc.get("comment"):
            parts.append(f"- **Note**: {sc['comment']}")
        parts.append("")

    concerns = review.get("concerns") or []
    if concerns:
        parts.append(f"### Concerns ({len(concerns)})")
        sev_emoji = {"blocker": "🚫", "major": "⚠️", "minor": "🟡", "nit": "💭"}
        for c in concerns:
            sev = (c.get("severity") or "minor").lower()
            cat = c.get("category") or "?"
            loc = c.get("location") or ""
            loc_str = f" @ `{loc}`" if loc else ""
            parts.append(
                f"- {sev_emoji.get(sev, '•')} **[{sev}/{cat}]**{loc_str} {c.get('comment', '')}"
            )
        parts.append("")

    if review.get("needs_adr"):
        parts += [
            f"### 🏛 ADR 필요",
            review.get("adr_reason", "이유 미명시"),
            "",
        ]

    positives = review.get("positives") or []
    if positives:
        parts.append("### 잘한 점")
        for p in positives:
            parts.append(f"- ✓ {p}")
        parts.append("")

    if verdict == "request_changes":
        parts += [
            "### 머지 전 필수 조건",
            "- [ ] CI(빌드/테스트) 전체 green 통과",
            "",
        ]

    parts += [
        "---",
        f"_cost ${call_info.cost_usd:.4f} · "
        f"{call_info.input_tokens} in / {call_info.output_tokens} out · model={call_info.model}_",
    ]
    return "\n".join(parts)
