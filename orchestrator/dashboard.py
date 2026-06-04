"""ah dashboard — CLI TUI 대시보드 (rich Live).

화면 구성 (4 패널):
  ┌─ Header ─────────────────────────────┐
  │ launchd 상태 (table)                  │
  ├──────────────────────────────────────┤
  │ PR 큐 (repo 별, table)                │
  ├──────────────────────────────────────┤
  │ 최근 로그 (color)  │  Cost 요약        │
  └──────────────────────────────────────┘

5초마다 자동 refresh. Ctrl-C 로 종료.

데이터 소스:
  - launchctl print / list
  - GitHub API (gh.list_prs by label)
  - ~/Library/Logs/agentic-harness/*.out (tail, ANSI strip)
  - cost_report (로그 파싱)
  - psutil-less ps (subprocess) — claude/ah 살아있는지
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


_LOG_DIR = Path.home() / "Library/Logs/agentic-harness"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LOG_LINE_RE = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+\[(\w+)\s*\]\s+(\S+)\s*(.*)$")

_LABELS_BY_INTEREST = [
    "ah:needs-execution",
    "ah:needs-review",
    "ah:in-debate",
    "ah:needs-critique",
    "ah:sot-urgent",
    "ah:sot-batch",
    "ah:awaiting-human",
]

_AGENT_COLORS = {
    "po": "yellow",
    "developer": "cyan",
    "reviewer": "green",
    "critique": "magenta",
    "pr_description": "blue",
    "local_claude": "cyan",
    "poll": "white",
    "sot": "yellow",
}


def _strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


# ── 데이터 수집 ─────────────────────────────────────────────────────────────


def _list_launchd_jobs() -> list[dict]:
    """등록된 agentic-harness launchd 작업 목록."""
    try:
        out = subprocess.check_output(
            ["launchctl", "list"], text=True, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    jobs = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, exit_code, label = parts[0], parts[1], parts[2]
        if "agentic-harness" not in label:
            continue
        slug = label.removeprefix("com.agentic-harness.")

        # plist 의 ProgramArguments 에서 --repo 정확히 추출
        repo = slug  # fallback
        next_fire = ""
        try:
            user_id = os.getuid()
            info = subprocess.check_output(
                ["launchctl", "print", f"gui/{user_id}/{label}"],
                text=True, stderr=subprocess.DEVNULL,
                timeout=3,
            )
            # ProgramArguments 라인들 — "--repo" 뒤 인자가 repo
            lines = info.splitlines()
            for i, ln in enumerate(lines):
                ln_strip = ln.strip()
                if ln_strip == "--repo" and i + 1 < len(lines):
                    repo = lines[i + 1].strip()
                if ln_strip.startswith("next fire ="):
                    next_fire = ln_strip.split("=", 1)[-1].strip()[:30]
        except Exception:
            pass

        jobs.append({
            "label": label,
            "slug": slug,
            "repo": repo,
            "pid": pid,
            "exit": exit_code,
            "running": pid != "-" and pid.isdigit(),
            "next_fire": next_fire,
        })
    return jobs


def _tail_log_lines(slug: str, n: int = 25) -> list[str]:
    """로그 마지막 n 라인. ANSI 제거."""
    fp = _LOG_DIR / f"{slug}.out"
    if not fp.exists():
        return []
    try:
        out = subprocess.check_output(
            ["tail", "-n", str(n), str(fp)], text=True, stderr=subprocess.DEVNULL,
        )
        return [_strip_ansi(ln).rstrip() for ln in out.splitlines() if ln.strip()]
    except Exception:
        return []


def _proc_status() -> dict:
    """claude / ah 프로세스 상태."""
    out = {"claude": [], "ah": []}
    try:
        ps_out = subprocess.check_output(
            ["ps", "-eo", "pid,etime,comm,args"], text=True,
        )
    except Exception:
        return out
    for line in ps_out.splitlines()[1:]:
        if "claude -p" in line:
            parts = line.split(None, 3)
            if len(parts) >= 3:
                out["claude"].append({"pid": parts[0], "etime": parts[1]})
        elif ".venv/bin/ah" in line or "/ah run" in line:
            parts = line.split(None, 3)
            if len(parts) >= 3:
                out["ah"].append({"pid": parts[0], "etime": parts[1]})
    return out


async def _pr_labels_for_repo(repo: str) -> dict:
    """repo 의 라벨 별 PR 갯수."""
    from orchestrator import gh
    out: dict[str, list] = {}
    for label in _LABELS_BY_INTEREST:
        try:
            prs = await gh.list_prs(repo, label=label, state="open", limit=10)
            if prs:
                out[label] = [
                    {
                        "number": p.number,
                        "title": p.title[:50],
                        "assignees": p.assignees,
                    }
                    for p in prs
                ]
        except Exception:
            continue
    return out


def _cost_summary() -> dict:
    """오늘/주간 누적 비용."""
    from orchestrator import cost_report as cr
    now = time.time()
    today_start = datetime.fromtimestamp(now).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    week_start = now - 7 * 86400

    today = cr.from_logs(today_start)
    week = cr.from_logs(week_start)

    return {
        "today_usd": sum(e.cost_usd for e in today),
        "today_count": len(today),
        "week_usd": sum(e.cost_usd for e in week),
        "week_count": len(week),
    }


# ── 패널 빌더 ──────────────────────────────────────────────────────────────


def _build_launchd_panel(jobs: list[dict], proc: dict) -> Panel:
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("repo", style="cyan", no_wrap=True)
    table.add_column("상태", justify="center", width=10)
    table.add_column("PID", justify="right", width=8)
    table.add_column("last exit", justify="right", width=10)
    table.add_column("다음 실행", width=20)

    if not jobs:
        table.add_row("[dim](등록된 launchd 없음)[/dim]", "", "", "", "")

    for j in jobs:
        state_color = "green" if j["running"] else "dim"
        state_text = "🟢 running" if j["running"] else "⚪ idle"
        exit_color = "green" if j["exit"] == "0" else "red"
        table.add_row(
            j["repo"],
            f"[{state_color}]{state_text}[/{state_color}]",
            j["pid"],
            f"[{exit_color}]{j['exit']}[/{exit_color}]",
            j["next_fire"] or "[dim]-[/dim]",
        )

    # 추가 process 정보
    proc_info = Text()
    if proc["claude"]:
        proc_info.append(f"  claude -p × {len(proc['claude'])}: ", style="dim")
        for c in proc["claude"][:3]:
            proc_info.append(f"PID {c['pid']} ({c['etime']})  ", style="cyan")
    if proc["ah"]:
        proc_info.append(f"  ah × {len(proc['ah'])}", style="dim")

    content: Group = Group(table, proc_info) if proc["claude"] or proc["ah"] else table
    return Panel(content, title="[bold]launchd[/bold]", border_style="blue")


def _build_pr_queue_panel(repo_labels: dict) -> Panel:
    """라벨 별 PR 표시."""
    table = Table(show_header=True, header_style="bold", expand=True)
    table.add_column("라벨", style="yellow", width=20)
    table.add_column("PR", style="cyan", width=80)

    has_any = False
    for repo, labels_dict in repo_labels.items():
        for label, prs in labels_dict.items():
            for p in prs:
                lock = " 🔒" if p["assignees"] else ""
                title = f"#{p['number']} {p['title']}{lock}"
                if len(repo_labels) > 1:
                    title = f"[{repo}] {title}"
                table.add_row(label, title)
                has_any = True

    if not has_any:
        table.add_row("[dim](처리 대기 작업 없음)[/dim]", "")

    return Panel(table, title="[bold]작업 큐[/bold]", border_style="yellow")


def _build_log_panel(slug_log_pairs: list[tuple[str, list[str]]]) -> Panel:
    """최근 로그 (agent 별 색)."""
    text = Text()
    if not slug_log_pairs:
        text.append("(로그 없음)", style="dim")
    for slug, lines in slug_log_pairs:
        if len(slug_log_pairs) > 1:
            text.append(f"── {slug} ──\n", style="bold dim")
        for ln in lines[-12:]:
            m = _LOG_LINE_RE.match(ln)
            if m:
                ts, level, key, rest = m.groups()
                agent = key.split(".")[0]
                color = _AGENT_COLORS.get(agent, "white")
                lvl_color = {"info": "dim", "warning": "yellow", "error": "red"}.get(level, "white")
                text.append(f"{ts} ", style="dim")
                text.append(f"[{level[:4]}] ", style=lvl_color)
                text.append(f"{key:30}", style=color)
                text.append(f" {rest[:80]}\n", style="dim")
            else:
                text.append(f"{ln[:120]}\n", style="dim")
    return Panel(text, title="[bold]최근 로그[/bold]", border_style="green")


def _build_cost_panel(cost: dict) -> Panel:
    budget = float(os.environ.get("SOT_UPDATE_BUDGET_USD_PER_WEEK", "0") or 0)

    table = Table(show_header=False, expand=True, padding=(0, 1))
    table.add_column("", style="bold")
    table.add_column("", justify="right")

    table.add_row("오늘", f"[bold cyan]${cost['today_usd']:.3f}[/bold cyan] ({cost['today_count']} 이벤트)")
    week_color = "yellow" if budget and cost["week_usd"] / budget > 0.8 else "white"
    week_str = f"[{week_color}]${cost['week_usd']:.3f}[/{week_color}] ({cost['week_count']} 이벤트)"
    if budget:
        week_str += f"  / cap ${budget:.2f}"
    table.add_row("이번주", week_str)

    if budget and cost["week_usd"] >= budget:
        table.add_row("[red bold]경고[/red bold]", "[red]budget cap 도달[/red]")

    return Panel(table, title="[bold]비용[/bold]", border_style="magenta")


# ── Layout 조합 ────────────────────────────────────────────────────────────


def _make_layout() -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="launchd", size=10),
        Layout(name="prs", size=10),
        Layout(name="bottom"),
    )
    layout["bottom"].split_row(
        Layout(name="logs", ratio=3),
        Layout(name="cost", ratio=1),
    )
    return layout


def _render_header(repos: list[str]) -> Panel:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    repos_str = " · ".join(repos) if repos else "(repo 없음)"
    text = Text()
    text.append("🤖 Agentic Harness ", style="bold")
    text.append(f"({repos_str})  ", style="cyan")
    text.append(f"{now_str}  ", style="dim")
    text.append("[q] quit  [Ctrl-C] exit", style="dim")
    return Panel(text, border_style="dim")


async def _gather_state(repos: list[str]) -> dict:
    """모든 패널 데이터 한 번에 수집."""
    jobs = _list_launchd_jobs()
    proc = _proc_status()
    cost = _cost_summary()

    # PR labels 병렬
    pr_results = await asyncio.gather(
        *[_pr_labels_for_repo(repo) for repo in repos],
        return_exceptions=True,
    )
    repo_labels: dict[str, dict] = {}
    for repo, res in zip(repos, pr_results):
        if isinstance(res, Exception):
            repo_labels[repo] = {}
        else:
            repo_labels[repo] = res

    # 로그
    slugs = [j["slug"] for j in jobs]
    if not slugs:
        # fallback — 로그 디렉토리 글러브
        slugs = [p.stem for p in _LOG_DIR.glob("*.out")]
    log_pairs = [(slug, _tail_log_lines(slug, 15)) for slug in slugs]

    return {
        "jobs": jobs, "proc": proc, "cost": cost,
        "repo_labels": repo_labels, "log_pairs": log_pairs, "repos": repos,
    }


def _render(state: dict) -> Layout:
    layout = _make_layout()
    layout["header"].update(_render_header(state["repos"]))
    layout["launchd"].update(_build_launchd_panel(state["jobs"], state["proc"]))
    layout["prs"].update(_build_pr_queue_panel(state["repo_labels"]))
    layout["logs"].update(_build_log_panel(state["log_pairs"]))
    layout["cost"].update(_build_cost_panel(state["cost"]))
    return layout


# ── CLI 진입점 ──────────────────────────────────────────────────────────────


async def run_dashboard(
    repos: Optional[list[str]] = None,
    refresh_sec: float = 5.0,
) -> None:
    """대시보드 무한 루프. Ctrl-C 또는 q 로 종료."""
    # repos 자동 감지 — launchd 등록된 거에서
    if not repos:
        jobs = _list_launchd_jobs()
        repos = [j["repo"] for j in jobs] if jobs else []

    if not repos:
        print("등록된 launchd 도 없고 --repo 도 지정 안 됨. 한 가지 필요.")
        print("  bash scripts/setup-local-launchd.sh <repo> 300   # 등록")
        print("  또는: ah dashboard --repo <owner>/<repo>")
        return

    console = Console()
    with Live(_make_layout(), console=console, refresh_per_second=1, screen=True) as live:
        try:
            while True:
                state = await _gather_state(repos)
                live.update(_render(state))
                await asyncio.sleep(refresh_sec)
        except KeyboardInterrupt:
            console.print("\n[dim]대시보드 종료[/dim]")
