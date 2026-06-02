"""CLI entry — `ah add-task` / `ah run` / `ah status`."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

import structlog
import typer
from anthropic import APIError

# .env 자동 로딩 — 의존성 줄이려고 직접 파싱
def _load_env(path: Path) -> None:
    """.env 파일 로드. 환경변수가 이미 있어도 .env 값이 비어있지 않으면 덮어씀.

    setdefault 가 안 되는 이유: claude-code 가 ANTHROPIC_API_KEY='' (빈 문자열)
    를 미리 export 한 환경에서 ah 가 실행되면, setdefault 는 빈 값을
    "이미 설정됨" 으로 보고 .env 의 진짜 값을 무시함.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        # 따옴표로 감싸진 값 처리 (e.g. KEY="value with spaces")
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            v = v[1:-1]
        if v:                                    # 빈 값은 skip (기존 환경변수 보존)
            os.environ[k] = v


_HARNESS_ROOT = Path(__file__).resolve().parent.parent
_load_env(_HARNESS_ROOT / ".env")

# structlog 기본 설정
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(),
    ],
)
log = structlog.get_logger()


from orchestrator import gh
from orchestrator.poller import poll_once, run_forever


app = typer.Typer(help="Agentic Harness — GitHub label 기반 워크플로우")


# ── add-task ────────────────────────────────────────────────────────────────


@app.command("add-task")
def add_task(
    description: str = typer.Argument(..., help="task 설명 (자유 텍스트, 한국어 OK)"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r",
                                       help="bucketplace/lore. 미지정 시 cwd git remote"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", "-c",
                                       help="repo 로컬 경로 (PO 가 SoT 읽음). 기본 ~/dev-private/<repo_name>"),
    raw: bool = typer.Option(False, "--raw",
                             help="PO 안 거치고 description 그대로 raw issue 생성"),
    title: Optional[str] = typer.Option(None, "--title", "-t",
                                        help="(--raw 시) issue 제목. 없으면 description 첫 줄"),
    label: list[str] = typer.Option(["ah:needs-execution"], "--label", "-l",
                                     help="(--raw 시) 라벨. 기본 ah:needs-execution"),
    dry_run: bool = typer.Option(False, "--dry-run",
                                 help="PO 결과만 출력하고 issue 안 만들기"),
) -> None:
    """task → PO 가 SoT 보고 1~N 개 issue 로 분할 생성.

    --raw 옵션 시 PO 안 거치고 description 그대로 issue 1개 만들기 (기존 동작).
    """
    asyncio.run(_add_task_impl(
        description=description, repo=repo, cwd=cwd, raw=raw,
        title=title, labels=list(label), dry_run=dry_run,
    ))


