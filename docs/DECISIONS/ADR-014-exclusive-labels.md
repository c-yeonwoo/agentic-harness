# ADR-014 — 워크플로우 라벨은 항상 정확히 1개 (배타적), Lock 은 Assignee

> 결정일: 2026-06-02
> 상태: Accepted

## 배경

ADR-012 후 라벨 8개 운영 중. 하지만 두 layer 가 섞여 있었음:

| 종류 | 라벨 | 역할 |
|---|---|---|
| 워크플로우 상태 | needs-execution / needs-review / in-debate / needs-critique / awaiting-human / sot-pending / sot-done | 다음 누가 작업할지 |
| Lock (직교) | `ah:in-progress` | 지금 누가 작업 중 |

작업 중 PR 은 **라벨 2개** (예: `ah:in-debate` + `ah:in-progress`) — 사용자가 GitHub UI 에서 보면 헷갈림. 게다가 reviewer 단계에서 `ah:in-debate` + `ah:needs-execution` 둘 다 부착되던 버그까지 있었음 (이전 커밋에서 정리).

## 결정

### 1. 워크플로우 라벨은 항상 정확히 1개 (배타적)

`needs-execution` / `needs-review` / `in-debate` / `needs-critique` / `awaiting-human` / `sot-pending` / `sot-done` 중 **정확히 하나만**. transition 시 add + remove 동시.

이로 인해:
- GitHub UI 에서 한 눈에 state 파악 가능
- agent prompt / poller / state machine 다이어그램 모두 일관
- "어떤 trigger 인지" 와 "어떤 state 인지" 가 같은 라벨로 표현 — 단순화

### 2. Lock 은 GitHub Assignee (라벨 폐기)

`ah:in-progress` 라벨 폐기. bot 의 GitHub assignee 가 lock 역할.

- `lock.acquire()` = `gh.assign(bot)` + race 검증 (re-fetch + assignees 확인)
- `lock.release()` = `gh.unassign(bot)`
- 다른 인스턴스 / crash 잔존: `bot in item.assignees` 면 poller 가 건너뜀

이로 인해:
- 라벨 배타성 보장 (워크플로우 라벨 1개 + 직교한 assignee)
- GitHub native UX — assignee 가 PR/issue 상단에 표시되어 "누가 작업 중" 명확
- STANDARD_LABELS 7개 (8 → 7)

### 3. Hermes 옛 흐름의 `ah:needs-execution` PR 트리거 폐기

이전엔 amend mode 트리거가 PR 의 `ah:needs-execution` 라벨이었음 (hermes pm 스크립트). 이젠 **PR amend = `ah:in-debate` 만**.

- 폴러는 `ah:in-debate` PR 만 amend mode 로 dispatch
- `ah:needs-execution` 은 **issue 전용** (새 task 의미)
- hermes 트랙 사용 시 pm 스크립트 도 `ah:in-debate` 사용하도록 갱신 필요 (별도 작업)

## 라벨 매트릭스 (최종)

| 라벨 | 어디 | 의미 | Poller trigger |
|------|------|------|----------------|
| `ah:needs-execution` | **issue** | 새 task | ✓ → developer (PR 생성) |
| `ah:needs-review` | **PR** | reviewer 큐 | ✓ → reviewer |
| `ah:in-debate` | **PR** | developer amend 큐 (debate cycle) | ✓ → developer amend |
| `ah:needs-critique` | **PR** | critique 큐 (미구현) | — |
| `ah:awaiting-human` | **PR** | 사람 결정 대기 | — |
| `ah:sot-pending` | **merged PR** | PO mode B 큐 (미구현) | — |
| `ah:sot-done` | **merged PR** | PO mode B 처리 완료 | — |
| ~~`ah:in-progress`~~ | ~~both~~ | ~~lock~~ → **assignee 로 대체** | — |

## 변경 사항

- `orchestrator/lock.py` — assignee 전용 acquire/release
- `orchestrator/gh.py` — STANDARD_LABELS 에서 `ah:in-progress` 제거
- `orchestrator/poller.py` — `no_label="ah:in-progress"` 필터 제거 → `_filter_unlocked` (`bot_user in assignees` 체크) 로 교체. legacy `ah:needs-execution` PR 픽업 제거
- `orchestrator/agents.py` — PR amend 의 `ah:needs-execution` 라벨 참조 정리 (in-debate 만)

## 트레이드오프

### 장점
- 라벨 1개 = 명확한 state. UI 깔끔
- assignee = native lock — GitHub 자체 race 안전성 활용
- state machine 단순화 (배타적이라 transition 검증도 단순)

### 단점
- GitHub label 필터로 "지금 작업 중인 것만" 보고 싶을 때 라벨 대신 assignee 필터 (`is:open assignee:c-yeonwoo`)
- assignee 만으로의 race window 가 라벨+assignee 보다 살짝 넓음 (하지만 launchd 단일 인스턴스 + 5분 주기라 실용상 문제 X)
- 기존 PR 에 `ah:in-progress` 라벨 stale 한 게 남아있을 수 있음 — 수동 정리 또는 ignore

### 폐기된 대안
- **B. lock = 워크플로우 라벨 swap (`needs-execution` → `in-progress` → `needs-review`)** — crash 시 원래 state 복구 어려움. 비추천.
- **C. 현 상태 유지 (라벨 2개 공존)** — 사용자가 불편 표시. 정리하기로.

## Migration

기존 라벨이 deployed 된 repo:
- 새로 만들 라벨: `ah:in-progress` 자동 안 만들어짐 (STANDARD_LABELS 에서 제거)
- 기존 `ah:in-progress` 라벨: GitHub repo 의 라벨 페이지에서 수동 삭제 가능 (또는 그대로 두면 사용 안 됨)
- stale PR 에 `ah:in-progress` 붙어있는 거: 수동 제거 또는 GitHub UI 에서 일괄 삭제

`ah init-labels` 재실행 시 신규 8 라벨 ensure (기존 라벨은 그대로 둠 — 멱등).
