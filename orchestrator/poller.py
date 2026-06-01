"""Main polling loop — Phase 1 (local daemon).

30초마다 label 별 issue/PR 발견 → 락 → agent 실행.
Phase 1 MVP 는 code-executor 만 활성. 다른 agent 는 Phase 2-3 에서 추가.
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
    max_executors = int(os.environ.get("MAX_PARALLEL_EXECUTORS", "3"))
    max_reviewers = int(os.environ.get("MAX_PARALLEL_REVIEWERS", "3"))

    # 1) ah:needs-execution issues → code-executor
    try:
        e_candidates = await gh.list_issues(
            repo, label="ah:needs-execution", no_label="ah:in-progress",
        )
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="executor")
        e_candidates = []

    if e_candidates:
        log.info("poll.found", count=len(e_candidates), agent="code-executor")
        slots = e_candidates[:max_executors]
        results = await asyncio.gather(
            *[agents.run_code_executor(repo, c, sot, bot_user, repo_cwd=cwd) for c in slots],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                stats["errors"] += 1
                log.warning("poll.executor_exc", error=str(r))
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
            *[agents.run_code_reviewer(repo, p, sot, bot_user) for p in slots],
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

    return stats


async def run_forever(repo: str, cwd: Path, interval: int = 30) -> None:
    """daemon 모드 — interval 초마다 poll."""
    bot_user = await gh.whoami()
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