async def _add_task_impl(*, description: str, repo: Optional[str],
                         cwd: Optional[Path], raw: bool,
                         title: Optional[str], labels: list[str],
                         dry_run: bool) -> None:
    from orchestrator.source_of_truth import _parse_git_remote, discover
    if repo is None:
        repo = _parse_git_remote(Path.cwd())
        if not repo:
            log.error("add-task.no_repo",
                      hint="cwd 가 git repo 아님 — --repo 옵션 지정")
            sys.exit(2)

    # raw 경로 — 기존 동작 (description → issue 1개)
    if raw:
        try:
            stats = await gh.ensure_standard_labels(repo)
            if stats["created"]:
                log.info("add-task.labels_created", repo=repo, labels=stats["created"])
        except Exception as exc:
            log.warning("add-task.labels_ensure_failed", error=str(exc))

        if title is None:
            title = description.splitlines()[0].strip()[:80]
        body = description.rstrip() + "\n\n---\n_Created via_ `ah add-task --raw`"
        try:
            issue = await gh.create_issue(repo, title=title, body=body, labels=labels)
        except RuntimeError as exc:
            log.error("add-task.failed", error=str(exc))
            sys.exit(1)
        print(f"\n✓ Issue 생성됨 (raw): #{issue.number}")
        print(f"  {issue.url}")
        print(f"  labels: {', '.join(issue.labels)}")
        return

    # PO 경로 — SoT 읽고 1~N issue 분할
    from orchestrator import agents

    if cwd is None:
        name = repo.split("/")[-1]
        for cand in (Path.home() / "dev-private" / name, Path.home() / "dev" / name):
            if (cand / ".git").exists():
                cwd = cand
                break
        if cwd is None:
            log.error("add-task.no_cwd",
                      hint="--cwd 로 repo 로컬 경로 지정. PO 가 SoT 읽으려면 필요")
            sys.exit(2)

    log.info("add-task.po_mode", repo=repo, cwd=str(cwd), dry_run=dry_run)
    print(f"\n▶ PO (mode A) 가 SoT 읽고 task 분석 중 …")

    sot = await discover(cwd)
    result = await agents.run_po_local(
        repo=repo, user_agenda=description, sot=sot,
        repo_cwd=cwd, dry_run=dry_run,
    )

    cost = result.get("cost_usd", 0.0)
    model = result.get("model", "")

    if not result.get("ok"):
        print(f"\n❌ PO 실패: {result.get('error', 'unknown')}")
        if result.get("summary"):
            print(f"   summary: {result['summary']}")
        print(f"   cost: ${cost:.4f}")
        sys.exit(1)

    if dry_run:
        print(f"\n✓ PO dry-run — {len(result.get('issues_preview', []))} 개 issue 후보 (생성 X)")
        print(f"  summary: {result.get('summary')}")
        print(f"  split: {result.get('split_rationale')}")
        for i, it in enumerate(result.get("issues_preview", []), 1):
            adr_mark = " 🏛" if it.get("needs_adr") else ""
            print(f"\n  [{i}] {it['title']}{adr_mark}")
            print(f"      labels: {', '.join(it['labels'])}")
            print(f"      body length: {len(it['body'])} chars")
        print(f"\n  cost: ${cost:.4f} · model: {model}")
        return

    created = result.get("created", [])
    summary = result.get("summary", "")
    split_rationale = result.get("split_rationale", "")

    print(f"\n✓ PO — {len([c for c in created if c.get('number')])} 개 issue 생성")
    print(f"  summary: {summary}")
    if split_rationale and split_rationale != summary:
        print(f"  split: {split_rationale}")
    for c in created:
        if c.get("number"):
            print(f"\n  #{c['number']} {c['title']}")
            print(f"      {c['url']}")
        else:
            print(f"\n  ❌ '{c['title']}' 생성 실패: {c.get('error', '?')}")
    print(f"\n  cost: ${cost:.4f} · model: {model}")


# ── run ─────────────────────────────────────────────────────────────────────


@app.command("run")
def run(
    repo: str = typer.Option(..., "--repo", "-r",
                             help="bucketplace/lore"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", "-c",
                                       help="source-of-truth 컨텍스트 cwd. 기본 ~/dev/{repo_name}"),
    interval: int = typer.Option(30, "--interval", "-i",
                                 help="폴링 간격 (초)"),
    once: bool = typer.Option(False, "--once",
                              help="1회 poll 후 종료 (cron / GitHub Actions 용)"),
    mode: Optional[str] = typer.Option(None, "--mode", "-m",
                                       help="hermes | local. 미지정 시 HARNESS_MODE env, 그것도 없으면 hermes"),
) -> None:
    """orchestrator daemon — `ah:needs-execution` issue 발견 시 code-executor 실행."""
    if cwd is None:
        # bucketplace/lore → ~/dev/lore
        name = repo.split("/")[-1]
        cwd = Path.home() / "dev" / name

    if not cwd.exists():
        log.error("run.cwd_not_found", cwd=str(cwd),
                  hint="--cwd 옵션으로 명시")
        sys.exit(2)

    if mode:
        os.environ["HARNESS_MODE"] = mode.strip().lower()
        log.info("run.mode_override", mode=os.environ["HARNESS_MODE"])

    asyncio.run(_run_impl(repo, cwd, interval, once))


