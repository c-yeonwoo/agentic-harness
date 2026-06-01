# Project Onboarding Guide (Reusable agent-harness)

이 문서는 palette 외 다른 프로젝트에 agent-harness 를 공통 적용하는 최소 절차다.
핵심 원칙은 다음:

- 코어 에이전트(executor/reviewer/lock/gh/sot)는 공통 재사용
- 프로젝트별로 바뀌는 것은 얇은 `.hermes/` 래퍼 + PO skill + repo/SSOT 경로

## 1) 전제

- target repo: `<owner>/<repo>`
- 로컬 경로: `~/dev-private/<project>`
- agent-harness 경로: `~/dev-private/agentic-harness`
- GitHub write 토큰: `<PROJECT>_AGENT_PAT`
  - 예: palette 는 `PALETTE_AGENT_PAT`

## 2) 프로젝트 디렉터리에 .hermes scaffold 생성

필수 파일 구조:

```
<project>/.hermes/
├── agent-context.md
├── skills/
│   └── <project>-po/
│       └── SKILL.md
├── scripts/
│   ├── <project>-pm.sh
│   ├── <project>-executor.sh
│   └── <project>-reviewer.sh
├── aliases.sh
├── bootstrap.sh
└── cron-setup.sh
```

## 3) 스크립트에서 프로젝트 변수만 교체

아래 변수 3개는 프로젝트별로 반드시 분리:

- `REPO="${PROJECT_REPO:-<owner>/<repo>}"`
- `REPO_CWD="${PROJECT_REPO_CWD:-$HOME/dev-private/<project>}"`
- `PROJECT_AGENT_PAT` 우선순위 주입

토큰 우선순위 권장(중요):

```
if [ -n "${PROJECT_AGENT_PAT:-}" ]; then
  export GH_TOKEN="$PROJECT_AGENT_PAT"
  export GITHUB_TOKEN="$PROJECT_AGENT_PAT"
fi
```

이렇게 하면 로컬 gh active account 와 무관하게, 해당 프로젝트 토큰으로 동작한다.

## 4) PM 큐 규약 (공통)

라벨 state machine 은 모든 프로젝트 동일하게 유지:

- `ah:needs-execution`
- `ah:needs-review`
- `ah:awaiting-human`
- `ah:in-progress`

PM 우선순위:
1. PR `ah:needs-review`
2. PR `ah:needs-execution` (amend)
3. Issue `ah:needs-execution` (new)

## 5) Reviewer verdict 규약 (공통)

- approve/concerns_noted -> `ah:awaiting-human`
- request_changes -> **PR 유지** + `ah:needs-execution` (amend 큐)

구버전의 "PR close + issue 재트리거"는 사용하지 않는다.

## 6) PO 전략: 2가지

### A. 프로젝트별 PO skill (권장)

`<project>-po` skill 에서:
- 해당 프로젝트 SSOT (`CLAUDE.md`, `docs/*`, `docs/DECISIONS/*`) 읽기
- 자연어 task -> structured issue 템플릿 변환
- `gh issue create --label ah:needs-execution`

### B. Generic PO 없이 바로 큐에 넣기

최소 진입:

```
ah add-task --repo <owner>/<repo> "<task description>"
```

가능은 하지만, scope/AC/힌트 품질이 낮아 executor 효율이 떨어질 수 있다.

## 7) Cron 등록 패턴

각 프로젝트는 cron job 이름을 분리:
- `<project>-pm`

예시:

```
hermes cron create 'every 5m' \
  --no-agent \
  --script <project>-pm.sh \
  --workdir /Users/$USER/dev-private/<project> \
  --name <project>-pm \
  --deliver local
```

## 8) SSOT 강제 방법

코어는 `repo_cwd` 기준으로 자동 수집:
- CLAUDE chain
- docs/*.md
- docs/DECISIONS/*.md
- .hermes/agent-context.md
- recent PR/issues

프로젝트가 달라도, `repo_cwd`와 repo명만 바르면 동일 로직 재사용 가능.

## 9) 첫 검증 체크리스트

1. `gh label list -R <owner>/<repo> | grep '^ah:'`
2. test issue 생성 (`ah:needs-execution`)
3. `hermes cron run <project>-pm`
4. PR 생성 + `ah:needs-review` 확인
5. reviewer 후 label 전이 확인

## 10) 추천 네이밍 규칙

- skill: `<project>-po`
- script: `<project>-pm.sh`, `<project>-executor.sh`, `<project>-reviewer.sh`
- cron: `<project>-pm`
- env var:
  - `<PROJECT>_REPO`
  - `<PROJECT>_REPO_CWD`
  - `<PROJECT>_AGENT_PAT`

이 규칙을 지키면 여러 프로젝트를 같은 머신에서 충돌 없이 동시에 운영 가능.
