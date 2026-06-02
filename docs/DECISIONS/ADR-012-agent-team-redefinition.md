# ADR-012 — Agent Team 재정의 (PO / developer / reviewer / critique)

> 결정일: 2026-06-02
> 상태: Accepted (이번 세션: rename + 새 라벨 + PO mode A. debate / critique / PO modeB 는 다음 세션)

## 배경

ADR-011 로 local 트랙 도입 후, 다음과 같은 사용자 의도가 명확해졌다:

> "PO, executor, reviewer, ssot-manager 가 하나의 팀 셋트가 제너릭하게 있고,
> 프로젝트마다 팀이 구성되어서 ssot 문서 보고, 캐싱해서 사용하는 프로세스."

기존 상태와 의도 사이의 gap:
- ❌ **PO 가 generic 이 아님** — `palette-po` 같은 프로젝트별 Hermes skill 로만 존재
- ❌ **ssot-manager 미구현** (Phase E placeholder)
- ⚠️ **executor 라는 이름**이 "사람이 시키는 대로 실행" 뉘앙스 — 코드 작성 + review 협상 책임을
  담기엔 부족
- ⚠️ **reviewer ↔ executor 가 단방향** — reviewer 가 request_changes 하면 executor 가
  무조건 amend. 잘못된 review 에도 반박 불가

## 결정

### 1. Team 구성 (4-agent generic team)

| Agent | 책임 | 권한 |
|-------|------|------|
| **PO (mode A)** | 자연어 agenda → SoT 대조 → 정리된 issue 1~N 개로 분할 | issue 생성 |
| **PO (mode B)** | merged PR scan → SoT (ARCHITECTURE/docs/ADR) 갱신 PR 자동 생성 | PR 생성 (자기 사이클 거침) |
| **developer** | 코드 작성 + review 협상 (납득되면 amend, 안 되면 counter comment 로 반박) | PR 생성/amend, 라벨 전이 |
| **reviewer** | SoT 와의 **정합성 게이트** — 컨벤션 / 설계 의도 / 기능 위반 판정 | approve / request_changes (block 권한) |
| **critique** | debate 끝난 후 final gate — 더 나은 방식 / 효율적 접근 비평 | suggestion-only (block 권한 없음) |

기존 `executor` → `developer` 로 리네임 (ADR-012 rename). `run_developer` /
`run_developer_amend` 새 함수명, `run_code_executor` / `run_code_executor_amend`
는 back-compat alias 로 유지.

PO 가 SSOT manager 역할 흡수 (Phase E 별도 agent 안 만듦) — 두 역할 모두 "문서/구조 관리"
라 자연스러운 결합.

### 2. State machine — 라벨 8개

| 라벨 | 의미 | 누가 부착 |
|------|------|----------|
| `ah:needs-execution` | developer 큐 (issue 신규 또는 PR amend) | PO / reviewer (request_changes) / critique tie-break |
| `ah:needs-review` | reviewer 큐 (PR) | developer (PR 생성/amend) |
| `ah:in-debate` ⭐ | developer 가 review 에 반박 중 (다음 tick reviewer 재평가) | developer (counter comment 후) |
| `ah:needs-critique` ⭐ | reviewer 통과 후 critique final gate 대기 | reviewer (approve) |
| `ah:awaiting-human` | 사람 merge / escalation | critique (suggestion 부착 후) / developer (실패 시) |
| `ah:in-progress` | 워커 점유 락 | pm (모든 agent 진입 시) |
| `ah:sot-pending` ⭐ | merged PR — PO mode B 가 SoT 갱신 필요 | PO mode B 가 매 tick 스캔해서 자동 부착 |
| `ah:sot-done` ⭐ | PO mode B 가 처리 완료 (skip) | PO mode B 처리 후 |

⭐ 신규 라벨 (이번 세션 추가).

### 3. Debate 흐름 (ADR-012 핵심)

```
reviewer → request_changes → [ah:in-debate]
                                  ↓
                          developer (PR 보고 평가)
                                  ↓
                ┌─────────────────┼─────────────────┐
              납득                 반박            cap 도달 (round ≥ 2)
                ↓                  ↓                  ↓
         amend commit          counter comment      critique tie-break
                ↓                  ↓                  ↓
       [ah:needs-execution]   [ah:needs-review]   reviewer 옳음 → [needs-execution]
        → developer amend     → reviewer 재평가    developer 옳음 → [needs-critique]
        (round++)              (round 유지)
```

**Round cap = 2** (Q&A 로 사용자 결정):
- round 1: 첫 debate (reviewer 지적 → developer 반박/amend)
- round 2: 두 번째 시도
- round ≥ 2 → critique 강제 개입 (tie-break)

라벨 `ah:in-debate` 가 PR 에 부착된 횟수 = round 카운트.

### 4. Critique 의 진입 시점 + 권한 (Q&A 로 사용자 결정)

