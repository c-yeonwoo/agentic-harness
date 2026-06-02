# ADR-011 — Local Claude Code Runner Mode

> 결정일: 2026-06-02
> 상태: Accepted (executor 만 prototype)

## 배경

기존 흐름은 raw LLM API (Anthropic / OpenAI) 를 Python ReAct 루프로 호출 → plan
JSON 생성 → `git_apply.py` 가 worktree 에 적용. 실사용에서 문제:

- **입력 손실**: 한 번에 전달되는 prompt 의 컨텍스트가 모델에서 충분히 활용 안 됨
  (특히 codex 계열). 같은 작업을 로컬 클로드 코드 인터랙티브 세션에서 시키면 훨씬
  정확하고 빠름.
- **brittleness**: plan JSON 의 `edit.old_str` 매칭이 LLM 출력 품질에 강하게 의존.
  `EditApplyError` retry 큐 / `_normalize_plan` (Haiku 의 stringified JSON 복구) /
  fuzzy + anchor fallback 같은 보호 레이어가 누적됨.
- **비용**: API 사용 시 토큰 비용 누적. 로컬 클코는 user OAuth/subscription → token
  비용 0 (플랜 한도 내).
- **품질**: 같은 모델이라도 인터랙티브 세션의 Read/Edit/Bash 네이티브 도구가 직접
  쓰이는 환경 vs API 의 단발 plan 출력은 결과물 품질 차이 큼.

## 결정

`orchestrator/runners/` 에 `Runner` 추상화를 둔다. 두 구현:

1. **`ApiRunner`** (default, mode=`hermes`) — 기존 ReAct + plan-apply 흐름. 백compat.
2. **`LocalClaudeRunner`** (mode=`local`) — `claude -p --output-format json
   --permission-mode bypassPermissions --add-dir <wt>` 로 헤드리스 spawn. claude 가
   worktree 안에서 Read/Edit/Write/Bash 직접 사용. plan JSON 폐기.

`agents.run_code_executor` / `run_code_executor_amend` 의 락 / SoT discover /
PR 생성 / 라벨 전이 로직은 그대로. 가운데 "LLM 호출 + apply" 부분만 `runner.execute()`
로 교체.

분기 env:
- `HARNESS_MODE=hermes|local` (default `hermes`)
- 역할별 override: `EXECUTOR_MODE`, `REVIEWER_MODE`

reviewer 는 ReAct 가 필요 없는 단일 LLM 호출 — 현재 Runner 우회 (`hermes` 만).
로컬 모드 reviewer 는 향후 작업.

PO / ssot-manager 는 Hermes skill 진입점 / 문서 갱신이라 Runner 와 무관 — 향후
별도 작업.

## 책임 경계 (LocalClaudeRunner)

`claude -p` 가 책임:
- worktree 안 파일 편집 (Read/Edit/Write/Grep/Glob)
- 로컬 검증 (lint/typecheck 실행 가능)
- 작업 끝나면 단일 JSON 출력 (summary / files_changed / pr_title / pr_body / ...)

harness 가 책임 (claude 에서 호출 금지):
- worktree 분기 / 정리 (`git worktree add` / `remove`)
- commit / push (`git add -A` / `git commit` / `git push`)
- PR 생성 (`gh pr create`)
- 라벨 전이 (`gh ... edit --add-label/--remove-label`)
- 락 (`ah:in-progress`)

claude 의 `--disallowedTools` 로 `Bash(git push:*)`, `Bash(git commit:*)`, `Bash(gh
pr:*)`, `Bash(gh issue:*)` 차단.

## 트레이드오프

**장점**:
- plan JSON / edit 매칭 / retry cap 인프라 전체가 무력화 (brittleness ↓)
- token 비용 0 (subscription 한도 내)
- 품질 = 인터랙티브 클코 세션과 동등
- 사용자의 ANTHROPIC_API_KEY / OPENAI_API_KEY 없어도 동작

