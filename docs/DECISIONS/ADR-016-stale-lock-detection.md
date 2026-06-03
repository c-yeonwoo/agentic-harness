# ADR-016 — Stale Lock 자동 감지 (PID + Timestamp 메타파일)

> 결정일: 2026-06-04
> 상태: Accepted

## 배경

ADR-014 에서 lock = GitHub assignee 로 단순화. 그런데:

- 사용자가 `launchctl kickstart -k` 실행 (in-progress ah 죽임)
- claude -p 가 외부 신호로 SIGKILL
- launchd ExitTimeOut 짧아서 SIGKILL
- Mac sleep / wake 중 process 좀비

위 케이스 모두 Python 의 `try/finally` 가 안 돌고 → `lock.release()` 못 호출
→ assignee=bot 영원히 남음 → 폴러가 영원히 그 PR/issue skip.

실제 발생: 12:49 에 reviewer start, 12:49:01 claude -p spawn 직후 사용자가
`kickstart -k` 호출 → SIGKILL → 30분간 PR #26 stuck.

## 결정

`<repo_cwd>/.hermes/cache/lock/<kind>-<n>.json` 에 락 메타 기록:
```json
{
  "pid": 12345,
  "bot_user": "c-yeonwoo",
  "acquired_at": 1717469940.123,
  "acquired_at_iso": "2026-06-04T13:45:40"
}
```

`lock.acquire(repo_cwd=...)` 호출 시 (assignee=bot 발견 시) **stale 판정**:

| 조건 | 판정 | 처리 |
|------|------|------|
| 메타파일 없음 | stale | takeover (이전 crash / kickstart -k 잔존) |
| PID 죽음 (`os.kill(pid, 0)` 실패) | stale | takeover |
| acquired_at 가 threshold 초과 (`STALE_LOCK_THRESHOLD_SEC`, default 1800s) | stale | takeover (sleep stuck 등) |
| PID 살아있고 threshold 안 | **진짜 락** | skip (False 반환) |

`lock.release` 가 정상 종료 시 메타파일 삭제. 비정상 종료 시 메타파일 남아있어도
다음 acquire 가 PID 검사로 stale 판단 → 자동 복구.

## 트레이드오프

### 장점
- **자동 복구** — manual `gh.unassign` 없이 다음 polling 사이클에 takeover
- **SIGKILL 내성** — Python finally 못 돌아도 OK
- **Mac sleep 후 stuck** 도 30분 후 자동 takeover
- **`kickstart -k` 후에도 safe** — 운영 룰 위반해도 자동 복구

### 단점
- 로컬 메타파일 — 다른 machine 의 polling 인스턴스와 안 공유 (단일 launchd 환경 가정)
- threshold 안에 진짜로 오래 도는 작업 (예: 30분 넘는 sonnet 작업) 은 false stale 가능
  → `STALE_LOCK_THRESHOLD_SEC` 늘리거나 `LOCAL_CLAUDE_TIMEOUT_SEC` 와 일관성 유지
- PID 재사용 — Linux/macOS 가 PID 빠르게 재사용. 다른 process 가 같은 PID 잡으면
  false alive. 실용상 30분 안에 PID 재사용 + 같은 PID 잡을 확률 무시 가능

## 환경변수

| Var | Default | 의미 |
|-----|---------|------|
| `STALE_LOCK_THRESHOLD_SEC` | 1800 (30분) | acquired_at 보다 이 만큼 지나면 stale 판정 |

## 변경

- `orchestrator/lock.py` — 메타파일 read/write/remove + `_is_stale()` 판정 + 자동 takeover
- `orchestrator/agents.py` — lock.acquire/release 에 `repo_cwd` 전달 (3 곳: developer / amend / reviewer)
- `_resolve_repo_cwd()` 헬퍼 — repo → cwd 자동 추론 (~/dev-private/<name> 우선)

## 운영 룰 (그대로 유지)

- `launchctl kickstart -k` **권장 안 함** (in-progress 죽임). `launchctl start` 사용
- 정상 종료 시 메타파일 자동 삭제 → 다음 acquire 가 즉시 가능
- 비정상 종료 시 메타파일 남음 → 다음 acquire 의 PID 검사로 자동 복구
