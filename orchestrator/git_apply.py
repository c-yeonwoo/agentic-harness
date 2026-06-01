"""LLM plan → 실제 PR 생성.

content-based 형식 (unified diff 아님 — apply 안정성 ↑).
LLM 이 만든 plan.commits[i].files[j] 는:
  { "path": "...", "action": "create|replace|delete", "content": "..." }

흐름:
  1. {repo_cwd}/.git 에서 임시 worktree 분기
  2. action 별로 파일 write / delete
  3. git add + commit
  4. push origin branch
  5. worktree cleanup
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()


class EditApplyError(RuntimeError):
    """edit action 의 old_str 매칭 실패 — agents.py 가 잡아서 retry 큐로 돌림.

    fields:
      path:           실패한 파일 경로 (relative to repo)
      edit_idx:       edits[N] 의 N (1-based)
      message:        사람 읽기용 설명
      old_str_head:   찾으려던 old_str 의 앞 200자 (LLM context 용)
    """
    def __init__(self, path: str, edit_idx: int, message: str, old_str_head: str = ""):
        self.path = path
        self.edit_idx = edit_idx
        self.message = message
        self.old_str_head = old_str_head
        super().__init__(f"{path} edits[{edit_idx}]: {message}")


async def _git(cwd: Path, *args: str, input_str: Optional[str] = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if input_str else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(
        input=input_str.encode() if input_str else None
    )
    if proc.returncode != 0:
        msg = stderr.decode().strip() or stdout.decode().strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg[:600]}")
    return stdout.decode()


async def _detect_default_branch(cwd: Path) -> str:
    try:
        out = await _git(cwd, "symbolic-ref", "refs/remotes/origin/HEAD")
        return out.strip().rsplit("/", 1)[-1]
    except RuntimeError:
        pass
    for cand in ("main", "master", "develop"):
        try:
            await _git(cwd, "rev-parse", "--verify", f"refs/remotes/origin/{cand}")
            return cand
        except RuntimeError:
            continue
    return "main"


def _fuzzy_replace(text: str, old: str, new: str) -> tuple[str, bool]:
    """LLM 이 indent 살짝 틀린 경우 살림.

    1. 줄별로 strip 한 후 match — text 의 슬라이딩 윈도우에서 strip 매칭
    2. 매칭되면 원본 indent 유지하면서 교체

    매칭 못 하면 (text, False) 반환.
    """
    old_lines = old.split("\n")
    text_lines = text.split("\n")

    if not old_lines:
        return text, False

    # strip 한 줄들이 정확히 한 곳에서 매칭되는지
    stripped_old = [l.strip() for l in old_lines]
    matches = []
    for i in range(len(text_lines) - len(old_lines) + 1):
        window = [text_lines[i + j].strip() for j in range(len(old_lines))]
        if window == stripped_old:
            matches.append(i)

    if len(matches) != 1:
        return text, False

    start = matches[0]
    # 원본 indent (각 줄의 leading whitespace) 보존하면서 new 적용
    # 간단화: 새 줄들에 첫 매칭 줄과 같은 leading whitespace 부여
    orig_lead = text_lines[start][:len(text_lines[start]) - len(text_lines[start].lstrip())]
    new_lines = new.split("\n")
    # new 의 첫 줄 leading 도 동일 lead 로 일치
    if new_lines and not new_lines[0].startswith(orig_lead):
        new_first_lead = new_lines[0][:len(new_lines[0]) - len(new_lines[0].lstrip())]
        if new_first_lead != orig_lead:
            # diff 만큼 모든 줄에 적용
            diff = len(orig_lead) - len(new_first_lead)
            if diff > 0:
                pad = " " * diff
                new_lines = [pad + l if l else l for l in new_lines]
            elif diff < 0:
                trim = -diff
                new_lines = [l[trim:] if len(l) >= trim and l[:trim].isspace() else l
                             for l in new_lines]

    result = text_lines[:start] + new_lines + text_lines[start + len(old_lines):]
    return "\n".join(result), True


def _anchor_replace(text: str, old: str, new: str) -> tuple[str, bool]:
    """첫/끝 anchor line 기반 보수적 교체.

    exact/fuzzy 모두 실패했을 때 마지막 fallback.
    """
    text_lines = text.split("\n")
    old_lines = old.split("\n")

    sig = [ln.strip() for ln in old_lines if ln.strip()]
    if len(sig) < 2:
        return text, False

    first = sig[0]
    last = sig[-1]
    stripped_text = [ln.strip() for ln in text_lines]

    first_idxs = [i for i, ln in enumerate(stripped_text) if ln == first]
    if not first_idxs:
        return text, False

    candidates: list[tuple[int, int]] = []
    old_len = max(1, len(old_lines))
    max_span = max(20, old_len * 4)

    for s in first_idxs:
        upper = min(len(text_lines), s + max_span)
        for e in range(s + 1, upper):
            if stripped_text[e] == last:
                candidates.append((s, e))

    if len(candidates) != 1:
        return text, False

    s, e = candidates[0]
    new_lines = new.split("\n")
    replaced = text_lines[:s] + new_lines + text_lines[e + 1:]
    return "\n".join(replaced), True


def _safe_path(root: Path, rel: str) -> Path:
    """경로 traversal 방지 — root 밖으로 못 나가게."""
    rel = rel.strip().lstrip("/")
    target = (root / rel).resolve()
    root_resolved = root.resolve()
    if not str(target).startswith(str(root_resolved) + "/") and target != root_resolved:
        raise RuntimeError(f"path traversal blocked: {rel}")
    return target


async def apply_plan_and_push(
    *,
    repo_cwd: Path,
    plan: dict,
    issue_number: int,
    existing_branch: Optional[str] = None,
) -> dict:
    """plan 받아 worktree + content write + commit + push.

    existing_branch:
      None        — 신규: base 에서 새 branch 따고 새 PR 만들 준비
      "<branch>"  — amend: 기존 branch 를 origin/<branch> 에서 체크아웃,
                    plan.branch_name 무시. 추가 commit 후 같은 branch 로 push.

    Returns: {"branch": str, "base": str, "commits_applied": int, "files_changed": int}
    """
    if not (repo_cwd / ".git").exists():
        raise RuntimeError(f"not a git repo: {repo_cwd}")

    base = await _detect_default_branch(repo_cwd)
    tmp_root = Path(tempfile.gettempdir()) / f"ah-{uuid.uuid4().hex[:8]}"
    push_branch = ""

    if existing_branch:
        # ── amend mode — 기존 branch 의 최신 head 에서 worktree 분기 ──
        branch = existing_branch.strip()
        push_branch = branch
        work_branch = f"ah-amend-{uuid.uuid4().hex[:8]}"
        log.info("apply.worktree_create", path=str(tmp_root), branch=branch,
                 base=base, mode="amend")
        try:
            await _git(repo_cwd, "fetch", "origin", branch)
            try:
                await _git(repo_cwd, "worktree", "prune")
            except RuntimeError:
                pass
            # 기존 branch 와 다른 임시 branch로 worktree 생성 (현재 checkout branch 충돌 회피)
            await _git(repo_cwd, "worktree", "add", "-b", work_branch,
                       str(tmp_root), f"origin/{branch}")
        except RuntimeError:
            if tmp_root.exists():
                shutil.rmtree(tmp_root, ignore_errors=True)
            raise
    else:
        # ── 신규 mode — base 에서 새 branch ──
        branch = plan.get("branch_name", "").strip()
        push_branch = branch
        if not branch:
            raise RuntimeError("plan.branch_name 비어있음")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/.")
        if not all(c in allowed for c in branch):
            raise RuntimeError(f"invalid branch_name (영문/숫자/-_/. 만 허용): {branch!r}")

        log.info("apply.worktree_create", path=str(tmp_root), branch=branch,
                 base=base, mode="new")
        try:
            await _git(repo_cwd, "fetch", "origin", base)
            try:
                await _git(repo_cwd, "worktree", "prune")
            except RuntimeError:
                pass
            try:
                await _git(repo_cwd, "branch", "-D", branch)
                log.info("apply.stale_branch_removed", branch=branch)
            except RuntimeError:
                pass
            await _git(repo_cwd, "worktree", "add", "-b", branch,
                       str(tmp_root), f"origin/{base}")
        except RuntimeError:
            if tmp_root.exists():
                shutil.rmtree(tmp_root, ignore_errors=True)
            raise

    try:
        commits = plan.get("commits") or []
        if not commits:
            raise RuntimeError(
                "plan.commits 가 비어있음 — code-executor 가 작업 못 했다고 판단 "
                "(approach 필드 확인)"
            )

        applied = 0
        total_files = 0

        for ci, commit in enumerate(commits, 1):
            files = commit.get("files") or []
            if not files:
                log.warning("apply.empty_commit", index=ci)
                continue

            for f in files:
                path = (f.get("path") or "").strip()
                action = (f.get("action") or "replace").strip().lower()
                content = f.get("content", "")
                if not path:
                    continue

                target = _safe_path(tmp_root, path)

                if action == "delete":
                    if target.exists():
                        target.unlink()
                        log.info("apply.file_deleted", path=path)
                elif action in ("create", "replace"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content, encoding="utf-8")
                    log.info("apply.file_written", path=path,
                             bytes=len(content), action=action)
                elif action == "edit":
                    # 부분 변경 — old_str / new_str 쌍을 순서대로 적용.
                    # old_str 이 정확히 한 번 등장해야 — 안 그러면 실패 (안전).
                    if not target.exists():
                        raise RuntimeError(f"edit action: 파일 없음 — {path}")
                    text = target.read_text(encoding="utf-8")
                    edits = f.get("edits") or []
                    if not edits:
                        log.warning("apply.edit_no_edits", path=path)
                        continue
                    for ei, edit in enumerate(edits, 1):
                        old = edit.get("old_str", "")
                        new = edit.get("new_str", "")
                        if not old:
                            raise RuntimeError(
                                f"edit action: edits[{ei}].old_str 비어있음 — {path}"
                            )
                        count = text.count(old)
                        if count == 0:
                            # 1) whitespace 정규화
                            text2, matched = _fuzzy_replace(text, old, new)
                            if matched:
                                text = text2
                                log.info("apply.edit_fuzzy_match", path=path, idx=ei)
                                continue

                            # 2) anchor(첫/끝 유의미 line) 기반 보수적 fallback
                            text3, matched2 = _anchor_replace(text, old, new)
                            if matched2:
                                text = text3
                                log.info("apply.edit_anchor_match", path=path, idx=ei)
                                continue

                            raise EditApplyError(
                                path=path, edit_idx=ei,
                                message="old_str 매칭 0회 (exact/fuzzy/anchor 모두 실패) — read_file 로 현재 본문 재확인 후 더 긴 unique context 로 plan 필요",
                                old_str_head=old[:200],
                            )
                        if count > 1:
                            raise EditApplyError(
                                path=path, edit_idx=ei,
                                message=f"old_str 매칭 {count}회 (1회여야) — 더 많은 context 추가해 unique 하게",
                                old_str_head=old[:200],
                            )
                        text = text.replace(old, new, 1)
                    target.write_text(text, encoding="utf-8")
                    log.info("apply.file_edited", path=path, edits=len(edits))
                else:
                    log.warning("apply.unknown_action", path=path, action=action)
                    continue
                total_files += 1

            # stage + commit
            await _git(tmp_root, "add", "-A")
            msg = commit.get("message") or f"[#{issue_number}] auto commit {ci}"
            try:
                await _git(tmp_root, "commit", "-m", msg)
            except RuntimeError as exc:
                if "nothing to commit" in str(exc):
                    log.warning("apply.nothing_to_commit", index=ci)
                    continue
                raise
            applied += 1

        if applied == 0:
            raise RuntimeError(
                "apply 된 commit 0 — files 가 모두 빈 / action 불명 / content 누락"
            )

        # push
        if existing_branch:
            # amend: 임시 worktree branch HEAD 를 원본 branch 로 직접 push
            await _git(tmp_root, "push", "origin", f"HEAD:{push_branch}")
        else:
            await _git(tmp_root, "push", "-u", "origin", push_branch)

        return {
            "branch": branch,
            "base": base,
            "commits_applied": applied,
            "files_changed": total_files,
        }
    finally:
        try:
            await _git(repo_cwd, "worktree", "remove", "--force", str(tmp_root))
        except RuntimeError as exc:
            log.warning("apply.worktree_cleanup_failed", error=str(exc))
            if tmp_root.exists():
                shutil.rmtree(tmp_root, ignore_errors=True)
