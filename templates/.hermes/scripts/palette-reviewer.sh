#!/usr/bin/env bash
# ============================================================================
# Project Reviewer — PR 번호 받아 agentic-harness.run_code_reviewer 호출
# ============================================================================
# 호출:
#   bash <project>-reviewer.sh <pr_number>
#
# 책임 분리:
#   diff + linked issue + verdict 판정 + 라벨 전이 모두
#   agentic-harness 의 agents.run_code_reviewer() 가 처리.
#   request_changes 시 PR 유지 + ah:needs-execution 라벨로 amend 큐 재진입 자동.
# ============================================================================
set -euo pipefail

PR_N="${1:?need PR number}"
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

# .env 자동 로드 (cron 으로 호출 시 shell env 비어있음)
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

# provider/model 기본값: codex 통일
export LLM_PROVIDER="${LLM_PROVIDER:-openai}"
export REVIEWER_MODEL="${REVIEWER_MODEL:-gpt-5.3-codex}"

# 빈 env 가드 — Anthropic fallback 사용 시만 의미
[ -z "${ANTHROPIC_BASE_URL:-}" ] && unset ANTHROPIC_BASE_URL || true
[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] && unset ANTHROPIC_AUTH_TOKEN || true

if [ ! -d "$AH_DIR/.venv" ]; then
  echo "❌ $AH_DIR/.venv 없음 — bootstrap 미수행" >&2
  gh pr edit "$PR_N" --repo "$REPO" \
    --remove-label "ah:in-progress" --add-label "ah:awaiting-human" || true
  gh pr comment "$PR_N" --repo "$REPO" \
    --body "❌ ${PROJECT_SLUG}-reviewer: agentic-harness venv 미설치 — 사람 확인" || true
  exit 1
fi

export AH_DIR REPO REPO_CWD PR_N

set +e
"$AH_DIR/.venv/bin/python" - <<'PYEOF'
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, os.environ['AH_DIR'])
from orchestrator import agents, gh, source_of_truth as sot_mod

async def main():
    repo = os.environ['REPO']
    pr_n = int(os.environ['PR_N'])
    pr = await gh.get_pr(repo, pr_n)
    sot = await sot_mod.discover(Path(os.environ['REPO_CWD']))
    bot_user = await gh.whoami()
    ok = await agents.run_code_reviewer(
        repo=repo, pr=pr, sot=sot, bot_user=bot_user,
    )
    sys.exit(0 if ok else 1)

asyncio.run(main())
PYEOF
EXIT=$?
set -e

# 락 해제 (verdict 별 라벨은 run_code_reviewer 가 부여함) ---------------------
gh pr edit "$PR_N" --repo "$REPO" --remove-label "ah:in-progress" 2>/dev/null || true
if [ "$EXIT" -ne 0 ]; then
  gh pr edit "$PR_N" --repo "$REPO" --add-label "ah:awaiting-human" || true
fi

exit "$EXIT"
