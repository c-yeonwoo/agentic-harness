"""GitHub CLI wrapper — gh 가 이미 인증되어 있어야 동작.

이 모듈은 `gh` subprocess 호출만 함. 직접 GitHub API 호출 (httpx) 은
나중에 GitHub App 으로 갈 때 swap. 지금은 단순성 우선.

모든 함수 async — subprocess 호출이 IO bound 라 asyncio 로 쉽게 병렬화.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger()


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    assignees: list[str]
    url: str

    @property
    def label_set(self) -> set[str]:
        return set(self.labels)


@dataclass
class PullRequest:
    number: int
    title: str
    body: str
    head_ref: str
    base_ref: str
    labels: list[str]
    assignees: list[str]
    state: str           # open / closed / merged
    merged: bool
    merged_at: Optional[str]
    url: str

    @property
    def label_set(self) -> set[str]:
        return set(self.labels)


# ── Internals ───────────────────────────────────────────────────────────────


async def _run(cmd: list[str], input_str: Optional[str] = None) -> str:
    """gh subprocess 실행. stderr 에 오류 있으면 raise."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_str else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(
        input=input_str.encode() if input_str else None
    )
    if proc.returncode != 0:
        msg = stderr.decode().strip() or "gh CLI 실패"
        log.warning("gh.call_failed", cmd=cmd, error=msg)
        raise RuntimeError(f"gh: {msg}")
    return stdout.decode()


def _parse_issue(raw: dict) -> Issue:
    return Issue(
        number=raw["number"],
        title=raw.get("title", ""),
        body=raw.get("body", ""),
        labels=[lab["name"] for lab in raw.get("labels", [])],
        assignees=[a["login"] for a in raw.get("assignees", [])],
        url=raw.get("url", ""),
    )


def _parse_pr(raw: dict) -> PullRequest:
    return PullRequest(
        number=raw["number"],
        title=raw.get("title", ""),
        body=raw.get("body", ""),
        head_ref=raw.get("headRefName", ""),
        base_ref=raw.get("baseRefName", ""),
        labels=[lab["name"] for lab in raw.get("labels", [])],
        assignees=[a["login"] for a in raw.get("assignees", [])],
        state=raw.get("state", "OPEN").lower(),
        merged=raw.get("state", "").upper() == "MERGED",
        merged_at=raw.get("mergedAt"),
        url=raw.get("url", ""),
    )


# ── Issues ──────────────────────────────────────────────────────────────────


async def list_issues(
    repo: str,
    label: Optional[str] = None,
    no_label: Optional[str] = None,
    state: str = "open",
    limit: int = 30,
) -> list[Issue]:
    """label 매칭 issue 목록. no_label 은 제외 필터.

    gh issue list 자체가 `--label X` 만 지원 (negation X) 이라
    `no_label` 은 client-side 필터.
    """
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", state,
        "--limit", str(limit),
        "--json", "number,title,body,labels,assignees,url",
    ]
    if label:
        cmd += ["--label", label]
    raw = json.loads(await _run(cmd))
    items = [_parse_issue(x) for x in raw]
    if no_label:
        items = [i for i in items if no_label not in i.label_set]
    return items


async def get_issue(repo: str, number: int) -> Issue:
    cmd = [
        "gh", "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,body,labels,assignees,url",
    ]
    return _parse_issue(json.loads(await _run(cmd)))


async def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str] = (),
) -> Issue:
    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ]
    for lab in labels:
        cmd += ["--label", lab]
    out = (await _run(cmd)).strip()
    # gh issue create 는 마지막 줄에 URL 만 출력
    url = out.splitlines()[-1].strip()
    number = int(url.rstrip("/").split("/")[-1])
    return await get_issue(repo, number)


async def comment_issue(repo: str, number: int, body: str) -> None:
    await _run([
        "gh", "issue", "comment", str(number),
        "--repo", repo,
        "--body-file", "-",
    ], input_str=body)


# ── Labels / assignees ──────────────────────────────────────────────────────


async def add_label(repo: str, kind: str, number: int, label: str) -> None:
    """kind: issue | pr"""
    await _run([
        "gh", kind, "edit", str(number),
        "--repo", repo,
        "--add-label", label,
    ])


