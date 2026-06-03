"""Runner 추상화 — agents.run_developer 가 어떤 실행 전략을 쓸지 분리.

두 모드:
  - local (default, ADR-011): LocalClaudeRunner — claude -p 헤드리스
  - hermes: ApiRunner — 기존 LLM ReAct + plan apply 흐름

HARNESS_MODE / DEVELOPER_MODE / REVIEWER_MODE env 로 분기.
(EXECUTOR_MODE 도 back-compat 으로 인식 — ADR-012 rename)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol


@dataclass
class ExecutionContext:
    """Runner.execute() input — 공통 컨텍스트."""
    repo: str                            # bucketplace/palette
    repo_cwd: Path                       # local repo path
    role: str                            # "executor" | "executor-amend"
    sot_prompt: str                      # SoT text (system prompt cache 영역)
    user_prompt: str                     # task-specific user message
    issue_or_pr_number: int              # issue (신규) or PR number (amend)
    existing_branch: Optional[str] = None  # amend 모드일 때
    title_hint: str = ""                 # branch_name fallback 용 (issue/PR title)
    model: Optional[str] = None          # provider-specific model override


@dataclass
class ExecutionResult:
    """Runner.execute() output — agents.py 가 이걸로 PR 생성/라벨 처리.

    ok=True 인데 branch 가 None 이면 "변경 없음 — 의도적 skip" 으로 처리.
    """
    ok: bool
    summary: str = ""
    branch: Optional[str] = None
    base: Optional[str] = None
    files_changed: int = 0
    commits_applied: int = 0

    # PR metadata — ApiRunner 는 LLM plan 에서, LocalClaudeRunner 는 JSON 결과에서
    pr_title: Optional[str] = None
    pr_body: Optional[str] = None
    verification: Optional[str] = None

    # 비용 / observability
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""

    # 에러 처리
    error: Optional[str] = None
    error_kind: Optional[str] = None     # "edit_apply" | "no_plan" | "crashed" | "no_changes"
    edit_apply_info: Optional[dict] = field(default_factory=lambda: None)

    # 디버깅
    tool_trace: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)


class Runner(Protocol):
    async def execute(self, ctx: ExecutionContext) -> ExecutionResult: ...


def resolve_local_model(role: str = "developer") -> str:
    """역할별 default model 결정 (local 모드 전용).

    lookup 우선순위:
      1. LOCAL_<ROLE>_MODEL — 예: LOCAL_PO_MODEL=sonnet
      2. LOCAL_CLAUDE_MODEL — 전체 global override
      3. role 별 hardcoded default:
         - PO / REVIEWER / CRITIQUE → 'sonnet' (가벼운 분석/판정 작업)
         - DEVELOPER / DEVELOPER-AMEND → 'opus'   (코드 작성/수정 — 품질 우선)

    환경변수 이름은 ROLE 을 대문자 + `-` → `_` 변환:
      developer       → LOCAL_DEVELOPER_MODEL
      developer-amend → LOCAL_DEVELOPER_AMEND_MODEL  (없으면 LOCAL_DEVELOPER_MODEL fallback)
      reviewer        → LOCAL_REVIEWER_MODEL
      po              → LOCAL_PO_MODEL
      critique        → LOCAL_CRITIQUE_MODEL
    """
    role_norm = role.upper().replace("-", "_")
    primary = os.environ.get(f"LOCAL_{role_norm}_MODEL")
    if primary:
        return primary.strip()

    # amend 는 developer 의 fallback 도 시도
    if role == "developer-amend":
        dev = os.environ.get("LOCAL_DEVELOPER_MODEL")
        if dev:
            return dev.strip()

    glb = os.environ.get("LOCAL_CLAUDE_MODEL")
    if glb:
        return glb.strip()

    defaults = {
        "po": "sonnet",
        "developer": "opus",          # 신규 PR — 큰 작업, 품질 우선
        "developer-amend": "sonnet",  # amend — 작은 수정 (코멘트/테스트/리뷰 반영) 위주, opus 의 1/5 비용
        "reviewer": "sonnet",
        "critique": "sonnet",
        "executor": "opus",           # back-compat alias
        "executor-amend": "sonnet",
    }
    return defaults.get(role, "sonnet")


def resolve_mode(role: str = "developer") -> str:
    """역할별 우선순위로 mode 결정.

    DEVELOPER_MODE / REVIEWER_MODE / PO_MODE / CRITIQUE_MODE > HARNESS_MODE > 'local'
    (EXECUTOR_MODE 도 DEVELOPER_MODE 별칭으로 인식 — ADR-012 rename back-compat)

    Default 가 'local' 인 이유 (ADR-011): PO/developer/reviewer/critique 모두 로컬
    클코가 의도, hermes 는 명시적 opt-in.
    """
    role_to_envs = {
        "developer": ["DEVELOPER_MODE", "EXECUTOR_MODE"],
        "developer-amend": ["DEVELOPER_MODE", "EXECUTOR_MODE"],
        "reviewer": ["REVIEWER_MODE"],
        "po": ["PO_MODE"],
        "critique": ["CRITIQUE_MODE"],
        # back-compat 별칭
        "executor": ["DEVELOPER_MODE", "EXECUTOR_MODE"],
        "executor-amend": ["DEVELOPER_MODE", "EXECUTOR_MODE"],
    }
    for env_name in role_to_envs.get(role, []):
        v = os.environ.get(env_name)
        if v:
            return v.strip().lower()
    return (os.environ.get("HARNESS_MODE") or "local").strip().lower()


def get_runner(role: str = "executor", mode: Optional[str] = None) -> Runner:
    """Runner 인스턴스 반환. mode 미지정 시 env 로 분기."""
    m = (mode or resolve_mode(role)).strip().lower()
    if m == "local":
        from orchestrator.runners.local_claude import LocalClaudeRunner
        return LocalClaudeRunner()
    if m in ("hermes", "api"):
        from orchestrator.runners.api import ApiRunner
        return ApiRunner()
    raise RuntimeError(
        f"unknown mode: {m!r} — HARNESS_MODE 는 'local' 또는 'hermes' 만 허용"
    )
