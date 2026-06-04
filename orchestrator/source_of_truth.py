"""Source of Truth 자동 발견 — agent prompt 빌드 시 prefix 로 주입.

4-tier 계층:
  A. 글로벌      ~/.claude/CLAUDE.md
  B. 조직        ~/dev/CLAUDE.md (예: ohouse)
  C. 프로젝트    {repo}/CLAUDE.md, {repo}/ARCHITECTURE.md, {repo}/.agentic.yml
  D. 동적        gh recent PRs/issues, git log

discover(cwd) 가 모든 tier 누적해 SourceOfTruth 반환.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog
import yaml

from orchestrator import gh

log = structlog.get_logger()


# CLAUDE.md 계층 탐색 시 멈출 경계 (홈 디렉토리 위로 안 감)
_CLAUDE_MD_NAME = "CLAUDE.md"
_HOME = Path.home()
_CACHE_TTL_SEC = int(os.environ.get("SOT_CACHE_TTL_SEC", "1800"))


@dataclass
class SourceOfTruth:
    repo: str                          # 'bucketplace/lore'
    cwd: Path
    claude_chain: list[Path]            # 글로벌 → 조직 → 프로젝트 (낮은 → 높은 우선순위)
    architecture_md: Optional[str]
    readme_md: Optional[str]
    agentic_yml: dict
    recent_prs: list[dict] = field(default_factory=list)
    recent_issues: list[dict] = field(default_factory=list)
    last_commits: list[str] = field(default_factory=list)
    # 추가 SoT 문서 — palette 처럼 docs/ 아래에 핵심 정책을 둔 프로젝트 지원
    agent_context_md: Optional[str] = None        # .hermes/agent-context.md or .agent-context.md
    docs_pages: dict = field(default_factory=dict)        # {filename: full text} — 핵심 정책 풀
    adr_summaries: list[dict] = field(default_factory=list)  # [{stem, summary}] — title + 첫 1.5KB

    def to_prompt(self) -> str:
        """agent system prompt 에 주입할 markdown."""
        parts: list[str] = []
        parts.append(f"# Source of Truth — {self.repo}")
        parts.append("")
        if self.claude_chain:
            parts.append("## CLAUDE.md (계층 — 글로벌→조직→프로젝트)")
            for p in self.claude_chain:
                parts.append(f"### {p}")
                parts.append(p.read_text(encoding="utf-8"))
                parts.append("")
        if self.agent_context_md:
            parts.append("## Agent Context (워커가 먼저 봐야 할 핵심 요약)")
            parts.append(self.agent_context_md)
            parts.append("")
        if self.architecture_md:
            parts.append("## ARCHITECTURE.md")
            parts.append(self.architecture_md)
            parts.append("")
        elif self.readme_md:
            parts.append("## README.md (ARCHITECTURE.md 없음 — README 대체)")
            parts.append(self.readme_md[:8000])      # cap
            parts.append("")
        if self.docs_pages:
            parts.append("## docs/* (핵심 정책 — 풀 inline)")
            for name, body in self.docs_pages.items():
                parts.append(f"### docs/{name}")
                parts.append(body)
                parts.append("")
        if self.adr_summaries:
            parts.append(
                "## docs/DECISIONS/ ADR (선택 inject — `SOT_INCLUDE_ADR=full` 또는 "
                "`.agentic.yml sot.adr_mode=full`. 본문은 Read 로 직접 펼침)"
            )
            for adr in self.adr_summaries:
                parts.append(f"### {adr['stem']}")
                parts.append(adr['summary'])
                parts.append("")
        if self.agentic_yml:
            parts.append("## .agentic.yml (명시 override)")
            parts.append(yaml.safe_dump(self.agentic_yml, allow_unicode=True))
            parts.append("")
        if self.recent_prs:
            parts.append("## 최근 PR (20)")
            for pr in self.recent_prs[:20]:
                merged = " [merged]" if pr.get("mergedAt") else ""
                parts.append(f"- #{pr['number']} {pr['title']}{merged}")
            parts.append("")
        if self.recent_issues:
            parts.append("## 최근 issue (20)")
            for it in self.recent_issues[:20]:
                parts.append(f"- #{it['number']} [{it['state']}] {it['title']}")
            parts.append("")
        if self.last_commits:
            parts.append("## 최근 커밋")
            for line in self.last_commits[:20]:
                parts.append(f"- {line}")
            parts.append("")
        return "\n".join(parts)


def _read_or_none(path: Path, limit: int = 60000) -> Optional[str]:
    try:
        text = path.read_text(encoding="utf-8")
        return text[:limit]
    except (FileNotFoundError, PermissionError):
        return None


def _cache_root(cwd: Path) -> Path:
    p = cwd / ".hermes" / "cache" / "sot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _mtime_key(path: Path) -> str:
    try:
        st = path.stat()
        return f"{path}:{st.st_mtime_ns}:{st.st_size}"
    except FileNotFoundError:
        return f"{path}:missing"


def _build_cache_key(cwd: Path, repo: str, claude_chain: list[Path]) -> str:
    tracked: list[str] = []
    tracked.extend([
        _mtime_key(cwd / "ARCHITECTURE.md"),
        _mtime_key(cwd / "README.md"),
        _mtime_key(cwd / ".agentic.yml"),
        _mtime_key(cwd / ".hermes" / "agent-context.md"),
        _mtime_key(cwd / ".agent-context.md"),
    ])
    tracked.extend(_mtime_key(p) for p in claude_chain)

    docs_dir = cwd / "docs"
    if docs_dir.exists():
        for p in sorted(docs_dir.rglob("*.md")):
            tracked.append(_mtime_key(p))

    git_head = ""
    try:
        git_head = subprocess.check_output(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        pass

    raw = f"repo={repo}\nhead={git_head}\n" + "\n".join(tracked)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _serialize_sot(s: SourceOfTruth) -> dict:
    return {
        "repo": s.repo,
        "cwd": str(s.cwd),
        "claude_chain": [str(p) for p in s.claude_chain],
        "architecture_md": s.architecture_md,
        "readme_md": s.readme_md,
        "agentic_yml": s.agentic_yml,
        "recent_prs": s.recent_prs,
        "recent_issues": s.recent_issues,
        "last_commits": s.last_commits,
        "agent_context_md": s.agent_context_md,
        "docs_pages": s.docs_pages,
        "adr_summaries": s.adr_summaries,
    }


def _deserialize_sot(payload: dict) -> SourceOfTruth:
    return SourceOfTruth(
        repo=payload.get("repo", ""),
        cwd=Path(payload.get("cwd", ".")),
        claude_chain=[Path(p) for p in payload.get("claude_chain", [])],
        architecture_md=payload.get("architecture_md"),
        readme_md=payload.get("readme_md"),
        agentic_yml=payload.get("agentic_yml") or {},
        recent_prs=payload.get("recent_prs") or [],
        recent_issues=payload.get("recent_issues") or [],
        last_commits=payload.get("last_commits") or [],
        agent_context_md=payload.get("agent_context_md"),
        docs_pages=payload.get("docs_pages") or {},
        adr_summaries=payload.get("adr_summaries") or [],
    )


def _load_cache(cwd: Path, key: str) -> Optional[SourceOfTruth]:
    fp = _cache_root(cwd) / f"{key}.json"
    if not fp.exists():
        return None
    try:
        payload = json.loads(fp.read_text(encoding="utf-8"))
        created_at = float(payload.get("created_at", 0))
        if time.time() - created_at > _CACHE_TTL_SEC:
            return None
        return _deserialize_sot(payload["sot"])
    except Exception as exc:
        log.warning("sot.cache_read_failed", error=str(exc), file=str(fp))
        return None


def _save_cache(cwd: Path, key: str, sot: SourceOfTruth) -> None:
    fp = _cache_root(cwd) / f"{key}.json"
    payload = {"created_at": time.time(), "sot": _serialize_sot(sot)}
    try:
        fp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:
        log.warning("sot.cache_write_failed", error=str(exc), file=str(fp))


def _parse_git_remote(cwd: Path) -> str:
    """`git remote get-url origin` 으로 'owner/repo' 추출."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except subprocess.CalledProcessError:
        return ""
    # git@github.com:bucketplace/lore.git  또는  https://github.com/bucketplace/lore.git
    if out.startswith("git@"):
        path = out.split(":", 1)[1]
    elif "github.com/" in out:
        path = out.split("github.com/", 1)[1]
    else:
        return ""
    return path.removesuffix(".git").strip()