async def remove_label(repo: str, kind: str, number: int, label: str) -> None:
    await _run([
        "gh", kind, "edit", str(number),
        "--repo", repo,
        "--remove-label", label,
    ])


async def assign(repo: str, kind: str, number: int, user: str) -> None:
    await _run([
        "gh", kind, "edit", str(number),
        "--repo", repo,
        "--add-assignee", user,
    ])


async def unassign(repo: str, kind: str, number: int, user: str) -> None:
    await _run([
        "gh", kind, "edit", str(number),
        "--repo", repo,
        "--remove-assignee", user,
    ])


# ── PRs ─────────────────────────────────────────────────────────────────────


async def list_prs(
    repo: str,
    label: Optional[str] = None,
    no_label: Optional[str] = None,
    state: str = "open",
    limit: int = 30,
) -> list[PullRequest]:
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", state,
        "--limit", str(limit),
        "--json", "number,title,body,headRefName,baseRefName,labels,assignees,state,mergedAt,url",
    ]
    if label:
        cmd += ["--label", label]
    raw = json.loads(await _run(cmd))
    items = [_parse_pr(x) for x in raw]
    if no_label:
        items = [p for p in items if no_label not in p.label_set]
    return items


async def get_pr(repo: str, number: int) -> PullRequest:
    cmd = [
        "gh", "pr", "view", str(number),
        "--repo", repo,
        "--json", "number,title,body,headRefName,baseRefName,labels,assignees,state,mergedAt,url",
    ]
    return _parse_pr(json.loads(await _run(cmd)))


async def comment_pr(repo: str, number: int, body: str) -> None:
    await _run([
        "gh", "pr", "comment", str(number),
        "--repo", repo,
        "--body-file", "-",
    ], input_str=body)


async def close_pr(repo: str, number: int, comment: Optional[str] = None) -> None:
    """PR close. comment 있으면 close 직전에 부착."""
    if comment:
        try:
            await comment_pr(repo, number, comment)
        except Exception as exc:
            log.warning("gh.close_pr_comment_failed", error=str(exc))
    await _run(["gh", "pr", "close", str(number), "--repo", repo])


async def reopen_issue(repo: str, number: int) -> None:
    """이미 closed 된 issue 다시 열기 (재트리거 용)."""
    await _run(["gh", "issue", "reopen", str(number), "--repo", repo])


async def pr_diff(repo: str, number: int, max_bytes: int = 80_000) -> str:
    """PR unified diff. 너무 크면 잘림."""
    out = await _run(["gh", "pr", "diff", str(number), "--repo", repo])
    if len(out) > max_bytes:
        return out[:max_bytes] + f"\n... [잘림 — {len(out)} bytes 중 {max_bytes}]\n"
    return out


async def pr_files(repo: str, number: int) -> list[dict]:
    """PR 변경 파일 메타 — additions / deletions / path."""
    out = await _run([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "files",
    ])
    return json.loads(out).get("files", [])


async def pr_comments(repo: str, number: int, limit: int = 20) -> list[dict]:
    """PR 의 사람/봇 코멘트들 — reviewer 가 사람 피드백 보게 하는 용도.

    Returns [{author, body, createdAt}, ...] (최신순 → 오래된 순).
    bot 자신이 단 코멘트도 포함 — 이전 review 결과를 다음 review 가 참고.
    """
    out = await _run([
        "gh", "pr", "view", str(number), "--repo", repo,
        "--json", "comments",
    ])
    raw = json.loads(out).get("comments", []) or []
    items = []
    for c in raw[-limit:]:
        items.append({
            "author": (c.get("author") or {}).get("login", "?"),
            "body": c.get("body", ""),
            "createdAt": c.get("createdAt", ""),
        })
    return items


async def pr_linked_issues(repo: str, number: int) -> list[int]:
    """PR body 에서 'Closes #N' / 'Fixes #N' 추출."""
    pr = await get_pr(repo, number)
    import re
    pattern = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
    return [int(m.group(1)) for m in pattern.finditer(pr.body or "")]


