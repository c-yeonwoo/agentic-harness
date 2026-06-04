# ADR-018 — Critique agent + 운영 도구 (cost / budget / drift)

> 결정일: 2026-06-04
> 상태: Accepted

## 배경

ADR-012 에서 4-agent team 정의 — PO / developer / reviewer / **critique**. PO /
developer / reviewer 구현. critique 는 placeholder.
또 운영 가시성 부족 — 비용 추적 / budget cap / drift 점검 도구 없음.

## 결정

### 1. Critique agent

reviewer 가 approve 한 PR 에 **메타 비평** — "이게 정말 최선인가, 더 단순한 방법은
없나" 물음. **block 권한 없음** (suggestion-only).

- `agents/critique-local.md` (prompt)
- `orchestrator/runners/local_claude.py`: `run_critique_local`
- `orchestrator/agents.py`: `run_code_critique` (wrapper, lock + label transition)
- `orchestrator/poller.py`: `ah:needs-critique` PR 픽업 단계 추가
- reviewer 가 approve 시 → `ah:needs-critique` (이전엔 `ah:awaiting-human` 직행)
- critique 끝나면 → `ah:awaiting-human` + suggestion 코멘트
- SoT 갱신 PR (`docs(sot):` prefix) 은 critique skip — 재귀 차단

### 2. `ah cost` 명령

- `orchestrator/cost_report.py` — 로그 파싱 + PR comments footer 파싱
- 소스: `~/Library/Logs/agentic-harness/*.out` 의 `cost=X model=Y` (ANSI strip)
  + (옵션) GitHub PR comments 의 `_cost $X · ... model=Y_` footer
- 옵션: `--since 1d|1w|1m`, `--repo X` (PR 합산), `--source auto|log|pr`
- 출력: 합계 + agent 별 / model 별 / 일별 breakdown

### 3. Budget cap (`SOT_UPDATE_BUDGET_USD_PER_WEEK`)

- PO mode B 진입 직전 검사
- 지난 7일 SoT 갱신 비용 누적이 budget 초과 시 → skip + warning
- 0 또는 미설정 = 무제한
- (참고: budget 은 SoT 갱신만. developer / reviewer cycle 비용 별도)

### 4. `ah sot-drift-check` 명령

- `agents/sot-drift-check-local.md` (prompt)
- `orchestrator/runners/local_claude.py`: `run_sot_drift_check_local`
- 점검 항목: 빌드 명령 / 디렉토리 구조 / GLOSSARY / ADR vs 코드 / 컨벤션
- sampling 위주 (전수 X) — ~$2 / 호출
- severity (none/minor/major) + drift 목록 출력
- `--create-issue` 옵션 (default true) — drift 발견 시 GitHub issue 자동 생성
- cron 자동화 X — 사람이 월 1회 정도 수동 실행

## 라벨 추가

`ah:needs-critique` 는 ADR-012 에 정의돼 있지만 실제 사용은 이번부터.

| 변화 | 이전 | 이후 |
|------|------|------|
| reviewer approve | → `ah:awaiting-human` 직행 | → `ah:needs-critique` |
| critique 끝 | (구현 X) | → `ah:awaiting-human` + suggestion 코멘트 |

PR title 이 `docs(sot):` 또는 `chore(sot)` 로 시작하면 critique skip — PO mode B
가 만든 SoT 갱신 PR 의 재귀 차단.

## 트레이드오프

### 장점
- Team 완성도 ↑ — 4-agent loop 마지막 단계 작동
- 메타 비평으로 "그냥 통과" PR 의 품질 한 단계 더 보강
- 운영 가시성 ↑ — `ah cost` 로 누적 비용 추적 가능
- Budget cap 으로 비용 폭주 방지
- Drift check 로 SoT 의 신뢰도 점검 가능

### 단점
- critique 1회 호출 ~$0.10 추가 — 1 사이클 비용 +$0.10
- critique 의 suggestion 이 noise 일 수 있음 (사람이 무시 가능 — block X 라 안전)
- drift check 는 sampling — 100% 정확하지 않음 (사람 검토 필요)
- 로그 파싱 의존 — log format 바뀌면 cost 집계 깨질 수 있음

## 비용 영향

1 사이클 (1 issue → merge):

| 단계 | 이전 | 이후 |
|------|------|------|
| PO + Developer + /pr-description + Reviewer + amend | $2.35 | $2.35 |
| **+ Critique (신규)** | — | **+$0.10** |
| SoT 갱신 (high) | — | +$0.30 / event |
| SoT 갱신 (medium batch) | — | +$2 / 주 |
| **1 사이클** | **$2.35** | **~$2.45** |

주간 운영:
- 15 PR / 주 가정
- critique: 15 × $0.10 = $1.50
- SoT urgent: 평균 1~2건 × $0.30 = $0.30~$0.60
- SoT batch: $2 (5개 이상 모이면)
- drift check: 월 1회 $2
- **주 ~$4 ~ $4.10**

## 운영 권장

```bash
# 일상
.venv/bin/ah cost --since 1d              # 매일 추적
.venv/bin/ah cost --since 1w --repo X     # 주간 정확 합산

# 비용 cap
echo 'SOT_UPDATE_BUDGET_USD_PER_WEEK=5' >> .env

# 월 1회
.venv/bin/ah sot-drift-check --repo c-yeonwoo/palette
```
