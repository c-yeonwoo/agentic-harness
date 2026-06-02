# Agentic Harness

> **Label-based agentic coding harness.** 사람이 자연어 agenda 만 던지면 PO 가 task 분할 → executor 가 코드 수정 + PR → reviewer 가 critique → 사람 merge → SoT 자동 갱신.
>
> Hermes-native. Provider-agnostic (Anthropic / OpenAI). Project-agnostic (palette 첫 use case).

---

## 아키텍처 (다이어그램)

```
[human] ─ hermes cli ──→ [task queue (t1, t2, t3 ...)]
                              │
                              ▼ need_execution
                         [palette-po]  ──←── SoT
                            (Hermes skill)
                              │
                              ▼
                       [github issue]
                              │
              ┌───────────────┴─────── hermes cron ───┐
              ▼                                        │
        [issue-finder] ──────→ [github issue]          │   ←── SoT
              │                                        │
              ▼ x N개 병렬                             │
        [code executor] ──→ [pr]  ──── need_review    │   ←── SoT
                                                       │
                              ┌────────────────────────┘
                              ▼ 반복 (need_execution)
                       [code reviewer (critique)]      ←── SoT
                              │
                              ▼ awaiting_human (작업 완료시)
                       [review comment] ─→ telegram alarm ─→ [human]
                              │                                │
                              │       comment, change label    │
                              │←───────────────────────────────┘
                              ▼ merge
                          [deploy]
                              │
                              ▼ trigger
                       [ssot manager] ──→ SoT 갱신 / 리인덱싱
```

각 agent 가 SoT (Single Source of Truth — `CLAUDE.md` chain + `docs/*` + `docs/DECISIONS/*` + recent PRs + agent-context) 를 참조. SoT 가 변경되면 모든 agent 즉시 반영.

---

## 핵심 결정

| # | 결정 | 사유 |
|---|------|------|
| 1 | **라벨이 곧 state machine** (4개: `ah:needs-execution` / `needs-review` / `awaiting-human` / `in-progress`) | DB/Redis 등 별도 state store 없음. GitHub label 이 single source. 깨지면 사람이 라벨로 복구. |
| 2 | **Hermes cron + bash script + Python ReAct** | Hermes 가 cron lifecycle / 격리 / 로그. Python (orchestrator/) 이 LLM tool-loop. 둘 분리 — Hermes 죽어도 `ah run` daemon fallback 가능. |
| 3 | **SoT 4-tier** | `~/.claude/CLAUDE.md` (글로벌) → `~/dev/CLAUDE.md` (조직) → `repo/CLAUDE.md` (프로젝트) → `docs/*` + `docs/DECISIONS/*` + recent PRs (20) + `.hermes/agent-context.md` |
| 4 | **ReAct + edit action 필수** | 500줄+ 파일에 `replace` 금지. `edits[{old_str, new_str}]` — old_str 1회 매칭, 실패 시 whitespace fuzzy fallback. |
| 5 | **plan 은 PR description 에만** | issue 엔 "PR 생성됨" 링크 + cost 한 줄. |
| 6 | **`Closes #N` 자동 추가** | merge 시 issue 자동 close. |
| 7 | **reviewer request_changes → PR 유지 + amend mode** | 기존 흐름 (PR close + 새 PR) 폐기 — review thread / git history / `Closes #N` 분산 문제. 같은 PR 의 branch 에 추가 commit. |
| 8 | **Provider-agnostic** | `llm.py` 가 Anthropic / OpenAI 둘 다 지원. `LLM_PROVIDER=openai|anthropic`. (Phase B — 진행 예정) |
| 9 | **prompt caching** | `cache_control: {type: "ephemeral"}` (Anthropic) / 자동 (OpenAI). ReAct 의 system prompt 반복 호출 시 cache hit. |
| 10 | **EditApplyError → 라벨 기반 self-heal retry (cap=1)** | edit 매칭 실패 시 PR 에 ❌ 코멘트 + `ah:needs-execution` 재부착. 다음 amend tick 에 직전 실패 정보 SoT 로 자동 inject. 2회 실패 시 `awaiting-human`. |
| 11 | **Runner 추상화 — `local` (default) / `hermes` 모드 분기** | Raw LLM API ReAct (Hermes) 의 입력 손실 / brittleness 우회. 기본은 `claude -p` 헤드리스 (`opus` alias = 4.7) — user OAuth/subscription, plan JSON / edit 매칭 인프라 무력화. 락/SoT/PR/라벨 로직 공유. `HARNESS_MODE=hermes` 로 opt-in 시만 API 모드. ADR-011. |
| 12 | **PR description 은 `/pr-description` 스킬에 위임** | developer 가 PR body 도 짜는 대신, commit/push 후 `claude -p "/pr-description"` 한 번 더 spawn — 실제 diff 기반 한국어 PR body. developer 의 JSON `pr_body` 는 스킬 실패 시 fallback. 책임 분리 + 일관된 포맷. |
| 13 | **Generic team 재정의 — PO / developer / reviewer / critique** | executor → developer rename. PO 가 SSoT manager 역할 흡수 (mode A: agenda→issue, mode B: merged PR→SoT 갱신 PR). reviewer 는 SoT 정합성 게이트, critique 은 improvement suggestion (block 권한 없음). debate cap=2, critique final gate. ADR-012. |
| 14 | **gh.py backend path-based 분기** | `~/dev-private/*` 에선 HTTP+`{REPO_BASE}_AGENT_PAT` (c-yeonwoo 개인 repo), `~/dev/*` 에선 gh CLI (ohouse work 인증). launchd plist 의 WorkingDirectory 가 repo cwd 라 자동 분기. `GH_BACKEND` env 로 override. ADR-013. |