async def submit_pr_review(
    repo: str, number: int, *,
    body: str,
    event: str = "COMMENT",       # APPROVE | REQUEST_CHANGES | COMMENT
) -> None:
    """정식 GitHub PR review 등록 (단순 comment 가 아닌)."""
    cmd = ["gh", "pr", "review", str(number), "--repo", repo, "--body-file", "-"]
    if event == "APPROVE":
        cmd.append("--approve")
    elif event == "REQUEST_CHANGES":
        cmd.append("--request-changes")
    else:
        cmd.append("--comment")
    await _run(cmd, input_str=body)


async def create_pr(
    repo: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
    labels: list[str] = (),
    draft: bool = False,
) -> PullRequest:
    cmd = [
        "gh", "pr", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
        "--head", head,
        "--base", base,
    ]
    if draft:
        cmd.append("--draft")
    for lab in labels:
        cmd += ["--label", lab]
    out = (await _run(cmd)).strip()
    # gh pr create 마지막 줄에 URL
    url = out.splitlines()[-1].strip()
    number = int(url.rstrip("/").split("/")[-1])
    return await get_pr(repo, number)


# ── Current user (BOT_USER 식별) ────────────────────────────────────────────


async def whoami() -> str:
    out = (await _run(["gh", "api", "user"])).strip()
    return json.loads(out)["login"]


# ── Labels ──────────────────────────────────────────────────────────────────


async def list_labels(repo: str) -> list[str]:
    """현재 repo의 라벨 이름 목록."""
    out = await _run([
        "gh", "label", "list", "--repo", repo, "--limit", "200",
        "--json", "name",
    ])
    return [lab["name"] for lab in json.loads(out)]


async def ensure_label(
    repo: str, name: str, color: str = "ededed", description: str = ""
) -> bool:
    """라벨 없으면 생성. 이미 있으면 skip. 생성 시 True."""
    try:
        cmd = [
            "gh", "label", "create", name,
            "--repo", repo,
            "--color", color,
        ]
        if description:
            cmd += ["--description", description]
        await _run(cmd)
        log.info("gh.label_created", repo=repo, label=name)
        return True
    except RuntimeError as exc:
        # gh label create 가 이미 존재 시 stderr 에 "already exists" 출력
        if "already exists" in str(exc).lower():
            return False
        raise


# 표준 라벨 정의 — color + description.
# `ah init-labels` 와 add-task 자동 호출 시 사용.
STANDARD_LABELS: list[tuple[str, str, str]] = [
    # 모두 ah: prefix — GitHub default 라벨과 명확히 구별.
    ("ah:needs-execution", "fbca04", "code-executor 대기 (issue)"),
    ("ah:needs-review",    "0e8a16", "code-reviewer 대기 (PR)"),
    ("ah:awaiting-human",  "1d76db", "사람 결정 대기 (merge / ADR 포함, PR)"),
    ("ah:in-progress",     "c5def5", "agent 가 처리 중 (락 — issue/PR)"),
]
# Note:
# - agent:po/executor/reviewer/sot-manager 는 PR body 푸터 / review 헤더로 충분.
# - need_adr 은 awaiting-human 에 통합. ADR 필요 여부는 reviewer comment 헤더에 표시.


async def ensure_standard_labels(repo: str) -> dict:
    """STANDARD_LABELS 모두 ensure. 생성/skip 통계 반환."""
    existing = set(await list_labels(repo))
    stats = {"created": [], "existed": []}
    for name, color, desc in STANDARD_LABELS:
        if name in existing:
            stats["existed"].append(name)
            continue
        try:
            await ensure_label(repo, name, color, desc)
            stats["created"].append(name)
        except RuntimeError as exc:
            log.warning("gh.label_ensure_failed", label=name, error=str(exc))
    return stats


# ── Recent activity (source-of-truth 용) ────────────────────────────────────


async def recent_prs(repo: str, limit: int = 20) -> list[dict]:
    """최근 PR — title / state / merged / labels / url 만. (PR 상세 안 가져옴)"""
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", str(limit),
        "--json", "number,title,state,labels,url,mergedAt,createdAt",
    ]
    return json.loads(await _run(cmd))


async def recent_issues(repo: str, limit: int = 20) -> list[dict]:
    cmd = [
        "gh", "issue", "list",
        "--repo", repo,
        "--state", "all",
        "--limit", str(limit),
        "--json", "number,title,state,labels,url,createdAt",
    ]
    return json.loads(await _run(cmd))
