# ============================================================================
# Project × Hermes shell aliases (project-agnostic)
# ============================================================================
# 사용:
#   source ~/dev-private/<project>/.hermes/aliases.sh
#   <project>-po "<자연어 task>"
# ============================================================================

HERMES_BIN="${HERMES_BIN:-$HOME/.local/bin/hermes}"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")}"   # e.g. palette
PROJECT_SLUG="${PROJECT_NAME// /-}"
PROJECT_PO_SKILL="${PROJECT_PO_SKILL:-${PROJECT_SLUG}-po}"
PROJECT_REPO="${PROJECT_REPO:-${PALETTE_REPO:-c-yeonwoo/${PROJECT_SLUG}}}"
PROJECT_PM_JOB="${PROJECT_PM_JOB:-${PROJECT_SLUG}-pm}"

# 동적 함수 생성: <project>-po / <project>-tick / <project>-queue / <project>-help
eval "
${PROJECT_SLUG}-po() {
  if [ \$# -eq 0 ]; then
    echo \"usage: ${PROJECT_SLUG}-po '<자연어 task 설명>'\"
    return 1
  fi
  if [ ! -x \"$HERMES_BIN\" ]; then
    echo \"❌ hermes 바이너리 없음: $HERMES_BIN\" >&2; return 1
  fi
  \"$HERMES_BIN\" chat \\
    -s \"$PROJECT_PO_SKILL\" \\
    --quiet \\
    --max-turns 8 \\
    --yolo \\
    -q \"\$*\"
}

${PROJECT_SLUG}-tick() {
  if [ ! -x \"$HERMES_BIN\" ]; then
    echo \"❌ hermes 바이너리 없음: $HERMES_BIN\" >&2; return 1
  fi
  \"$HERMES_BIN\" cron run \"$PROJECT_PM_JOB\"
}

${PROJECT_SLUG}-queue() {
  local REPO=\"$PROJECT_REPO\"
  echo \"── ${PROJECT_SLUG} queue ──\"
  echo \"  needs-execution: \$(gh issue list -R \"\$REPO\" --label ah:needs-execution --state open --json number --jq length)\"
  echo \"  needs-review:    \$(gh pr list   -R \"\$REPO\" --label ah:needs-review     --state open --json number --jq length)\"
  echo \"  in-progress:     \$((\$(gh issue list -R \"\$REPO\" --label ah:in-progress --state open --json number --jq length) + \$(gh pr list -R \"\$REPO\" --label ah:in-progress --state open --json number --jq length)))\"
  echo \"  awaiting-human:  \$((\$(gh issue list -R \"\$REPO\" --label ah:awaiting-human --state open --json number --jq length) + \$(gh pr list -R \"\$REPO\" --label ah:awaiting-human --state open --json number --jq length)))\"
}

${PROJECT_SLUG}-help() {
  cat <<EOF
${PROJECT_SLUG} × Hermes 명령:
  ${PROJECT_SLUG}-po \"<자연어>\"   issue 생성 (PO skill: $PROJECT_PO_SKILL)
  ${PROJECT_SLUG}-tick              cron 즉시 1회 실행
  ${PROJECT_SLUG}-queue             큐 상태 한 줄 요약
  ${PROJECT_SLUG}-help              이 도움말

cron 자동 실행 활성화 (1회):
  $HERMES_BIN gateway install
EOF
}
"

