"""Machine-friendly status: one line for an agent's context, or JSON.

Both forms are meant to run unattended -- from a session-start hook or a
status bar -- so they never prompt, give up on the network after a few
seconds, and fall back to whatever was fetched last.
"""
from __future__ import annotations

import fnmatch
import time
from pathlib import Path

from . import gitops as g
from .config import Config

FETCH_TIMEOUT = 10


def ago(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def gather(root: Path, cfg: Config, *, fetch: bool) -> dict:
    ref = cfg.remote_ref
    fetch_error = None
    if fetch:
        try:
            g.fetch(root, cfg.remote, cfg.branch, timeout=FETCH_TIMEOUT, prompt=False)
        except g.GitError as exc:
            fetch_error = str(exc).splitlines()[-1]
    since = g.seconds_since_fetch(root)

    report = {
        "prefix": cfg.prefix,
        "remote": cfg.remote,
        "branch": cfg.branch,
        "fetch": "cached" if not fetch else ("failed" if fetch_error else "live"),
        "fetch_error": fetch_error,
        "seconds_since_fetch": None if since is None else int(since),
        "joined": False,
        "incoming": {"commits": [], "files": [], "repo_owned": []},
        "outgoing": [],
        "diverged": False,
        "uncommitted_in_prefix": g.dirty_prefix(root, cfg.prefix),
        "untracked_in_prefix": g.untracked_in_prefix(root, cfg.prefix),
    }
    if not g.ref_exists(root, ref) or not g.shares_history(root, ref):
        report["summary"] = summarize(report)
        return report

    report["joined"] = True
    files = g.incoming_files(root, ref)
    report["incoming"] = {
        "commits": g.incoming_commits(root, ref),
        "files": files,
        "repo_owned": [f for f in files
                       if any(fnmatch.fnmatch(f, p) for p in cfg.repo_owned)],
    }
    # Local changes since the last sync, not the diff against the remote tip:
    # while behind, that diff would also list the coauthors' edits, reversed.
    base = g.git("merge-base", "HEAD", ref, cwd=root)
    report["outgoing"] = [
        {"status": e.status[0], "path": e.path, "last_touched": e.last_touched}
        for e in g.outgoing(root, base, cfg.prefix)
    ]
    report["diverged"] = bool(report["incoming"]["commits"] and report["outgoing"])
    report["summary"] = summarize(report)
    return report


def _paths(paths: list[str], limit: int = 3) -> str:
    shown = ", ".join(paths[:limit])
    return shown + (f" +{len(paths) - limit} more" if len(paths) > limit else "")


def summarize(r: dict) -> str:
    """One line, in words, that an agent can quote to the user as is."""
    if not r["joined"]:
        if r["fetch_error"]:
            return f"Overleaf: could not reach the project ({r['fetch_error']})"
        return "Overleaf: not connected yet; run 'subleaf init'"

    parts = []
    commits, outgoing = r["incoming"]["commits"], r["outgoing"]
    if commits:
        authors = ", ".join(dict.fromkeys(c["author"] for c in commits))
        newest = ago(time.time() - commits[0]["time"])
        parts.append(f"{len(commits)} new commit{'s' * (len(commits) > 1)} to pull "
                     f"({authors}, {newest}: {_paths(r['incoming']['files'])})")
    if outgoing:
        parts.append(f"{len(outgoing)} file{'s' * (len(outgoing) > 1)} to push "
                     f"({_paths([e['path'] for e in outgoing])})")
    if not parts:
        parts.append("in sync")
    if r["diverged"]:
        parts.append("both sides changed, pull before pushing")
    owned = r["incoming"]["repo_owned"]
    if owned:
        parts.append(f"{len(owned)} generated file{'s' * (len(owned) > 1)} edited on "
                     "Overleaf, will be overwritten when regenerated")
    if r["uncommitted_in_prefix"]:
        parts.append(f"{r['prefix']}/ has uncommitted changes, pull and push blocked")

    line = "Overleaf: " + " · ".join(parts)
    since = r["seconds_since_fetch"]
    if r["fetch"] == "failed":
        line += (f" (could not reach Overleaf; as of the last fetch, "
                 f"{ago(since) if since is not None else 'never'})")
    elif r["fetch"] == "cached" and since is not None:
        line += f" (fetched {ago(since)})"
    return line