---

## 모듈

```
agentic-harness/
├── orchestrator/
│   ├── runners/            # Runner 추상화 (ADR-011)
│   │   ├── __init__.py     # Runner Protocol / ExecutionContext / get_runner
│   │   ├── api.py          # ApiRunner — Hermes 모드 (LLM ReAct + plan apply)
│   │   └── local_claude.py # LocalClaudeRunner — `claude -p` 헤드리스
│   ├── llm.py              # Anthropic / OpenAI dual-support (Hermes 전용)
│   ├── claude.py           # Anthropic SDK + cost 추적 + caching
│   ├── code_tools.py       # ReAct tools (read_file / list_files / search_text / submit_plan) — Hermes 전용
│   ├── git_apply.py        # worktree prep + plan apply + commit/push (공용)
│   ├── gh.py               # gh CLI wrapper
│   ├── source_of_truth.py  # SoT 4-tier 발견 + to_prompt()
│   ├── lock.py             # ah:in-progress + assignee 락
│   ├── poller.py           # 30초 폴링 (Hermes cron 으로 대체 가능)
│   └── agents.py           # run_code_executor / run_code_executor_amend / run_code_reviewer
├── agents/
│   ├── developer.md            # developer prompt (Hermes / plan JSON) ── ADR-012
│   ├── developer-local.md      # developer prompt (Local / direct edit + JSON 결과)
│   ├── code-reviewer.md        # reviewer prompt (Hermes)
│   ├── code-reviewer-local.md  # reviewer prompt (Local / Read+Grep+JSON)
│   ├── po-local.md             # PO mode A — 자연어 → issue 분할 (ADR-012)
│   ├── code-executor.md        # ⚠ deprecated → developer.md (fallback)
│   └── code-executor-local.md  # ⚠ deprecated → developer-local.md (fallback)
├── cli/
│   └── main.py             # ah CLI — add-task, run, status, init-labels
├── docs/
│   ├── ARCHITECTURE.md     # 본 README 의 다이어그램 + agent 별 책임
│   └── DECISIONS/          # ADR (Phase 별 결정 기록)
└── pyproject.toml
```

---

## Phase plan

| Phase | 산출물 | 상태 |
|-------|--------|------|
| **A** | MVP — PO + executor + reviewer + amend mode + EditApplyError retry | ✅ palette PoC 검증 |
| **B** | LLM provider 추상화 (`llm.py`) — Anthropic / OpenAI dual-support | ✅ |
| **B'** | Runner 추상화 — `HARNESS_MODE=local` 로 claude -p 헤드리스 (ADR-011). executor 부터 prototype | 🟡 진행 중 |
| **C** | task queue 명시 + 병렬 executor (N건 동시 dispatch) | 🔲 |
| **D** | `issue-finder` agent — 코드베이스 자동 scan (TODO/FIXME/ADR drift) → issue 생성 | 🔲 |
| **E** | `ssot manager` — merge 후 SoT (ARCHITECTURE/GLOSSARY/ADR) 자동 갱신 PR | 🔲 |
| **F** | human signal — telegram/slack alarm (awaiting-human 시) | 🔲 |

---

## 두 트랙

| 트랙 | cron / 트리거 | LLM | 모델 | 권장 상황 |
|------|---------------|-----|------|----------|
| **local** (default) | macOS launchd | `claude -p` 헤드리스 (user OAuth) | opus 4.7 | 일상. 비용 0, 품질 ↑ |
| **hermes** | Hermes cron + bash wrappers | OpenAI / Anthropic API | codex / gpt-5 / claude-* | API 모델로 비교/디버깅, 사내 gateway 사용 시 |

