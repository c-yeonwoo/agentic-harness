"""Label + assignee 기반 분산 락.

여러 poller 인스턴스 (local + GitHub Actions) 가 동시에 같은 item 픽업
못 하게. `ah:in-progress` 라벨 + BOT_USER assignee 둘 다 사용 — race
window 좁힘.
"""
from __future__ import annotations

import structlog

from orchestrator import gh

log = structlog.get_logger()


IN_PROGRESS_LABEL = "ah:in-progress"


async def acquire(repo: str, kind: str, number: int, bot_user: str) -> bool:
    """락 시도. True 면 성공, False 면 다른 poller / 사람이 가져감.

    kind: 'issue' | 'pr'
    """
    try:
        await gh.add_label(repo, kind, number, IN_PROGRESS_LABEL)
        await gh.assign(repo, kind, number, bot_user)
    except Exception as exc:
        log.warning("lock.acquire_failed", repo=repo, kind=kind, number=number, error=str(exc))
        return False

    # Race 검증 — assignee 가 실제로 bot 인지
    if kind == "issue":
        item = await gh.get_issue(repo, number)
    else:
        item = await gh.get_pr(repo, number)

    if bot_user not in item.assignees:
        log.info("lock.race_lost", repo=repo, kind=kind, number=number,
                 actual_assignees=item.assignees)
        return False
    return True


async def release(repo: str, kind: str, number: int, bot_user: str) -> None:
    """락 해제 — agent 작업 완료 후 호출."""
    try:
        await gh.remove_label(repo, kind, number, IN_PROGRESS_LABEL)
    except Exception:
        pass
    try:
        await gh.unassign(repo, kind, number, bot_user)
    except Exception:
        pass