**단점**:
- **동시성 제약**: 한 user 계정의 동시 헤드리스 인스턴스 수 / rate limit 영향. 초기에는
  `MAX_PARALLEL_EXECUTORS` 보수적으로 (1~2) 잡고 시작.
- **observability 약화**: 현재의 `cost_usd` / `tool_trace` / token 정확도가 떨어짐.
  claude -p 의 envelope (`total_cost_usd`, `usage`) 로 일부 회복은 됨.
- **모델 선택 자유도 ↓**: claude 코드의 기본 모델 / `--model` 옵션 범위 내에서만.
  Codex / GPT-4o / Sonnet 비교 같은 건 hermes mode 로만 가능.
- **권한 모드**: `bypassPermissions` 사용 — bot 컨텍스트에선 필요하지만 일반 user
  컨텍스트에선 위험. worktree 가 임시 디렉토리이고 `--disallowedTools` 로 git/gh
  쓰기 차단되어 있어 blast radius 는 제한적.

## 폐기된 대안

- ❌ **클로드 코드의 빌트인 스케줄링 (`/schedule`, `CronCreate`, `mcp__scheduled-tasks`)
  으로 cron 대체**
  - 사유: 이건 Anthropic 인프라의 remote agent 라 로컬 fs / git / gh 직접 접근 X.
  - 대체: macOS launchd LaunchAgent — `scripts/setup-local-launchd.sh` 자동 설치.

- ❌ **`/loop` 또는 `ScheduleWakeup` 으로 polling**
  - 사유: 인터랙티브 세션 살아있을 때만 동작. 데몬 부적합.

- ❌ **local mode 에서 Hermes cron 재사용**
  - 사유: Hermes 의 `palette-pm.sh` → `palette-executor.sh <N>` 디스패치 패턴은
    "API 모드의 bash wrapper" 가 본질. local mode 에선 `ah run --once` 가 폴링 +
    병렬 dispatch + Runner 호출 다 자체적으로 해서 디스패처 / wrapper 불필요.
    Hermes 인프라 의존을 끊고 launchd 만 사용.
  - 대체: 두 트랙을 peer 로 — local 은 launchd + `ah run --once`, hermes 는
    기존 `.hermes/` 그대로. 사용자 선택.

## 단계 (Rollout)

| Phase | 산출물 | 상태 |
|-------|--------|------|
| B'.1 | Runner 추상화 + LocalClaudeRunner (executor 만) | ✅ |
| B'.2 | palette repo 인공 issue 로 end-to-end smoke test | 🟡 사용자 라이브 검증 |
| B'.3 | 기본 mode = `local`, default model = `opus` (4.7) | ✅ |
| B'.4 | reviewer 로컬 모드 (`run_reviewer_local` + code-reviewer-local.md) | ✅ |
| B'.5 | PR body 를 `/pr-description` 스킬로 생성 (commit 후 추가 spawn) | ✅ |
| B'.6 | 실 사용 1주 — failure mode / 동시성 한계 관찰 | 🔲 |
| B'.7 | PO / ssot-manager (별도 entry 라 작업 분리) | 🔲 |

## 후속 결정 (#12 — PR description skill)

executor 의 prompt JSON 에 `pr_body` 가 있지만, **실제 PR body 는 commit/push
후 `/pr-description` 스킬을 한 번 더 `claude -p` 로 호출해서 생성**한다. 이유:

- 스킬이 `git log main..HEAD` / `git diff --stat` 기반으로 실제 변경을 읽어
  더 정확한 body 작성
- executor 의 부담 ↓ (코드 작업에 집중)
- 일관된 PR 포맷 (스킬 한 곳에서 관리)
- 스킬 실패 / 빈 결과 시 `payload.pr_body` 로 fallback — 항상 PR 은 생성됨

비 main base 의 경우 prompt 에 `(base 브랜치는 X)` hint 추가해서 스킬에 전달.
스킬 자체 파일은 `~/.claude/commands/pr-description.md` — 사용자가 직접 관리.
