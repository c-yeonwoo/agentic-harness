"""ah cost 명령 — 누적 비용 집계.

소스:
  1. ~/Library/Logs/agentic-harness/*.out 로그의 `cost=X` 패턴 (structlog 출력)
  2. GitHub PR comments 의 `_cost $X · ... model=Y_` footer (정확함)

소스 1 은 빠른 추정, 소스 2 는 정확.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


LOG_DIR = Path.home() / "Library/Logs/agentic-harness"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_COST_RE = re.compile(r"cost\s*=\s*([\d.]+)")
_MODEL_RE = re.compile(r"model\s*=\s*(\S+)")
_AGENT_RE = re.compile(r"^\s*\d+:\d+:\d+\s+\[\w+\s*\]\s+(\S+)")
_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})")
_FOOTER_RE = re.compile(
    r"_cost \$(\d+\.\d+)\s*·\s*(\d+)\s*in\s*/\s*(\d+)\s*out\s*·\s*model=(\S+?)_"
)


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@dataclass
class CostEntry:
    when: float                  # epoch
    cost_usd: float
    agent: str = ""              # developer / reviewer / po / critique / pr_description / ...
    model: str = ""
    repo: str = ""
    source: str = "log"          # 'log' 또는 'pr_comment'


@dataclass
class CostReport:
    entries: list = field(default_factory=list)
    since: Optional[float] = None    # epoch
    until: Optional[float] = None

    @property
    def total(self) -> float:
        return sum(e.cost_usd for e in self.entries)

    def by_agent(self) -> dict:
        out: dict[str, float] = {}
        for e in self.entries:
            out[e.agent or "(unknown)"] = out.get(e.agent or "(unknown)", 0) + e.cost_usd
        return out

    def by_model(self) -> dict:
        out: dict[str, float] = {}
        for e in self.entries:
            out[e.model or "(unknown)"] = out.get(e.model or "(unknown)", 0) + e.cost_usd
        return out

    def by_day(self) -> dict:
        out: dict[str, float] = {}
        for e in self.entries:
            d = datetime.fromtimestamp(e.when).strftime("%Y-%m-%d")
            out[d] = out.get(d, 0) + e.cost_usd
        return dict(sorted(out.items()))


def _parse_since(spec: str) -> float:
    """'1d' / '1w' / '1h' / ISO date → epoch."""
    spec = spec.strip().lower()
    now = time.time()
    if spec.endswith("h"):
        return now - int(spec[:-1]) * 3600
    if spec.endswith("d"):
        return now - int(spec[:-1]) * 86400
    if spec.endswith("w"):
        return now - int(spec[:-1]) * 7 * 86400
    if spec.endswith("m"):
        return now - int(spec[:-1]) * 30 * 86400
    # ISO date fallback
    try:
        return datetime.fromisoformat(spec).timestamp()
    except Exception:
        raise ValueError(f"unknown since spec: {spec!r}")


def _agent_from_log_key(key: str) -> str:
    """log key (예: 'reviewer.start' / 'developer.amend.pushed') → agent 이름."""
    head = key.split(".")[0]
    return {
        "po": "po",
        "developer": "developer",
        "reviewer": "reviewer",
        "critique": "critique",
        "local_claude": "developer",   # spawn 로그
        "pr_description": "pr_description",
    }.get(head, head)


def from_logs(since_epoch: float, until_epoch: Optional[float] = None) -> list[CostEntry]:
    """로그 파일들에서 cost 추출. ANSI escape 코드 제거 후 regex.

    timestamp 는 로그 라인의 HH:MM:SS + file mtime 의 date 부분 조합.
    """
    entries: list[CostEntry] = []
    if not LOG_DIR.exists():
        return entries
    for p in LOG_DIR.glob("*.out"):
        try:
            file_mtime = p.stat().st_mtime
            file_date = datetime.fromtimestamp(file_mtime).date()
            with p.open(encoding="utf-8", errors="replace") as f:
                for raw in f:
                    line = _strip_ansi(raw)
                    if "cost=" not in line and "cost =" not in line:
                        continue
                    m = _COST_RE.search(line)
                    if not m:
                        continue
                    cost = float(m.group(1))
                    if cost <= 0:
                        continue
                    # 시각 = file_date + HH:MM:SS (시간 매치 없으면 file mtime fallback)
                    tm = _TIME_RE.search(line)
                    if tm:
                        h, mn, s = (int(tm.group(i)) for i in (1, 2, 3))
                        when = datetime.combine(file_date,
                                                datetime.min.time().replace(hour=h, minute=mn, second=s)).timestamp()
                        # 자정 넘어간 라인 보정 — file mtime 보다 미래면 전날
                        if when > file_mtime + 3600:
                            when -= 86400
                    else:
                        when = file_mtime

                    if when < since_epoch:
                        continue
                    if until_epoch and when > until_epoch:
                        continue
                    mm = _MODEL_RE.search(line)
                    model = mm.group(1) if mm else ""
                    am = _AGENT_RE.search(line)
                    agent = _agent_from_log_key(am.group(1)) if am else ""
                    slug = p.stem
                    entries.append(CostEntry(
                        when=when, cost_usd=cost,
                        agent=agent, model=model,
                        repo=slug.replace("-", "/", 1) if "-" in slug else slug,
                        source="log",
                    ))
        except Exception:
            continue
    return entries


async def from_pr_comments(repo: str, since_epoch: float, limit: int = 100) -> list[CostEntry]:
    """GitHub PR comments 의 _cost $X · ... model=Y_ footer 파싱 (정확)."""
    from orchestrator import gh
    entries: list[CostEntry] = []
    try:
        recent_prs = await gh.recent_prs(repo, limit=limit)
    except Exception:
        return entries

    for pr_info in recent_prs:
        pr_n = pr_info["number"]
        try:
            comments = await gh.pr_comments(repo, pr_n, limit=50)
        except Exception:
            continue
        for c in comments:
            body = c.get("body", "") or ""
            for m in _FOOTER_RE.finditer(body):
                cost = float(m.group(1))
                model = m.group(4)
                created = c.get("createdAt", "")
                try:
                    when = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                except Exception:
                    when = time.time()
                if when < since_epoch:
                    continue
                # agent guess from comment body 키워드
                agent = "?"
                for keyword, name in [
                    ("code-reviewer", "reviewer"),
                    ("critique", "critique"),
                    ("PO ", "po"),
                    ("amend", "developer-amend"),
                    ("Generated by agentic-harness developer", "developer"),
                    ("pr-description", "pr_description"),
                ]:
                    if keyword in body:
                        agent = name
                        break
                entries.append(CostEntry(
                    when=when, cost_usd=cost,
                    agent=agent, model=model,
                    repo=repo, source="pr_comment",
                ))
    return entries


def format_report(report: CostReport) -> str:
    lines = []
    since_str = (datetime.fromtimestamp(report.since).strftime("%Y-%m-%d %H:%M")
                 if report.since else "(all)")
    lines.append(f"\n📊 비용 리포트 (since {since_str}) — 총 {len(report.entries)} 이벤트\n")
    lines.append(f"  **합계: ${report.total:.4f}**")
    lines.append("")

    by_agent = report.by_agent()
    if by_agent:
        lines.append("  agent 별:")
        for k, v in sorted(by_agent.items(), key=lambda x: -x[1]):
            lines.append(f"    {k:24} ${v:.4f}")
        lines.append("")

    by_model = report.by_model()
    if by_model:
        lines.append("  model 별:")
        for k, v in sorted(by_model.items(), key=lambda x: -x[1]):
            lines.append(f"    {k:24} ${v:.4f}")
        lines.append("")

    by_day = report.by_day()
    if by_day:
        lines.append("  일별:")
        for k, v in by_day.items():
            lines.append(f"    {k}  ${v:.4f}")
        lines.append("")

    return "\n".join(lines)
