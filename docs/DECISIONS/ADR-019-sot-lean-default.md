# ADR-019 — SoT inject 슬림화 + ADR 본문 lazy load

> 날짜: 2026-06-04
> 상태: Accepted

## 결정

agent 의 SoT inject 에서 **ADR 본문 기본 제외** (`SOT_INCLUDE_ADR=off` default).
agent 는 필요 시 `docs/DECISIONS/` 에서 `Read` 로 직접 펼침. ARCHITECTURE.md 의
"핵심 결정 요약" 섹션이 ADR 번호 매핑 제공.

새 ADR 양식은 `_TEMPLATE.md` 기준 50~100줄. 기존 ADR-011 ~ 018 은 그대로 (역사 보존).

## 이유

매 agent call 마다 ADR 8개 × 1.5KB ≈ 3K tokens 추가 inject 됐음. 75 call/주 기준
sonnet ~$1/주 ADR 만으로 비용 발생. ADR 본문은 사람 검토용 — agent 일상 작업에
거의 안 쓰임.

## 대안 / 폐기 옵션

- **A: 그대로 두기** — token 부담 누적
- **B: ADR title 만 inject** — 채택 (`SOT_INCLUDE_ADR=titles`). 본문은 lazy load
- **C: 본문 다 inject** — 채택 (`SOT_INCLUDE_ADR=full`). 의도적으로 켤 때만

## 영향

- `orchestrator/source_of_truth.py` — `adr_mode` 옵션 추가 (off/titles/full)
- agent prompts 4종 (developer / reviewer / critique / po) — "필요시 Read" 한 줄
- `docs/DECISIONS/_TEMPLATE.md` 신규
- `agents/sot-bootstrap-local.md` / `agents/po-mode-b-local.md` — ARCHITECTURE 의
  "핵심 결정 요약" 섹션 생성 / 갱신 룰 명시
- 비용: 75 call/주 기준 sonnet ~$1/주 절감 (~15% SoT 사이즈 ↓)

## 참고

- 관련 코드: `orchestrator/source_of_truth.py:discover`
- 관련 ADR: ADR-011 (SoT 4-tier 도입)
- 옵션 켜기: `SOT_INCLUDE_ADR=titles|full` 또는 `.agentic.yml` 의 `sot.adr_mode`
