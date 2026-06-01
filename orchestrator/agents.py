"""Agent 실행 — Phase 1 MVP 는 code-executor 만.

각 agent 는:
  1. SOT prompt + issue context 로 Claude 호출
  2. 결과 JSON 파싱
  3. issue 에 plan 코멘트 + 다음 라벨 부착 (ah:needs-review)
  4. 락 해제

코드 직접 수정은 안 함 (안전성). 사람이 plan 보고 apply or 거절.
Phase 2 에서 worktree + sandbox 붙으면 자동 PR 까지.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

import structlog

from orchestrator import code_tools, gh, git_apply, llm, lock
from orchestrator.git_apply import EditApplyError
from orchestrator.source_of_truth import SourceOfTruth

log = structlog.get_logger()


AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents"


def _load_agent_prompt(name: str) -> str:
    return (AGENTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _extract_json(text: str) -> Optional[dict]:
    """LLM 응답에서 JSON 추출. ```json ... ``` 감싸기도 처리."""
    text = text.strip()
    # backtick 감싸기 strip
    m = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        log.warning("agent.json_parse_failed", error=str(exc), text=text[:300])
        return None


def _coerce(value, expected_type):
    """LLM 이 nested array/object 를 stringified JSON 으로 넣는 경우 복구.

    Anthropic SDK 가 tool input schema 를 strict 검증 안 하는 경우가 있음 —
    Haiku 가 commits/files/edits 를 string 으로 출력하면 그대로 dict 에 들어옴.
    """
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


# ── code-executor ────────────────────────────────────────────────────────────


async def run_code_executor(
    repo: str,
    issue: gh.Issue,
    sot: SourceOfTruth,
    bot_user: str,
    model: Optional[str] = None,
    repo_cwd: Optional[Path] = None,
) -> bool:
    """`ah:needs-execution` issue 1개 처리 → plan comment + `ah:needs-review` 라벨.

    Returns True if successful (next agent 가 받을 수 있는 상태로 전이).
    """
    model = model or os.environ.get("EXECUTOR_MODEL", "gpt-5.3-codex")

    if not await lock.acquire(repo, "issue", issue.number, bot_user):
        log.info("executor.lock_skipped", issue=issue.number)
        return False

    try:
        try:
            system_prompt = _load_agent_prompt("code-executor") + "\n\n" + sot.to_prompt()
            user_msg = (
                f"# Issue #{issue.number}: {issue.title}\n\n"
                f"URL: {issue.url}\n"
                f"Labels: {', '.join(issue.labels)}\n\n"
                f"## Body\n{issue.body}\n\n"
                f"---\n\n"
                f"**먼저 list_files / read_file / search_text 도구로 관련 코드 파악한 후** "
                f"plan JSON 출력. 추측 X. 도구 결과 확인 후에만 file content 작성."
            )

            # repo_cwd 결정 (apply 단계와 같음)
            cwd = repo_cwd
            if cwd is None:
                name = repo.split("/")[-1]
                cwd = Path.home() / "dev" / name
            if not (cwd / ".git").exists():
                raise RuntimeError(f"repo cwd 가 git repo 아님: {cwd}")

            async def _tool_exec(name: str, args: dict) -> str:
                return await code_tools.execute_tool(cwd, name, args)

            result, call_info, tool_trace = await llm.call_with_tools(
                model=model,
                system=system_prompt,
                user=user_msg,
                tools=code_tools.TOOL_SCHEMAS,
                tool_executor=_tool_exec,
                stop_tool="submit_plan",          # LLM 이 이 도구 호출하면 즉시 plan 확정
                max_iterations=15,
                max_tokens=16000,
                cost_cap_usd=float(os.environ.get("EXECUTOR_COST_CAP", "0.80")),
            )

            log.info("executor.tools_used", issue=issue.number,
                     tool_calls=len(tool_trace),
                     tools=[t["name"] for t in tool_trace])

            # stop_tool 로 종료된 경우 result 는 plan dict, 아니면 text (실패 메시지)
            if isinstance(result, dict):
                plan = _normalize_plan(result)
            else:
                await gh.comment_issue(repo, issue.number,
                    f"❌ code-executor: submit_plan 도구 호출 없이 종료.\n\n"
                    f"<details><summary>last text</summary>\n\n```\n{str(result)[:2000]}\n```\n\n"
                    f"tools called: {[t['name'] for t in tool_trace]}\n</details>"
                )
                return False

            # plan 은 PR description (pr_body) 에 들어감 — issue 엔 PR 링크만 (아래).
            # ── 실제 PR 생성 — worktree + apply + push + gh pr create ──
            # cwd 는 위에서 이미 결정됨 (tool_exec 와 같은 경로)
            apply_info = await git_apply.apply_plan_and_push(
                repo_cwd=cwd, plan=plan, issue_number=issue.number,
            )

            # 본문에 'Closes #N' 자동 추가 — PR merge 시 issue 자동 close
            pr_body = (plan.get("pr_body") or "").rstrip()
            if f"#{issue.number}" not in pr_body:
                pr_body += f"\n\n---\nCloses #{issue.number}"
            pr_body += "\n\n_🤖 Generated by agentic-harness code-executor_"

            pr = await gh.create_pr(
                repo,
                title=plan.get("pr_title") or f"[#{issue.number}] {issue.title}",
                body=pr_body,
                head=apply_info["branch"],
                base=apply_info["base"],
                labels=["ah:needs-review"],
            )

            # issue 에 PR 링크 코멘트 + ah:needs-execution 제거
            await gh.comment_issue(repo, issue.number,
                f"✅ **PR 생성됨** → {pr.url}\n\n"
                f"- branch: `{apply_info['branch']}`\n"
                f"- commits: {apply_info['commits_applied']}\n"
                f"- 다음 단계: code-reviewer 가 PR 받아 review (`ah:needs-review`)\n\n"
                f"_executor cost ${call_info.cost_usd:.4f} · "
                f"{call_info.input_tokens} in / {call_info.output_tokens} out · "
                f"model={call_info.model}_"
            )
            try:
                await gh.remove_label(repo, "issue", issue.number, "ah:needs-execution")
            except Exception:
                pass
            log.info("executor.pr_created", issue=issue.number, pr=pr.number, url=pr.url)
            return True
        except EditApplyError as exc:
            # edit 매칭 실패 — issue 큐 (PR 아직 없음) 로 자동 retry
            log.warning("executor.edit_apply_failed",
                        issue=issue.number, path=exc.path, edit_idx=exc.edit_idx)
            try:
                await gh.comment_issue(repo, issue.number,
                    f"❌ **edit 매칭 실패 — 자동 retry**\n\n"
                    f"- 파일: `{exc.path}`\n"
                    f"- edit index: `{exc.edit_idx}`\n"
                    f"- 사유: {exc.message}\n\n"
                    f"찾으려던 `old_str` (앞 200자):\n"
                    f"```\n{exc.old_str_head}\n```\n\n"
                    f"💡 다음 tick 의 executor 가 이 코멘트를 SOT context 로 보고 "
                    f"read_file 로 정확한 현재 내용 확인 후 plan 재생성합니다. "
                    f"무한 retry 멈추려면 `ah:needs-execution` 라벨 제거하세요."
                )
                # 라벨 ah:needs-execution 다시 부착 (apply 실패라 line 202 의 remove 실행 안 됐을 수도)
                try:
                    await gh.add_label(repo, "issue", issue.number, "ah:needs-execution")
                except Exception:
                    pass
            except Exception as inner:
                log.warning("executor.edit_apply_comment_failed", error=str(inner))
            # True 반환 — script 가 awaiting-human 부여하지 않게 (의미상 retry 큐 진행)
            return True
        except Exception as exc:
            # 어떤 예외든 issue 코멘트로 노출 — 디버깅 가시성
            log.exception("executor.crashed", issue=issue.number, error=str(exc))
            try:
                err_type = type(exc).__name__
                await gh.comment_issue(repo, issue.number,
                    f"❌ **code-executor 예외 발생**\n\n"
                    f"```\n{err_type}: {exc}\n```\n\n"
                    f"<details><summary>traceback</summary>\n\n"
                    f"```\n{_format_tb(exc)}\n```\n\n</details>"
                )
            except Exception as inner:
                log.warning("executor.error_comment_failed", error=str(inner))
            return False
    finally:
        await lock.release(repo, "issue", issue.number, bot_user)


async def run_code_executor_amend(
    repo: str,
    pr: gh.PullRequest,
    sot: SourceOfTruth,
    bot_user: str,
    model: Optional[str] = None,
    repo_cwd: Optional[Path] = None,
) -> bool:
    """`ah:needs-execution` PR 1개 처리 → 같은 branch 에 추가 commit + `ah:needs-review`.

    이전 흐름 (PR close + 새 PR) 대비:
      - GitHub review thread 와 git history 보존
      - reviewer 의 직전 review comment 가 SOT 의 recent PR comments 로 자연 주입됨
      - Closes #N 그대로 유효

    Returns True if successful.
    """
    model = model or os.environ.get("EXECUTOR_MODEL", "gpt-5.3-codex")

    if not await lock.acquire(repo, "pr", pr.number, bot_user):
        log.info("executor.amend_lock_skipped", pr=pr.number)
        return False

    try:
        try:
            # PR 본문 + 최근 review comment (reviewer 가 요구한 변경) 추출
            recent_comments = await gh.pr_comments(repo, pr.number, limit=20)
            review_summary = ""
            for c in recent_comments[-5:]:
                snippet = c["body"][:1500]
                review_summary += f"\n--- {c['author']} @ {c['createdAt']} ---\n{snippet}\n"

            # ── retry cap (1회) — 같은 file 의 edit 실패가 2번 이상이면 사람 결정 대기 ──
            # 카운트 방식: PR 코멘트 중 "edit 매칭 실패" prefix 의 개수
            EDIT_FAIL_MARK = "❌ **edit 매칭 실패"
            edit_fail_count = sum(1 for c in recent_comments if EDIT_FAIL_MARK in c["body"])
            if edit_fail_count >= 2:
                log.warning("executor.amend.retry_cap_reached",
                            pr=pr.number, count=edit_fail_count)
                try:
                    await gh.remove_label(repo, "pr", pr.number, "ah:needs-execution")
                except Exception:
                    pass
                try:
                    await gh.add_label(repo, "pr", pr.number, "ah:awaiting-human")
                except Exception:
                    pass
                await gh.comment_pr(repo, pr.number,
                    f"🛑 **retry cap (1회) 도달 — 사람 결정 대기**\n\n"
                    f"두 차례 amend 시도에서 edit 매칭 실패 ({edit_fail_count}회). "
                    f"자동 retry 중단. 사람이 직접 수정하거나, 다른 model (sonnet) 로 retry 검토.\n\n"
                    f"_무한 retry 방지 cap. 다시 시도하려면 위 ❌ 코멘트들 hide 후 라벨 `ah:needs-execution` 재부착._"
                )
                return True

            # ── retry hint — 직전 amend 가 실패했다면 LLM 한테 강한 신호 ──
            retry_hint = ""
            if edit_fail_count == 1:
                # 마지막 ❌ 코멘트 추출 — 어떤 file / edit / old_str 이 실패했는지
                last_fail = None
                for c in reversed(recent_comments):
                    if EDIT_FAIL_MARK in c["body"]:
                        last_fail = c["body"]
                        break
                retry_hint = (
                    "\n\n---\n\n"
                    "⚠️ **이번이 retry #1 / cap 1 — 직전 amend 가 edit 매칭 실패** ⚠️\n\n"
                    "직전 시도에서 plan 의 edit 가 `old_str` 매칭 실패로 abort. "
                    "이번엔 **반드시 다른 전략으로** 처리해야 함:\n\n"
                    "1. **위 ❌ 코멘트 (직전 실패 정보) 정확히 읽기** — 어떤 file / 어떤 edit / 어떤 old_str 이 실패했는지\n"
                    "2. **`read_file` 도구로 PR branch 의 그 file 의 현재 정확한 내용 확인** "
                    "(직전 plan 이 변경한 후 상태 — 추측 절대 X)\n"
                    "3. **직전과 같은 `old_str` 절대 금지**. 새 plan 작성 시:\n"
                    "   - 같은 영역에 여러 edit 가 있으면 single edit 으로 합치기 (multi-edit 의 순서 의존성 회피)\n"
                    "   - `old_str` 은 충분한 unique context (5+ 줄) 포함\n"
                    "   - SoT 의 CONVENTIONS / GLOSSARY / ADR 참고해서 도메인 정합성 유지\n"
                    "4. 이번에도 실패하면 자동 retry 없음 — awaiting-human 으로 escalate\n\n"
                    "참고할 SoT: 시스템 prompt 의 CLAUDE.md / docs/* / DECISIONS/* 이미 inject 되어 있음."
                    f"\n\n## 직전 실패 코멘트 (원문)\n{last_fail[:2000] if last_fail else '(원문 추출 실패)'}\n"
                )

            system_prompt = _load_agent_prompt("code-executor") + "\n\n" + sot.to_prompt()
            user_msg = (
                f"# PR #{pr.number} (amend mode): {pr.title}\n\n"
                f"URL: {pr.url}\n"
                f"branch: `{pr.head_ref}` (이미 존재 — 추가 commit 만)\n"
                f"Labels: {', '.join(pr.labels)}\n\n"
                f"## PR body\n{pr.body}\n\n"
                f"## 최근 review 의견 (이걸 반영해야 함)\n{review_summary or '(없음)'}\n\n"
                f"---\n\n"
                f"**이 PR 의 branch `{pr.head_ref}` 에 추가 commit 을 만드는 amend 작업이다.**\n"
                f"- 새 PR 만들지 않음 (plan.branch_name 은 무시됨 — 기존 branch 사용)\n"
                f"- reviewer 가 지적한 blocker / concern 을 우선 해결\n"
                f"- 먼저 list_files / read_file / search_text 로 현재 상태 파악 후 plan 출력"
                f"{retry_hint}"
            )

            cwd = repo_cwd
            if cwd is None:
                name = repo.split("/")[-1]
                cwd = Path.home() / "dev" / name
            if not (cwd / ".git").exists():
                raise RuntimeError(f"repo cwd 가 git repo 아님: {cwd}")

            async def _tool_exec(name: str, args: dict) -> str:
                return await code_tools.execute_tool(cwd, name, args)

            result, call_info, tool_trace = await llm.call_with_tools(
                model=model,
                system=system_prompt,
                user=user_msg,
                tools=code_tools.TOOL_SCHEMAS,
                tool_executor=_tool_exec,
                stop_tool="submit_plan",
                max_iterations=15,
                max_tokens=16000,
                cost_cap_usd=float(os.environ.get("EXECUTOR_COST_CAP", "0.80")),
            )

            log.info("executor.amend.tools_used", pr=pr.number,
                     tool_calls=len(tool_trace),
                     tools=[t["name"] for t in tool_trace])

            if isinstance(result, dict):
                plan = _normalize_plan(result)
            else:
                await gh.comment_pr(repo, pr.number,
                    f"❌ code-executor (amend): submit_plan 도구 호출 없이 종료.\n\n"
                    f"<details><summary>last text</summary>\n\n```\n{str(result)[:2000]}\n```\n\n"
                    f"tools called: {[t['name'] for t in tool_trace]}\n</details>"
                )
                return False

            # 기존 branch 에 추가 commit (existing_branch=pr.head_ref)
            apply_info = await git_apply.apply_plan_and_push(
                repo_cwd=cwd,
                plan=plan,
                issue_number=pr.number,                 # commit msg 용
                existing_branch=pr.head_ref,
            )

            # 라벨 swap: needs-execution 제거 + needs-review
            try:
                await gh.remove_label(repo, "pr", pr.number, "ah:needs-execution")
            except Exception as exc:
                log.warning("executor.amend.remove_label_failed", error=str(exc))
            try:
                await gh.add_label(repo, "pr", pr.number, "ah:needs-review")
            except Exception as exc:
                log.warning("executor.amend.add_label_failed", error=str(exc))

            await gh.comment_pr(repo, pr.number,
                f"✅ **amend commit 추가됨** ({apply_info['commits_applied']} commit)\n\n"
                f"- branch: `{apply_info['branch']}`\n"
                f"- 다음: code-reviewer 가 다시 review (`ah:needs-review`)\n\n"
                f"_executor cost ${call_info.cost_usd:.4f} · "
                f"{call_info.input_tokens} in / {call_info.output_tokens} out · "
                f"model={call_info.model}_"
            )

            log.info("executor.amend.pushed", pr=pr.number, branch=apply_info["branch"],
                     commits=apply_info["commits_applied"])
            return True
        except EditApplyError as exc:
            # edit 매칭 실패 — PR 의 ah:needs-execution 라벨 유지하고 자동 retry 큐로
            log.warning("executor.amend.edit_apply_failed",
                        pr=pr.number, path=exc.path, edit_idx=exc.edit_idx)
            try:
                await gh.comment_pr(repo, pr.number,
                    f"❌ **edit 매칭 실패 — 자동 retry**\n\n"
                    f"- 파일: `{exc.path}`\n"
                    f"- edit index: `{exc.edit_idx}`\n"
                    f"- 사유: {exc.message}\n\n"
                    f"찾으려던 `old_str` (앞 200자):\n"
                    f"```\n{exc.old_str_head}\n```\n\n"
                    f"💡 다음 tick 의 executor (amend) 가 이 코멘트를 SOT context 로 보고 "
                    f"read_file 로 PR branch 의 정확한 현재 내용 확인 후 plan 재생성합니다. "
                    f"무한 retry 멈추려면 PR 의 `ah:needs-execution` 라벨 제거하세요."
                )
                # 라벨 swap: in-progress 만 제거하고 needs-execution 다시 부착 → 자동 retry
                try:
                    await gh.add_label(repo, "pr", pr.number, "ah:needs-execution")
                except Exception as exc2:
                    log.warning("executor.amend.relabel_failed", error=str(exc2))
            except Exception as inner:
                log.warning("executor.amend.edit_apply_comment_failed", error=str(inner))
            # 호출자가 awaiting-human 부여 안 하도록 True 반환 (성공으로 처리 — 의미상 retry 진행)
            return True
        except Exception as exc:
            log.exception("executor.amend.crashed", pr=pr.number, error=str(exc))
            try:
                err_type = type(exc).__name__
                await gh.comment_pr(repo, pr.number,
                    f"❌ **code-executor (amend) 예외 발생**\n\n"
                    f"```\n{err_type}: {exc}\n```\n\n"
                    f"<details><summary>traceback</summary>\n\n"
                    f"```\n{_format_tb(exc)}\n```\n\n</details>"
                )
            except Exception as inner:
                log.warning("executor.amend.error_comment_failed", error=str(inner))
            return False
    finally:
        await lock.release(repo, "pr", pr.number, bot_user)


def _format_tb(exc: BaseException, limit: int = 30) -> str:
    """간결한 traceback (마지막 N 프레임)."""
    import traceback
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
    full = "".join(lines)
    return full[-3000:]                          # cap


# ── code-reviewer ────────────────────────────────────────────────────────────


async def run_code_reviewer(
    repo: str,
    pr: gh.PullRequest,
    sot: SourceOfTruth,
    bot_user: str,
    model: Optional[str] = None,
) -> bool:
    """`ah:needs-review` PR 1개 review → comment + 라벨 전이.

    verdict 별 처리:
      - approve / concerns_noted → `ah:awaiting-human` (사람 merge 결정 대기)
      - request_changes         → PR close + linked issue 에 `ah:needs-execution`
                                   재부착 → 다음 폴링 사이클에 executor 재시도.
                                   (review comment 가 다음 사이클 SOT 의 recent PRs
                                   에 잡혀 context 로 들어감)
    """
    model = model or os.environ.get("REVIEWER_MODEL", "gpt-5.3-codex")

    if not await lock.acquire(repo, "pr", pr.number, bot_user):
        log.info("reviewer.lock_skipped", pr=pr.number)
        return False

    try:
        try:
            # PR 추가 정보 — diff + linked issues + files + 사람 코멘트
            diff = await gh.pr_diff(repo, pr.number)
            files = await gh.pr_files(repo, pr.number)
            linked = await gh.pr_linked_issues(repo, pr.number)
            comments = await gh.pr_comments(repo, pr.number, limit=20)

            # linked issue body 도 SOT 에 추가 (scope check)
            linked_summary = ""
            for n in linked[:3]:
                try:
                    iss = await gh.get_issue(repo, n)
                    linked_summary += f"\n### Linked issue #{n}: {iss.title}\n{iss.body[:1500]}\n"
                except Exception:
                    pass

            # 사람이 남긴 PR comment + 이전 review comment — reviewer 가 재라벨링 시
            # 사람 피드백 반영하도록.
            comments_summary = ""
            human_feedback_present = False
            if comments:
                lines = []
                for c in comments[-10:]:                # 최신 10개
                    author = c["author"]
                    is_bot = author.lower().endswith("[bot]") or author == bot_user
                    if not is_bot and c["body"].strip():
                        human_feedback_present = True
                    snippet = c["body"][:1200]
                    lines.append(f"--- {author} @ {c['createdAt']} ---\n{snippet}")
                comments_summary = "\n\n".join(lines)

            system_prompt = _load_agent_prompt("code-reviewer") + "\n\n" + sot.to_prompt()
            files_summary = "\n".join(
                f"- `{f['path']}` (+{f['additions']} / -{f['deletions']})"
                for f in files
            )
            reroute_hint = ""
            if human_feedback_present:
                reroute_hint = (
                    "\n\n⚠ **이 PR 은 사람이 직접 `ah:needs-review` 라벨을 다시 붙였거나 "
                    "코멘트로 추가 피드백을 남겼을 가능성이 높음.** 위 PR comments 의 사람 "
                    "피드백을 반드시 반영해서 review. 단순 approve 대신 사람이 지적한 "
                    "포인트를 verdict 에 정확히 반영할 것.\n"
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

            text, call_info = await llm.call(
                model=model,
                system=system_prompt,
                user=user_msg,
                max_tokens=4000,
            )

            review = _extract_json(text)
            if review is None:
                await gh.comment_pr(repo, pr.number,
                    f"❌ code-reviewer: LLM 응답 JSON 파싱 실패.\n\n"
                    f"<details><summary>raw</summary>\n\n```\n{text[:2000]}\n```\n\n</details>"
                )
                return False

            # 코멘트 + 정식 review 등록
            comment_body = _format_review_comment(review, call_info)
            verdict = (review.get("verdict") or "concerns_noted").lower()
            event_map = {
                "approve": "APPROVE",
                "request_changes": "REQUEST_CHANGES",
                "concerns_noted": "COMMENT",
            }
            try:
                # bot 이 자기 PR 을 approve 못 함 → COMMENT 로 폴백
                event = event_map.get(verdict, "COMMENT")
                await gh.submit_pr_review(repo, pr.number, body=comment_body, event=event)
            except Exception:
                # review submit 실패 시 일반 comment 폴백
                await gh.comment_pr(repo, pr.number, comment_body)

            # 라벨 전이
            try:
                await gh.remove_label(repo, "pr", pr.number, "ah:needs-review")
            except Exception as exc:
                log.warning("reviewer.remove_label_failed", error=str(exc))

            if verdict == "request_changes":
                # ── 새 흐름 (PR 유지 + amend) ──
                # GitHub native iterative review: 같은 PR 의 branch 에 추가 commit.
                # 이전 흐름 (PR close + issue 재트리거) 은 review thread / git history
                # 분산 문제로 폐기.
                try:
                    await gh.add_label(repo, "pr", pr.number, "ah:needs-execution")
                except Exception as exc:
                    log.warning("reviewer.add_amend_label_failed",
                                pr=pr.number, error=str(exc))
                try:
                    await gh.comment_pr(repo, pr.number,
                        "🔁 **code-reviewer 가 request_changes** — 같은 PR 의 branch 에 "
                        "추가 commit 으로 보강합니다 (executor amend mode).\n\n"
                        "사람 개입 필요하면 PR 의 `ah:needs-execution` 라벨 떼서 멈출 수 있습니다."
                    )
                except Exception as exc:
                    log.warning("reviewer.amend_comment_failed",
                                pr=pr.number, error=str(exc))
            else:
                # approve / concerns_noted → 사람 merge 결정 대기
                try:
                    await gh.add_label(repo, "pr", pr.number, "ah:awaiting-human")
                except Exception as exc:
                    log.warning("reviewer.add_label_failed", error=str(exc))

            log.info("reviewer.done", pr=pr.number, verdict=verdict,
                     needs_adr=bool(review.get("needs_adr")),
                     retriggered=(verdict == "request_changes"),
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
        await lock.release(repo, "pr", pr.number, bot_user)


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

    parts += [
        "---",
        f"_cost ${call_info.cost_usd:.4f} · "
        f"{call_info.input_tokens} in / {call_info.output_tokens} out · model={call_info.model}_",
    ]
    return "\n".join(parts)


# _format_plan_comment 는 폐기 — plan 은 PR description (pr_body) 로 표시. issue
# 엔 "PR 생성됨" 링크 + cost 한 줄만 (위 run_code_executor 참고).
