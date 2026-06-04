# ADR-020 — CLI TUI 대시보드 (`ah dashboard`)

> 날짜: 2026-06-04
> 상태: Accepted

## 결정

`rich.Live` 기반 터미널 풀스크린 대시보드. `ah dashboard` 한 명령. 4 패널:
launchd 상태 / PR 큐 / 최근 로그 / 비용. 5초마다 자동 refresh.

## 이유

multi-repo 운영 늘리기 전에 **가시성** 필요. 현재 launchd 상태 / 라벨 큐 / 로그 / 비용
확인하려면 4개 명령 따로 — 한 화면에 모아야 효율 ↑.

## 대안 / 폐기 옵션

- **A: Web UI (FastAPI + Chart.js)** — 차트 좋지만 always-on 서버 부담, 600+ LOC
- **B: 정적 HTML 생성 (cron)** — 1분 지연, 시각화 약함
- **C: Slack/Telegram 알람만** — 대시보드 X, 이벤트 push 만

→ **TUI (rich)** 채택 — 빠른 구현 (~400 LOC), SSH 친화, 의존성 적음, 풀스크린 시각화 충분.

## 영향

- `orchestrator/dashboard.py` (신규) — 데이터 수집 + Layout + Live loop
- `cli/main.py`: `ah dashboard --repo X --interval 5` 명령
- 의존성: `rich` (이미 transitively 있음)
- multi-repo 자동 감지 (launchctl list 에서 `com.agentic-harness.*` 파싱)

## 참고

- 관련 코드: `orchestrator/dashboard.py`, `cli/main.py:dashboard`
- 데이터 소스: `launchctl list/print` + `gh.list_prs` + `~/Library/Logs/agentic-harness/*.out` + `cost_report`
- 후속 확장 후보: 인터랙티브 키 (`s` trigger / `k` kill stale / `f` filter), Web UI (필요 시)
