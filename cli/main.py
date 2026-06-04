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


# ── init (all-in-one) ──────────────────────────────────────────────────────


@app.command("init")
def init(
    repo: str = typer.Option(..., "--repo", "-r",
                             help="bucketplace/lore 또는 c-yeonwoo/palette"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", "-c",
                                       help="repo 로컬 경로. 기본 ~/dev-private/<name> 또는 ~/dev/<name>"),
    interval: int = typer.Option(300, "--interval", "-i",
                                 help="launchd cron 주기 (초)"),
    skip_labels: bool = typer.Option(False, "--skip-labels", help="라벨 생성 skip"),
    skip_sot: bool = typer.Option(False, "--skip-sot", help="SoT 부트스트랩 skip"),
    skip_cron: bool = typer.Option(False, "--skip-cron", help="launchd 등록 skip"),
    force_sot: bool = typer.Option(False, "--force-sot",
                                   help="기존 SoT 파일 덮어쓰기 (위험)"),
) -> None:
    """한방 초기 셋업 — 라벨 8개 + SoT 부트스트랩 + macOS launchd cron.

    어느 프로젝트든 이 명령 하나로 agent team 동작 시작 가능.
    각 단계 멱등 — 기존 라벨/파일/cron 있으면 skip.
    """
    if cwd is None:
        name = repo.split("/")[-1]
        for cand in (Path.home() / "dev-private" / name, Path.home() / "dev" / name):
            if (cand / ".git").exists():
                cwd = cand
                break
        if cwd is None:
            log.error("init.no_cwd", hint="--cwd 로 repo 로컬 경로 지정")
            sys.exit(2)

    asyncio.run(_init_impl(
        repo=repo, cwd=cwd, interval=interval,
        skip_labels=skip_labels, skip_sot=skip_sot, skip_cron=skip_cron,
        force_sot=force_sot,
    ))


async def _init_impl(*, repo: str, cwd: Path, interval: int,
                     skip_labels: bool, skip_sot: bool, skip_cron: bool,
                     force_sot: bool) -> None:
    print(f"\n▶ agentic-harness init — {repo}")
    print(f"  cwd: {cwd}")
    print(f"  interval: {interval}s\n")

    # ── 1. 라벨 ────────────────────────────────────────────────────────────
    if skip_labels:
        print("⏭  [1/3] 라벨 생성 skip")
    else:
        print("▶ [1/3] 라벨 (ah:* 8개) ensure …")
        try:
            stats = await gh.ensure_standard_labels(repo)
            if stats["created"]:
                print(f"   ✓ 생성됨 ({len(stats['created'])}): {', '.join(stats['created'])}")
            if stats["existed"]:
                print(f"   - 이미 존재 ({len(stats['existed'])})")
        except Exception as exc:
            print(f"   ❌ 실패: {exc}")
            sys.exit(1)

    # ── 2. SoT 부트스트랩 ──────────────────────────────────────────────────
    if skip_sot:
        print("\n⏭  [2/3] SoT 부트스트랩 skip")
    else:
        print("\n▶ [2/3] SoT 부트스트랩 (claude -p 가 코드베이스 스캔) …")
        if force_sot:
            print("   ⚠ --force-sot — 기존 SoT 파일 덮어쓰기 모드")
        from orchestrator import agents
        result = await agents.run_sot_bootstrap(
            repo=repo, repo_cwd=cwd, force_regenerate=force_sot,
        )
        if not result.get("ok"):
            print(f"   ❌ SoT 부트스트랩 실패: {result.get('error', '?')}")
            print(f"   (라벨/cron 은 계속 진행)")
        else:
            print(f"   ✓ {result.get('summary', '')}")
            det = result.get("detected") or {}
            print(f"   detected: {det.get('language', '?')} / "
                  f"{det.get('framework', '?')} / {det.get('build', '?')}")
            created = result.get("files_created") or []
            skipped = result.get("files_skipped") or []
            updated = result.get("files_updated") or []
            if created:
                print(f"   생성: {', '.join(created)}")
            if updated:
                print(f"   갱신: {', '.join(updated)}")
            if skipped:
                print(f"   skip: {len(skipped)}개 (이미 존재)")
            todos = result.get("todos") or []
            if todos:
                print(f"   TODO ({len(todos)}):")
                for t in todos[:5]:
                    print(f"     - {t}")
                if len(todos) > 5:
                    print(f"     ... ({len(todos)-5}개 더)")
            print(f"   cost: ${result.get('cost_usd', 0):.4f}")

    # ── 3. launchd cron ────────────────────────────────────────────────────
    if skip_cron:
        print("\n⏭  [3/3] launchd 등록 skip")
    else:
        print("\n▶ [3/3] macOS LaunchAgent 등록 …")
        import subprocess
        script = _HARNESS_ROOT / "scripts" / "setup-local-launchd.sh"
        if not script.exists():
            print(f"   ❌ {script} 없음")
            sys.exit(1)
        try:
            rc = subprocess.run(
                ["bash", str(script), repo, str(interval), str(cwd)],
                check=False,
            ).returncode
            if rc != 0:
                print(f"   ❌ launchd 등록 실패 (rc={rc})")
        except Exception as exc:
            print(f"   ❌ launchd 실패: {exc}")

    # ── 마무리 ──────────────────────────────────────────────────────────────
    print(f"\n✓ init 완료. 다음 단계:")
    print(f"")
    print(f"  # 작업 던지기:")
    print(f"  .venv/bin/ah add-task \"<자연어>\" --repo {repo} --dry-run")
    print(f"  (--dry-run 으로 PO 결과 미리보고 OK 면 --dry-run 빼고 실제 생성)")
    print(f"")
    print(f"  # launchd 상태:")
    print(f"  bash scripts/setup-local-launchd.sh --status")
    print(f"")
    print(f"  # 로그:")
    print(f"  tail -f ~/Library/Logs/agentic-harness/{repo.replace('/', '-')}.out")
    print(f"")


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


