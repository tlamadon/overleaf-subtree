"""Git operations against the Overleaf remote.

The asymmetry here is deliberate and is the main lesson this tool encodes:

  merge history in, publish trees out.

Pulling is a real merge, so the coauthors' commits enter your history with
their authorship, and git does conflict detection.  Publishing sends a single
commit built from the prefix's tree.  It is tempting to publish a rewritten
history instead -- ``git subtree split`` does exactly that -- but its output
travels to the remote and returns on the next pull as synthesized twins of
your own commits.  Two commits carrying the same ``git-subtree-mainline``
trailer make every later split abort with "cache for <sha> already exists",
and no amount of cache clearing helps because the twins are in the history.
"""
from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True, capture: bool = True,
        timeout: float | None = None, env: dict | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            capture_output=capture,
            timeout=timeout,
            env={**os.environ, **env} if env else None,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"git {' '.join(args)} timed out after {timeout:g}s") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise GitError(f"git {' '.join(args)} failed:\n{detail}")
    return (proc.stdout or "").strip() if capture else ""


def git_ok(*args: str, cwd: Path) -> bool:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True
    ).returncode == 0


@dataclass(frozen=True)
class Entry:
    """One path in the outgoing change."""

    status: str          # A, M, D, R...
    path: str            # relative to the Overleaf project root
    last_touched: str    # who last changed it there, and when


def has_remote(root: Path, remote: str) -> bool:
    return git_ok("remote", "get-url", remote, cwd=root)


def toplevel(start: Path) -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=start))


def remote_url(root: Path, remote: str) -> str:
    return git("remote", "get-url", remote, cwd=root)


def default_branch(root: Path, url: str) -> str:
    """The branch the remote's HEAD points at.  Overleaf's is ``master``.

    Also the first contact with the remote, so a wrong URL or missing
    credentials fail here, before anything has been changed.
    """
    out = git("ls-remote", "--symref", url, "HEAD", cwd=root)
    for line in out.splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split("\t")[0].removeprefix("ref: refs/heads/")
    return "master"


def shares_history(root: Path, remote_ref: str) -> bool:
    """False until the remote has been joined into this branch once."""
    return git_ok("merge-base", "HEAD", remote_ref, cwd=root)


def tracked_in_prefix(root: Path, prefix: str) -> bool:
    return bool(git("ls-files", "--", prefix, cwd=root))


def fetch(root: Path, remote: str, branch: str, *, timeout: float | None = None,
          prompt: bool = True) -> None:
    """``prompt=False`` fails instead of asking for credentials, for hooks."""
    git("fetch", "-q", remote, branch, cwd=root, timeout=timeout,
        env=None if prompt else {"GIT_TERMINAL_PROMPT": "0"})


def ref_exists(root: Path, ref: str) -> bool:
    return git_ok("rev-parse", "--verify", "-q", ref, cwd=root)


def seconds_since_fetch(root: Path) -> float | None:
    path = root / git("rev-parse", "--git-path", "FETCH_HEAD", cwd=root)
    return time.time() - path.stat().st_mtime if path.exists() else None


def local_tree(root: Path, prefix: str) -> str:
    """The tree object for the prefix at HEAD -- what would be published."""
    return git("rev-parse", f"HEAD:{prefix}", cwd=root)


def behind(root: Path, remote_ref: str) -> int:
    """Commits on the remote not yet merged into HEAD."""
    return int(git("rev-list", "--count", f"HEAD..{remote_ref}", cwd=root))


def incoming(root: Path, remote_ref: str, limit: int = 10) -> list[str]:
    out = git(
        "log", f"--format=%h  %s  (%an, %ar)", f"HEAD..{remote_ref}", cwd=root
    )
    return out.splitlines()[:limit] if out else []


def incoming_commits(root: Path, remote_ref: str) -> list[dict]:
    out = git("log", "--format=%h%x1f%an%x1f%at%x1f%s", f"HEAD..{remote_ref}",
              cwd=root)
    commits = []
    for line in out.splitlines():
        sha, author, at, subject = line.split("\x1f", 3)
        commits.append({"sha": sha, "author": author, "time": int(at),
                        "subject": subject})
    return commits