두 트랙은 **공존**한다 — 같은 라벨 state machine + SoT + GitHub flow 위에서 동작.
모드 분기는 `HARNESS_MODE` env (또는 `EXECUTOR_MODE` / `REVIEWER_MODE`) 만으로 결정.

---

## 사용 — local 트랙 (권장)

```bash
# 1. agentic-harness venv
cd ~/dev-private/agentic-harness
python3.12 -m venv .venv && .venv/bin/pip install -e .

# 2. claude code OAuth 로그인 (한 번)
claude    # 인터랙티브 — /login

# 3. 프로젝트에 ah: 라벨 4개 생성
.venv/bin/ah init-labels --repo c-yeonwoo/palette

# 4. .env 에 PAT (LLM key 는 불필요)
echo 'PALETTE_AGENT_PAT=ghp_...' >> .env

# 5. macOS LaunchAgent 등록 — 5분마다 ah run --once 자동 실행
bash scripts/setup-local-launchd.sh c-yeonwoo/palette 300

# 6. 작업 던지기 — PO 가 SoT 보고 1~N 개 issue 로 분할 생성 (ADR-012)
.venv/bin/ah add-task "친구 카운트 +1 안 됨 — 수정" --repo c-yeonwoo/palette
#   --dry-run 으로 미리 확인 가능
#   --raw 로 PO 안 거치고 raw issue 1개

# 7. 이후는 자동
#    LaunchAgent 가 5분마다:
#      ah:needs-execution issue/PR → developer (claude -p opus)
#                                  → PR 생성/amend (PR body 는 /pr-description 스킬)
#      ah:needs-review PR          → reviewer (claude -p opus, SoT 정합성)
#                                  → ah:needs-critique (approve) 또는
#                                    ah:in-debate (request_changes) (다음 세션 구현)
#      ah:needs-critique PR        → critique (suggestion-only) → awaiting-human
#                                    (다음 세션 구현)
#      ah:sot-pending merged PR    → PO mode B → SoT 갱신 PR (다음 세션 구현)
#    사람은 PR 보고 merge 결정만.

# 디버깅
bash scripts/setup-local-launchd.sh --status
tail -f ~/Library/Logs/agentic-harness/c-yeonwoo-palette.{out,err}
launchctl start com.agentic-harness.c-yeonwoo-palette     # 즉시 1회 실행
```

---

## 사용 — hermes 트랙 (API 모드)

```bash
# 1. agentic-harness venv (위 1번과 동일)

# 2. .env 에 LLM 키 + PAT
cat >> .env <<EOF
HARNESS_MODE=hermes
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
EXECUTOR_MODEL=gpt-5.3-codex
REVIEWER_MODEL=gpt-5.3-codex
PALETTE_AGENT_PAT=ghp_...
EOF

# 3. 프로젝트의 .hermes/ 셋업 (Hermes cron 등록)
cd ~/dev-private/palette
bash .hermes/bootstrap.sh         # ah: 라벨 4개 생성
bash .hermes/cron-setup.sh        # Hermes cron 등록
~/.local/bin/hermes gateway install

# 4. agenda 던지기 — Hermes skill 통해서
hermes chat -s palette-po "친구 카운트 +1 안 됨 — 수정"

# 이후 흐름은 local 트랙과 동일 (다른 점은 LLM 호출이 API 로 빠짐)
```

자세한 docs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/DECISIONS/ADR-011-local-claude-runner.md`](docs/DECISIONS/ADR-011-local-claude-runner.md), [`docs/PROJECT_SCAFFOLD.md`](docs/PROJECT_SCAFFOLD.md)

---

## 다른 프로젝트로 import

1. 그 프로젝트 안에 `.hermes/` scaffold 작성 (`templates/.hermes/` 또는 `scripts/scaffold-init.sh` 사용)
2. `.hermes/skills/<project>-po/SKILL.md` — 자연어 → issue 변환 (project-specific SoT 참조)
3. `.hermes/scripts/<project>-{pm,executor,reviewer}.sh` — `~/dev-private/agentic-harness/orchestrator` 의 함수 호출 wrapper
4. `bash .hermes/bootstrap.sh` 로 라벨 생성
5. `bash .hermes/cron-setup.sh` 로 Hermes cron 등록

→ 같은 platform, project 별 SoT / skill / cron 만 갈아끼움.

---

## License

MIT
