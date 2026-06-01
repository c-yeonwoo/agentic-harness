"""Code 조회 도구 — agent ReAct loop 에서 LLM 이 호출.

3개 도구:
  read_file       — 파일 내용 (line range 옵션)
  list_files      — 디렉토리 트리 (glob 패턴 옵션)
  search_text     — grep (regex 또는 literal)

모든 경로는 repo_cwd 기준. path traversal 방지.

Anthropic tool use JSON schema 함께 정의 — claude.call_with_tools 에 직접 전달.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Optional


# ── JSON schemas — Anthropic tool definition ────────────────────────────────


TOOL_SCHEMAS: list[dict] = [
    {
        "name": "read_file",
        "description": (
            "Read a file from the repo (relative to repo root). "
            "Use to see current code before planning changes. "
            "Optional line range — read_file(path='lore-ui/src/app/page.tsx', start=100, end=200) "
            "for a specific section."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative path from repo root, e.g. 'lore-ui/src/app/page.tsx'",
                },
                "start": {
                    "type": "integer",
                    "description": "Optional 1-based start line (default 1)",
                },
                "end": {
                    "type": "integer",
                    "description": "Optional 1-based end line (default end of file)",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List files in a directory (recursive glob). "
            "Use to discover structure before reading specific files. "
            "Default pattern '**/*' excludes node_modules, .git, build artifacts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Relative directory from repo root (default '.' = root)",
                },
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern (default '*'). e.g. '**/*.tsx', '**/page.tsx'",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max files to return (default 100)",
                },
            },
            "required": [],
        },
    },
    # ── submit_plan — terminal tool. LLM 이 이 도구 호출하면 plan 확정 ──
    {
        "name": "submit_plan",
        "description": (
            "최종 plan 을 제출하고 작업 종료. 모든 코드 조사가 끝났을 때만 호출. "
            "입력 값이 PR 생성에 그대로 사용됨."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "한 줄, 한국어"},
                "approach": {"type": "string", "description": "여러 줄 한국어 — 접근 방법"},
                "branch_name": {
                    "type": "string",
                    "description": "feat/fix/refactor/chore-... kebab-case + issue 번호",
                },
                "commits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "message": {"type": "string", "description": "[#N] feat: ..."},
                            "files": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "action": {
                                            "type": "string",
                                            "enum": ["create", "replace", "delete", "edit"],
                                            "description": (
                                                "create: 새 파일 / "
                                                "replace: 작은 파일(<200줄) 통째 / "
                                                "delete: 삭제 / "
                                                "edit: 큰 파일 부분 변경 (권장 — old_str/new_str 쌍으로)"
                                            ),
                                        },
                                        "content": {
                                            "type": "string",
                                            "description": "create/replace 시 전체 파일 내용",
                                        },
                                        "edits": {
                                            "type": "array",
                                            "description": "edit action 일 때 — 부분 변경 쌍들",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "old_str": {
                                                        "type": "string",
                                                        "description": (
                                                            "교체 대상 — 파일 안에 정확히 한 번 등장해야 함. "
                                                            "context 충분히 (3-5줄) 포함해서 unique 확보."
                                                        ),
                                                    },
                                                    "new_str": {
                                                        "type": "string",
                                                        "description": "교체 후 문자열",
                                                    },
                                                },
                                                "required": ["old_str", "new_str"],
                                            },
                                        },
                                    },
                                    "required": ["path", "action"],
                                },
                            },
                        },
                        "required": ["message", "files"],
                    },
                },
                "pr_title": {"type": "string"},
                "pr_body": {"type": "string", "description": "Closes #N 포함"},
                "verification": {"type": "string"},
                "scope_warning": {"type": "string", "description": "없으면 빈 문자열"},
            },
            "required": ["summary", "branch_name", "commits", "pr_title", "pr_body"],
        },
    },
    {
        "name": "search_text",
        "description": (
            "Grep — search literal or regex across repo. "
            "Returns lines matching pattern with file:line prefix. "
            "Use to find usages, related code, similar patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Literal string or regex pattern",
                },
                "path": {
                    "type": "string",
                    "description": "Optional path prefix filter (e.g. 'lore-ui/src')",
                },
                "is_regex": {
                    "type": "boolean",
                    "description": "Treat query as regex (default false = literal)",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max matches (default 50)",
                },
            },
            "required": ["query"],
        },
    },
]


# ── Tool execution ──────────────────────────────────────────────────────────


# 무시할 패턴 — list_files 기본 exclude
_IGNORE_DIRS = {
    ".git", "node_modules", ".next", ".venv", "venv", "__pycache__",
    "dist", "build", "out", "target", ".gradle", ".idea", ".vscode",
    ".codegraph", ".kotlin", ".coderabbit",
}


def _safe_path(root: Path, rel: str) -> Path:
    """경로 traversal 방지."""
    rel = (rel or "").strip().lstrip("/")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and not str(target).startswith(str(root_resolved) + "/"):
        raise RuntimeError(f"path traversal blocked: {rel}")
    return target


async def read_file_tool(repo_cwd: Path, *, path: str,
                          start: Optional[int] = None,
                          end: Optional[int] = None) -> str:
    target = _safe_path(repo_cwd, path)
    if not target.exists():
        return f"(파일 없음: {path})"
    if not target.is_file():
        return f"(파일 아님: {path})"
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"(읽기 실패: {exc})"

    lines = text.splitlines()
    total = len(lines)
    s = max(1, start or 1)
    e = min(total, end or total)
    chunk = lines[s - 1:e]
    # 헤더에 line range, 본문은 line number 없이 원본 그대로 — edit action 의
    # old_str 에 line number 가 섞이지 않게.
    header = (
        f"--- {path} (lines {s}-{e} / {total}) ---\n"
        f"# 본문은 원본 그대로. line number 없음.\n"
        f"# edit action 의 old_str 에 쓸 때 line number 추가하지 말 것.\n"
    )
    body = "\n".join(chunk)
    if len(body) > 30_000:
        body = body[:30_000] + f"\n... [잘림 — 더 보려면 start/end 좁히기]"
    return header + body


async def list_files_tool(repo_cwd: Path, *, directory: str = ".",
                           pattern: str = "*",
                           max_results: int = 100) -> str:
    base = _safe_path(repo_cwd, directory)
    if not base.exists() or not base.is_dir():
        return f"(디렉토리 없음: {directory})"

    results: list[str] = []
    try:
        for p in base.rglob(pattern):
            # ignore dirs
            if any(part in _IGNORE_DIRS for part in p.parts):
                continue
            if not p.is_file():
                continue
            rel = p.relative_to(repo_cwd)
            results.append(str(rel))
            if len(results) >= max_results:
                break
    except Exception as exc:
        return f"(glob 실패: {exc})"

    if not results:
        return f"(매칭 없음: {directory}/{pattern})"
    header = f"--- {len(results)} files in {directory} matching {pattern} ---\n"
    return header + "\n".join(sorted(results))


async def search_text_tool(repo_cwd: Path, *, query: str,
                            path: str = "",
                            is_regex: bool = False,
                            max_results: int = 50) -> str:
    base = _safe_path(repo_cwd, path) if path else repo_cwd
    if not base.exists():
        return f"(경로 없음: {path})"

    # ripgrep 우선 — 없으면 git grep, 그것도 없으면 직접
    try:
        cmd = ["rg", "--line-number", "--no-heading"]
        if not is_regex:
            cmd.append("--fixed-strings")
        cmd += ["--max-count", str(max(1, max_results)), "--", query, str(base)]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(repo_cwd),
        )
        stdout, _ = await proc.communicate()
        if proc.returncode in (0, 1):                 # 0=match, 1=no match
            out = stdout.decode().strip()
            if not out:
                return f"(매칭 없음: {query!r})"
            # repo_cwd prefix 제거
            lines = [l.replace(str(repo_cwd) + "/", "") for l in out.splitlines()[:max_results]]
            header = f"--- search '{query}' ({len(lines)} hits) ---\n"
            return header + "\n".join(lines)
    except FileNotFoundError:
        pass

    # fallback — Python 직접 (느림)
    flags = 0 if is_regex else re.escape
    try:
        pat = re.compile(query if is_regex else re.escape(query))
    except re.error as exc:
        return f"(invalid regex: {exc})"

    hits: list[str] = []
    for p in base.rglob("*"):
        if any(part in _IGNORE_DIRS for part in p.parts):
            continue
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                rel = p.relative_to(repo_cwd)
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"(매칭 없음: {query!r})"
    header = f"--- search '{query}' ({len(hits)} hits) ---\n"
    return header + "\n".join(hits)


# ── Dispatcher ──────────────────────────────────────────────────────────────


async def execute_tool(repo_cwd: Path, name: str, args: dict) -> str:
    """LLM tool_use → 실제 도구 실행. 결과는 string."""
    if name == "read_file":
        return await read_file_tool(repo_cwd, **args)
    if name == "list_files":
        return await list_files_tool(repo_cwd, **args)
    if name == "search_text":
        return await search_text_tool(repo_cwd, **args)
    return f"(unknown tool: {name})"