# ── SoT update (ADR-017) ─────────────────────────────────────────────────────


@app.command("sot-batch")
def sot_batch(
    repo: str = typer.Option(..., "--repo", "-r"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", "-c"),
    threshold: int = typer.Option(5, "--threshold",
                                  help="이 갯수 이상 모이면 batch 처리"),
    force: bool = typer.Option(False, "--force",
                               help="threshold 무시하고 1개 이상이면 처리"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """주간 batch — `ah:sot-batch` 라벨 merged PR 들 모아서 SoT 갱신 PR 1개 생성.

    launchd weekly cron 에서 자동 호출되거나 사람이 수동 트리거.
    """
    if cwd is None:
        name = repo.split("/")[-1]
        for cand in (Path.home() / "dev-private" / name, Path.home() / "dev" / name):
            if (cand / ".git").exists():
                cwd = cand; break
        if cwd is None:
            log.error("sot-batch.no_cwd"); sys.exit(2)
    asyncio.run(_sot_batch_impl(repo, cwd, threshold, force, dry_run))


async def _sot_batch_impl(repo: str, cwd: Path, threshold: int,
                          force: bool, dry_run: bool) -> None:
    from orchestrator import agents

    # ah:sot-batch + merged PR 수집
    prs_raw = await gh.list_prs(repo, label="ah:sot-batch", state="closed", limit=50)
    merged = [p for p in prs_raw if p.merged]
    pr_numbers = [p.number for p in merged]

    print(f"\n▶ ah:sot-batch + merged PRs: {len(pr_numbers)}")
    for p in merged:
        print(f"  - #{p.number} {p.title[:60]}")

    if not pr_numbers:
        print("\n  (큐 비어있음 — 처리할 PR 없음)")
        return

    if len(pr_numbers) < threshold and not force:
        print(f"\n  threshold ({threshold}) 미달 — skip. --force 또는 더 모이길 대기.")
        return

    if dry_run:
        print(f"\n  --dry-run — 위 {len(pr_numbers)} 개 PR 분석 + SoT PR 생성 예정")
        return

    print(f"\n▶ PO mode B (batch) 실행 — {len(pr_numbers)} PR 통합 분석 ...")
    res = await agents.run_po_mode_b(
        repo=repo, repo_cwd=cwd,
        pr_numbers=pr_numbers, mode="batch",
    )

    if not res.get("ok"):
        print(f"\n❌ 실패: {res.get('error')}")
        sys.exit(1)

    if res.get("no_changes"):
        print(f"\n  변경 없음 — SoT 영향 실질 0. 라벨만 정리.")
    else:
        print(f"\n✓ SoT 갱신 PR 생성: #{res.get('pr_number')}")
        print(f"  {res.get('pr_url')}")
        print(f"  files: {', '.join(res.get('files_changed', []))}")

    # 처리된 PR 들의 ah:sot-batch 라벨 제거
    for n in pr_numbers:
        try:
            await gh.remove_label(repo, "pr", n, "ah:sot-batch")
        except Exception:
            pass

    print(f"  cost: ${res.get('cost_usd', 0):.4f} · model: {res.get('model', '')}")
    if res.get("todos"):
        print(f"  TODO:")
        for t in res["todos"][:5]:
            print(f"    - {t}")


@app.command("dashboard")
def dashboard(
    repo: list[str] = typer.Option([], "--repo", "-r",
                                    help="대상 repo (여러 번 가능). 미지정 시 launchd 에서 자동 감지"),
    interval: float = typer.Option(5.0, "--interval", "-i",
                                   help="refresh 주기 (초). default 5"),
) -> None:
    """로컬 agentic-harness 작업 상태 + 로그 + 비용 TUI 대시보드.

    Ctrl-C 로 종료.
    """
    from orchestrator.dashboard import run_dashboard
    asyncio.run(run_dashboard(repos=list(repo) or None, refresh_sec=interval))


@app.command("sot-drift-check")
def sot_drift_check(
    repo: str = typer.Option(..., "--repo", "-r"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", "-c"),
    create_issue: bool = typer.Option(True, "--create-issue/--no-create-issue",
                                      help="drift 발견 시 GitHub issue 자동 생성"),
) -> None:
    """SoT 가 실제 코드 반영하는지 점검 (월 1회 권장, ~$2)."""
    if cwd is None:
        name = repo.split("/")[-1]
        for cand in (Path.home() / "dev-private" / name, Path.home() / "dev" / name):
            if (cand / ".git").exists():
                cwd = cand; break
        if cwd is None:
            log.error("sot-drift-check.no_cwd"); sys.exit(2)
    asyncio.run(_sot_drift_check_impl(repo, cwd, create_issue))


async def _sot_drift_check_impl(repo: str, cwd: Path, create_issue: bool) -> None:
    from orchestrator.runners.local_claude import run_sot_drift_check_local
    from orchestrator.source_of_truth import discover

    print(f"\n▶ SoT drift check — {repo}")
    print(f"  cwd: {cwd}")
    print(f"  점검 중 (claude -p sonnet) ...\n")

    sot = await discover(cwd)
    res = await run_sot_drift_check_local(
        repo_cwd=cwd, repo=repo, sot_prompt=sot.to_prompt(),
    )

    if res.error:
        print(f"❌ 실패: {res.error}")
        print(f"  cost: ${res.cost_usd:.4f}")
        sys.exit(1)

    sev_emoji = {"none": "✅", "minor": "🟡", "major": "🔴"}.get(res.severity, "❓")
    print(f"{sev_emoji} severity: {res.severity}")
    print(f"  {res.summary}\n")

    if res.drifts:
        print(f"  drift {len(res.drifts)}건:")
        for i, d in enumerate(res.drifts, 1):
            print(f"\n  [{i}] {d.get('kind', '?')} ({d.get('severity', '?')})")
            print(f"      {d.get('what', '?')}")
            if d.get("evidence"):
                print(f"      증거: {d['evidence']}")
            if d.get("suggested_fix"):
                print(f"      제안: {d['suggested_fix']}")

    if create_issue and res.create_issue and res.severity != "none":
        try:
            issue = await gh.create_issue(
                repo, title=res.issue_title or f"SoT drift 점검 — {len(res.drifts)}건",
                body=res.issue_body, labels=[],
            )
            print(f"\n✓ Issue 생성됨: #{issue.number}")
            print(f"  {issue.url}")
        except Exception as exc:
            print(f"\n  ⚠ issue 생성 실패: {exc}")
    elif res.severity != "none":
        print(f"\n  (--no-create-issue 또는 create_issue=false — issue 안 만들음)")

    print(f"\n  cost: ${res.cost_usd:.4f} · model: {res.model}")


@app.command("cost")
def cost(
    since: str = typer.Option("1w", "--since", "-s",
                              help="기간 (1d / 1w / 1m / ISO date). default 1주"),
    repo: Optional[str] = typer.Option(None, "--repo", "-r",
                                       help="repo 명시 시 PR comments 도 합산 (정확). 미지정 시 로그만"),
    source: str = typer.Option("auto", "--source",
                               help="auto | log | pr — auto = 로그 + (repo 있으면) PR"),
) -> None:
    """누적 비용 집계 — 로그 파일 + (옵션) GitHub PR comments."""
    from orchestrator import cost_report as cr
    try:
        since_epoch = cr._parse_since(since)
    except ValueError as exc:
        log.error("cost.bad_since", error=str(exc)); sys.exit(2)
    asyncio.run(_cost_impl(since_epoch, repo, source))


async def _cost_impl(since_epoch: float, repo: Optional[str], source: str) -> None:
    from orchestrator import cost_report as cr
    entries = []
    if source in ("auto", "log"):
        log_entries = cr.from_logs(since_epoch)
        entries.extend(log_entries)
        log.info("cost.log_entries", count=len(log_entries))
    if source in ("auto", "pr") and repo:
        try:
            pr_entries = await cr.from_pr_comments(repo, since_epoch)
            entries.extend(pr_entries)
            log.info("cost.pr_entries", count=len(pr_entries), repo=repo)
        except Exception as exc:
            log.warning("cost.pr_failed", error=str(exc))

    report = cr.CostReport(entries=entries, since=since_epoch)
    print(cr.format_report(report))


@app.command("sot-refresh")
def sot_refresh(
    pr: int = typer.Argument(..., help="대상 PR 번호 (merged 여야 함)"),
    repo: str = typer.Option(..., "--repo", "-r"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", "-c"),
) -> None:
    """단일 PR 즉시 SoT 갱신 — `ah:sot-urgent` PR 의 즉시 트리거 수동 버전."""
    if cwd is None:
        name = repo.split("/")[-1]
        for cand in (Path.home() / "dev-private" / name, Path.home() / "dev" / name):
            if (cand / ".git").exists():
                cwd = cand; break
        if cwd is None:
            log.error("sot-refresh.no_cwd"); sys.exit(2)
    asyncio.run(_sot_refresh_impl(repo, cwd, pr))


async def _sot_refresh_impl(repo: str, cwd: Path, pr_number: int) -> None:
    from orchestrator import agents
    print(f"\n▶ PO mode B (urgent, manual) — PR #{pr_number} ...")
    res = await agents.run_po_mode_b(
        repo=repo, repo_cwd=cwd, pr_numbers=[pr_number], mode="urgent",
    )
    if not res.get("ok"):
        print(f"\n❌ 실패: {res.get('error')}")
        sys.exit(1)
    if res.get("no_changes"):
        print(f"\n  변경 없음 — SoT 영향 실질 0.")
    else:
        print(f"\n✓ SoT 갱신 PR 생성: #{res.get('pr_number')}")
        print(f"  {res.get('pr_url')}")
        print(f"  files: {', '.join(res.get('files_changed', []))}")
    try:
        await gh.remove_label(repo, "pr", pr_number, "ah:sot-urgent")
    except Exception:
        pass
    print(f"  cost: ${res.get('cost_usd', 0):.4f}")


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
