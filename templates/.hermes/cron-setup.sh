#!/usr/bin/env bash
# ============================================================================
# Hermes cron 등록 — <project>-pm 1개 등록 (executor/reviewer 는 pm 이 직접 호출)
# ============================================================================
set -euo pipefail

HERMES="${HERMES_BIN:-$HOME/.local/bin/hermes}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKDIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_NAME="${PROJECT_NAME:-$(basename "$WORKDIR")}"         # e.g. palette
PROJECT_SLUG="${PROJECT_NAME// /-}"

if [ ! -x "$HERMES" ]; then
  echo "❌ hermes 바이너리 없음: $HERMES" >&2
  echo "   설치: curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash" >&2
  exit 1
fi

# [1] ~/.hermes/scripts/ 에 cron 스크립트 복사 (Hermes 가 symlink path traversal 차단)
HERMES_SCRIPT_DIR="$HOME/.hermes/scripts"
mkdir -p "$HERMES_SCRIPT_DIR"

for role in pm executor reviewer; do
  src="$SCRIPT_DIR/scripts/${PROJECT_SLUG}-${role}.sh"
  dst="$HERMES_SCRIPT_DIR/${PROJECT_SLUG}-${role}.sh"
  if [ ! -f "$src" ]; then
    echo "❌ script 없음: $src" >&2
    echo "   (프로젝트 prefix(${PROJECT_SLUG})와 파일명이 일치하는지 확인)" >&2
    exit 1
  fi
  cp "$src" "$dst"
  chmod +x "$dst"
  echo "  script: $src → $dst"
done

# [2] ~/.hermes/skills/devops/ 에 <project>-po SKILL.md 복사
HERMES_SKILLS_DIR="$HOME/.hermes/skills/devops/${PROJECT_SLUG}-po"
mkdir -p "$HERMES_SKILLS_DIR"
SKILL_SRC="$SCRIPT_DIR/skills/${PROJECT_SLUG}-po/SKILL.md"
if [ ! -f "$SKILL_SRC" ]; then
  echo "❌ skill 없음: $SKILL_SRC" >&2
  echo "   (프로젝트 prefix(${PROJECT_SLUG})와 skills 디렉터리명이 일치하는지 확인)" >&2
  exit 1
fi
cp "$SKILL_SRC" "$HERMES_SKILLS_DIR/SKILL.md"
echo "  skill:  $SKILL_SRC → $HERMES_SKILLS_DIR/SKILL.md"
echo "         (확인: hermes skills list | grep ${PROJECT_SLUG}-po)"

JOB_NAME="${PROJECT_SLUG}-pm"
SCRIPT_NAME="${PROJECT_SLUG}-pm.sh"

# 기존 job 있으면 pass (idempotent)
if "$HERMES" cron list 2>&1 | grep -q "$JOB_NAME"; then
  echo "  $JOB_NAME cron 이미 등록됨 (skip — 재등록하려면 hermes cron remove $JOB_NAME 먼저)"
  exit 0
fi

# Hermes schedule syntax: 'every 5m' (반복). 단순 '5m' 은 once-only (Repeat 0/1).
"$HERMES" cron create 'every 5m' \
  --no-agent \
  --script "$SCRIPT_NAME" \
  --workdir "$WORKDIR" \
  --name "$JOB_NAME" \
  --deliver local

echo ""
echo "✅ $JOB_NAME cron 등록 완료. 확인:"
echo "   $HERMES cron list"
echo "   $HERMES cron run $JOB_NAME    # 다음 scheduler tick 에 1회 실행"
echo ""
echo "⚠ Hermes gateway 미설치 시 cron 은 manual trigger 만 — 자동 5분 주기는 안 돕니다."
echo "  $HERMES status  로 gateway 상태 확인."
echo "  설치:  $HERMES gateway install   # launchd 서비스로 등록"
