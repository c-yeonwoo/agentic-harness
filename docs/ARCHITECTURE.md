# Agentic Harness — Architecture

> 본 문서는 README 의 다이어그램을 agent 별 책임 / 데이터 흐름 / 라벨 전이 / SoT 구조로 상세화한 것.

---

## 1. 데이터 모델 — GitHub Label + Issue + PR

### 1.1 라벨 4개

| 라벨 | 의미 | 누가 부여 / 제거 |
|------|------|------------------|
| `ah:needs-execution` | executor 큐 (issue: 신규 / PR: amend retry) | PO 부착 / pm 가 in-progress 로 swap |
| `ah:needs-review` | reviewer 큐 (PR) | executor 가 부착 / pm 가 in-progress 로 swap |
| `ah:awaiting-human` | 사람 결정 대기 (PR: merge 또는 라벨 떼서 흐름 멈춤) | reviewer / executor (실패 시 / retry cap 도달 시) 부착 |
| `ah:in-progress` | 워커 점유 락 (auto) | pm 가 dispatch 시 부착 / 워커 종료 시 제거 |

라벨이 곧 state. DB / Redis 등 별도 state store 없음.

### 1.2 흐름 (label transitions)

```
 ┌─────────────────────────────────────────────────────────────────────┐
 │ issue                                                                │
 │   (empty)                                                            │
 │     ↓ PO 생성                                                        │
 │   ah:needs-execution                                                 │
 │     ↓ pm dispatch (락)                                              │
 │   ah:in-progress                                                     │
 │     ↓ executor 성공                                                  │
 │   (라벨 제거) → PR 생성                                              │
 │     OR executor 실패 (EditApplyError) → ah:needs-execution + ❌ 댓글 │
 │     OR executor 실패 (기타) → ah:awaiting-human                      │
 │                                                                       │
 │ PR                                                                    │
 │   ah:needs-review (executor 가 부착)                                 │
 │     ↓ pm dispatch (락)                                              │
 │   ah:in-progress                                                     │
 │     ↓ reviewer                                                       │
 │   approve / concerns_noted   → ah:awaiting-human (사람 merge)        │
 │   request_changes            → ah:needs-execution (amend 큐로 자동)  │
 │     ↓ pm dispatch (다음 tick)                                       │
 │   ah:in-progress (PR 락)                                            │
 │     ↓ executor amend mode                                            │
 │   성공 → ah:needs-review (다시 reviewer)                             │
 │   실패 1차 → ah:needs-execution + ❌ 댓글 (cap=1 검사)              │
 │   실패 2차+ → ah:awaiting-human (cap 도달, 사람 결정)                │
 └─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Agent 별 책임

### 2.1 `palette-po` (Hermes skill, project-specific)

- **Input**: 자연어 agenda (사용자)
- **Output**: GitHub issue (`ah:needs-execution` 라벨)
- **책임**:
  1. SoT 가벼운 스캔 (GLOSSARY / ARCHITECTURE / CONVENTIONS / 최근 ADR title)
  2. scope 판정 (단일 도메인 / 다중 / ADR 필요)
  3. agenda 가 크면 task 분할 (t1, t2, t3 ...) — 각 task 별로 issue 생성
  4. issue body 템플릿 (Scope / Affected files / Acceptance criteria / Out of scope / Hints)
  5. `gh issue create --label ah:needs-execution`
- **모델**: PO 단계는 reasoning 강 모델 권장 (sonnet / o1)

### 2.2 `pm` (Hermes cron, every 5m, no-agent)

- **Input**: 라벨 큐 (GitHub API)
- **Output**: 1 tick 당 1건 dispatch (또는 Phase C 후 N건 병렬)
- **책임**:
  1. WIP 가드 (`ah:in-progress` 카운트 ≤ cap)
  2. 우선순위 큐 스캔:
     1) PR `ah:needs-review` → reviewer
     2) PR `ah:needs-execution` → executor amend
     3) issue `ah:needs-execution` → executor 신규
  3. 락 부여 (`ah:in-progress` 부착, 큐 라벨 제거)
  4. 워커 script 호출
- **모델**: LLM 없음 (단순 bash script — Hermes cron `--no-agent`)

### 2.3 `code-executor` (Runner 추상화 — Hermes ReAct 또는 Local claude -p)

- **Input**: issue 1건 (신규 모드) 또는 PR 1건 (amend 모드)
- **Output**: 새 PR (`ah:needs-review`) 또는 기존 PR 에 추가 commit + 라벨 swap
- **공통 (agents.py)**:
  1. 락 획득 (`ah:in-progress`)
  2. SoT discover + user prompt 빌드
  3. `ExecutionContext` 만들어 `Runner.execute()` 호출 (mode 분기는 `HARNESS_MODE` env)
  4. `ExecutionResult` 의 `ok` / `error_kind` 에 따라 PR 생성 + 라벨 전이
  5. 락 해제
- **Hermes mode (`ApiRunner`, default)**:
  1. SoT inject (system prompt 의 cache 영역)
  2. ReAct tool-loop — `list_files` / `read_file` / `search_text` / `submit_plan`
  3. plan 정규화 (`_normalize_plan`)
  4. `apply_plan_and_push` — worktree + create/edit/replace/delete + edit fuzzy fallback
- **Local mode (`LocalClaudeRunner`, ADR-011)**:
  1. `prepare_worktree` (신규: base 에서 / amend: origin/<head_ref> 에서)
  2. `claude -p --output-format json --permission-mode bypassPermissions` spawn
     (cwd = worktree, `--disallowedTools` 로 git/gh 쓰기 차단)
  3. claude 가 Read/Edit/Write/Bash 로 직접 편집 + 최종 JSON 출력
  4. `stage_commit_push_all` — worktree 의 모든 변경을 1 commit + push
  5. `cleanup_worktree`
- **에러 처리** (공통):
  - `edit_apply` (Hermes 만) → ❌ 코멘트 + `ah:needs-execution` 재부착 (cap=1)
  - `no_changes` (Local) → ❌ 코멘트 + awaiting-human (모델이 작업 거부)
  - `no_plan` / `crashed` → ❌ 코멘트 + 호출자가 awaiting-human
- **모델**: Hermes 는 EXECUTOR_MODEL env (codex / sonnet). Local 은 claude -p 의
  현 user OAuth 세션 (`LOCAL_CLAUDE_MODEL` 로 override 가능)

### 2.4 `code-reviewer` (Python single-call)

- **Input**: PR 1건 (`ah:needs-review`)
- **Output**: review comment (inline + summary) + 라벨 전이
- **책임**:
  1. SoT inject + PR diff + linked issue + 최근 comment 20개
  2. 단일 LLM call (JSON 응답)
  3. verdict 별 분기:
     - `approve` / `concerns_noted` → `ah:awaiting-human` (사람 merge)
     - `request_changes` → `ah:needs-execution` (amend 큐, PR 유지)
  4. sot-gate 검사 (선택) — diff 가 SoT 갱신 동반 필요인지
- **모델**: 작은 모델 OK (haiku / gpt-4o-mini) — diff 분석 단순

### 2.5 `issue-finder` (Phase D, Hermes cron, every 1h)

- **Input**: 코드베이스 자체
- **Output**: 새 issue (`ah:needs-execution`)
- **책임**:
  1. TODO / FIXME 자동 scan
  2. ADR 룰 위반 detection (raw hex, 라벨 컨벤션 깨짐 등)
  3. lint 위반 누적 → issue
  4. dry-run 모드 (config) — issue 안 만들고 report 만

### 2.6 `ssot-manager` (Phase E, GitHub Action 또는 cron)

- **Input**: merge 된 PR
- **Output**: SoT 갱신 PR (`ah:needs-review`)
- **책임**:
  1. PR diff 영향 분석
  2. ARCHITECTURE.md / GLOSSARY.md / CONVENTIONS.md 갱신 후보 식별
  3. 자동 PR 생성 (사람이 review 후 merge)

---

## 3. SoT (Single Source of Truth) 4-tier

```
Tier 1 (글로벌) — ~/.claude/CLAUDE.md
   ↓
