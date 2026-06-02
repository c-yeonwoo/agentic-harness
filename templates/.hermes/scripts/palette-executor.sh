#!/usr/bin/env bash
# ============================================================================
# Project Developer — issue/PR 번호 받아 agentic-harness developer 호출
# (옛 이름 'executor'. 파일명은 hermes cron 호환 위해 유지. ADR-012 rename.)
# ============================================================================
# 호출:
#   bash <project>-executor.sh <issue_number>
#
# 의존:
#   - ~/dev/agentic-harness/.venv (pip install -e .)
#   - PROJECT_AGENT_PAT (GH token), ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY
#
# 책임 분리:
#   ReAct + edit + worktree + push + PR + Closes #N + cost 추적 모두
#   agentic-harness 의 agents.run_developer() 가 처리.
#   이 script 는 venv activate + entrypoint 호출 + 락 해제만.
# ============================================================================
set -euo pipefail

TARGET_N="${1:?need number}"
KIND="${2:-issue}"          # 'issue' (default, 신규) | 'pr' (amend mode)
# cron에서는 스크립트가 ~/.hermes/scripts 에서 실행될 수 있으므로 PWD/스크립트경로만으로 추론하면 오동작.
# 우선순위: PROJECT_NAME env > PROJECT_REPO_CWD basename > script filename slug > PWD basename
SCRIPT_BASE="$(basename "${BASH_SOURCE[0]}")"
INFER_SLUG=""
case "$SCRIPT_BASE" in
  *-pm.sh) INFER_SLUG="${SCRIPT_BASE%-pm.sh}" ;;
  *-executor.sh) INFER_SLUG="${SCRIPT_BASE%-executor.sh}" ;;
  *-reviewer.sh) INFER_SLUG="${SCRIPT_BASE%-reviewer.sh}" ;;
esac
PROJECT_NAME="${PROJECT_NAME:-$(basename "${PROJECT_REPO_CWD:-${INFER_SLUG:-$PWD}}")}"   # e.g. palette
PROJECT_SLUG="${PROJECT_NAME// /-}"
PROJECT_UPPER="$(printf '%s' "$PROJECT_NAME" | tr '[:lower:]-' '[:upper:]_')"
REPO="${PROJECT_REPO:-c-yeonwoo/${PROJECT_SLUG}}"
AH_DIR="${AGENTIC_HARNESS_DIR:-$HOME/dev-private/agentic-harness}"
REPO_CWD="${PROJECT_REPO_CWD:-$HOME/dev-private/${PROJECT_SLUG}}"

# .env 자동 로드 (cron 으로 호출 시 shell env 비어있음 — 토큰을 .env 에 두고 source)
# 우선순위: agentic-harness/.env (ANTHROPIC_*, GH_TOKEN 등) → palette/.env (있으면)
for env_file in "$AH_DIR/.env" "$REPO_CWD/.env"; do
  if [ -f "$env_file" ]; then
    set -a; . "$env_file"; set +a
  fi
done

# PAT 별칭 정규화 — <PROJECT>_AGENT_PAT > PROJECT_AGENT_PAT > PROJECT_AGENT_PAT > GH_TOKEN > GITHUB_TOKEN
TOKEN_VAR_NAME="${PROJECT_UPPER}_AGENT_PAT"
PROJECT_PAT="${!TOKEN_VAR_NAME:-${PROJECT_AGENT_PAT:-${PROJECT_AGENT_PAT:-}}}"
if [ -n "${PROJECT_PAT:-}" ]; then
  export GH_TOKEN="$PROJECT_PAT"
  export GITHUB_TOKEN="$PROJECT_PAT"
fi

# Harness mode 분기 ----------------------------------------------------------
# HARNESS_MODE=local  (default, ADR-011) — claude -p 헤드리스 (user OAuth).
#                                          OPENAI_API_KEY 불필요. CLAUDE_BIN 만.
# HARNESS_MODE=hermes                    — raw LLM API (OPENAI/ANTHROPIC). 디버깅용.
# .env 에 HARNESS_MODE 설정하면 위 set -a; . "$env_file" 단계에서 자동 export 됨.
export HARNESS_MODE="${HARNESS_MODE:-local}"

# Local mode 모델 — 역할별 default (LOCAL_<ROLE>_MODEL 또는 LOCAL_CLAUDE_MODEL override)
# 강제 export 안 함 — 미설정 시 코드의 role별 default (PO=sonnet, DEVELOPER=opus, ...) 사용

# Hermes mode 만 의미 있는 변수 (HARNESS_MODE=hermes 일 때)
# ADR-012 rename: DEVELOPER_MODEL 우선, EXECUTOR_MODEL 은 back-compat
export LLM_PROVIDER="${LLM_PROVIDER:-openai}"
export DEVELOPER_MODEL="${DEVELOPER_MODEL:-${EXECUTOR_MODEL:-gpt-5.3-codex}}"
export EXECUTOR_MODEL="${EXECUTOR_MODEL:-$DEVELOPER_MODEL}"

# 빈 env 가드 — Anthropic fallback 사용 시만 의미
[ -z "${ANTHROPIC_BASE_URL:-}" ] && unset ANTHROPIC_BASE_URL || true
[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] && unset ANTHROPIC_AUTH_TOKEN || true

# agentic-harness venv 점검
if [ ! -d "$AH_DIR/.venv" ]; then
  echo "❌ $AH_DIR/.venv 없음 — bootstrap 미수행" >&2
  gh "$KIND" edit "$TARGET_N" --repo "$REPO" \
    --remove-label "ah:in-progress" --add-label "ah:awaiting-human" || true
  gh "$KIND" comment "$TARGET_N" --repo "$REPO" \
    --body "❌ ${PROJECT_SLUG}-executor: agentic-harness venv 미설치 — 사람 확인" || true
  exit 1
fi

# entrypoint 실행 -----------------------------------------------------------
# python child process 가 받을 수 있도록 명시적 export
export AH_DIR REPO REPO_CWD TARGET_N KIND

set +e
"$AH_DIR/.venv/bin/python" - <<'PYEOF'
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['AH_DIR'])
from orchestrator import agents, gh, source_of_truth as sot_mod

async def main():
    repo = os.environ['REPO']
    target_n = int(os.environ['TARGET_N'])
    kind = os.environ.get('KIND', 'issue')
    sot = await sot_mod.discover(Path(os.environ['REPO_CWD']))
    bot_user = await gh.whoami(repo)
    repo_cwd = Path(os.environ['REPO_CWD'])

    if kind == 'pr':
        # amend mode — PR 의 branch 에 추가 commit
        pr = await gh.get_pr(repo, target_n)
        ok = await agents.run_developer_amend(
            repo=repo, pr=pr, sot=sot, bot_user=bot_user, repo_cwd=repo_cwd,
        )
    else:
        # 신규 mode — issue → 새 PR
        issue = await gh.get_issue(repo, target_n)
        ok = await agents.run_developer(
            repo=repo, issue=issue, sot=sot, bot_user=bot_user, repo_cwd=repo_cwd,
        )
    sys.exit(0 if ok else 1)

asyncio.run(main())
PYEOF
EXIT=$?
set -e

# 락 해제 (성공 시 agents.run_developer* 가 라벨 swap 처리 — 락만) ---
gh "$KIND" edit "$TARGET_N" --repo "$REPO" --remove-label "ah:in-progress" 2>/dev/null || true
if [ "$EXIT" -ne 0 ]; then
  gh "$KIND" edit "$TARGET_N" --repo "$REPO" --add-label "ah:awaiting-human" || true
fi

exit "$EXIT"