async def _run_impl(repo: str, cwd: Path, interval: int, once: bool) -> None:
    try:
        if once:
            bot_user = await gh.whoami(repo)
            stats = await poll_once(repo, cwd, bot_user)
            print(f"\n✓ poll once 완료: {stats}")
        else:
            await run_forever(repo, cwd, interval)
    except APIError as exc:
        log.error("run.api_error", error=str(exc))
        sys.exit(1)


# ── init-labels ─────────────────────────────────────────────────────────────


@app.command("init-labels")
def init_labels(
    repo: str = typer.Option(..., "--repo", "-r",
                             help="bucketplace/lore"),
) -> None:
    """표준 라벨 (ah:needs-execution / ah:needs-review / ah:awaiting-human / ah:in-progress) 일괄 생성."""
    asyncio.run(_init_labels_impl(repo))


async def _init_labels_impl(repo: str) -> None:
    try:
        stats = await gh.ensure_standard_labels(repo)
    except Exception as exc:
        log.error("init-labels.failed", error=str(exc))
        sys.exit(1)
    print(f"\n## {repo} — 라벨 동기화\n")
    if stats["created"]:
        print(f"  ✓ 생성됨 ({len(stats['created'])}):")
        for n in stats["created"]:
            print(f"    - {n}")
    if stats["existed"]:
        print(f"  - 이미 존재 ({len(stats['existed'])}): {', '.join(stats['existed'])}")
    print()


# ── inspect ─────────────────────────────────────────────────────────────────


@app.command("inspect")
def inspect(
    issue: int = typer.Argument(..., help="issue 또는 PR 번호"),
    repo: str = typer.Option(..., "--repo", "-r"),
    kind: str = typer.Option("issue", "--kind", "-k", help="issue | pr"),
) -> None:
    """issue/PR 상태 + 최근 comment 표시 — agent 결과 디버깅 용."""
    asyncio.run(_inspect_impl(repo, issue, kind))


async def _inspect_impl(repo: str, number: int, kind: str) -> None:
    # gh CLI 직접 호출 — 코멘트 + 메타 한번에
    import subprocess, json as _json
    try:
        out = subprocess.check_output([
            "gh", kind, "view", str(number),
            "--repo", repo,
            "--json", "title,body,labels,assignees,state,url,comments",
        ], text=True)
        data = _json.loads(out)
    except subprocess.CalledProcessError as exc:
        print(f"❌ {exc}")
        sys.exit(1)

    print(f"\n## #{number}: {data['title']}")
    print(f"   {data['url']}")
    print(f"   state: {data['state']}")
    labs = [l["name"] for l in data.get("labels", [])]
    print(f"   labels: {', '.join(labs) or '(none)'}")
    asg = [a["login"] for a in data.get("assignees", [])]
    print(f"   assignees: {', '.join(asg) or '(none)'}")
    print()
    print("### Body")
    print(data.get("body", "(empty)") or "(empty)")
    print()
    comments = data.get("comments", [])
    print(f"### Comments ({len(comments)})")
    for c in comments[-5:]:                        # 최근 5개
        author = c.get("author", {}).get("login", "?")
        body = c.get("body", "")
        print(f"\n  --- {author} ---")
        print("  " + body.replace("\n", "\n  "))
    print()


# ── status ──────────────────────────────────────────────────────────────────


@app.command("status")
def status(
    repo: str = typer.Option(..., "--repo", "-r"),
) -> None:
    """현재 라벨별 task 수."""
    asyncio.run(_status_impl(repo))


async def _status_impl(repo: str) -> None:
    labels = ["ah:needs-execution", "ah:needs-review", "ah:awaiting-human"]
    print(f"\n## {repo}\n")
    for lab in labels:
        try:
            issues = await gh.list_issues(repo, label=lab, limit=50)
            prs = await gh.list_prs(repo, label=lab, limit=50)
            in_progress = sum(1 for i in issues + prs if "ah:in-progress" in i.labels)
            print(f"  {lab:22} issue={len(issues):3}  pr={len(prs):3}  in-progress={in_progress}")
        except Exception as exc:
            print(f"  {lab:22} (err: {exc})")
    print()


if __name__ == "__main__":
    app()
