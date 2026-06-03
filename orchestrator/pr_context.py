"""PR 컨텍스트 통합 fetch + 캐싱 + debate summary.

ADR-015: PR diff / files / linked / comments 한 번 fetch → 캐시.
같은 head_sha + 같은 comments_count 면 cache hit — agent 사이 GitHub API
round-trip 절약. round 2+ debate 시 누적 comments 를 LLM 호출로 요약 →
다음 prompt 에 raw 대신 요약 inject (token 절약).

캐시 위치: `<repo_cwd>/.hermes/cache/pr/<n>.json`
무효화 키: head_sha + len(comments) + (선택) updated_at
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

import structlog

from orchestrator import gh

log = structlog.get_logger()


_PR_CACHE_TTL_SEC = int(os.environ.get("PR_CACHE_TTL_SEC", "600"))  # 10분


@dataclass
class PRContext:
    """PR 의 캐시된 종합 컨텍스트 — agent prompt 에 inject."""
    pr_number: int
    head_sha: str
    comments_count: int

    # PR 본문 메타
    title: str = ""
    body: str = ""
    head_ref: str = ""
    base_ref: str = ""
    labels: list = field(default_factory=list)
    url: str = ""

    # 페치 데이터
    diff: str = ""
    files: list = field(default_factory=list)
    linked_issues: list = field(default_factory=list)
    comments: list = field(default_factory=list)

    # debate summary (round 2+ 시 LLM 호출로 생성, 다음 호출에 재사용)
    debate_summary: Optional[str] = None
    debate_round: int = 0

    fetched_at: float = 0.0


def _cache_dir(repo_cwd: Path) -> Path:
    p = repo_cwd / ".hermes" / "cache" / "pr"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _serialize(ctx: PRContext) -> dict:
    return asdict(ctx)


def _deserialize(payload: dict) -> PRContext:
    return PRContext(**payload)


async def _load_cache(repo_cwd: Path, pr_number: int) -> Optional[PRContext]:
    fp = _cache_dir(repo_cwd) / f"{pr_number}.json"
    if not fp.exists():
        return None
    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
        if time.time() - payload.get("fetched_at", 0) > _PR_CACHE_TTL_SEC:
            return None
        return _deserialize(payload)
    except Exception as exc:
        log.warning("pr_cache.read_failed", pr=pr_number, error=str(exc))
        return None


def _save_cache(repo_cwd: Path, ctx: PRContext) -> None:
    fp = _cache_dir(repo_cwd) / f"{ctx.pr_number}.json"
    try:
        fp.write_text(json.dumps(_serialize(ctx), ensure_ascii=False),
                      encoding="utf-8")
    except Exception as exc:
        log.warning("pr_cache.write_failed", pr=ctx.pr_number, error=str(exc))


async def discover_pr(
    *,
    repo: str,
    repo_cwd: Path,
    pr_number: int,
    diff_max_bytes: int = 80_000,
    comments_limit: int = 30,
    force_refresh: bool = False,
    pr: Optional[gh.PullRequest] = None,
) -> PRContext:
    """PR 의 diff / files / linked / comments 한 번에 fetch + 캐싱.

    cache 무효화: head_sha 변경 OR comments_count 변경 OR TTL 초과.
    같은 tick 내 reviewer / developer-amend 가 공유 호출하면 1회 fetch 로 끝.

    Args:
      pr: 이미 get_pr 한 객체 있으면 재사용 (한 번의 round-trip 더 절약)
    """
    if pr is None:
        pr = await gh.get_pr(repo, pr_number)

    # 캐시 검증
    if not force_refresh:
        cached = await _load_cache(repo_cwd, pr_number)
        if cached is not None:
            # head_sha 같고 comments_count 도 같으면 hit
            # (head_sha 빈 문자열이면 fallback 으로 updated_at 비교)
            if pr.head_sha and cached.head_sha == pr.head_sha:
                # comments_count 변경 검증 — light 호출
                try:
                    fresh_comments = await gh.pr_comments(
                        repo, pr_number, limit=comments_limit,
                    )
                    if len(fresh_comments) == cached.comments_count:
                        log.info("pr.cache_hit", pr=pr_number,
                                 head=pr.head_sha[:8] if pr.head_sha else "",
                                 comments=cached.comments_count)
                        # comments 만 최신으로 (혹시 같은 갯수면 같은 내용)
                        cached.comments = fresh_comments
                        return cached
                except Exception as exc:
                    log.warning("pr.cache_verify_failed", pr=pr_number, error=str(exc))

    # Fresh fetch — 4개 API 병렬
    log.info("pr.cache_miss", pr=pr_number,
             head=(pr.head_sha[:8] if pr.head_sha else ""))

    diff_task = gh.pr_diff(repo, pr_number, max_bytes=diff_max_bytes)
    files_task = gh.pr_files(repo, pr_number)
    linked_task = gh.pr_linked_issues(repo, pr_number)
    comments_task = gh.pr_comments(repo, pr_number, limit=comments_limit)

    diff, files, linked, comments = await asyncio.gather(
        diff_task, files_task, linked_task, comments_task,
        return_exceptions=False,
    )

    # debate round 카운트 — "🔁 debate round" prefix
    DEBATE_MARK = "🔁 **debate round"
    debate_round = sum(1 for c in comments if DEBATE_MARK in c.get("body", ""))

    ctx = PRContext(
        pr_number=pr_number,
        head_sha=pr.head_sha or "",
        comments_count=len(comments),
        title=pr.title,
        body=pr.body,
        head_ref=pr.head_ref,
        base_ref=pr.base_ref,
        labels=list(pr.labels),
        url=pr.url,
        diff=diff,
        files=files,
        linked_issues=linked,
        comments=comments,
        debate_round=debate_round,
        fetched_at=time.time(),
    )

    # 캐시 저장 (debate_summary 는 처음엔 빈 채로, 별도 호출로 채움)
    _save_cache(repo_cwd, ctx)
    return ctx


# ── Debate summary (round 2+ 시 누적 comments 요약) ───────────────────────────


SUMMARY_MARKER = "<!-- ah:debate-summary:v1 -->"


def _extract_existing_summary(comments: list[dict]) -> Optional[str]:
    """PR 코멘트에서 가장 최근 debate summary HTML 주석 추출."""
    for c in reversed(comments):
        body = c.get("body", "") or ""
        if SUMMARY_MARKER in body:
            # marker 줄 다음의 본문 추출
            try:
                _, after = body.split(SUMMARY_MARKER, 1)
                return after.strip()
            except ValueError:
                continue
    return None


def needs_summary(ctx: PRContext, threshold_round: int = 2) -> bool:
    """summary 생성/갱신 필요한지 — round 2 이상 + 새 comments 누적."""
    if ctx.debate_round < threshold_round:
        return False
    # 이미 summary 있고 그 이후 comment 변화 없으면 skip
    existing = _extract_existing_summary(ctx.comments)
    if existing and ctx.debate_summary == existing:
        return False
    return True


async def generate_debate_summary(
    *,
    repo_cwd: Path,
    ctx: PRContext,
    model: Optional[str] = None,
    timeout_sec: int = 300,
) -> Optional[str]:
    """누적 comments + diff 를 요약. round 2+ 일 때만 호출 (cost 절약).

    결과는 PR 에 HTML 주석으로 게시 + ctx.debate_summary 에 채움.
    """
    from orchestrator.runners.local_claude import _spawn_claude
    from orchestrator.runners import resolve_local_model

    model = model or resolve_local_model("po")  # 요약은 sonnet 충분

    # 압축 대상 — comments + reviewer verdict / developer amend 변천사
    comments_text = "\n\n".join(
        f"--- {c.get('author','?')} @ {c.get('createdAt','?')} ---\n{c.get('body','')[:1500]}"
        for c in ctx.comments[-15:]   # 최근 15개
    )

    system_prompt = (
        "너는 GitHub PR 의 debate 진행 상황을 요약하는 도우미다. "
        "reviewer 와 developer 가 주고받은 코멘트 / amend 이력을 보고, "
        "다음 agent 가 빠르게 따라잡을 수 있는 한국어 요약을 작성한다.\n\n"
        "출력 형식:\n"
        "## 요약 (round N 까지)\n"
        "- **reviewer 의 핵심 지적**: ...\n"
        "- **developer 의 응답 / amend**: ...\n"
        "- **남은 쟁점**: ...\n"
        "- **다음 agent 가 봐야 할 포인트**: ...\n\n"
        "JSON 출력 X, 그냥 마크다운. 500자 이내."
    )

    user_prompt = (
        f"# PR #{ctx.pr_number}: {ctx.title}\n\n"
        f"## 최근 코멘트 ({len(ctx.comments[-15:])} 개)\n\n{comments_text}\n\n"
        f"---\n위 진행 상황 요약."
    )

    log.info("pr.summary.spawn", pr=ctx.pr_number, round=ctx.debate_round, model=model)

    try:
        rc, stdout, stderr = await _spawn_claude(
            cwd=repo_cwd,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_sec=timeout_sec,
            extra_disallowed=["Edit", "Write", "Bash(*)"],
            model=model,
        )
    except Exception as exc:
        log.warning("pr.summary.spawn_failed", pr=ctx.pr_number, error=str(exc))
        return None

    from orchestrator.runners.local_claude import _parse_claude_json_envelope
    assistant_text, env = _parse_claude_json_envelope(stdout)
    if assistant_text is None:
        assistant_text = stdout

    if rc != 0 or env.get("is_error") or not assistant_text.strip():
        log.warning("pr.summary.failed", pr=ctx.pr_number, rc=rc,
                    stderr_head=stderr[:300])
        return None

    summary = assistant_text.strip()

    # PR 에 HTML 주석으로 게시 (사람 눈에 보임 — narrator 도 보일 수 있게 raw 도 같이)
    try:
        from orchestrator import gh as _gh
        body = (
            f"📋 **debate summary (round {ctx.debate_round})**\n\n"
            f"{summary}\n\n"
            f"---\n"
            f"{SUMMARY_MARKER}\n{summary}"
        )
        # caller (agents.py) 에서 repo 를 모르면 안 함 — caller 가 직접 post
        # 여기서는 summary 만 반환, ctx.debate_summary 에 채움
        ctx.debate_summary = summary
    except Exception as exc:
        log.warning("pr.summary.post_failed", pr=ctx.pr_number, error=str(exc))

    # 캐시 업데이트
    _save_cache(repo_cwd, ctx)
    log.info("pr.summary.done", pr=ctx.pr_number, summary_chars=len(summary))
    return summary
