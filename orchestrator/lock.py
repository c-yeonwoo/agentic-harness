"""Assignee 기반 분산 락 — 워크플로우 라벨과 직교.

ADR-014: lock 은 `ah:in-progress` 라벨이 아니라 GitHub native assignee 만 사용.
이유: 워크플로우 라벨이 항상 정확히 1개여야 한다 (배타적).
race 검증: assign 후 재조회 → bot 이 실제로 assignees 에 들어갔는지.
"""
from __future__ import annotations

import structlog

from orchestrator import gh

log = structlog.get_logger()


async def acquire(repo: str, kind: str, number: int, bot_user: str) -> bool:
    """락 시도 — bot 을 assignee 로. True 면 성공.

    이미 다른 인스턴스 (혹은 직전 crash 잔존) 가 assignee 면 False.
    kind: 'issue' | 'pr'
    """
    # 1) 사전 체크 — 이미 잡힌 락은 양보 (보수적)
    try:
        item = await gh.get_issue(repo, number) if kind == "issue" else await gh.get_pr(repo, number)
    except Exception as exc:
        log.warning("lock.precheck_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
        return False
    if bot_user in item.assignees:
        log.info("lock.already_held",
                 repo=repo, kind=kind, number=number,
                 assignees=item.assignees)
        return False

    # 2) assign
    try:
        await gh.assign(repo, kind, number, bot_user)
    except Exception as exc:
        log.warning("lock.assign_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
        return False

    # 3) race 검증 — 진짜로 assignees 에 들어갔는지
    try:
        refreshed = await gh.get_issue(repo, number) if kind == "issue" else await gh.get_pr(repo, number)
    except Exception as exc:
        log.warning("lock.verify_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
        return False
    if bot_user not in refreshed.assignees:
        log.info("lock.race_lost",
                 repo=repo, kind=kind, number=number,
                 actual=refreshed.assignees)
        return False
    return True


async def release(repo: str, kind: str, number: int, bot_user: str) -> None:
    """락 해제 — bot assignee 제거."""
    try:
        await gh.unassign(repo, kind, number, bot_user)
    except Exception as exc:
        log.warning("lock.release_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
