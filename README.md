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

---

## 모듈

```
agentic-harness/
├── orchestrator/
│   ├── llm.py              # (Phase B) Anthropic / OpenAI dual-support
│   ├── claude.py           # (현재) Anthropic SDK + cost 추적 + caching
│   ├── code_tools.py       # ReAct tools (read_file / list_files / search_text / submit_plan)
│   ├── git_apply.py        # worktree + edit action + push (EditApplyError)
│   ├── gh.py               # gh CLI wrapper
│   ├── source_of_truth.py  # SoT 4-tier 발견 + to_prompt()
│   ├── lock.py             # ah:in-progress + assignee 락
│   ├── poller.py           # 30초 폴링 (Hermes cron 으로 대체 가능)
│   └── agents.py           # run_code_executor / run_code_executor_amend / run_code_reviewer
├── agents/
│   ├── code-executor.md    # executor prompt
│   └── code-reviewer.md    # reviewer prompt
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
| **B** | LLM provider 추상화 (`llm.py`) — Anthropic / OpenAI dual-support | 🔲 진행 예정 |
| **C** | task queue 명시 + 병렬 executor (N건 동시 dispatch) | 🔲 |
| **D** | `issue-finder` agent — 코드베이스 자동 scan (TODO/FIXME/ADR drift) → issue 생성 | 🔲 |
| **E** | `ssot manager` — merge 후 SoT (ARCHITECTURE/GLOSSARY/ADR) 자동 갱신 PR | 🔲 |
| **F** | human signal — telegram/slack alarm (awaiting-human 시) | 🔲 |

---

## 사용 (palette 첫 use case)

```bash
# 1. 환경
export PALETTE_AGENT_PAT=ghp_...
export ANTHROPIC_API_KEY=sk-ant-...    # 또는 OPENAI_API_KEY (Phase B 후)

# 2. agent-harness venv
cd ~/dev-private/agentic-harness
python3.12 -m venv .venv && .venv/bin/pip install -e .

# 3. 프로젝트의 .hermes/ 셋업 (palette 의 경우)
cd ~/dev-private/palette
bash .hermes/bootstrap.sh         # ah: 라벨 4개 생성
bash .hermes/cron-setup.sh        # Hermes cron 등록

# 4. Hermes gateway (5분 자동 활성)
~/.local/bin/hermes gateway install

# 5. agenda 던지기
hermes chat -s palette-po "친구 카운트 +1 안 됨 — 수정"
# 또는 shell wrapper
palette-po "친구 카운트 +1 안 됨 — 수정"

# 6. 사이클 자동 진행
#   PO → issue → executor → PR → reviewer → awaiting-human
# 사람은 PR review 후 merge 만
```

자세한 docs: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/PROJECT_SCAFFOLD.md`](docs/PROJECT_SCAFFOLD.md)

---

## 다른 프로젝트로 import

1. 그 프로젝트 안에 `.hermes/` scaffold 작성 (`palette/.hermes/` 참고)
2. `.hermes/skills/<project>-po/SKILL.md` — 자연어 → issue 변환 (project-specific SoT 참조)
3. `.hermes/scripts/<project>-{pm,executor,reviewer}.sh` — `~/dev-private/agentic-harness/orchestrator` 의 함수 호출 wrapper
4. `bash .hermes/bootstrap.sh` 로 라벨 생성
5. `bash .hermes/cron-setup.sh` 로 Hermes cron 등록

→ 같은 platform, project 별 SoT / skill / cron 만 갈아끼움.

---

## License

MIT
