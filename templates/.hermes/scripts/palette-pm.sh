#!/usr/bin/env bash
# ============================================================================
# Project PM — 라벨 큐 스캔 + 1건 dispatch (Hermes cron --no-agent 로 호출됨)
# ============================================================================
# 사용:
#   hermes cron create '5m' --no-agent \
#     --script <project>-pm.sh \
#     --workdir /Users/ys.choi/dev-private/<project> \
#     --name <project>-pm
#
# 동작:
#   1) ah:in-progress 카운트 (WIP)
#   2) ah:needs-review PR 우선 → reviewer 호출
#   3) 없으면 ah:needs-execution issue → executor 호출
#   4) 락 부여 (ah:in-progress) + 큐 라벨 제거
#   5) executor/reviewer 호출 시 그 안에서 락 해제
#
# 종료 코드: 0 (idle) | 0 (dispatched, success) | 1 (worker crashed)
# 출력: 1줄 요약 (Hermes cron 이 delivery target 으로 전달)
# ============================================================================
set -euo pipefail

# cron에서는 스크립트가 ~/.hermes/scripts 에서 실행될 수 있으므로 PWD/스크립트경로만으로 추론하면 오동작.
# 우선순위: PROJECT_NAME env > PROJECT_REPO_CWD basename > script filename slug > PWD basename
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
WIP_CAP="${PROJECT_WIP_CAP:-5}"

# .env 자동 로드 (cron 으로 호출 시 shell env 비어있음 — PAT 등)
AH_DIR="${AGENTIC_HARNESS_DIR:-$HOME/dev-private/agentic-harness}"
REPO_CWD="${PROJECT_REPO_CWD:-$HOME/dev-private/${PROJECT_SLUG}}"
for env_file in "$AH_DIR/.env" "$REPO_CWD/.env"; do
  if [ -f "$env_file" ]; then
    set -a; . "$env_file"; set +a
  fi
done

# PAT 별칭 정규화 — <PROJECT>_AGENT_PAT > PROJECT_AGENT_PAT > PROJECT_AGENT_PAT > GH_TOKEN > GITHUB_TOKEN.
# .env 의 빈 줄 (GH_TOKEN=) 이 inherited 환경을 덮어쓸 수 있어 명시적 우선순위.
TOKEN_VAR_NAME="${PROJECT_UPPER}_AGENT_PAT"
PROJECT_PAT="${!TOKEN_VAR_NAME:-${PROJECT_AGENT_PAT:-${PROJECT_AGENT_PAT:-}}}"
if [ -n "${PROJECT_PAT:-}" ]; then
  export GH_TOKEN="$PROJECT_PAT"
  export GITHUB_TOKEN="$PROJECT_PAT"
fi

# 빈 env 가드 — Anthropic SDK 가 ANTHROPIC_BASE_URL="" 을 자동 사용해 connection error 유발
[ -z "${ANTHROPIC_BASE_URL:-}" ] && unset ANTHROPIC_BASE_URL || true
[ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] && unset ANTHROPIC_AUTH_TOKEN || true

# 0. WIP 가드 ---------------------------------------------------------------
WIP_ISS=$(gh issue list --repo "$REPO" --label "ah:in-progress" --state open --json number | jq length)
WIP_PR=$(gh pr list --repo "$REPO" --label "ah:in-progress" --state open --json number | jq length)
WIP=$((WIP_ISS + WIP_PR))
if [ "$WIP" -ge "$WIP_CAP" ]; then
  echo "WIP=$WIP >= $WIP_CAP, skip"; exit 0
fi

# 1. 큐 스캔 (우선순위) ------------------------------------------------------
#   1) PR ah:needs-review            (reviewer)
#   2) PR ah:needs-execution         (executor amend — reviewer 가 changes 요구)
#   3) issue ah:needs-execution      (executor new — 신규 task)
TARGET_KIND=""; TARGET_NUM=""; WORKER=""; AMEND=""

PR_REVIEW=$(gh pr list --repo "$REPO" --label "ah:needs-review" --state open \
        --json number,createdAt --jq 'sort_by(.createdAt) | .[0].number // empty')
PR_AMEND=$(gh pr list --repo "$REPO" --label "ah:needs-execution" --state open \
        --json number,createdAt --jq 'sort_by(.createdAt) | .[0].number // empty')
ISS_NEW=$(gh issue list --repo "$REPO" --label "ah:needs-execution" --state open \
        --json number,createdAt --jq 'sort_by(.createdAt) | .[0].number // empty')

if [ -n "$PR_REVIEW" ]; then
  TARGET_KIND=pr;    TARGET_NUM="$PR_REVIEW"; WORKER="${PROJECT_SLUG}-reviewer"
elif [ -n "$PR_AMEND" ]; then
  TARGET_KIND=pr;    TARGET_NUM="$PR_AMEND";  WORKER="${PROJECT_SLUG}-executor"; AMEND="1"
elif [ -n "$ISS_NEW" ]; then
  TARGET_KIND=issue; TARGET_NUM="$ISS_NEW";   WORKER="${PROJECT_SLUG}-executor"
else
  exit 0   # idle — Hermes cron --no-agent 는 empty stdout 이면 silent
fi

# 2. 락 부여 ----------------------------------------------------------------
SUBCMD="$([ "$TARGET_KIND" = "pr" ] && echo pr || echo issue)"
# 큐 라벨: PR review → needs-review, PR amend / issue new → needs-execution
if [ "$WORKER" = "${PROJECT_SLUG}-reviewer" ]; then
  QUEUE_LABEL="ah:needs-review"
else
  QUEUE_LABEL="ah:needs-execution"
fi

gh "$SUBCMD" edit "$TARGET_NUM" --repo "$REPO" \
  --remove-label "$QUEUE_LABEL" --add-label "ah:in-progress" >/dev/null
gh "$SUBCMD" comment "$TARGET_NUM" --repo "$REPO" \
  --body "🤖 ${PROJECT_SLUG}-pm · $(date -Iseconds) · dispatch → $WORKER$([ -n "$AMEND" ] && echo ' (amend)')" >/dev/null

# 3. 워커 호출 (foreground — 한 tick 안에 처리) ------------------------------
echo "dispatched #$TARGET_NUM → $WORKER$([ -n "$AMEND" ] && echo ' (amend)') (WIP $((WIP+1))/$WIP_CAP)"

if [ "$WORKER" = "${PROJECT_SLUG}-executor" ]; then
  # AMEND 면 두 번째 인자 'pr' 로, 신규면 'issue'
  KIND="$([ -n "$AMEND" ] && echo pr || echo issue)"
  exec bash "$SCRIPT_DIR/${PROJECT_SLUG}-executor.sh" "$TARGET_NUM" "$KIND"
else
  exec bash "$SCRIPT_DIR/${PROJECT_SLUG}-reviewer.sh" "$TARGET_NUM"
fi
