# Project × Hermes Agent — 자동 작업 하네스 (현재 예시: palette)

> palette 레포 위에 도는 라벨 기반 agentic coding 하네스.
> Hermes cron 이 5분마다 watchdog script 실행 → 라벨 큐 보고 executor/reviewer dispatch.
> ReAct + edit + PR 은 `~/dev/agentic-harness/` 의 검증된 코드 재사용.

---

## 아키텍처

```
[사람] 자연어 task — 두 트랙 중 하나로 호출
   ├─ Claude Code 채팅:   /add-task <설명>          (palette/.claude/commands/add-task.md)
   └─ shell (Hermes):     palette-po "<설명>"       (alias for hermes chat -s palette-po)
              ↓ (두 트랙 모두 동일 SOT 보고 동일 issue body 생성)
   gh issue create + ah:needs-execution      # PO 가 호출
              ↓
        Hermes cron (5분, --no-agent)
              ↓
   palette-pm.sh                              # 라벨 큐 스캔 + 1건 dispatch
   ├─ ah:needs-review PR 우선                # PR 마무리가 새로 시작보다 가치 큼
   │  → palette-reviewer.sh <pr_num>
   │     └─ python -c "agents.run_code_reviewer(...)"
   │        ├─ verdict=approve|concerns_noted → ah:awaiting-human
   │        └─ verdict=request_changes       → PR 유지 + ah:needs-execution (amend 큐)
   │
   └─ ah:needs-execution issue 큐
      → palette-executor.sh <issue_num>
         └─ python -c "agents.run_code_executor(...)"
            ├─ ReAct (list_files/read_file/search_text/edit/submit_plan)
            ├─ worktree + commit + push
            └─ gh pr create --label "ah:needs-review" (+ Closes #N)
```

## 구성 파일

| 경로 | 역할 |
|------|------|
| `../.claude/commands/add-task.md` | **PO (Claude Code 트랙)** — `/add-task <자연어>` slash command |
| `skills/palette-po/SKILL.md`      | **PO (Hermes 트랙)** — Hermes skill, frontmatter + instructions |
| `aliases.sh`                      | shell wrapper — `palette-po`, `palette-tick`, `palette-queue` 함수 |
| `scripts/palette-pm.sh` | 5분마다 호출. 라벨 폴링 + dispatch + 락 부여 |
| `scripts/palette-executor.sh` | `agents.run_code_executor()` 실행 wrapper |
| `scripts/palette-reviewer.sh` | `agents.run_code_reviewer()` 실행 wrapper |
| `cron-setup.sh` | Hermes cron 에 palette-pm 1개 등록 (idempotent) |
| `bootstrap.sh` | 라벨 4개 생성 + venv 점검 + env 검증 |
| `agent-context.md` | 워커 prompt 진입 시 자동 주입할 핵심 컨텍스트 |
| `agents.legacy/` | Phase A 초안 yaml — reference 만, 사용 X |

## 라벨 컨벤션 (4개, `ah:` prefix)

| 라벨 | 의미 |
|------|------|
| `ah:needs-execution` | executor 큐 (issue) |
| `ah:needs-review`    | reviewer 큐 (PR) |
| `ah:awaiting-human`  | 사람 결정 대기 — merge / 라벨 떼서 다음 흐름 |
| `ah:in-progress`     | 워커 점유 락 — 자동 부여/해제 (사람 수동 X) |

## 핵심 설계 결정 (변경 금지) — agentic-harness 가 이미 모두 준수

1. **라벨 4개** (`ah:` prefix) — DB/Redis 안 씀, 라벨이 곧 state.
2. **SOT 4-tier** — `~/.claude/CLAUDE.md` → `~/dev-private/CLAUDE.md` → `palette/CLAUDE.md` → `palette/docs/*` + recent PRs/issues.
3. **ReAct + edit action 필수** — 500줄+ 파일에 `replace` 금지. `old_str` 1회 매칭, 실패 시 whitespace fuzzy fallback.
4. **plan 은 PR description 에만** — issue 엔 "PR 생성됨" 링크 + cost 한 줄.
5. **`Closes #N` 자동 추가** — merge 시 issue 자동 close.
6. **reviewer request_changes → PR 유지 + ah:needs-execution(amend)** — 무한 루프 cap 없음, 사람이 PR 라벨 떼서 멈춤.
7. **`claude-haiku-4-5` 기본**.
8. **OAuth token** (`sk-ant-oat...`) → `auth_token` (api_key 로 보내면 401).

## Hermes ↔ agentic-harness 책임 분리

| 책임 | Hermes | agentic-harness |
|------|--------|-----------------|
| cron 스케줄 / 트리거 | ✅ `cron create '5m' --no-agent` | — |
| process lifecycle / 로그 | ✅ | — |
| LLM tool-loop (ReAct) | — | ✅ `claude.call_with_tools` |
| `submit_plan` JSON + plan 정규화 | — | ✅ |
| git worktree + edit + fuzzy fallback | — | ✅ `git_apply.py` |
| GitHub label / PR / Closes #N | — | ✅ `gh.py` + `agents.py` |
| SOT 4-tier 컨텍스트 주입 | — | ✅ `source_of_truth.py` |
| cost 추적 / model 선택 | — | ✅ `claude.py` |

