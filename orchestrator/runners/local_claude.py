"""LocalClaudeRunner — `claude -p` 헤드리스 spawn.

흐름:
  1. prepare_worktree (신규/amend)
  2. claude -p --output-format json --permission-mode bypassPermissions \
            --add-dir <wt> --system-prompt <role+sot> "<user prompt>" 실행 (cwd=wt)
     → claude 가 Read/Edit/Write/Bash 로 worktree 안에서 직접 편집
  3. claude 가 마지막에 출력한 JSON (summary / pr_title / pr_body / files_changed / branch_name) 파싱
  4. stage_commit_push_all — worktree 의 모든 변경을 1 commit + push
  5. cleanup_worktree
  6. ExecutionResult 반환

특징:
  - plan JSON 폐기. 실제 변경은 worktree 의 git diff 로 확인.
  - claude 가 gh/git 직접 호출 못 하게 --disallowedTools 로 막음 (harness 단독 책임)
  - 사용자 OAuth/subscription 사용 → token 비용 0 (claude -p 가 자동)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

from orchestrator.git_apply import (
    cleanup_worktree,
    detect_worktree_changes,
    prepare_worktree,
    stage_commit_push_all,
)
from orchestrator.runners import ExecutionContext, ExecutionResult

log = structlog.get_logger()


CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")

# claude 가 직접 호출하면 안 되는 것 — harness 가 단독 책임.
# disallowed 보다 allowed-list 가 더 안전하지만, 클코의 Bash 패턴 매칭이 enough.
DEFAULT_DISALLOWED_TOOLS = [
    "Bash(git push:*)",
    "Bash(git push)",
    "Bash(git commit:*)",
    "Bash(git commit)",
    "Bash(gh pr:*)",
    "Bash(gh issue:*)",
]

# PR description 생성용 (commit 후 두 번째 spawn)
PR_DESCRIPTION_DISALLOWED_TOOLS = [
    "Bash(git push:*)",
    "Bash(git push)",
    "Bash(git commit:*)",
    "Bash(git commit)",
    "Bash(gh pr:*)",
    "Bash(gh issue:*)",
    "Edit",
    "Write",
]

# pr-description skill 호출 명령. 빈 값이면 skip (executor JSON 의 pr_body 사용).
PR_DESCRIPTION_SKILL = os.environ.get("LOCAL_PR_DESCRIPTION_SKILL", "/pr-description")


def _load_agent_prompt() -> str:
    """로컬 모드 전용 system prompt — agents/developer-local.md (구: code-executor-local.md)."""
    agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
    for name in ("developer-local.md", "code-executor-local.md"):
        p = agents_dir / name
        if p.exists():
            return p.read_text(encoding="utf-8")
    # fallback — 파일 없으면 최소 instruction 만
    return (
        "You are the agentic-harness developer running in local Claude Code mode.\n"
        "Edit files directly in the current working directory using Read/Edit/Write.\n"
        "Do NOT run git/gh commands — the harness handles commit/push/PR.\n"
        "When done, output a single JSON object on the final line with fields: "
        "summary, files_changed, verification, branch_name, pr_title, pr_body, "
        "scope_warning."
    )


# claude -p --output-format json 의 마지막 assistant message 에서 JSON 객체 추출
_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}\s*$")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```")


def _extract_result_json(text: str) -> Optional[dict]:
    """헤드리스 응답 마지막에서 JSON 추출. 펜스/꼬리 휴리스틱 둘 다 시도."""
    text = (text or "").strip()
    if not text:
        return None
    # 1) ```json ... ``` 블록
    fences = _JSON_FENCE_RE.findall(text)
    for blob in reversed(fences):
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    # 2) 텍스트 끝부분의 { ... }
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    # 3) 전체가 JSON 일 경우
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    return None


def _slugify(text: str, max_len: int = 40) -> str:
    """branch_name fallback — title 에서 영문/숫자/하이픈만."""
    s = re.sub(r"[^a-zA-Z0-9\s-]", "", text).strip().lower()
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s[:max_len].strip("-") or "task"


def _default_branch_name(role: str, issue_number: int, title_hint: str) -> str:
    """LLM 이 branch_name 안 줬을 때 fallback."""
    prefix = "feat" if role == "executor" else "fix"
    slug = _slugify(title_hint)
    return f"{prefix}/{slug}-{issue_number}"


def _sanitized_subprocess_env() -> dict:
    """claude -p 자식 프로세스용 env — Anthropic / Claude Code 컨텍스트 제거.

    문제 1: 사내 ANTHROPIC_BASE_URL 등이 살아있으면 OAuth credential 이 거기로
            가서 401.
    문제 2: 부모 Claude Code 세션이 자식에 `CLAUDE_CODE_SDK_HAS_OAUTH_REFRESH=1`
            같은 env 를 심어둠. 자식 claude 가 그걸 보고 "부모가 OAuth 토큰
            refresh 해줄 거야" 라고 IPC 기다리다 fail → 401.
            ah 가 일반 Python 프로세스라 그 IPC 못 함.

    해결: CLAUDE_CODE_OAUTH_TOKEN (사용자 명시 override) 만 빼고 CLAUDE_CODE_*
          전부 strip. 자식 claude 는 ~/.claude/.credentials.json 직접 읽음.
    """
    sub_env = dict(os.environ)

    # 1. Anthropic / OpenAI 인증 관련 — gateway / API key
    for k in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_SMALL_FAST_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DISABLE_PROMPT_CACHING",
    ):
        sub_env.pop(k, None)

    # 2. Claude Code 부모 세션 컨텍스트 — CLAUDE_CODE_OAUTH_TOKEN 빼고 전부 strip
    keep = {"CLAUDE_CODE_OAUTH_TOKEN"}
    for k in list(sub_env.keys()):
        if k.startswith("CLAUDE_CODE_") and k not in keep:
            sub_env.pop(k, None)

    return sub_env


async def _spawn_claude(
    *,
    cwd: Path,
    system_prompt: str,
    user_prompt: str,
    timeout_sec: int,
    extra_disallowed: Optional[list[str]] = None,
    model: Optional[str] = None,
) -> tuple[int, str, str]:
    """claude -p 헤드리스 spawn. (exit_code, stdout, stderr) 반환.

    cwd: worktree 디렉토리. claude 의 모든 도구 호출이 여기 기준.

    구현 노트:
      - `--disallowedTools` 가 variadic 이라 뒤이은 positional prompt 를 흡수함.
        그래서 prompt 는 stdin 으로 전달 (안전).
      - shell env 에 ANTHROPIC_API_KEY / AUTH_TOKEN / BASE_URL 이 살아있으면
        claude 가 API 모드로 빠져 401. user OAuth 사용을 위해 subprocess env
        에서 명시적으로 unset.
    """
    disallowed = list(DEFAULT_DISALLOWED_TOOLS) + list(extra_disallowed or [])

    cmd: list[str] = [
        CLAUDE_BIN,
        "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--add-dir", str(cwd),
        "--no-session-persistence",
        "--system-prompt", system_prompt,
        "--disallowedTools", *disallowed,
    ]
    if model:
        cmd += ["--model", model]
    # prompt 는 stdin — variadic 옵션 충돌 회피

    log.info("local_claude.spawn",
             cwd=str(cwd), cmd_preview=" ".join(shlex.quote(c) for c in cmd[:6]) + " …",
             prompt_chars=len(user_prompt), system_chars=len(system_prompt),
             disallowed=len(disallowed))

    sub_env = _sanitized_subprocess_env()
    log.info("local_claude.env_sanitized", kept_claude_code_oauth=bool(sub_env.get("CLAUDE_CODE_OAUTH_TOKEN")))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=sub_env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=user_prompt.encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return 124, "", f"claude -p timed out after {timeout_sec}s"

    return proc.returncode or 0, stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")


async def _spawn_pr_description_skill(
    *,
    cwd: Path,
    base_branch: str,
    timeout_sec: int = 300,
    model: Optional[str] = None,
) -> tuple[Optional[str], dict, int, str]:
    """commit/push 후 worktree 에서 `/pr-description` 스킬 실행.

    스킬은 `git log main..HEAD` / `git diff` 로 현재 branch 의 변경을 분석해서
    한국어 PR body 를 생성한다 (~/.claude/commands/pr-description.md).

    Returns: (pr_body_text, envelope, exit_code, stderr).
              pr_body_text 가 None 이면 실패 — fallback 사용.
    """
    if not PR_DESCRIPTION_SKILL:
        return None, {}, 0, "(skill disabled via LOCAL_PR_DESCRIPTION_SKILL='')"

    # 스킬은 'main..HEAD' 가정 — 비 main base 면 hint 로 보강
    prompt = PR_DESCRIPTION_SKILL
    if base_branch and base_branch != "main":
        prompt += (
            f"\n\n(base 브랜치는 `{base_branch}` 입니다. "
            f"스킬에 적힌 'main' 대신 `{base_branch}` 로 git log/diff 하세요.)"
        )

    cmd = [
        CLAUDE_BIN,
        "-p",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", *PR_DESCRIPTION_DISALLOWED_TOOLS,
    ]
    if model:
        cmd += ["--model", model]

    sub_env = _sanitized_subprocess_env()
    log.info("pr_description.spawn", cwd=str(cwd), model=model or "(default)",
             skill=PR_DESCRIPTION_SKILL, base=base_branch)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=sub_env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return None, {}, 124, f"pr-description timed out after {timeout_sec}s"

    rc = proc.returncode or 0
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    text, env = _parse_claude_json_envelope(stdout)

    if rc != 0 or env.get("is_error") or not text or not text.strip():
        log.warning("pr_description.failed",
                    rc=rc, is_error=env.get("is_error"),
                    api_status=env.get("api_error_status"),
                    stderr_head=stderr[:300], text_head=(text or "")[:300])
        return None, env or {}, rc, stderr

    return text.strip(), env, rc, stderr


def _parse_claude_json_envelope(stdout: str) -> tuple[Optional[str], dict]:
    """`claude -p --output-format json` 의 envelope 파싱.

    엔벨로프 스키마 (Claude Code 2.x):
      { "type": "result", "subtype": "success", "result": "<assistant text>",
        "total_cost_usd": ..., "usage": {...}, ... }

    Returns: (assistant_text, meta) — assistant_text 가 None 이면 파싱 실패.
    """
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return None, {}
    if not isinstance(env, dict):
        return None, {}
    return env.get("result"), env


@dataclass
class LocalBootstrapResult:
    """run_sot_bootstrap_local 의 반환 — 어떤 파일을 만들었는지 + 메타."""
    summary: str
    detected: dict                          # {language, framework, build, ...}
    files_created: list[str]
    files_skipped: list[str]
    files_updated: list[str]
    todos: list[str]
    raw_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: Optional[str] = None


async def run_sot_bootstrap_local(
    *,
    repo_cwd: Path,
    repo: str,
    force_regenerate: bool = False,
    sot_prompt: str = "",
    timeout_sec: int = 900,
    model: Optional[str] = None,
) -> LocalBootstrapResult:
    """새 프로젝트의 SoT 초안 작성 — CLAUDE.md / docs/ARCHITECTURE.md / GLOSSARY / CONVENTIONS / ADR-000.

    claude -p 가 repo_cwd 에서 직접 Edit/Write 로 파일 작성 (worktree 아님 — 실제 repo).
    기존 파일은 force_regenerate=False 면 skip.
    """
    from orchestrator.runners import resolve_local_model
    model = model or resolve_local_model("po")  # PO 와 비슷한 분석 작업 → sonnet default

    agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
    role_prompt_path = agents_dir / "sot-bootstrap-local.md"
    if not role_prompt_path.exists():
        return LocalBootstrapResult(
            summary="", detected={}, files_created=[], files_skipped=[],
            files_updated=[], todos=[],
            error=f"agents/sot-bootstrap-local.md 없음 — {role_prompt_path}",
        )
    role_prompt = role_prompt_path.read_text(encoding="utf-8")
    system_prompt = role_prompt
    if sot_prompt:
        system_prompt += "\n\n## 기존 SoT (있는 부분)\n\n" + sot_prompt

    user_prompt = (
        f"# SoT Bootstrap\n\n"
        f"target repo: {repo}\n"
        f"cwd: `{repo_cwd}`\n"
        f"force_regenerate: {force_regenerate}\n\n"
        f"위 cwd 의 코드베이스를 Glob/Read 로 빠르게 스캔하고, 빠진 SoT 파일들을 "
        f"`Write` 로 작성. 기존 파일은 {'덮어쓰기 허용' if force_regenerate else 'skip'}.\n\n"
        f"작업 끝나면 system prompt 의 schema 그대로 JSON 출력."
    )

    # SoT bootstrap 은 Edit/Write 허용 (다른 agent 와 달리)
    # 단 git push / commit / gh 는 금지 — harness 책임
    bootstrap_disallowed = [
        "Bash(git push:*)", "Bash(git push)",
        "Bash(git commit:*)", "Bash(git commit)",
        "Bash(gh pr:*)", "Bash(gh issue:*)",
    ]

    log.info("sot_bootstrap.spawn", cwd=str(repo_cwd), repo=repo, model=model,
             force=force_regenerate)

    # _spawn_claude 의 disallowed 기본은 Edit/Write 금지가 아니라 git push 만 금지 — 그대로 OK
    rc, stdout, stderr = await _spawn_claude(
        cwd=repo_cwd,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout_sec=timeout_sec,
        extra_disallowed=[],  # SoT 작성용 — Edit/Write 허용
        model=model,
    )

    assistant_text, env = _parse_claude_json_envelope(stdout)
    if assistant_text is None:
        assistant_text = stdout

    cost = float(env.get("total_cost_usd") or 0.0)
    usage = env.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    used_model = env.get("model") or model or ""
    is_error_envelope = bool(env.get("is_error"))

    if rc != 0 or is_error_envelope:
        api_status = env.get("api_error_status")
        detail = (assistant_text or "").strip() or stderr.strip() or stdout.strip()[:600]
        err = f"SoT bootstrap failed — rc={rc} api_status={api_status}: {detail[:600]}"
        log.warning("sot_bootstrap.failed",
                    rc=rc, api_status=api_status, stderr_head=stderr[:500])
        return LocalBootstrapResult(
            summary="", detected={}, files_created=[], files_skipped=[],
            files_updated=[], todos=[],
            raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model, error=err,
        )

    payload = _extract_result_json(assistant_text)
    if not isinstance(payload, dict):
        m = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", assistant_text or "")
        if m:
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                payload = None
    if not isinstance(payload, dict):
        payload = {}

    return LocalBootstrapResult(
        summary=payload.get("summary") or "",
        detected=payload.get("detected") or {},
        files_created=payload.get("files_created") or [],
        files_skipped=payload.get("files_skipped") or [],
        files_updated=payload.get("files_updated") or [],
        todos=payload.get("todos") or [],
        raw_text=assistant_text or "",
        cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
        model=used_model,
    )


@dataclass
class LocalPoResult:
    """run_po_local 의 반환 — N 개 issue spec list + 메타.

    실제 issue 생성은 agents.run_po_local 에서 gh.create_issue 로.
    """
    issues: list[dict]                  # [{title, body, labels, needs_adr, adr_reason, scope_warning}, ...]
    summary: str                        # PO 의 분할 rationale 요약
    split_rationale: str = ""
    raw_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: Optional[str] = None


@dataclass
class LocalPoModeBResult:
    """PO mode B (SoT 갱신) 결과."""
    summary: str = ""
    mode: str = ""                      # urgent | batch
    analyzed_prs: list = None           # type: ignore
    files_changed: list = None          # type: ignore
    files_skipped: list = None          # type: ignore
    pr_title: str = ""
    pr_body: str = ""
    todos: list = None                  # type: ignore
    raw_text: str = ""
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: Optional[str] = None

    def __post_init__(self):
        if self.analyzed_prs is None: self.analyzed_prs = []
        if self.files_changed is None: self.files_changed = []
        if self.files_skipped is None: self.files_skipped = []
        if self.todos is None: self.todos = []


async def run_po_mode_b_local(
    *,
    repo_cwd: Path,
    repo: str,
    target_prs: list,                   # [{number, title, body, diff (str), files (list), ...}]
    sot_prompt: str,
    mode: str = "urgent",               # urgent | batch
    timeout_sec: int = 1200,
    model: Optional[str] = None,
) -> LocalPoModeBResult:
    """PO mode B — merged PR(s) 보고 SoT 파일 갱신 (Read/Edit/Write 직접).

    실제 git commit / PR 생성은 호출자 (agents.run_po_mode_b) 가 담당.
    """
    from orchestrator.runners import resolve_local_model
    model = model or resolve_local_model("po")  # default: sonnet

    agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
    role_prompt_path = agents_dir / "po-mode-b-local.md"
    if not role_prompt_path.exists():
        return LocalPoModeBResult(
            mode=mode, error=f"agents/po-mode-b-local.md 없음 — {role_prompt_path}",
        )
    role_prompt = role_prompt_path.read_text(encoding="utf-8")
    system_prompt = role_prompt + "\n\n## 현재 SoT\n\n" + sot_prompt

    # target_prs 를 prompt 에 압축
    prs_text = ""
    for p in target_prs:
        diff_excerpt = (p.get("diff") or "")[:5000]
        files_summary = "\n".join(
            f"  - {f.get('path', '?')} (+{f.get('additions', 0)} / -{f.get('deletions', 0)})"
            for f in (p.get("files") or [])[:20]
        )
        prs_text += (
            f"\n### PR #{p.get('number')}: {p.get('title', '')}\n"
            f"URL: {p.get('url', '')}\n"
            f"Body:\n{(p.get('body') or '')[:1500]}\n\n"
            f"Files changed ({len(p.get('files') or [])}):\n{files_summary}\n\n"
            f"Diff excerpt (~5KB):\n```diff\n{diff_excerpt}\n```\n"
        )

    user_prompt = (
        f"# PO Mode B — SoT 갱신 (mode={mode})\n\n"
        f"target repo: {repo}\n"
        f"cwd: `{repo_cwd}`\n"
        f"분석 대상 PR 갯수: {len(target_prs)}\n\n"
        f"## PRs\n{prs_text}\n\n"
        f"---\n\n"
        f"위 PR 들의 변경을 분석하고, **현재 cwd 의 SoT 파일** 을 Read 로 확인 후 "
        f"필요한 부분만 Edit/Write 로 갱신해. 최소 수정 원칙. "
        f"애매하면 `> TODO: ...` 명시. "
        f"작업 끝나면 system prompt 의 schema 그대로 JSON 출력."
    )

    log.info("po.mode_b.spawn", repo=repo, mode=mode, prs=len(target_prs), model=model)

    rc, stdout, stderr = await _spawn_claude(
        cwd=repo_cwd,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout_sec=timeout_sec,
        extra_disallowed=[],  # Edit / Write 허용 — SoT 파일 직접 수정
        model=model,
    )

    assistant_text, env = _parse_claude_json_envelope(stdout)
    if assistant_text is None:
        assistant_text = stdout

    cost = float(env.get("total_cost_usd") or 0.0)
    usage = env.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    used_model = env.get("model") or model or ""
    is_error = bool(env.get("is_error"))

    if rc != 0 or is_error:
        api_status = env.get("api_error_status")
        detail = (assistant_text or "").strip() or stderr.strip() or stdout.strip()[:600]
        err = f"PO mode B failed — rc={rc} api_status={api_status}: {detail[:600]}"
        log.warning("po.mode_b.failed",
                    rc=rc, api_status=api_status, stderr_head=stderr[:300])
        return LocalPoModeBResult(
            mode=mode, raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model, error=err,
        )

    payload = _extract_result_json(assistant_text)
    if not isinstance(payload, dict):
        m = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", assistant_text or "")
        if m:
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                payload = None
    if not isinstance(payload, dict):
        payload = {}

    return LocalPoModeBResult(
        summary=payload.get("summary") or "",
        mode=payload.get("mode") or mode,
        analyzed_prs=payload.get("analyzed_prs") or [],
        files_changed=payload.get("files_changed") or [],
        files_skipped=payload.get("files_skipped") or [],
        pr_title=payload.get("pr_title") or "",
        pr_body=payload.get("pr_body") or "",
        todos=payload.get("todos") or [],
        raw_text=assistant_text or "",
        cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok, model=used_model,
    )


async def run_po_mode_a_local(
    *,
    repo_cwd: Path,
    repo: str,
    user_agenda: str,
    sot_prompt: str,
    timeout_sec: int = 600,
    model: Optional[str] = None,
) -> LocalPoResult:
    """PO mode A — 자연어 agenda → 정리된 issue 1~N 개로 분할.

    실제 GitHub issue 생성은 호출자 (agents.run_po_local) 가 결과 보고 처리.
    """
    from orchestrator.runners import resolve_local_model
    model = model or resolve_local_model("po")  # default: sonnet

    agents_dir = Path(__file__).resolve().parent.parent.parent / "agents"
    role_prompt_path = agents_dir / "po-local.md"
    if not role_prompt_path.exists():
        return LocalPoResult(issues=[], summary="", error=f"agents/po-local.md 없음 — {role_prompt_path}")
    role_prompt = role_prompt_path.read_text(encoding="utf-8")
    system_prompt = role_prompt + "\n\n" + sot_prompt

    user_prompt = (
        f"# Agenda (사용자 자연어 입력)\n\n{user_agenda}\n\n"
        f"---\n\n"
        f"## 대상 repo\n"
        f"- {repo}\n"
        f"- cwd: `{repo_cwd}`\n\n"
        f"위 agenda 를 SoT 와 대조해서 정리된 issue 1개 또는 N개로 분할해. "
        f"system prompt 의 출력 schema 그대로 JSON 출력."
    )

    # PO 는 read-only — Edit/Write/git/gh 모두 차단
    log.info("po.mode_a.spawn", cwd=str(repo_cwd), repo=repo, model=model,
             agenda_chars=len(user_agenda))

    rc, stdout, stderr = await _spawn_claude(
        cwd=repo_cwd,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout_sec=timeout_sec,
        extra_disallowed=["Edit", "Write"],
        model=model,
    )

    assistant_text, env = _parse_claude_json_envelope(stdout)
    if assistant_text is None:
        assistant_text = stdout

    cost = float(env.get("total_cost_usd") or 0.0)
    usage = env.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    used_model = env.get("model") or model or ""
    is_error_envelope = bool(env.get("is_error"))

    if rc != 0 or is_error_envelope:
        api_status = env.get("api_error_status")
        detail = (assistant_text or "").strip() or stderr.strip() or stdout.strip()[:600]
        err = f"PO mode A failed — rc={rc} is_error={is_error_envelope} api_status={api_status}: {detail[:600]}"
        log.warning("po.mode_a.failed", rc=rc, is_error=is_error_envelope,
                    api_status=api_status, stderr_head=stderr[:500])
        return LocalPoResult(
            issues=[], summary="", raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model, error=err,
        )

    payload = _extract_result_json(assistant_text)
    if not isinstance(payload, dict):
        # fenced JSON 다시 시도
        m = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", assistant_text or "")
        if m:
            try:
                payload = json.loads(m.group(1))
            except json.JSONDecodeError:
                payload = None

    if not isinstance(payload, dict):
        return LocalPoResult(
            issues=[], summary="", raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model, error="PO 응답 JSON 파싱 실패",
        )

    issues_raw = payload.get("issues") or []
    if not isinstance(issues_raw, list):
        return LocalPoResult(
            issues=[], summary="", raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model, error="PO 응답의 'issues' 가 array 가 아님",
        )

    # 기본값 채우기 + 검증
    issues: list[dict] = []
    for it in issues_raw:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        body = (it.get("body") or "").strip()
        if not title or not body:
            log.warning("po.mode_a.skipped_issue", title=title[:60], reason="title/body 누락")
            continue
        labels = it.get("labels") or ["ah:needs-execution"]
        if "ah:needs-execution" not in labels:
            labels = list(labels) + ["ah:needs-execution"]
        issues.append({
            "title": title[:200],
            "body": body,
            "labels": labels,
            "needs_adr": bool(it.get("needs_adr")),
            "adr_reason": it.get("adr_reason") or "",
            "scope_warning": it.get("scope_warning") or "",
        })

    return LocalPoResult(
        issues=issues,
        summary=payload.get("summary") or "",
        split_rationale=payload.get("split_rationale") or "",
        raw_text=assistant_text or "",
        cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
        model=used_model,
    )


@dataclass
class LocalReviewerResult:
    """run_reviewer_local 의 반환 타입 — agents.py 가 기존 review dict 처럼 사용."""
    review: Optional[dict]               # 파싱된 review JSON. None 이면 실패.
    raw_text: str                        # claude 의 마지막 메시지 (디버깅)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    error: Optional[str] = None          # 실패 사유 (rc != 0 / is_error / JSON 파싱 실패)


async def run_reviewer_local(
    *,
    repo_cwd: Path,
    system_prompt: str,
    user_prompt: str,
    timeout_sec: int = 1800,
    model: Optional[str] = None,
) -> LocalReviewerResult:
    """로컬 모드 reviewer — claude -p 한 번 호출해서 PR review JSON 받기.

    reviewer 는 코드 수정 X — Edit/Write/git 쓰기 명령 모두 차단.
    cwd 는 repo 자체 (worktree 아님) — claude 가 필요시 Read/Grep 으로 추가 탐색.
    """
    from orchestrator.runners import resolve_local_model
    model = model or resolve_local_model("reviewer")  # default: sonnet
    reviewer_disallowed = list(DEFAULT_DISALLOWED_TOOLS) + ["Edit", "Write"]

    log.info("reviewer.local.spawn", cwd=str(repo_cwd), model=model)

    rc, stdout, stderr = await _spawn_claude(
        cwd=repo_cwd,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        timeout_sec=timeout_sec,
        extra_disallowed=["Edit", "Write"],
        model=model,
    )

    assistant_text, env = _parse_claude_json_envelope(stdout)
    if assistant_text is None:
        assistant_text = stdout

    cost = float(env.get("total_cost_usd") or 0.0)
    usage = env.get("usage") or {}
    in_tok = int(usage.get("input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    used_model = env.get("model") or model or ""
    is_error_envelope = bool(env.get("is_error"))

    if rc != 0 or is_error_envelope:
        api_status = env.get("api_error_status")
        detail = (assistant_text or "").strip() or stderr.strip() or stdout.strip()[:600]
        err = f"claude -p reviewer failed — rc={rc} is_error={is_error_envelope} api_status={api_status}: {detail[:600]}"
        log.warning("reviewer.local.failed",
                    rc=rc, is_error=is_error_envelope, api_status=api_status,
                    stderr_head=stderr[:500])
        return LocalReviewerResult(
            review=None, raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model, error=err,
        )

    # review JSON 추출 — 펜스 / 꼬리 / 전체 JSON 시도
    review = _extract_result_json(assistant_text)
    if review is None:
        # _extract_result_json 은 일반 JSON 형식 — fenced ```json``` 도 시도 추가
        m = re.search(r"```(?:json)?\s*\n(\{[\s\S]*?\})\s*\n```", assistant_text or "")
        if m:
            try:
                review = json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

    if not isinstance(review, dict):
        return LocalReviewerResult(
            review=None, raw_text=assistant_text or "",
            cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok,
            model=used_model,
            error="review JSON 파싱 실패",
        )

    return LocalReviewerResult(
        review=review, raw_text=assistant_text or "",
        cost_usd=cost, input_tokens=in_tok, output_tokens=out_tok, model=used_model,
    )


class LocalClaudeRunner:
    """로컬 클로드 코드 헤드리스 모드 — claude -p 가 worktree 직접 편집."""

    async def execute(self, ctx: ExecutionContext) -> ExecutionResult:
        timeout_sec = int(os.environ.get("LOCAL_CLAUDE_TIMEOUT_SEC", "1800"))  # 30 min
        # 역할별 모델 — developer=opus, amend=opus (코드 작성 품질)
        from orchestrator.runners import resolve_local_model
        model = ctx.model or resolve_local_model(ctx.role)

        # 1) worktree 분기 (신규/amend)
        branch_name = (
            None
            if ctx.existing_branch
            else _default_branch_name(ctx.role, ctx.issue_or_pr_number, ctx.title_hint)
        )

        try:
            wt = await prepare_worktree(
                repo_cwd=ctx.repo_cwd,
                branch_name=branch_name,
                existing_branch=ctx.existing_branch,
            )
        except Exception as exc:
            log.exception("local_claude.worktree_failed", error=str(exc))
            return ExecutionResult(
                ok=False, error=f"worktree 준비 실패: {exc}", error_kind="crashed",
            )

        try:
            # 2) system prompt 조립 (역할 정의 + SoT)
            role_prompt = _load_agent_prompt()
            system_prompt = role_prompt + "\n\n" + ctx.sot_prompt

            # user prompt 에 worktree 경로 명시 — claude 가 cwd 인식하지만 보조
            user_prompt = (
                f"{ctx.user_prompt}\n\n"
                f"---\n\n"
                f"## 실행 컨텍스트\n"
                f"- worktree: `{wt.path}` (현재 cwd)\n"
                f"- base branch: `{wt.base}`\n"
                f"- target branch: `{wt.branch}` ({'amend' if ctx.existing_branch else '신규'})\n\n"
                f"이 디렉토리 안에서만 작업해. Read/Edit/Write/Grep/Glob/Bash 자유롭게 사용. "
                f"단 git/gh 명령은 호출하지 마 (harness 가 처리). "
                f"작업 완료 후 마지막에 단일 JSON 객체 출력 (schema 는 system prompt 참조)."
            )

            # 3) claude -p spawn
            rc, stdout, stderr = await _spawn_claude(
                cwd=wt.path,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_sec=timeout_sec,
                model=model,
            )

            # 4) envelope 먼저 파싱 — claude -p 는 에러도 stdout JSON 으로 내보냄
            #    (예: {"is_error": true, "api_error_status": 401, "result": "..."}).
            #    rc 만 보고 abort 하면 진짜 원인 (auth/cost cap/timeout 등) 이 묻힘.
            assistant_text, env = _parse_claude_json_envelope(stdout)
            if assistant_text is None:
                assistant_text = stdout

            cost = float(env.get("total_cost_usd") or 0.0)
            usage = env.get("usage") or {}
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            used_model = env.get("model") or model or ""
            is_error_envelope = bool(env.get("is_error"))
            api_status = env.get("api_error_status")
            stop_reason = env.get("stop_reason") or env.get("terminal_reason")

            if rc != 0 or is_error_envelope:
                # 진단 정보 우선순위: envelope.result > stderr > stdout > "(no output)"
                parts = []
                if rc != 0:
                    parts.append(f"rc={rc}")
                if is_error_envelope:
                    parts.append("is_error=true")
                if api_status:
                    parts.append(f"api_status={api_status}")
                if stop_reason:
                    parts.append(f"stop_reason={stop_reason}")
                envelope_msg = (assistant_text or "").strip()
                detail = envelope_msg or stderr.strip() or stdout.strip()[:600] or "(no output)"
                log.warning(
                    "local_claude.failed",
                    rc=rc, is_error=is_error_envelope, api_status=api_status,
                    stop_reason=stop_reason,
                    stderr_head=stderr[:500], stdout_head=stdout[:500],
                )
                return ExecutionResult(
                    ok=False,
                    error=f"claude -p failed — {' / '.join(parts) or 'no diagnostics'}: {detail[:600]}",
                    error_kind="crashed",
                    cost_usd=cost,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    model=used_model,
                    extra={
                        "stdout_head": stdout[:2000],
                        "stderr_head": stderr[:2000],
                        "envelope": {k: env.get(k) for k in
                                     ("type", "subtype", "is_error", "api_error_status",
                                      "stop_reason", "terminal_reason", "num_turns",
                                      "duration_ms", "session_id")
                                     if k in env},
                    },
                )

            payload = _extract_result_json(assistant_text) or {}
            summary = payload.get("summary") or "(summary 없음)"

            # 5) worktree 변경 감지
            changes = await detect_worktree_changes(wt)
            if changes["files_changed"] == 0:
                log.warning("local_claude.no_changes",
                            stdout_head=assistant_text[:500])
                return ExecutionResult(
                    ok=False,
                    error=(
                        "claude -p 실행은 끝났지만 worktree 변경 없음. "
                        "프롬프트 부족 or 모델이 작업 거부 가능."
                    ),
                    error_kind="no_changes",
                    summary=summary,
                    cost_usd=cost,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    model=used_model,
                    extra={"payload": payload, "assistant_text_head": assistant_text[:1500]},
                )

            # 6) commit + push (1 commit)
            issue_tag = f"#{ctx.issue_or_pr_number}"
            commit_msg = payload.get("pr_title") or summary or f"[{issue_tag}] auto"
            if issue_tag not in commit_msg:
                commit_msg = f"[{issue_tag}] {commit_msg}"
            commit_msg = commit_msg[:300]

            try:
                push_info = await stage_commit_push_all(
                    repo_cwd=ctx.repo_cwd, wt=wt, commit_message=commit_msg,
                )
            except Exception as exc:
                log.exception("local_claude.commit_push_failed", error=str(exc))
                return ExecutionResult(
                    ok=False,
                    error=f"commit/push 실패: {exc}",
                    error_kind="crashed",
                    summary=summary,
                    cost_usd=cost,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    model=used_model,
                )

            # 7) PR body 생성 — `/pr-description` 스킬을 worktree 에서 추가 spawn.
            #    **신규 PR (existing_branch=None) 일 때만**. amend mode 는 PR body 이미
            #    있으므로 skip (이전엔 매 amend 마다 호출해서 ~$0.30 낭비).
            pr_body_via_skill: Optional[str] = None
            pr_desc_cost = 0.0
            pr_desc_model = ""
            if PR_DESCRIPTION_SKILL and not ctx.existing_branch:
                pr_body_via_skill, pr_desc_env, pr_desc_rc, _ = await _spawn_pr_description_skill(
                    cwd=wt.path,
                    base_branch=push_info["base"],
                    model=model,
                )
                if pr_desc_env:
                    pr_desc_cost = float(pr_desc_env.get("total_cost_usd") or 0.0)
                    pr_desc_model = pr_desc_env.get("model") or ""
                log.info("local_claude.pr_description.done",
                         used_skill=bool(pr_body_via_skill),
                         body_chars=len(pr_body_via_skill or ""),
                         cost=pr_desc_cost, rc=pr_desc_rc)
            elif ctx.existing_branch:
                log.info("local_claude.pr_description.skipped_amend", pr=ctx.issue_or_pr_number)

            final_pr_body = pr_body_via_skill or payload.get("pr_body")
            pr_body_source = "pr-description-skill" if pr_body_via_skill else "executor-json"

            return ExecutionResult(
                ok=True,
                summary=summary,
                branch=push_info["branch"],
                base=push_info["base"],
                files_changed=push_info["files_changed"],
                commits_applied=push_info["commits_applied"],
                pr_title=payload.get("pr_title"),
                pr_body=final_pr_body,
                verification=payload.get("verification"),
                cost_usd=cost + pr_desc_cost,
                input_tokens=in_tok,
                output_tokens=out_tok,
                model=used_model or pr_desc_model,
                extra={
                    "payload": payload,
                    "changed_paths": changes["paths"],
                    "pr_body_source": pr_body_source,
                    "pr_description_cost_usd": pr_desc_cost,
                },
            )
        finally:
            await cleanup_worktree(repo_cwd=ctx.repo_cwd, wt=wt)
