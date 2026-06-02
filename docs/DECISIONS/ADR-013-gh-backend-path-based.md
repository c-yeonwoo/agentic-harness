# ADR-013 — gh.py backend: path-based 분기 (HTTP / gh CLI)

> 결정일: 2026-06-02
> 상태: Accepted

## 배경

기존 `gh.py` 는 `gh` CLI subprocess 만 사용. 사용자의 환경:

- `~/dev/<work>` — bucketplace / ohouse 등 work repo. `gh` CLI 이 work 계정 (ohouse)
  으로 인증되어 있음
- `~/dev-private/<personal>` — c-yeonwoo 개인 repo. `gh` CLI 인증 안 됨 / 잘못된 계정

`palette-executor.sh` 가 `GH_TOKEN=$PALETTE_AGENT_PAT` 로 환경변수 override 하는
방식도 있지만, `ah` CLI 직접 호출이나 launchd cron 경로에서 누락되기 쉬움.

## 결정

`orchestrator/gh.py` 에 두 backend (HTTP + CLI) 공존. **cwd path** 로 자동 분기:

| cwd 위치 | backend | 인증 |
|---------|---------|------|
| `~/dev-private/<repo>/...` | **HTTP** (httpx + GitHub REST v3) | `{REPO_BASE_UPPER}_AGENT_PAT` 또는 `GH_TOKEN` |
| `~/dev/<repo>/...` | **CLI** (`gh` subprocess) | gh CLI 인증 |
| 그 외 | PAT 있으면 HTTP, 없으면 CLI | (조건부) |

**env override** — `GH_BACKEND=http|cli` 로 강제 가능.

`Path.cwd().resolve().relative_to(home/'dev-private')` 패턴으로 검사 — 단순 substring
매칭 (`'dev-private' in parts`) 대신 명확한 prefix check. `~/src/dev-private-x/` 같은
false positive 차단.

### PAT 우선순위 (HTTP backend)

1. `{REPO_BASE_UPPER}_AGENT_PAT` (예: `c-yeonwoo/palette` → `PALETTE_AGENT_PAT`)
2. `GH_TOKEN`
3. `GITHUB_TOKEN`

대시는 언더스코어로 변환 (`ohouse/comm-store-pl` → `COMM_STORE_PL_AGENT_PAT`).

### launchd 와의 정합성

`scripts/setup-local-launchd.sh` 가 만드는 plist 는 `WorkingDirectory=<repo_cwd>`.
- c-yeonwoo/palette 의 launchd → cwd=`~/dev-private/palette` → HTTP 자동 선택
- bucketplace/lore 의 launchd → cwd=`~/dev/lore` → CLI 자동 선택

별도 설정 / wrapper 없음.

## 호출부 영향

- `Issue` / `PullRequest` dataclass 그대로
- 모든 public 함수 시그니처 그대로
- `whoami()` 만 옵션 `repo` 인자 받게 확장 (per-repo PAT 사용 위해) — back-compat
  유지 (`whoami()` 도 동작)
- `agents.py` / `poller.py` / 템플릿 스크립트 — 수정 X (단 `whoami(repo)` 로 갱신
  권장)

## 트레이드오프

### 장점
- dev-private 의 c-yeonwoo 개인 repo 가 ohouse work 인증과 섞이지 않음
- launchd cron 에서도 동작 (gh CLI 의 keychain 의존성 제거)
- 기존 hermes 트랙 (gh CLI 기반) 영향 X — 자동 fallback
- 명확한 backend 선택 — env override + path-based

### 단점
- 코드량 증가 — 모든 public 함수가 if/else 로 두 path
- 두 backend 의 응답 shape 약간 다름 (gh CLI 의 `headRefName` vs REST 의 `head.ref`)
  → 두 parser 유지 (`_parse_*_http` / `_parse_*_cli`)
- HTTP backend 는 `--label` 필터 PR 조회 시 `/issues` → detail GET 2-step (gh CLI 는
  `gh pr list --label X` 한 번에) → 약간 느림. 보통 limit=30 이라 무시 가능

## 폐기 결정

- ❌ **gh CLI 단독 + `GH_TOKEN` env override** — palette-executor.sh 는 동작하지만
  ah CLI 직접 호출 / launchd 진입점에서 누락. cwd path 자동 분기가 robust.
- ❌ **HTTP backend 단독** — work repo 에서 gh CLI 의 풍부한 인증 (SSO / GH App) 등을
  포기. 두 backend 공존이 가장 호환적.
- ❌ **repo owner 기반 분기** (c-yeonwoo → HTTP) — 이름 hardcoding. path-based 가
  유연.
