#!/usr/bin/env bash
# ============================================================================
# Local mode smoke test — claude -p 헤드리스로 end-to-end 1건 처리.
#
# 검증 흐름:
#   1. 지정한 repo 에 인공 issue 생성 (`ah:needs-execution` 라벨)
#   2. HARNESS_MODE=local ah run --once 호출
#   3. PR 생성 / 라벨 전이 확인 (ah:needs-review)
#   4. 사람이 PR / issue 정리
#
# 사용:
#   bash scripts/smoke-local-mode.sh <repo>           # 예: c-yeonwoo/palette
#   bash scripts/smoke-local-mode.sh <repo> <cwd>     # repo 의 로컬 경로 명시
#
# 환경 (.env 에서 자동 로드되거나 export 해둘 것):
#   - GH_TOKEN 또는 <PROJECT>_AGENT_PAT — issue/PR 생성 권한
#   - CLAUDE_BIN — 선택, default 'claude'
#
# 주의:
#   - 진짜 GitHub issue + PR 이 만들어짐. 테스트 후 직접 close/delete.
#   - 현재 user OAuth 세션 사용 (token 비용 0). claude code 안에서 이 스크립트
#     실행 시 OAuth child 전달 실패로 401 가능 — 일반 터미널에서 직접 실행 권장.
# ============================================================================
set -euo pipefail

REPO="${1:?usage: smoke-local-mode.sh <owner/repo> [repo_cwd]}"
REPO_CWD="${2:-$HOME/dev-private/$(basename "$REPO")}"
AH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$REPO_CWD/.git" ]; then
    echo "❌ $REPO_CWD 는 git repo 가 아님 (basename 추론 실패). 두 번째 인자로 명시:" >&2
    echo "   bash scripts/smoke-local-mode.sh $REPO /path/to/local/clone" >&2
    exit 2
fi

# .env 자동 로드
if [ -f "$AH_DIR/.env" ]; then
    set -a; . "$AH_DIR/.env"; set +a
fi

# PAT 정규화 — <REPO_BASE>_AGENT_PAT > GH_TOKEN
REPO_BASE_UPPER="$(basename "$REPO" | tr '[:lower:]-' '[:upper:]_')"
PAT_VAR="${REPO_BASE_UPPER}_AGENT_PAT"
if [ -n "${!PAT_VAR:-}" ]; then
    export GH_TOKEN="${!PAT_VAR}"
fi

if [ -z "${GH_TOKEN:-}" ]; then
    echo "❌ GH_TOKEN 또는 ${PAT_VAR} 미설정 — .env 확인" >&2
    exit 2
fi

# ── 1. 인공 issue 생성 ──────────────────────────────────────────────────────
TS=$(date +%s)
TITLE="[smoke] local-mode test ${TS}"
BODY=$(cat <<EOF
agentic-harness 의 \`HARNESS_MODE=local\` 모드 smoke test 용 자동 생성 issue.

## 작업 내용

repo 루트의 \`SMOKE_TEST_LOG.md\` 파일에 다음 줄을 **append** 하세요:

\`\`\`
- ${TS}: local-mode smoke test 통과 (executor 가 자동 추가)
\`\`\`

파일이 없으면 새로 생성. 한 줄만 추가하면 됨.

## 검증

- 위 파일이 변경됐는지 확인
- \`pr_body\` 에 \`## 개요\` / \`## 검증\` 섹션 있는지

---
_smoke-local-mode.sh 가 만든 임시 issue. 작업 끝나면 close + 브랜치 정리._
EOF
)

echo "▶ issue 생성 중 ($REPO) …"
ISSUE_URL=$(gh issue create --repo "$REPO" \
    --title "$TITLE" \
    --body "$BODY" \
    --label "ah:needs-execution")
ISSUE_NUM=$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')
echo "  ✓ #$ISSUE_NUM : $ISSUE_URL"

# ── 2. local 모드로 1회 실행 ────────────────────────────────────────────────
echo ""
echo "▶ ah run --once --repo $REPO --cwd $REPO_CWD --mode local"
echo "  - executor: claude -p (opus 4.7) → worktree 편집 → commit/push"
echo "  - PR body : /pr-description 스킬 추가 spawn"
echo "  - reviewer: (PR 가 ah:needs-review 상태로 가면 다음 tick 에 처리)"
echo "  - 1회 spawn timeout: ${LOCAL_CLAUDE_TIMEOUT_SEC:-1800}s"
echo ""

export HARNESS_MODE=local
export LOCAL_CLAUDE_MODEL="${LOCAL_CLAUDE_MODEL:-opus}"
"$AH_DIR/.venv/bin/ah" run --once --repo "$REPO" --cwd "$REPO_CWD" --mode local

# ── 3. 결과 확인 ────────────────────────────────────────────────────────────
echo ""
echo "▶ issue #$ISSUE_NUM 최종 상태 확인 …"
gh issue view "$ISSUE_NUM" --repo "$REPO" --json state,labels,url \
    --jq '{state, labels: [.labels[].name], url}'

echo ""
echo "▶ 최근 PR (이 issue 와 연결된) …"
gh pr list --repo "$REPO" --label ah:needs-review --limit 5 \
    --json number,title,labels,url \
    --jq '.[] | select(.title | contains("'$ISSUE_NUM'") or contains("smoke"))'

echo ""
echo "✓ smoke test 완료. issue #$ISSUE_NUM 및 생성된 PR 은 수동으로 close/정리하세요."