Tier 2 (조직)   — ~/dev/CLAUDE.md  (예: 사내 컨벤션)
   ↓
Tier 3 (프로젝트) — <repo>/CLAUDE.md
   ↓
Tier 4 (도메인) — <repo>/docs/*.md
                  <repo>/docs/DECISIONS/*.md (ADR)
                  <repo>/.hermes/agent-context.md
                  recent PRs (20) + recent issues (20)
```

각 agent 의 system prompt 에 위 전체가 inject. caching 으로 cost 절감.

`source_of_truth.py:discover(cwd)` 가 자동 발견:
- `cwd/CLAUDE.md` chain (parent 까지)
- `cwd/ARCHITECTURE.md` 또는 `cwd/docs/ARCHITECTURE.md`
- `cwd/docs/*.md` (FEATURE_SPEC 제외)
- `cwd/docs/DECISIONS/*.md` (title + 첫 1.5KB)
- `cwd/.hermes/agent-context.md` (있으면)
- gh API: recent PR + issue

---

## 4. 트랙별 진입점

agentic-harness 는 두 가지 트랙으로 동작 가능 — 같은 라벨 state machine /
SoT / GitHub flow 위에서, cron 트리거와 LLM 진입점만 다름.

### 4.1 local 트랙 (default, ADR-011)

- **cron**: macOS `launchd` LaunchAgent (`~/Library/LaunchAgents/com.agentic-harness.<slug>.plist`)
  - `StartInterval=300` (5분) — `ah run --once --repo X --cwd Y --mode local`
  - 설치: `bash scripts/setup-local-launchd.sh <repo>`
  - 로그: `~/Library/Logs/agentic-harness/<slug>.{out,err}`
- **LLM**: `claude -p` 헤드리스 — user OAuth/subscription, opus 4.7
- **PO 진입**: `ah add-task "<자연어>"` (또는 claude code 안에서 slash command)
- **PR body 생성**: `/pr-description` 스킬 (claude code 의 ~/.claude/commands/pr-description.md)
- **불필요**: `.hermes/` 디렉토리, Hermes gateway, OPENAI/ANTHROPIC API key

### 4.2 hermes 트랙 (API 모드)

- **cron**: Hermes (`hermes cron create 'every 5m' --no-agent --script foo.sh`)
- **dispatcher**: `palette-pm.sh` → `palette-executor.sh <N>` / `palette-reviewer.sh <N>`
- **LLM**: OpenAI (gpt-5.3-codex) 또는 Anthropic (claude-*) API
- **PO 진입**: `hermes chat -s palette-po "<자연어>"` (Hermes skill)
- **PR body 생성**: executor 의 plan JSON 의 `pr_body` 필드
- **필요**: `.hermes/` 디렉토리, Hermes gateway, API key

### 4.3 공통 — agentic-harness 가 맡는 책임

- **Runner 추상화 (`runners/`)** — local / hermes 분기점
- **LLM tool-loop (ReAct)** — hermes 전용 (`llm.py`, `code_tools.py`, `_normalize_plan`)
- **claude -p spawn** — local 전용 (`runners/local_claude.py`)
- **git worktree + edit + push** — `git_apply.py` (두 모드 공유)
- **GitHub label / PR / Closes #N** — `gh.py`
- **SoT 발견 + caching** — `source_of_truth.py`
- **cost 추적 + model 선택** — `_PRICING` (hermes) / claude envelope (local)

→ 두 트랙은 peer. Hermes 가 죽어도 local 트랙 영향 X, 반대도 마찬가지.

---

## 5. Project 별 .hermes/ scaffold

각 프로젝트가 자체 SoT + skill + script 가짐. agent-harness 의 Python entrypoint 만 import.

```
<project>/
├── CLAUDE.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── CONVENTIONS.md
│   ├── GLOSSARY.md
│   └── DECISIONS/
└── .hermes/
    ├── agent-context.md              # 워커 prompt 진입 시 자동 주입
    ├── skills/
    │   └── <project>-po/SKILL.md     # PO skill (Hermes 트랙)
    ├── scripts/
    │   ├── <project>-pm.sh           # 라벨 큐 dispatch
    │   ├── <project>-executor.sh     # agents.run_code_executor 호출
    │   └── <project>-reviewer.sh     # agents.run_code_reviewer 호출
    ├── aliases.sh                    # shell wrapper (선택)
    ├── bootstrap.sh                  # ah: 라벨 4개 생성
    └── cron-setup.sh                 # Hermes cron 등록
```

추가로 Claude Code 트랙 (선택):
```
<project>/.claude/commands/add-task.md   # /add-task slash command
```

---

## 6. 비용 / Latency 추정 (palette PoC 기준)

| 단계 | LLM | input tokens | output tokens | cost | time |
|------|-----|--------------|---------------|------|------|
| PO (Hermes skill) | sonnet | ~3000 (SoT 가벼움) | ~1000 | $0.10 | ~30s |
| executor 신규 (ReAct 7-9 iter) | haiku / sonnet | 누적 ~40K-50K (caching 후 -90%) | ~5K | $0.31 (haiku) / $1.00 (sonnet) | ~5min |
| reviewer (single call) | haiku | ~15K | ~2K | $0.02 | ~15s |
| executor amend | haiku / sonnet | 비슷 | 비슷 | $0.30 / $1.00 | ~5min |
| **총 (1 사이클, haiku, success)** | | | | **~$0.45** | **~6min** |
| **총 (1 사이클, sonnet, success)** | | | | **~$2.10** | **~6min** |

caching 효과: ReAct iter 2 이상에서 system prompt (SoT 21K tokens) cache hit → input cost 0.1x + rate limit 면제.

---

## 7. 핵심 의존성

- Python ≥ 3.11
- `anthropic ≥ 0.40.0` (Phase B 후 `openai` 도)
- `pyyaml`, `httpx`, `structlog`, `typer`
- `gh` CLI (인증 필요)
- `git` (worktree 지원, 2.20+)

---

## 8. 참고 — 폐기된 결정

- ❌ **reviewer request_changes → PR close + 새 PR 사이클** (이전 결정 #7)
  - 사유: review thread / git history / `Closes #N` 분산
  - 대체: PR 유지 + 라벨 swap + amend mode (현재 결정 #7)

---

## License

MIT
