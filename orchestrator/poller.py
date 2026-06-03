"""Main polling loop — local daemon.

interval (default 30s) 마다 라벨 별 issue/PR 발견 → 락 → agent 실행.
launchd / Hermes cron 의 호출 단위는 `ah run --once` (이 poll_once).

ADR-014: lock 은 assignee 기반 (ah:in-progress 라벨 폐기). 워크플로우 라벨은
항상 정확히 1개. 폴러는 "라벨 X 인데 bot_user 가 아직 assignee 가 아닌 것" 만
픽업해서 다른 인스턴스 / 직전 crash 충돌 회피.
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


def _filter_unlocked(items, bot_user: str):
    """bot 이 이미 assignee 인 항목 (= 다른 인스턴스가 잡았거나 직전 crash 잔존) 제외."""
    return [x for x in items if bot_user not in x.assignees]


async def poll_once(repo: str, cwd: Path, bot_user: str) -> dict:
    """1회 polling. 발견된 task 처리 후 통계 반환."""
    stats = {"executed": 0, "skipped": 0, "errors": 0}

    sot = await discover(cwd)
    max_devs = int(
        os.environ.get("MAX_PARALLEL_DEVELOPERS")
        or os.environ.get("MAX_PARALLEL_EXECUTORS", "3")
    )
    max_reviewers = int(os.environ.get("MAX_PARALLEL_REVIEWERS", "3"))

    # 1) ah:needs-execution issues → developer (new task)
    try:
        e_raw = await gh.list_issues(repo, label="ah:needs-execution")
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="developer")
        e_raw = []
    e_candidates = _filter_unlocked(e_raw, bot_user)

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
        r_raw = await gh.list_prs(repo, label="ah:needs-review")
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="reviewer")
        r_raw = []
    r_candidates = _filter_unlocked(r_raw, bot_user)

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

    # 3) ah:in-debate PRs → developer amend (debate cycle)
    try:
        d_raw = await gh.list_prs(repo, label="ah:in-debate")
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="developer-amend")
        d_raw = []
    d_candidates = _filter_unlocked(d_raw, bot_user)

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

    # 4) ah:sot-urgent merged PRs → PO mode B (즉시 단일 PR 분석) — ADR-017
    try:
        u_raw = await gh.list_prs(
            repo, label="ah:sot-urgent", state="closed", limit=10,
        )
        # merged 만 (closed but not merged 면 무시)
        u_merged = [p for p in u_raw if p.merged]
    except Exception as exc:
        log.warning("poll.list_failed", error=str(exc), agent="po-mode-b-urgent")
        u_merged = []

    if u_merged:
        log.info("poll.found", count=len(u_merged), agent="po-mode-b-urgent")
        # 즉시 모드는 1번에 1개 처리 (병렬 X — SoT 동시 수정 충돌 방지)
        for p in u_merged[:1]:
            try:
                res = await agents.run_po_mode_b(
                    repo=repo, repo_cwd=cwd,
                    pr_numbers=[p.number], mode="urgent",
                )
                if res.get("ok"):
                    # 처리됐으면 ah:sot-urgent 라벨 제거 (재처리 방지)
                    try:
                        await gh.remove_label(repo, "pr", p.number, "ah:sot-urgent")
                    except Exception:
                        pass
                    stats.setdefault("sot_updated", 0)
                    stats["sot_updated"] += 1
                    log.info("po.mode_b.urgent.done", pr=p.number,
                             sot_pr=res.get("pr_url"), cost=res.get("cost_usd"))
                else:
                    stats["errors"] += 1
                    log.warning("po.mode_b.urgent.failed",
                                pr=p.number, error=res.get("error"))
            except Exception as exc:
                stats["errors"] += 1
                log.warning("poll.po_b_urgent_exc", error=str(exc))

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
