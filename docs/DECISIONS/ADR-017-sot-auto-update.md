# ADR-017 — SoT 자동 갱신 (PO mode B, tiered)

> 결정일: 2026-06-04
> 상태: Accepted (구현 완료)

## 배경

ADR-012 에서 PO mode B (merged PR → SoT 갱신 PR) 는 placeholder 였음. 단순하게
"매 merged PR 마다 LLM 호출" 하면 비용 ↑↑ — 대부분 PR 은 SoT 영향 없는데도 분석.

예: palette 가 주 15 PR merge → 매번 $0.30 호출 시 주 $4.50. 80% 가 작은 fix
인데 거기에 LLM 쓰는 건 낭비.

## 결정

**Tiered 자동 갱신** — 영향도 기반 분기:

### Tier 1: 영향도 판정 (비용 0)

Reviewer 가 이미 review 할 때 추가 비용 0 으로 `sot_impact` 함께 판정 (필드 추가):

| sot_impact | 자동 부착 라벨 | 처리 |
|---|---|---|
| `high` | `ah:sot-urgent` | merge 후 polling 이 픽업 → PO mode B 즉시 |
| `medium` | `ah:sot-batch` | 주간 batch 큐 |
| `low` | (라벨 없음) | noop — SoT 영향 없음 |

기준:
- high — BREAKING / 새 ADR / 새 도메인 / 새 외부 의존성
- medium — ≥10 파일 또는 ≥500줄, 도메인 안 큰 리팩터
- low — 작은 fix / typo / 테스트만 / isolated 변경

### Tier 2: 즉시 트리거 (high)

poller.py 의 4번째 단계:
- `gh.list_prs(state='closed', label='ah:sot-urgent')` + merged 필터
- 발견 시 PO mode B 단일 모드 (1 PR 분석)
- 처리 끝나면 라벨 제거 (재처리 방지)
- 비용: ~$0.30 / event

### Tier 3: 주간 batch (medium)

별도 launchd plist (`com.agentic-harness.<slug>.weekly.plist`):
- 매주 일요일 02:00 발동
- `ah sot-batch --repo X` 실행 — `ah:sot-batch` 라벨 merged PR 들 모음
- threshold 5 미달이면 skip (다음 주로 미루기)
- 5 이상이면 통합 분석 → SoT PR 1개
- 4주 누적 시 강제 처리 (drift 방지) — `--force` 또는 사람 수동
- 비용: ~$2 / 주

### Tier 4: 사람 수동 override

```bash
# 사람이 자동 감지 무시하고 강제 즉시
gh pr edit <N> --add-label ah:sot-urgent

# 또는 즉시 단일 처리 (라벨 안 거치고)
.venv/bin/ah sot-refresh <N> --repo c-yeonwoo/palette

# batch 수동 (threshold 무시)
.venv/bin/ah sot-batch --repo c-yeonwoo/palette --force
```

## 비용 비교

가정: 주 15 PR merge, 평균 60% medium, 10% high, 30% low.

| 전략 | 주당 비용 | 신선도 |
|---|---|---|
| 매 PR LLM (naive) | $4.50 | 즉시 |
| **Tier 1+2+3 (제안)** | **$0.45 + $2 = $2.45** | high 즉시 / medium 1주 |
| Tier 1 만 (LLM 0) | $0 | drift 누적 |

→ 약 **2x 절감 + 신선도 합리적** (critical 만 즉시, 나머지 batch).

## 라벨 명명 (ADR-014 기반)

```
ah:sot-urgent   — 즉시 갱신 (merge 시 polling 이 트리거)
ah:sot-batch    — 배치 큐 (주간 5개 이상이면 처리)
```

옛 `ah:sot-pending` / `ah:sot-done` 폐기 (의미 모호). 처리 끝나면 라벨 제거.

## 변경 사항

- `orchestrator/gh.py` STANDARD_LABELS — sot-urgent / sot-batch 추가
- `agents/code-reviewer*.md` — `sot_impact` 필드 + 판정 기준
- `agents.run_code_reviewer` — sot_impact 따라 라벨 자동 부착
- `agents/po-mode-b-local.md` (신규) — PO mode B prompt
- `orchestrator/runners/local_claude.py` — `run_po_mode_b_local` + `LocalPoModeBResult`
- `orchestrator/agents.py` — `run_po_mode_b` wrapper (worktree + PR 생성)
- `orchestrator/poller.py` — sot-urgent merged PR 발견 시 즉시 트리거 단계 추가
- `cli/main.py` — `ah sot-batch` / `ah sot-refresh` 명령
- `scripts/setup-local-launchd.sh` — `--weekly` 옵션 (별도 plist 생성)

## 트레이드오프

### 장점
- 매 PR LLM 호출 X — 비용 ~2x 절감
- 영향도 판정이 reviewer 와 묶여서 추가 LLM 0
- high / medium / low 분리로 운영 가시성 ↑
- 사람 수동 override 가능 — 자동 감지 잘못해도 복구
- self-dogfood — SoT 갱신 PR 도 일반 사이클 (reviewer / 사람 merge) 거침

### 단점
- medium 영향 변경이 1주일 늦게 SoT 반영 — 그 사이 다른 PR 이 옛 SoT 로 작업할 수 있음
- batch threshold (5) 미달 시 더 늦어짐 (drift 위험 — 4주 cap 으로 완화)
- reviewer 의 sot_impact 판정이 잘못되면 batch 큐에 누락
- 재귀 — SoT 갱신 PR 자체에도 reviewer 가 sot_impact 판정 가능 → 자기-트리거 막아야 (PR title 의 `docs(sot)` prefix 로 reviewer 가 skip)

## 미래 작업

- Budget cap (`SOT_UPDATE_BUDGET_USD_PER_WEEK`) — 넘으면 skip + alert
- Drift check (월 1회) — `ah sot-drift-check` 별도 명령
- self-trigger 차단 — SoT 갱신 PR 은 reviewer 가 sot_impact=low 강제

## 운영 룰

- `ah:sot-urgent` PR 이 생기면 5분 안에 PO mode B 자동 트리거. SoT 갱신 PR 생김.
- `ah:sot-batch` PR 들은 주간 weekly cron 발동 시 통합 처리.
- weekly cron 등록: `bash scripts/setup-local-launchd.sh --weekly c-yeonwoo/palette`
- 수동: `.venv/bin/ah sot-batch --repo X --force` (threshold 무시)
- 자기 PR 무시: SoT 갱신 PR 자체에는 라벨 자동 부착 X (reviewer 가 sot-update 패턴 인식)
