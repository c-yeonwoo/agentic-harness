"""Assignee + 로컬 PID/timestamp 락 — stale lock 자동 감지 (ADR-016).

ADR-014: lock = GitHub assignee (ah:in-progress 라벨 폐기).
ADR-016 (이 파일): assignee 외 로컬 메타파일 `<repo_cwd>/.hermes/cache/lock/`
        에 PID + 시작 시각 기록. acquire 시 stale (PID 죽음 / threshold 초과) 면
        자동 takeover. SIGKILL / kickstart -k / 크래시 후 stale lock 자동 복구.

acquire 흐름:
  1. 메타파일 + assignee 둘 다 검사
     - bot 이 assignee 가 아니면 그냥 새로 잡음 (정상 경로)
     - bot 이 assignee 면:
        - 메타파일 없음 → 이전 crash 의 stale → takeover
        - 메타파일 있는데 PID 죽음 → stale → takeover
        - 메타파일 있고 PID 살아있는데 threshold 초과 (default 30분) → stale → takeover
        - 그 외 → 진짜 락 잡혀있음 → skip (False 반환)
  2. takeover / 새 락: gh.assign(bot) + 메타파일 쓰기 (PID + timestamp)
  3. race verify

release:
  1. 메타파일 삭제
  2. gh.unassign(bot)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

import structlog

from orchestrator import gh

log = structlog.get_logger()


_STALE_THRESHOLD_SEC = int(os.environ.get("STALE_LOCK_THRESHOLD_SEC", "1800"))  # 30분


def _lock_dir(repo_cwd: Path) -> Path:
    p = repo_cwd / ".hermes" / "cache" / "lock"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _lock_file(repo_cwd: Path, kind: str, number: int) -> Path:
    return _lock_dir(repo_cwd) / f"{kind}-{number}.json"


def _pid_alive(pid: int) -> bool:
    """PID 살아있는지 — kill(pid, 0) 으로 가벼운 체크."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # 다른 user 의 process — 살아있긴 함 (우리 락 아님 의미)
        return True


def _read_meta(repo_cwd: Path, kind: str, number: int) -> Optional[dict]:
    fp = _lock_file(repo_cwd, kind, number)
    if not fp.exists():
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("lock.meta_read_failed", file=str(fp), error=str(exc))
        return None


def _write_meta(repo_cwd: Path, kind: str, number: int, bot_user: str) -> None:
    fp = _lock_file(repo_cwd, kind, number)
    payload = {
        "pid": os.getpid(),
        "bot_user": bot_user,
        "acquired_at": time.time(),
        "acquired_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }
    try:
        fp.write_text(json.dumps(payload), encoding="utf-8")
    except Exception as exc:
        log.warning("lock.meta_write_failed", file=str(fp), error=str(exc))


def _remove_meta(repo_cwd: Path, kind: str, number: int) -> None:
    fp = _lock_file(repo_cwd, kind, number)
    try:
        if fp.exists():
            fp.unlink()
    except Exception as exc:
        log.warning("lock.meta_remove_failed", file=str(fp), error=str(exc))


def _is_stale(meta: Optional[dict]) -> tuple[bool, str]:
    """메타 정보로 stale 판정. (stale, reason) 반환."""
    if meta is None:
        return True, "no_meta_file (crash / kickstart -k 잔존)"
    pid = int(meta.get("pid", 0))
    acquired_at = float(meta.get("acquired_at", 0))
    if not _pid_alive(pid):
        return True, f"pid_dead (pid={pid})"
    age = time.time() - acquired_at
    if age > _STALE_THRESHOLD_SEC:
        return True, f"threshold_exceeded (age={age:.0f}s > {_STALE_THRESHOLD_SEC}s)"
    return False, ""


async def acquire(
    repo: str, kind: str, number: int, bot_user: str,
    repo_cwd: Optional[Path] = None,
) -> bool:
    """락 시도 — bot assignee + 로컬 메타 기록. True 면 성공.

    repo_cwd 가 None 이면 메타파일 안 쓰고 assignee 만 사용 (호환성).
    """
    # 1) 사전 체크 — 현재 assignee 상태
    try:
        item = await gh.get_issue(repo, number) if kind == "issue" else await gh.get_pr(repo, number)
    except Exception as exc:
        log.warning("lock.precheck_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
        return False

    if bot_user in item.assignees:
        # 이미 assignee — stale 판정
        if repo_cwd:
            meta = _read_meta(repo_cwd, kind, number)
            stale, reason = _is_stale(meta)
            if stale:
                log.warning("lock.stale_takeover",
                            repo=repo, kind=kind, number=number, reason=reason,
                            meta=meta)
                # takeover — 메타만 갱신하고 진행 (assignee 는 이미 bot)
                _write_meta(repo_cwd, kind, number, bot_user)
                return True
            else:
                log.info("lock.already_held_alive",
                         repo=repo, kind=kind, number=number,
                         pid=meta.get("pid") if meta else None,
                         age_sec=round(time.time() - meta.get("acquired_at", 0))
                                if meta else None)
                return False
        else:
            # repo_cwd 없으면 보수적으로 양보
            log.info("lock.already_held_no_cwd",
                     repo=repo, kind=kind, number=number,
                     assignees=item.assignees)
            return False

    # 2) 새 락 acquire — assign
    try:
        await gh.assign(repo, kind, number, bot_user)
    except Exception as exc:
        log.warning("lock.assign_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
        return False

    # 3) race 검증
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

    # 4) 메타 쓰기
    if repo_cwd:
        _write_meta(repo_cwd, kind, number, bot_user)
    return True


async def release(
    repo: str, kind: str, number: int, bot_user: str,
    repo_cwd: Optional[Path] = None,
) -> None:
    """락 해제 — 메타 삭제 + bot assignee 제거."""
    if repo_cwd:
        _remove_meta(repo_cwd, kind, number)
    try:
        await gh.unassign(repo, kind, number, bot_user)
    except Exception as exc:
        log.warning("lock.release_failed",
                    repo=repo, kind=kind, number=number, error=str(exc))
