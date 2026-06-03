"""GitHub client — path-based backend 분기 (HTTP / gh CLI).

분기 규칙 (ADR-013):
  - cwd 가 `~/dev-private/*` 아래  → HTTP backend ({REPO_BASE}_AGENT_PAT 사용)
  - cwd 가 `~/dev/*` 아래            → gh CLI backend (gh CLI 인증 사용)
  - 그 외 (또는 GH_BACKEND env)      → PAT 있으면 HTTP, 없으면 CLI

이전엔 gh CLI 단독이었지만, dev-private (c-yeonwoo 개인 repo) 가 gh CLI 의
ohouse work 인증과 섞이는 문제가 있어 HTTP backend 추가. 두 backend 모두
유지 — 같은 시그니처, 호출부 (agents.py / poller.py 등) 무수정.

PAT 우선순위 (HTTP backend):
  1. `{REPO_BASE_UPPER}_AGENT_PAT` — 예: PALETTE_AGENT_PAT for c-yeonwoo/palette
  2. `GH_TOKEN`
  3. `GITHUB_TOKEN`
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

log = structlog.get_logger()

GITHUB_API_BASE = "https://api.github.com"
_HTTP_TIMEOUT = float(os.environ.get("GH_HTTP_TIMEOUT", "30"))


# ── Types ────────────────────────────────────────────────────────────────────


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
    head_sha: str = ""   # PR cache 무효화 key (head commit SHA)
    updated_at: str = "" # comments_count 와 함께 cache 무효화 보조

    @property
    def label_set(self) -> set[str]:
        return set(self.labels)


# ── Backend 분기 ────────────────────────────────────────────────────────────


def _pat_env_name(repo: str) -> str:
    """c-yeonwoo/palette → PALETTE_AGENT_PAT. 대시는 언더스코어로."""
    base = repo.split("/")[-1]
    return base.upper().replace("-", "_") + "_AGENT_PAT"


def _use_http(repo: str) -> bool:
    """`~/dev-private/*` → HTTP, `~/dev/*` → CLI, 그 외 → PAT 있으면 HTTP.

    cwd 가 `~/dev-private/` 의 하위인지로 판정 (`dev-private` 라는 이름의 디렉토리가
    경로 어딘가에 끼는 false positive 방지).

    GH_BACKEND env override 가능 ('http' | 'cli').
    """
    override = os.environ.get("GH_BACKEND", "").strip().lower()
    if override in ("http", "cli"):
        return override == "http"

    cwd = Path.cwd().resolve()
    home = Path.home().resolve()
    dev_private = home / "dev-private"
    dev = home / "dev"

    try:
        cwd.relative_to(dev_private)
        return True
    except ValueError:
        pass
    try:
        cwd.relative_to(dev)
        return False
    except ValueError:
        pass

    # 두 디렉토리 어느 쪽도 아니면 PAT 존재 여부로
    return bool(os.environ.get(_pat_env_name(repo)))


# ── HTTP backend ─────────────────────────────────────────────────────────────


def _pat_for(repo: str) -> str:
    primary = _pat_env_name(repo)
    pat = os.environ.get(primary)
    if pat:
        return pat
    for fallback in ("GH_TOKEN", "GITHUB_TOKEN"):
        pat = os.environ.get(fallback)
        if pat:
            return pat
    raise RuntimeError(
        f"HTTP backend: PAT 없음 for {repo}. "
        f"{primary} 또는 GH_TOKEN / GITHUB_TOKEN 설정 필요."
    )


def _headers(repo: str, accept: str = "application/vnd.github+json") -> dict:
    return {
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {_pat_for(repo)}",
        "User-Agent": "agentic-harness",
    }


async def _request(
    repo: str,
    method: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[Any] = None,
    accept: str = "application/vnd.github+json",
    expect_404_ok: bool = False,
) -> httpx.Response:
    url = f"{GITHUB_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        r = await client.request(
            method, url, headers=_headers(repo, accept), params=params, json=json_body,
        )
    if r.status_code == 404 and expect_404_ok:
        return r
    if r.status_code >= 400:
        body = r.text[:600]
        log.warning("gh.http_error", method=method, path=path,
                    status=r.status_code, body=body[:300])
        raise RuntimeError(
            f"GitHub API {method} {path} → {r.status_code}: {body[:300]}"
        )
    return r


async def _get_json(repo: str, path: str, params: Optional[dict] = None) -> Any:
    return (await _request(repo, "GET", path, params=params)).json()


async def _post_json(repo: str, path: str, payload: dict) -> Any:
    r = await _request(repo, "POST", path, json_body=payload)
    return r.json() if r.content else {}


async def _patch_json(repo: str, path: str, payload: dict) -> Any:
    r = await _request(repo, "PATCH", path, json_body=payload)
    return r.json() if r.content else {}


async def _delete(repo: str, path: str) -> None:
    await _request(repo, "DELETE", path, expect_404_ok=True)


def _parse_issue_http(raw: dict) -> Issue:
    return Issue(
        number=raw["number"],
        title=raw.get("title", "") or "",
        body=raw.get("body", "") or "",
        labels=[lab["name"] for lab in raw.get("labels", []) if isinstance(lab, dict)],
        assignees=[a["login"] for a in raw.get("assignees", []) if isinstance(a, dict)],
        url=raw.get("html_url", raw.get("url", "")) or "",
    )


def _parse_pr_http(raw: dict) -> PullRequest:
    state = (raw.get("state") or "open").lower()
    merged = bool(raw.get("merged") or raw.get("merged_at"))
    if merged:
        state = "merged"
    head = raw.get("head") or {}
    base = raw.get("base") or {}
    return PullRequest(
        number=raw["number"],
        title=raw.get("title", "") or "",
        body=raw.get("body", "") or "",
        head_ref=head.get("ref", "") if isinstance(head, dict) else "",
        base_ref=base.get("ref", "") if isinstance(base, dict) else "",
        labels=[lab["name"] for lab in raw.get("labels", []) if isinstance(lab, dict)],
        assignees=[a["login"] for a in raw.get("assignees", []) if isinstance(a, dict)],
        state=state,
        merged=merged,
        merged_at=raw.get("merged_at"),
        url=raw.get("html_url", "") or "",
        head_sha=head.get("sha", "") if isinstance(head, dict) else "",
        updated_at=raw.get("updated_at", "") or "",
    )


# ── gh CLI backend ───────────────────────────────────────────────────────────


async def _gh_run(cmd: list[str], input_str: Optional[str] = None) -> str:
    """gh subprocess. stderr 에 오류면 raise."""
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
        log.warning("gh.cli_failed", cmd=cmd, error=msg)
        raise RuntimeError(f"gh: {msg}")
    return stdout.decode()


def _parse_issue_cli(raw: dict) -> Issue:
    return Issue(
        number=raw["number"],
        title=raw.get("title", "") or "",
        body=raw.get("body", "") or "",
        labels=[lab["name"] for lab in raw.get("labels", [])],
        assignees=[a["login"] for a in raw.get("assignees", [])],
        url=raw.get("url", "") or "",
    )


def _parse_pr_cli(raw: dict) -> PullRequest:
    return PullRequest(
        number=raw["number"],
        title=raw.get("title", "") or "",
        body=raw.get("body", "") or "",
        head_ref=raw.get("headRefName", ""),
        base_ref=raw.get("baseRefName", ""),
        labels=[lab["name"] for lab in raw.get("labels", [])],
        assignees=[a["login"] for a in raw.get("assignees", [])],
        state=(raw.get("state") or "open").lower(),
        merged=raw.get("state", "").upper() == "MERGED",
        merged_at=raw.get("mergedAt"),
        url=raw.get("url", "") or "",
        head_sha=raw.get("headRefOid", "") or "",
        updated_at=raw.get("updatedAt", "") or "",
    )


# ── Issues ──────────────────────────────────────────────────────────────────


async def list_issues(
    repo: str,
    label: Optional[str] = None,
    no_label: Optional[str] = None,
    state: str = "open",
    limit: int = 30,
) -> list[Issue]:
    if _use_http(repo):
        params: dict[str, Any] = {"state": state, "per_page": min(limit, 100)}
        if label:
            params["labels"] = label
        data = await _get_json(repo, f"/repos/{repo}/issues", params=params)
        issues_only = [x for x in data if "pull_request" not in x]
        items = [_parse_issue_http(x) for x in issues_only[:limit]]
    else:
        cmd = [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", state,
            "--limit", str(limit),
            "--json", "number,title,body,labels,assignees,url",
        ]
        if label:
            cmd += ["--label", label]
        items = [_parse_issue_cli(x) for x in json.loads(await _gh_run(cmd))]

    if no_label:
        items = [i for i in items if no_label not in i.label_set]
    return items


async def get_issue(repo: str, number: int) -> Issue:
    if _use_http(repo):
        return _parse_issue_http(await _get_json(repo, f"/repos/{repo}/issues/{number}"))
    cmd = [
        "gh", "issue", "view", str(number),
        "--repo", repo,
        "--json", "number,title,body,labels,assignees,url",
    ]
    return _parse_issue_cli(json.loads(await _gh_run(cmd)))


async def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str] = (),
) -> Issue:
    if _use_http(repo):
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        return _parse_issue_http(await _post_json(repo, f"/repos/{repo}/issues", payload))

    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for lab in labels:
        cmd += ["--label", lab]
    out = (await _gh_run(cmd)).strip()
    url = out.splitlines()[-1].strip()
    number = int(url.rstrip("/").split("/")[-1])
    return await get_issue(repo, number)


async def comment_issue(repo: str, number: int, body: str) -> None:
    if _use_http(repo):
        await _post_json(repo, f"/repos/{repo}/issues/{number}/comments", {"body": body})
        return
    await _gh_run([
        "gh", "issue", "comment", str(number),
        "--repo", repo, "--body-file", "-",
    ], input_str=body)


# ── Labels / assignees ──────────────────────────────────────────────────────


async def add_label(repo: str, kind: str, number: int, label: str) -> None:
    if _use_http(repo):
        await _post_json(
            repo, f"/repos/{repo}/issues/{number}/labels", {"labels": [label]},
        )
        return
    await _gh_run([
        "gh", kind, "edit", str(number), "--repo", repo, "--add-label", label,
    ])


async def remove_label(repo: str, kind: str, number: int, label: str) -> None:
    if _use_http(repo):
        from urllib.parse import quote
        await _delete(
            repo, f"/repos/{repo}/issues/{number}/labels/{quote(label, safe='')}",
        )
        return
    await _gh_run([
        "gh", kind, "edit", str(number), "--repo", repo, "--remove-label", label,
    ])


async def assign(repo: str, kind: str, number: int, user: str) -> None:
    if _use_http(repo):
        await _post_json(
            repo, f"/repos/{repo}/issues/{number}/assignees", {"assignees": [user]},
        )
        return
    await _gh_run([
        "gh", kind, "edit", str(number), "--repo", repo, "--add-assignee", user,
    ])


async def unassign(repo: str, kind: str, number: int, user: str) -> None:
    if _use_http(repo):
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.request(
                "DELETE",
                f"{GITHUB_API_BASE}/repos/{repo}/issues/{number}/assignees",
                headers=_headers(repo),
                json={"assignees": [user]},
            )
        if r.status_code >= 400 and r.status_code != 404:
            raise RuntimeError(f"unassign → {r.status_code}: {r.text[:300]}")
        return
    await _gh_run([
        "gh", kind, "edit", str(number), "--repo", repo, "--remove-assignee", user,
    ])


# ── PRs ─────────────────────────────────────────────────────────────────────


async def list_prs(
    repo: str,
    label: Optional[str] = None,
    no_label: Optional[str] = None,
    state: str = "open",
    limit: int = 30,
) -> list[PullRequest]:
    if _use_http(repo):
        if label:
            # /pulls 는 label 필터 미지원 — /issues (PR 포함) 후 detail GET
            params: dict[str, Any] = {
                "state": state, "labels": label, "per_page": min(limit, 100),
            }
            data = await _get_json(repo, f"/repos/{repo}/issues", params=params)
            pr_only = [x for x in data if "pull_request" in x]
            items: list[PullRequest] = []
            for x in pr_only[:limit]:
                try:
                    full = await _get_json(repo, f"/repos/{repo}/pulls/{x['number']}")
                    items.append(_parse_pr_http(full))
                except Exception as exc:
                    log.warning("gh.pr_detail_failed", number=x["number"], error=str(exc))
        else:
            data = await _get_json(
                repo, f"/repos/{repo}/pulls",
                params={"state": state, "per_page": min(limit, 100)},
            )
            items = [_parse_pr_http(x) for x in data[:limit]]
    else:
        cmd = [
            "gh", "pr", "list",
            "--repo", repo,
            "--state", state,
            "--limit", str(limit),
            "--json", "number,title,body,headRefName,headRefOid,baseRefName,labels,assignees,state,mergedAt,url,updatedAt",
        ]
        if label:
            cmd += ["--label", label]
        items = [_parse_pr_cli(x) for x in json.loads(await _gh_run(cmd))]

    if no_label:
        items = [p for p in items if no_label not in p.label_set]
    return items


async def get_pr(repo: str, number: int) -> PullRequest:
    if _use_http(repo):
        return _parse_pr_http(await _get_json(repo, f"/repos/{repo}/pulls/{number}"))
    cmd = [
        "gh", "pr", "view", str(number),
        "--repo", repo,
        "--json", "number,title,body,headRefName,headRefOid,baseRefName,labels,assignees,state,mergedAt,url,updatedAt",
    ]
    return _parse_pr_cli(json.loads(await _gh_run(cmd)))


async def comment_pr(repo: str, number: int, body: str) -> None:
    if _use_http(repo):
        # PR comments are issue comments
        await _post_json(repo, f"/repos/{repo}/issues/{number}/comments", {"body": body})
        return
    await _gh_run([
        "gh", "pr", "comment", str(number),
        "--repo", repo, "--body-file", "-",
    ], input_str=body)


async def close_pr(repo: str, number: int, comment: Optional[str] = None) -> None:
    if comment:
        try:
            await comment_pr(repo, number, comment)
        except Exception as exc:
            log.warning("gh.close_pr_comment_failed", error=str(exc))

    if _use_http(repo):
        await _patch_json(repo, f"/repos/{repo}/pulls/{number}", {"state": "closed"})
        return
    await _gh_run(["gh", "pr", "close", str(number), "--repo", repo])


async def reopen_issue(repo: str, number: int) -> None:
    if _use_http(repo):
        await _patch_json(repo, f"/repos/{repo}/issues/{number}", {"state": "open"})
        return
    await _gh_run(["gh", "issue", "reopen", str(number), "--repo", repo])


async def pr_diff(repo: str, number: int, max_bytes: int = 80_000) -> str:
    if _use_http(repo):
        r = await _request(
            repo, "GET", f"/repos/{repo}/pulls/{number}",
            accept="application/vnd.github.v3.diff",
        )
        out = r.text
    else:
        out = await _gh_run(["gh", "pr", "diff", str(number), "--repo", repo])
    if len(out) > max_bytes:
        return out[:max_bytes] + f"\n... [잘림 — {len(out)} bytes 중 {max_bytes}]\n"
    return out


async def pr_files(repo: str, number: int) -> list[dict]:
    if _use_http(repo):
        data = await _get_json(
            repo, f"/repos/{repo}/pulls/{number}/files",
            params={"per_page": 100},
        )
        return [
            {
                "path": f.get("filename", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "status": f.get("status", ""),
            }
            for f in data
        ]
    out = await _gh_run([
        "gh", "pr", "view", str(number), "--repo", repo, "--json", "files",
    ])
    return json.loads(out).get("files", [])


async def pr_comments(repo: str, number: int, limit: int = 20) -> list[dict]:
    if _use_http(repo):
        data = await _get_json(
            repo, f"/repos/{repo}/issues/{number}/comments",
            params={"per_page": 100},
        )
        return [
            {
                "author": (c.get("user") or {}).get("login", "?"),
                "body": c.get("body", "") or "",
                "createdAt": c.get("created_at", ""),
            }
            for c in data[-limit:]
        ]
    out = await _gh_run([
        "gh", "pr", "view", str(number), "--repo", repo, "--json", "comments",
    ])
    raw = json.loads(out).get("comments", []) or []
    return [
        {
            "author": (c.get("author") or {}).get("login", "?"),
            "body": c.get("body", ""),
            "createdAt": c.get("createdAt", ""),
        }
        for c in raw[-limit:]
    ]


async def pr_linked_issues(repo: str, number: int) -> list[int]:
    """PR body 에서 'Closes #N' / 'Fixes #N' 추출."""
    pr = await get_pr(repo, number)
    pattern = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)
    return [int(m.group(1)) for m in pattern.finditer(pr.body or "")]


async def submit_pr_review(
    repo: str, number: int, *,
    body: str,
    event: str = "COMMENT",       # APPROVE | REQUEST_CHANGES | COMMENT
) -> None:
    """정식 PR review 등록."""
    if _use_http(repo):
        await _post_json(
            repo, f"/repos/{repo}/pulls/{number}/reviews",
            {"body": body, "event": event},
        )
        return
    cmd = ["gh", "pr", "review", str(number), "--repo", repo, "--body-file", "-"]
    if event == "APPROVE":
        cmd.append("--approve")
    elif event == "REQUEST_CHANGES":
        cmd.append("--request-changes")
    else:
        cmd.append("--comment")
    await _gh_run(cmd, input_str=body)


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
    if _use_http(repo):
        payload = {
            "title": title, "body": body, "head": head, "base": base, "draft": draft,
        }
        data = await _post_json(repo, f"/repos/{repo}/pulls", payload)
        pr = _parse_pr_http(data)
        if labels:
            try:
                await _post_json(
                    repo, f"/repos/{repo}/issues/{pr.number}/labels",
                    {"labels": list(labels)},
                )
                return await get_pr(repo, pr.number)
            except Exception as exc:
                log.warning("gh.create_pr_label_failed", number=pr.number, error=str(exc))
        return pr

    cmd = [
        "gh", "pr", "create",
        "--repo", repo, "--title", title, "--body", body,
        "--head", head, "--base", base,
    ]
    if draft:
        cmd.append("--draft")
    for lab in labels:
        cmd += ["--label", lab]
    out = (await _gh_run(cmd)).strip()
    url = out.splitlines()[-1].strip()
    number = int(url.rstrip("/").split("/")[-1])
    return await get_pr(repo, number)


# ── Current user (BOT_USER 식별) ────────────────────────────────────────────


_whoami_cache: Optional[str] = None


async def whoami(repo: Optional[str] = None) -> str:
    """현재 backend 의 user login.

    HTTP backend: PAT 의 user
    CLI backend: gh auth status 의 user
    repo 인자 있으면 그에 맞는 backend 사용.
    """
    global _whoami_cache
    if _whoami_cache:
        return _whoami_cache

    # backend 결정 — repo 있으면 그것 기반, 없으면 cwd 기반
    use_http = _use_http(repo) if repo else (Path.cwd().resolve().parts.__contains__("dev-private"))

    if use_http:
        # PAT 찾기 — repo 있으면 그 PAT, 없으면 GH_TOKEN/GITHUB_TOKEN
        if repo:
            try:
                token = _pat_for(repo)
            except RuntimeError:
                token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        else:
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("whoami: HTTP backend 인데 PAT 없음")
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
            "User-Agent": "agentic-harness",
        }
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(f"{GITHUB_API_BASE}/user", headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(f"whoami → {r.status_code}: {r.text[:300]}")
        login = r.json().get("login")
    else:
        out = (await _gh_run(["gh", "api", "user"])).strip()
        login = json.loads(out).get("login")

    if not login:
        raise RuntimeError("whoami: 응답에 login 없음")
    _whoami_cache = login
    return login


# ── Labels ──────────────────────────────────────────────────────────────────


async def list_labels(repo: str) -> list[str]:
    if _use_http(repo):
        data = await _get_json(repo, f"/repos/{repo}/labels", params={"per_page": 100})
        return [lab["name"] for lab in data]
    out = await _gh_run([
        "gh", "label", "list", "--repo", repo, "--limit", "200", "--json", "name",
    ])
    return [lab["name"] for lab in json.loads(out)]


async def ensure_label(
    repo: str, name: str, color: str = "ededed", description: str = ""
) -> bool:
    """라벨 없으면 생성. 이미 있으면 skip. 생성 시 True."""
    if _use_http(repo):
        payload = {"name": name, "color": color}
        if description:
            payload["description"] = description
        try:
            await _post_json(repo, f"/repos/{repo}/labels", payload)
            log.info("gh.label_created", repo=repo, label=name)
            return True
        except RuntimeError as exc:
            if "already_exists" in str(exc).lower() or "already exists" in str(exc).lower():
                return False
            raise

    try:
        cmd = ["gh", "label", "create", name, "--repo", repo, "--color", color]
        if description:
            cmd += ["--description", description]
        await _gh_run(cmd)
        log.info("gh.label_created", repo=repo, label=name)
        return True
    except RuntimeError as exc:
        if "already exists" in str(exc).lower():
            return False
        raise


# 표준 라벨 정의 — color + description.
# 전체 state machine: ADR-012 (Agent team 재정의) 참조.
STANDARD_LABELS: list[tuple[str, str, str]] = [
    # ADR-014: 워크플로우 라벨은 항상 정확히 1개 (배타적). lock 은 assignee 로 직교.
    # ADR-017: SoT 갱신은 urgent (즉시) / batch (주간) 2-tier.
    ("ah:needs-execution", "fbca04", "developer 대기 (issue) — 새 task"),
    ("ah:needs-review",    "0e8a16", "reviewer 대기 (PR)"),
    ("ah:in-debate",       "d4c5f9", "developer amend 대기 (PR) — reviewer push back"),
    ("ah:needs-critique",  "5319e7", "critique final gate 대기 (PR)"),
    ("ah:awaiting-human",  "1d76db", "사람 결정 대기 (merge / escalation)"),
    ("ah:sot-urgent",      "b60205", "SoT 즉시 갱신 (BREAKING / ADR / 큰 구조 변경) — merge 시 PO mode B 즉시 트리거"),
    ("ah:sot-batch",       "fef2c0", "SoT 배치 갱신 큐 (중간 영향) — 주간 cron 에서 5개 이상 모이면 처리"),
]
# Note: ah:sot-pending / ah:sot-done 은 ADR-017 에서 폐기 (구현 전 갈아엎음).
# 옛 라벨이 GitHub repo 에 있어도 무해 (poller 가 사용 안 함).


async def ensure_standard_labels(repo: str) -> dict:
    """STANDARD_LABELS 모두 ensure. 생성/skip 통계 반환."""
    existing = set(await list_labels(repo))
    stats: dict = {"created": [], "existed": []}
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
    if _use_http(repo):
        data = await _get_json(
            repo, f"/repos/{repo}/pulls",
            params={"state": "all", "sort": "created", "direction": "desc",
                    "per_page": min(limit, 100)},
        )
        return [
            {
                "number": p["number"],
                "title": p.get("title", ""),
                "state": (p.get("state") or "").lower(),
                "labels": [l["name"] for l in p.get("labels", []) if isinstance(l, dict)],
                "url": p.get("html_url", ""),
                "mergedAt": p.get("merged_at"),
                "createdAt": p.get("created_at"),
            }
            for p in data[:limit]
        ]

    cmd = [
        "gh", "pr", "list", "--repo", repo,
        "--state", "all", "--limit", str(limit),
        "--json", "number,title,state,labels,url,mergedAt,createdAt",
    ]
    return json.loads(await _gh_run(cmd))


async def recent_issues(repo: str, limit: int = 20) -> list[dict]:
    if _use_http(repo):
        data = await _get_json(
            repo, f"/repos/{repo}/issues",
            params={"state": "all", "sort": "created", "direction": "desc",
                    "per_page": min(limit, 100)},
        )
        issues_only = [i for i in data if "pull_request" not in i]
        return [
            {
                "number": i["number"],
                "title": i.get("title", ""),
                "state": (i.get("state") or "").lower(),
                "labels": [l["name"] for l in i.get("labels", []) if isinstance(l, dict)],
                "url": i.get("html_url", ""),
                "createdAt": i.get("created_at"),
            }
            for i in issues_only[:limit]
        ]

    cmd = [
        "gh", "issue", "list", "--repo", repo,
        "--state", "all", "--limit", str(limit),
        "--json", "number,title,state,labels,url,createdAt",
    ]
    return json.loads(await _gh_run(cmd))
