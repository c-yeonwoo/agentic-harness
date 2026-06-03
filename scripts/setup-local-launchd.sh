#!/usr/bin/env bash
# ============================================================================
# Local mode launchd installer — macOS 의 LaunchAgent 로 `ah run --once` 주기 실행.
#
# Local mode 는 Hermes cron / bash 디스패처 / .hermes 디렉토리 모두 불필요.
# 이 스크립트가 ~/Library/LaunchAgents/com.agentic-harness.<slug>.plist 를
# 만들고 launchctl 로 등록한다.
#
# 사용:
#   bash scripts/setup-local-launchd.sh <repo> [interval_seconds] [repo_cwd]
#     예: bash scripts/setup-local-launchd.sh c-yeonwoo/palette 300
#
#   bash scripts/setup-local-launchd.sh --uninstall <repo>
#     해당 LaunchAgent 제거.
#
#   bash scripts/setup-local-launchd.sh --status [repo]
#     설치된 agentic-harness LaunchAgent 목록 / 상태.
#
#   bash scripts/setup-local-launchd.sh --weekly <repo> [repo_cwd]
#     주간 SoT batch (ADR-017) — 일요일 02:00 에 `ah sot-batch` 실행.
#     별도 plist (com.agentic-harness.<slug>.weekly.plist).
#
# Default interval: 300 초 (5분).
# 로그: ~/Library/Logs/agentic-harness/<slug>.{out,err}
# ============================================================================
set -euo pipefail

AH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAUNCHD_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/agentic-harness"

mkdir -p "$LAUNCHD_DIR" "$LOG_DIR"

# ── 모드 분기 ──────────────────────────────────────────────────────────────
MODE="install"
case "${1:-}" in
    --uninstall|-u)
        MODE="uninstall"
        shift
        ;;
    --status|-s)
        MODE="status"
        shift
        ;;
    --weekly|-w)
        MODE="weekly"
        shift
        ;;
    --help|-h|"")
        sed -n '2,25p' "${BASH_SOURCE[0]}"
        exit 0
        ;;
esac

