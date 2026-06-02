"""Main polling loop — local daemon.

interval (default 30s) 마다 라벨 별 issue/PR 발견 → 락 → agent 실행.
launchd / Hermes cron 의 호출 단위는 `ah run --once` (이 poll_once).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Optional

import structlog

from orchestrator import agents, gh
from orchestrator.source_of_truth import discover

log = structlog.get_logger()


async def poll_once(repo: str, cwd: Path, bot_user: str) -> dict:
    """1회 polling. 발견된 task 처리 후 통계 반환."""
    stats = {"executed": 0, "skipped": 0, "errors": 0}

    sot = await discover(cwd)
    # MAX_PARALLEL_DEVELOPERS / MAX_PARALLEL_EXECUTORS 둘 다 인식 (ADR-012)
    max_devs = int(
        os.environ.get("MAX_PARALLEL_DEVELOPERS")
        or os.environ.get("MAX_PARALLEL_EXECUTORS", "3")
    )
    max_reviewers = int(os.environ.get("MAX_PARALLEL_REVIEWERS", "3"))

    # 1) ah:needs-execution issues → developer
    try:
        e_candidates = await gh.list_issues(
            repo, label="ah:needs-execution", no_label="ah:in-progress",
        )
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="developer")
        e_candidates = []

    if e_candidates:
        log.info("poll.found", count=len(e_candidates), agent="developer")
        slots = e_candidates[:max_devs]
        results = await asyncio.gather(
            *[agents.run_developer(repo, c, sot, bot_user, repo_cwd=cwd) for c in slots],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                stats["errors"] += 1
                log.warning("poll.developer_exc", error=str(r))
            elif r:
                stats["executed"] += 1
            else:
                stats["skipped"] += 1

    # 2) ah:needs-review PRs → code-reviewer
    try:
        r_candidates = await gh.list_prs(
            repo, label="ah:needs-review", no_label="ah:in-progress",
        )
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="reviewer")
        r_candidates = []

    if r_candidates:
        log.info("poll.found", count=len(r_candidates), agent="code-reviewer")
        slots = r_candidates[:max_reviewers]
        results = await asyncio.gather(
            *[agents.run_code_reviewer(repo, p, sot, bot_user, repo_cwd=cwd) for p in slots],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                stats["errors"] += 1
                log.warning("poll.reviewer_exc", error=str(r))
            elif r:
                stats.setdefault("reviewed", 0)
                stats["reviewed"] += 1
            else:
                stats["skipped"] += 1

    # 3) ah:in-debate PRs → developer amend (ADR-012 debate cycle)
    #    reviewer 가 concerns_noted / request_changes 면 PR 에 ah:in-debate 만 붙음.
    #    poller 가 이걸 발견 → developer amend mode.
    #    (옛 hermes 흐름은 ah:needs-execution PR 도 amend trigger 였음 — 같이 봄)
    try:
        d_candidates = await gh.list_prs(
            repo, label="ah:in-debate", no_label="ah:in-progress",
        )
        # back-compat: 옛 흐름이 만든 ah:needs-execution PR 도 amend 대상
        legacy = await gh.list_prs(
            repo, label="ah:needs-execution", no_label="ah:in-progress",
        )
        # dedup
        seen = {p.number for p in d_candidates}
        for p in legacy:
            if p.number not in seen:
                d_candidates.append(p)
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="developer-amend")
        d_candidates = []

    if d_candidates:
        log.info("poll.found", count=len(d_candidates), agent="developer-amend")
        slots = d_candidates[:max_devs]
        results = await asyncio.gather(
            *[agents.run_developer_amend(repo, p, sot, bot_user, repo_cwd=cwd) for p in slots],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                stats["errors"] += 1
                log.warning("poll.amend_exc", error=str(r))
            elif r:
                stats.setdefault("amended", 0)
                stats["amended"] += 1
            else:
                stats["skipped"] += 1

    return stats


async def run_forever(repo: str, cwd: Path, interval: int = 30) -> None:
    """daemon 모드 — interval 초마다 poll."""
    bot_user = await gh.whoami(repo)
    log.info("poller.start", repo=repo, cwd=str(cwd), bot=bot_user, interval=interval)
    while True:
        try:
            stats = await poll_once(repo, cwd, bot_user)
            if any(stats.values()):
                log.info("poller.tick", **stats)
        except KeyboardInterrupt:
            log.info("poller.stop_by_user")
            return
        except Exception as exc:
            log.warning("poller.tick_failed", error=str(exc))
        await asyncio.sleep(interval)