→ Hermes 가 죽어도 `cd ~/dev/agentic-harness && ah run --repo c-yeonwoo/palette` 로 백업.

---

## 첫 셋업 (Phase B)

```bash
# 1. 환경변수
export PALETTE_AGENT_PAT="ghp_..."            # repo + write + pr 권한
export ANTHROPIC_AUTH_TOKEN="sk-ant-oat..."   # 또는 ANTHROPIC_API_KEY

# 2. agentic-harness venv 점검 (없으면 setup)
if [ ! -d ~/dev/agentic-harness/.venv ]; then
  cd ~/dev/agentic-harness
  python3.12 -m venv .venv && .venv/bin/pip install -e .
fi

# 3. 라벨 4개 + 환경 점검
bash .hermes/bootstrap.sh

# 4. Hermes cron 등록 (palette-pm 만)
bash .hermes/cron-setup.sh

# 5. cron 동작 확인
~/.local/bin/hermes cron list                 # palette-pm 보이는지
~/.local/bin/hermes cron run palette-pm       # 즉시 1회 실행 — idle 이면 silent
```

## Phase C — 한 사이클 검증

```text
# 0. shell alias 활성화 (1회 — ~/.zshrc 에 한 줄 추가)
   echo 'source ~/dev-private/palette/.hermes/aliases.sh' >> ~/.zshrc
   source ~/.zshrc

# 1. test task — 두 트랙 중 한 가지
#    A) Claude Code 채팅:  /add-task frontend/README.md 의 develpoment → development 오타 수정
#    B) shell (Hermes):    PROJECT_NAME=palette palette-po "frontend/README.md 의 develpoment → development 오타"
#  → 둘 다 PO 가 scope/AC/affected files 채워서 issue 생성 + ah:needs-execution 라벨

# 2. 즉시 1 tick — pm → executor → PR 생성까지 대기 (Hermes gateway 없어도 OK)
palette-tick                                 # (= ~/.local/bin/hermes cron run palette-pm)

# 3. PR 확인
gh pr list -R c-yeonwoo/palette --label "ah:needs-review"

# 4. 다시 1 tick — pm → reviewer → ah:awaiting-human
~/.local/bin/hermes cron run palette-pm

# 5. 사람 검토 후 merge — PR 의 `Closes #N` 으로 issue 자동 close
```

성공 기준: 사람 개입 0회로 1~4 자동 진행. 5 (merge) 만 사람.

---

## WIP / 안전 정책

- 동시 `ah:in-progress` ≤ **5** (pm 가드, `PALETTE_WIP_CAP` 로 override)
- stale 30분 → `ah:awaiting-human` 으로 escalate (TODO)
- 재진입 cap 없음 — 사람이 라벨 떼서 멈춤

## 트러블슈팅

| 증상 | 원인 / 대응 |
|------|-------------|
| `hermes cron list` 에 palette-pm 없음 | `bash .hermes/cron-setup.sh` 재실행 |
| `Failed to create job: Script path escapes...` | Hermes 가 symlink path-traversal 차단. `cron-setup.sh` 가 cp 로 복사 (sync 필요 시 재실행) |
| `5m` 으로 등록했는데 "Repeat 0/1" | Hermes 의 once-mode. `'every 5m'` 으로 등록 필요 (cron-setup.sh 가 이미 처리) |
| `Gateway is not running — cron jobs will NOT fire` | `~/.local/bin/hermes gateway install` 로 launchd 등록. 미설치 시 `hermes cron run palette-pm` 으로 manual trigger 만 가능 |
| gh API 403/404 (라벨/PR) | `gh auth status` 가 c-yeonwoo 또는 PAT 쓰는 계정인지 확인. nick603 은 palette 에 read-only |
| Python `ModuleNotFoundError: orchestrator` | `~/dev/agentic-harness/.venv` 미설치 또는 `pip install -e .` 누락 |
| LLM 401 | `ANTHROPIC_AUTH_TOKEN` 와 `ANTHROPIC_API_KEY` 헷갈림. `sk-ant-oat...` 는 auth_token 으로 |
| PR fuzzy edit 실패 | `_fuzzy_replace` 도 실패 시 issue 에 ❌ 댓글 부착됨. 사람이 수정 또는 라벨 다시 부착 |
| executor 무한 루프 | reviewer 가 매번 request_changes → 사람이 issue/PR 라벨 모두 떼서 멈춤 |

## 보고 항목 (Phase D)

- Hermes 가 실제 지원하는 기능 ↔ 가정 사이 gap (이미 정리됨: yaml→cron 모델)
- 자체 daemon (launchd) 대비 비용 / latency / 안정성
- 첫 PoC PR URL + cost 한 줄
- 본 프로젝트 확대 권장 여부