# ── status (repo 인자 선택) ───────────────────────────────────────────────
if [ "$MODE" = "status" ]; then
    echo "▶ 설치된 agentic-harness LaunchAgent:"
    launchctl list 2>/dev/null | grep agentic-harness || echo "  (없음)"
    echo ""
    echo "▶ plist 파일:"
    ls -la "$LAUNCHD_DIR"/com.agentic-harness.*.plist 2>/dev/null || echo "  (없음)"
    echo ""
    echo "▶ 최근 로그:"
    ls -lt "$LOG_DIR"/*.out 2>/dev/null | head -5 || echo "  (없음)"
    exit 0
fi

# install / uninstall 은 repo 필수
REPO="${1:?repo 인자 필요 — 예: c-yeonwoo/palette}"
SLUG="$(echo "$REPO" | tr '/' '-' | tr '[:upper:]' '[:lower:]')"
LABEL="com.agentic-harness.${SLUG}"
PLIST="$LAUNCHD_DIR/${LABEL}.plist"

# ── uninstall ─────────────────────────────────────────────────────────────
if [ "$MODE" = "uninstall" ]; then
    if [ ! -f "$PLIST" ]; then
        echo "❌ $PLIST 없음 — 이미 제거됨" >&2
        exit 0
    fi
    launchctl unload -w "$PLIST" 2>/dev/null || true
    rm "$PLIST"
    echo "✓ unload + 삭제: $PLIST"
    # weekly plist 도 함께 정리
    WEEKLY_PLIST="$LAUNCHD_DIR/${LABEL}.weekly.plist"
    if [ -f "$WEEKLY_PLIST" ]; then
        launchctl unload -w "$WEEKLY_PLIST" 2>/dev/null || true
        rm "$WEEKLY_PLIST"
        echo "✓ weekly LaunchAgent 도 제거: $WEEKLY_PLIST"
    fi
    exit 0
fi

# ── weekly (SoT batch — ADR-017) ──────────────────────────────────────────
if [ "$MODE" = "weekly" ]; then
    REPO_CWD="${2:-$HOME/dev-private/$(basename "$REPO")}"
    AH_BIN="${AGENTIC_HARNESS_DIR:-$HOME/dev-private/agentic-harness}/.venv/bin/ah"
    [ -x "$AH_BIN" ] || { echo "❌ $AH_BIN 없음" >&2; exit 2; }

    WEEKLY_PLIST="$LAUNCHD_DIR/${LABEL}.weekly.plist"
    WEEKLY_LABEL="${LABEL}.weekly"
    CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo /opt/homebrew/bin/claude)}"
    LAUNCHD_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$(dirname "$CLAUDE_BIN")"

    cat > "$WEEKLY_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${WEEKLY_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${AH_BIN}</string>
        <string>sot-batch</string>
        <string>--repo</string>
        <string>${REPO}</string>
        <string>--cwd</string>
        <string>${REPO_CWD}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>0</integer>   <!-- 일요일 -->
        <key>Hour</key>
        <integer>2</integer>   <!-- 02:00 -->
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>RunAtLoad</key>
    <false/>
    <key>WorkingDirectory</key>
    <string>${REPO_CWD}</string>
    <key>AbandonProcessGroup</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${SLUG}.weekly.out</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${SLUG}.weekly.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key><string>${LAUNCHD_PATH}</string>
        <key>HOME</key><string>${HOME}</string>
        <key>HARNESS_MODE</key><string>local</string>
        <key>CLAUDE_BIN</key><string>${CLAUDE_BIN}</string>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>10</integer>
</dict>
</plist>
EOF

    launchctl unload -w "$WEEKLY_PLIST" 2>/dev/null || true
    launchctl load -w "$WEEKLY_PLIST"
    echo ""
    echo "✓ Weekly SoT batch LaunchAgent 등록됨"
    echo "  - label : ${WEEKLY_LABEL}"
    echo "  - plist : ${WEEKLY_PLIST}"
    echo "  - 주기  : 매주 일요일 02:00"
    echo "  - 동작  : ah sot-batch --repo ${REPO} (threshold 5)"
    echo "  - 로그  : ${LOG_DIR}/${SLUG}.weekly.{out,err}"
    echo ""
    echo "▶ 수동 1회 트리거:"
    echo "    launchctl start ${WEEKLY_LABEL}"
    echo ""
    echo "▶ 또는 즉시 처리:"
    echo "    .venv/bin/ah sot-batch --repo ${REPO} --force"
    exit 0
fi

# ── install ───────────────────────────────────────────────────────────────
INTERVAL_SEC="${2:-300}"
REPO_CWD="${3:-$HOME/dev-private/$(basename "$REPO")}"

if [ ! -d "$REPO_CWD/.git" ]; then
    echo "❌ $REPO_CWD 는 git repo 아님 — 세 번째 인자로 명시:" >&2
    echo "   bash scripts/setup-local-launchd.sh $REPO $INTERVAL_SEC /path/to/clone" >&2
    exit 2
fi

AH_BIN="$AH_DIR/.venv/bin/ah"
if [ ! -x "$AH_BIN" ]; then
    echo "❌ $AH_BIN 없음 — agentic-harness venv 설치 안 됨" >&2
    echo "   cd $AH_DIR && python3.12 -m venv .venv && .venv/bin/pip install -e ." >&2
    exit 2
fi

# claude CLI 위치 — launchd 는 빈 PATH 시작이라 명시 필요
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || echo /opt/homebrew/bin/claude)}"
LAUNCHD_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$(dirname "$CLAUDE_BIN")"

# StartCalendarInterval 생성 — StartInterval 보다 sleep/wake 후 신뢰성 ↑
# (절대 시간 기준 매 N 분 — 깬 직후 다음 슬롯에 catch-up 자동)
INTERVAL_MIN=$((INTERVAL_SEC / 60))
if [ "$INTERVAL_MIN" -lt 1 ]; then
    echo "⚠️  interval $INTERVAL_SEC < 60초 — 1분으로 보정 (StartCalendarInterval 분 단위 한계)" >&2
    INTERVAL_MIN=1
fi
if [ $((60 % INTERVAL_MIN)) -ne 0 ] && [ "$INTERVAL_MIN" -lt 60 ]; then
    echo "⚠️  60분이 ${INTERVAL_MIN}분으로 안 나눠짐 — 시간 경계 근처에선 간격이 틀어질 수 있음" >&2
fi

CAL_ENTRIES=""
if [ "$INTERVAL_MIN" -ge 60 ]; then
    # 1시간 이상 — 매 시간 한 번 + 분 = 0
    CAL_ENTRIES="
        <dict><key>Minute</key><integer>0</integer></dict>"
else
    for ((m=0; m<60; m+=INTERVAL_MIN)); do
        CAL_ENTRIES="${CAL_ENTRIES}
        <dict><key>Minute</key><integer>${m}</integer></dict>"
    done
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${AH_BIN}</string>
        <string>run</string>
        <string>--once</string>
        <string>--repo</string>
        <string>${REPO}</string>
        <string>--cwd</string>
        <string>${REPO_CWD}</string>
        <string>--mode</string>
        <string>local</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>${CAL_ENTRIES}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>${REPO_CWD}</string>
    <key>AbandonProcessGroup</key>
    <true/>
    <key>ExitTimeOut</key>
    <integer>3600</integer>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/${SLUG}.out</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/${SLUG}.err</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${LAUNCHD_PATH}</string>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>HARNESS_MODE</key>
        <string>local</string>
        <key>CLAUDE_BIN</key>
        <string>${CLAUDE_BIN}</string>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>Nice</key>
    <integer>5</integer>
</dict>
</plist>
EOF

# 기존 등록 있으면 unload 먼저 (이미 같은 label 등록되어 있을 수 있음)
launchctl unload -w "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo ""
echo "✓ LaunchAgent 등록됨"
echo "  - label   : ${LABEL}"
echo "  - plist   : ${PLIST}"
echo "  - interval: ${INTERVAL_SEC}s"
echo "  - repo    : ${REPO} (${REPO_CWD})"
echo "  - logs    : ${LOG_DIR}/${SLUG}.{out,err}"
echo ""
echo "▶ 수동 1회 실행 (즉시 트리거):"
echo "    launchctl start ${LABEL}"
echo ""
echo "▶ 상태 확인:"
echo "    launchctl list | grep agentic-harness"
echo "    tail -f ${LOG_DIR}/${SLUG}.out"
echo ""
echo "▶ 제거:"
echo "    bash $0 --uninstall ${REPO}"