def incoming_files(root: Path, remote_ref: str) -> list[str]:
    """Paths, relative to the project root, touched by unmerged remote commits."""
    out = git("log", "--format=", "--name-only", f"HEAD..{remote_ref}", cwd=root)
    return list(dict.fromkeys(line for line in out.splitlines() if line))


def outgoing(root: Path, remote_ref: str, prefix: str) -> list[Entry]:
    """The change publishing would make to the project.

    Compares the remote's root tree against ``HEAD:<prefix>``.  Both hold the
    same paths, so they diff directly -- no history rewriting required.
    """
    raw = git(
        "diff", "--name-status", remote_ref, f"HEAD:{prefix}", cwd=root
    )
    entries: list[Entry] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        touched = ""
        if status.startswith(("M", "D", "R")):
            touched = git(
                "log", "-1", "--format=%an, %ar", remote_ref, "--", path,
                cwd=root, check=False,
            ) or "never touched there"
        entries.append(Entry(status=status, path=path, last_touched=touched))
    return entries


def shortstat(root: Path, remote_ref: str, prefix: str) -> str:
    return git("diff", "--shortstat", remote_ref, f"HEAD:{prefix}", cwd=root)


def full_diff(root: Path, remote_ref: str, prefix: str) -> str:
    return git("diff", remote_ref, f"HEAD:{prefix}", cwd=root)


def remote_is_ancestor(root: Path, remote_ref: str) -> bool:
    """True when publishing cannot discard anything the remote has."""
    return git_ok("merge-base", "--is-ancestor", remote_ref, "HEAD", cwd=root)


def merge_in(root: Path, remote: str, branch: str, prefix: str, message: str) -> str:
    """Fetch and merge the remote into HEAD, mapped under the prefix."""
    git("fetch", remote, branch, cwd=root, capture=False)
    return git(
        "merge", "--no-ff", f"-Xsubtree={prefix}", "FETCH_HEAD", "-m", message,
        cwd=root, capture=False,
    )


def import_subtree(root: Path, remote_ref: str, prefix: str, message: str) -> None:
    """First join, into an empty prefix: the project's files, its history kept."""
    git("merge", "-q", "-s", "ours", "--no-commit", "--allow-unrelated-histories",
        remote_ref, cwd=root)
    git("read-tree", f"--prefix={prefix}/", "-u", remote_ref, cwd=root)
    git("commit", "-q", "-m", message, cwd=root)


def join_subtree(root: Path, remote_ref: str, prefix: str, message: str) -> None:
    """First join, into a prefix that already has files: an ordinary merge
    with no common ancestor, so any file that differs on the two sides
    conflicts and is left for the user to resolve."""
    git("merge", "--no-ff", "--allow-unrelated-histories", f"-Xsubtree={prefix}",
        remote_ref, "-m", message, cwd=root, capture=False)


def publish(root: Path, remote: str, branch: str, remote_ref: str,
            prefix: str, message: str) -> str:
    """Push the prefix's tree as one commit on top of the remote tip."""
    tree = local_tree(root, prefix)
    commit = git("commit-tree", tree, "-p", remote_ref, "-m", message, cwd=root)
    git("push", remote, f"{commit}:{branch}", cwd=root, capture=False)
    return commit


def record_published(root: Path, commit: str, message: str) -> None:
    """Merge the published commit into HEAD, keeping HEAD's tree unchanged.

    Without this, the next pull merges against the project as it was *before*
    the push, and a coauthor's edit next to lines you just published
    conflicts with your own change.
    """
    git("merge", "-q", "-s", "ours", "--no-edit", "-m", message, commit, cwd=root)


# ---------------------------------------------------------------- worktree

def staged_anywhere(root: Path) -> bool:
    # Compares against HEAD, or the empty tree in a repo with no commits yet.
    return not git_ok("diff", "--cached", "--quiet", cwd=root)


def dirty_prefix(root: Path, prefix: str) -> bool:
    return not git_ok("diff-index", "--quiet", "HEAD", "--", prefix, cwd=root)


def prefix_status(root: Path, prefix: str) -> str:
    return git("status", "--short", "--", prefix, cwd=root)


def untracked_in_prefix(root: Path, prefix: str) -> list[str]:
    out = git(
        "ls-files", "--others", "--exclude-standard", "--", prefix, cwd=root
    )
    return out.splitlines() if out else []