def _git_log(cwd: Path, limit: int = 20) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(cwd), "log", f"-{limit}", "--oneline", "--decorate"],
            text=True, stderr=subprocess.DEVNULL,
        )
        return out.strip().splitlines()
    except subprocess.CalledProcessError:
        return []


def _collect_claude_chain(cwd: Path) -> list[Path]:
    """cwd 부터 위로 올라가며 CLAUDE.md 수집. ~/.claude/CLAUDE.md 까지 포함.

    반환 순서: 낮은 우선순위 (global) → 높은 우선순위 (프로젝트).
    agent prompt 에선 뒤쪽이 더 가까운 컨텍스트.
    """
    found: list[Path] = []

    # ~/.claude/CLAUDE.md (글로벌)
    global_md = _HOME / ".claude" / _CLAUDE_MD_NAME
    if global_md.exists():
        found.append(global_md)

    # ~ ~ /dev/CLAUDE.md, ... /cwd/CLAUDE.md
    # cwd 부터 위로 올라가면서 발견 — _HOME 까지만
    chain_from_cwd: list[Path] = []
    cur = cwd.resolve()
    while cur != cur.parent:
        md = cur / _CLAUDE_MD_NAME
        if md.exists():
            chain_from_cwd.append(md)
        if cur == _HOME:
            break
        cur = cur.parent

    # cwd 가 가장 가까움 → 마지막에 위치하도록 reverse
    chain_from_cwd.reverse()
    # global_md 가 chain_from_cwd 에 또 포함됐을 수도 — dedup
    seen = {p.resolve() for p in found}
    for p in chain_from_cwd:
        if p.resolve() not in seen:
            found.append(p)
            seen.add(p.resolve())
    return found