- **시점**: **debate 끝난 후 final gate**. reviewer + developer 합의 후 메타 비평.
- **권한**: **Suggestion-only** — block 권한 없음. 항상 `ah:awaiting-human` 으로 전이.
  사람이 merge 결정 시 critique 의 suggestion 참고.
- **예외**: debate cap 도달 시 tie-break 권한 발동 (reviewer 옳음 / developer 옳음 판정).

### 5. reviewer vs critique 의 책임 boundary

겹침 방지:
- **reviewer**: SoT (CLAUDE.md / ARCHITECTURE / 컨벤션) 와의 **정합성 게이트**.
  "이게 이 프로젝트 룰에 맞는가" — *deny / pass* 판정.
- **critique**: SoT 와 무관하게 **더 나은 방식** 비평. "이게 최선인가, 다른 접근은 없나"
  — *improvement suggestion*. block 권한 없음.

### 6. SoT 캐싱 (이미 구현됨 — ADR-011 후속 확인)

- 4-tier (글로벌 → 조직 → 프로젝트 → 동적) 자동 발견 — `source_of_truth.discover(cwd)`
- `.hermes/cache/sot/<hash>.json` 에 30분 TTL 캐시 (`SOT_CACHE_TTL_SEC` env)
- mtime + git HEAD 기반 무효화 — CLAUDE.md / docs/* / ADR / commit 변경 자동 감지
- 모든 agent (PO / developer / reviewer / critique) 가 `sot.to_prompt()` 로 동일 SoT inject

### 7. 프로젝트별 team 인스턴스화

각 프로젝트당:
- `launchctl` LaunchAgent 1개 = team 1개 (`com.agentic-harness.<slug>.plist`)
- `ah run --once --repo X --cwd Y --mode local` 가 tick 마다 라벨 큐 순회 + agent dispatch
- SoT 는 프로젝트 cwd 안에서만 발견 (CLAUDE.md / docs / ADR 모두 프로젝트 소유)
- `.agentic.yml` (선택) — 프로젝트별 agent override 가능 (다음 세션 확장)

→ "프로젝트마다 팀 구성, SoT 보고 캐싱" 의도 달성.

## 트레이드오프

### 장점
- **debate 패턴** — 잘못된 review 에 developer 가 반박 가능 → 품질 ↑
- **critique 분리** — reviewer 는 정합성, critique 은 improvement. 책임 명확
- **PO + SSOT manager 통합** — agent 수 줄임, SoT 자기 사이클로 일관성 ↑
- **이름 정합** — `developer` 가 책임 (코드 + 협상) 잘 표현

### 단점
- **agent 수 증가** (3 → 4) — orchestration 복잡도 ↑
- **라벨 4개 추가** (4 → 8) — state machine 변화량 큼
- **debate cap 도달 시 tie-break 로직** — critique 의 reviewer/developer 판정 정확도가
  전체 흐름의 신뢰도에 강하게 의존
- **PO mode B (SoT 자동 갱신)** — 잘못 갱신하면 SoT 가 망가짐 → reviewer 가 자기 SoT 갱신
  PR 도 보게 되므로 dogfood 자체가 안전망 (그래도 risk)

## 단계 (Rollout)

| Phase | 산출물 | 상태 |
|-------|--------|------|
| C'.1 | executor → developer rename (back-compat alias) | ✅ |
| C'.2 | 새 라벨 4개 추가 (`in-debate`, `needs-critique`, `sot-pending`, `sot-done`) | ✅ |
| C'.3 | PO mode A — `agents/po-local.md` + `run_po_local()` + `ah add-task` 통합 | ✅ |
| C'.4 | Debate 흐름 — developer 가 review 받으면 (a) amend or (b) counter | 🔲 다음 세션 |
| C'.5 | Critique local agent — `agents/critique-local.md` + `run_critique_local()` | 🔲 |
| C'.6 | PO mode B — merged PR scan → SoT 갱신 PR 자동 생성 | 🔲 |
| C'.7 | State machine 통합 갱신 — poller, label 전이, retry cap | 🔲 |
| C'.8 | reviewer 책임 boundary 명확화 (code-reviewer-local.md 갱신) | 🔲 |
| C'.9 | `.agentic.yml` 의 `enabled_agents` 지원 (옵션) | 🔲 |

## 폐기 결정

- ❌ **executor 이름 유지** — "사람이 시키는 대로 실행" 뉘앙스. ADR-012 로 developer 로 리네임.
- ❌ **별도 ssot-manager agent** — PO 가 modeB 로 흡수. 둘 다 "문서/구조 관리" 라 자연스러운 결합.
- ❌ **reviewer 가 critique 까지 담당** — boundary 불명확. critique 분리해서 reviewer 는
  정합성 게이트, critique 은 improvement suggestion 으로 명확히.
