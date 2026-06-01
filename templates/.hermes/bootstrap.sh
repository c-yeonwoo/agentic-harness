#!/usr/bin/env bash
# Palette × Hermes 첫 셋업 — 1회 실행
# 라벨 컨벤션: ah: prefix 4개 (agentic-harness 와 호환)
set -euo pipefail

REPO="${PALETTE_REPO:-c-yeonwoo/palette}"

echo "=== [1/4] gh CLI 인증 확인 ==="
if ! gh auth status >/dev/null 2>&1; then
  echo "  ❌ gh auth 안 됨 — gh auth login 먼저"
  exit 1
fi

echo ""
echo "=== [2/4] ah: 라벨 4개 생성 (이미 있으면 무시) ==="
gh label create "ah:needs-execution" --repo "$REPO" --color 0E8A16 \
  --description "executor 큐 — issue: 코드 변경 + PR 생성 대기" 2>/dev/null || echo "  ah:needs-execution (skip)"
gh label create "ah:needs-review"    --repo "$REPO" --color FBCA04 \
  --description "reviewer 큐 — PR: 리뷰 + verdict 대기" 2>/dev/null || echo "  ah:needs-review (skip)"
gh label create "ah:awaiting-human"  --repo "$REPO" --color D93F0B \
  --description "사람 결정 대기 — merge / 라벨 떼서 다음 흐름" 2>/dev/null || echo "  ah:awaiting-human (skip)"
gh label create "ah:in-progress"     --repo "$REPO" --color 5319E7 \
  --description "워커 점유 락 — 자동 부여/해제 (사람 수동 X)" 2>/dev/null || echo "  ah:in-progress (skip)"

echo ""
echo "=== [3/4] worktree 디렉토리 + agentic-harness 점검 ==="
mkdir -p /tmp/palette-agent-workspaces
echo "  /tmp/palette-agent-workspaces 준비 완료"

if [ ! -d "$HOME/dev/agentic-harness" ]; then
  echo "  ⚠ ~/dev/agentic-harness 없음 — Hermes executor/reviewer 가 그 코드를 import 함"
  echo "    git clone https://github.com/c-yeonwoo/agentic-harness ~/dev/agentic-harness"
  echo "    cd ~/dev/agentic-harness && python3.12 -m venv .venv && .venv/bin/pip install -e ."
else
  echo "  ✅ ~/dev/agentic-harness 존재"
fi

echo ""
echo "=== [4/4] 환경변수 점검 ==="
MISSING=0
for v in PALETTE_AGENT_PAT; do
  if [ -z "${!v:-}" ]; then
    echo "  ❌ $v 미설정 — export $v=ghp_..."
    MISSING=1
  else
    echo "  ✅ $v 설정됨 (${!v:0:6}...)"
  fi
done
# Anthropic auth — token 또는 api_key 둘 중 하나만 있으면 OK
if [ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]; then
  echo "  ✅ ANTHROPIC_AUTH_TOKEN 설정됨 (${ANTHROPIC_AUTH_TOKEN:0:10}...) [OAuth path]"
elif [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "  ✅ ANTHROPIC_API_KEY 설정됨 (${ANTHROPIC_API_KEY:0:10}...) [api_key path]"
else
  echo "  ❌ ANTHROPIC_AUTH_TOKEN 또는 ANTHROPIC_API_KEY 둘 중 하나는 필요"
  MISSING=1
fi
[ $MISSING -eq 1 ] && exit 1

echo ""
echo "셋업 완료. 다음 단계:"
echo "  1) Hermes cron 등록     — bash .hermes/cron-setup.sh"
echo "  2) shell alias 활성화   — echo 'source ~/dev-private/palette/.hermes/aliases.sh' >> ~/.zshrc && source ~/.zshrc"
echo "  3) 동작 확인           — palette-help"
echo "  4) test task           — palette-po '<자연어 task>'  # 또는 Claude Code 에서 /add-task"
echo "  5) 즉시 1 tick         — palette-tick"
echo "  6) 큐 상태             — palette-queue"