async def discover(cwd: Path | str) -> SourceOfTruth:
    """cwd 기준 SourceOfTruth 합성. 모든 file IO 는 동기지만 gh 호출만 async."""
    cwd = Path(cwd).resolve()
    repo = _parse_git_remote(cwd)
    if not repo:
        log.warning("sot.no_git_remote", cwd=str(cwd))

    claude_chain = _collect_claude_chain(cwd)
    cache_key = _build_cache_key(cwd, repo, claude_chain)
    cached = _load_cache(cwd, cache_key)
    if cached is not None:
        log.info("sot.cache_hit", key=cache_key[:12], ttl_sec=_CACHE_TTL_SEC)
        return cached

    log.info("sot.cache_miss", key=cache_key[:12])

    # 정적 파일들 — ARCHITECTURE.md 는 cwd 또는 docs/ 둘 다 지원
    arch = _read_or_none(cwd / "ARCHITECTURE.md") or _read_or_none(cwd / "docs" / "ARCHITECTURE.md")
    readme = _read_or_none(cwd / "README.md")
    # agent-context.md (palette 의 .hermes/agent-context.md 같은 명시 요약본)
    agent_ctx = (
        _read_or_none(cwd / ".hermes" / "agent-context.md")
        or _read_or_none(cwd / ".agent-context.md")
    )

    agentic_yml = {}
    agentic_path = cwd / ".agentic.yml"
    if agentic_path.exists():
        try:
            agentic_yml = yaml.safe_load(agentic_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            log.warning("sot.agentic_yml_parse_failed", error=str(exc))

    # docs/*.md 핵심 정책 (FEATURE_SPEC / ARCHITECTURE 제외 — 각각 크고 별도)
    docs_pages: dict[str, str] = {}
    docs_dir = cwd / "docs"
    inline_docs = set((agentic_yml.get("sot", {}) or {}).get("inline_docs", []))
    max_inline_chars = int((agentic_yml.get("sot", {}) or {}).get("max_inline_chars", 8000))
    if docs_dir.exists():
        skip = {"ARCHITECTURE.md", "FEATURE_SPEC.md"}
        for p in sorted(docs_dir.glob("*.md")):
            if p.name in skip:
                continue
            if inline_docs and p.name not in inline_docs:
                continue
            txt = _read_or_none(p, limit=max_inline_chars)
            if txt:
                docs_pages[p.name] = txt

    # docs/DECISIONS/*.md — ADR 본문 inject (ADR-019: 기본 OFF, token 절약)
    # 옵션: SOT_INCLUDE_ADR=true / titles 시 title list 만, full 시 본문
    # 또는 .agentic.yml 의 sot.adr_mode: off | titles | full
    adr_summaries: list[dict] = []
    sot_cfg = (agentic_yml.get("sot", {}) or {})
    adr_mode = (
        sot_cfg.get("adr_mode")
        or os.environ.get("SOT_INCLUDE_ADR", "off")
    ).strip().lower()
    # backward compat: true / yes → full
    if adr_mode in ("true", "yes", "1"):
        adr_mode = "full"
    if adr_mode not in ("off", "titles", "full"):
        adr_mode = "off"

    adr_dir = docs_dir / "DECISIONS"
    if adr_dir.exists() and adr_mode != "off":
        for p in sorted(adr_dir.glob("*.md")):
            if adr_mode == "titles":
                # title 만 — 첫 # 헤더 라인
                try:
                    first_line = p.read_text(encoding="utf-8").splitlines()[0]
                    adr_summaries.append({"stem": p.stem, "summary": first_line})
                except Exception:
                    continue
            else:  # full
                txt = _read_or_none(p, limit=1500)
                if txt:
                    adr_summaries.append({"stem": p.stem, "summary": txt})

    last_commits = _git_log(cwd, limit=20)

    # 동적 — GitHub
    recent_prs: list[dict] = []
    recent_issues: list[dict] = []
    if repo:
        try:
            recent_prs, recent_issues = await asyncio.gather(
                gh.recent_prs(repo, limit=20),
                gh.recent_issues(repo, limit=20),
            )
        except Exception as exc:
            log.warning("sot.gh_recent_failed", error=str(exc))

    sot = SourceOfTruth(
        repo=repo,
        cwd=cwd,
        claude_chain=claude_chain,
        architecture_md=arch,
        readme_md=readme,
        agentic_yml=agentic_yml,
        recent_prs=recent_prs,
        recent_issues=recent_issues,
        last_commits=last_commits,
        agent_context_md=agent_ctx,
        docs_pages=docs_pages,
        adr_summaries=adr_summaries,
    )
    _save_cache(cwd, cache_key, sot)
    return sot
