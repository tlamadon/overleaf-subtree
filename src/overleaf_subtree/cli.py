"""Command line interface."""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from pathlib import Path

from . import gitops as g
from .config import Config, ConfigError, find_root, load

RESET, BOLD, RED, YELLOW, DIM = "\033[0m", "\033[1m", "\033[31m", "\033[33m", "\033[2m"


def _colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"{code}{text}{RESET}" if _colour() else text


def _setup(args) -> tuple[Path, Config]:
    root = find_root(Path(args.directory) if args.directory else None)
    cfg = load(root)
    if not g.has_remote(root, cfg.remote):
        raise ConfigError(
            f"no git remote named '{cfg.remote}'.  Add it with:\n"
            f"  git remote add {cfg.remote} https://git@git.overleaf.com/<project-id>"
        )
    return root, cfg


def _is_repo_owned(cfg: Config, path: str) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in cfg.repo_owned)


def _print_entries(cfg: Config, entries) -> None:
    for e in entries:
        if e.status.startswith("A"):
            print(f"  add     {e.path}")
        elif e.status.startswith("R"):
            print(f"  rename  {e.path}")
        else:
            verb = "MODIFY" if e.status.startswith("M") else "DELETE"
            print(
                f"  {_c(verb, BOLD)}  {e.path:<52} "
                f"{_c('(there: ' + e.last_touched + ')', DIM)}"
            )


# ------------------------------------------------------------------ commands

def cmd_status(args) -> int:
    root, cfg = _setup(args)
    g.fetch(root, cfg.remote, cfg.branch)
    ref = cfg.remote_ref

    head = g.git("log", "-1", "--format=%h  %s  (%an, %ar)", ref, cwd=root)
    branch = g.git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    local = g.git("rev-parse", "--short", "HEAD", cwd=root)
    print(f"Overleaf  {head}")
    print(f"Local     {cfg.prefix}/ of {local} on branch {branch}\n")

    n_behind = g.behind(root, ref)
    print(f"  behind  {n_behind} commit(s) on Overleaf not yet ingested\n")
    if n_behind:
        print("Incoming -- run 'subleaf pull':")
        for line in g.incoming(root, ref):
            print(f"    {line}")
        print()

    entries = g.outgoing(root, ref, cfg.prefix)
    if not entries:
        print("Content: identical; nothing to publish.")
    else:
        counts = {"A": 0, "M": 0, "D": 0}
        for e in entries:
            counts[e.status[0]] = counts.get(e.status[0], 0) + 1
        print(
            f"Outgoing: {counts.get('A', 0)} added, "
            f"{counts.get('M', 0)} modified, {counts.get('D', 0)} deleted"
        )
        owned = [e.path for e in entries if _is_repo_owned(cfg, e.path)]
        if owned:
            print(_c(f"\n  {len(owned)} of these are repo-owned generated files:", YELLOW))
            for p in owned[:10]:
                print(f"    {p}")
            print("  Publishing replaces whatever was edited there.")
        if n_behind:
            print(_c("\nDiverged -- both sides moved.  Pull before publishing.", YELLOW))

    if g.dirty_prefix(root, cfg.prefix):
        print(f"\n{cfg.prefix}/ has uncommitted changes (pull and push refuse until settled):")
        print(g.prefix_status(root, cfg.prefix))
    untracked = g.untracked_in_prefix(root, cfg.prefix)
    if untracked:
        print(f"\n{cfg.prefix}/ has untracked files -- these will NOT be published:")
        for p in untracked[:10]:
            print(f"    {p}")
    return 0


def cmd_diff(args) -> int:
    root, cfg = _setup(args)
    g.fetch(root, cfg.remote, cfg.branch)
    print(g.full_diff(root, cfg.remote_ref, cfg.prefix))
    return 0


def cmd_pull(args) -> int:
    root, cfg = _setup(args)
    if g.staged_anywhere(root):
        print("Staged changes present; commit or unstage them first.", file=sys.stderr)
        return 1
    if g.dirty_prefix(root, cfg.prefix):
        print(f"{cfg.prefix}/ has uncommitted changes; commit or stash first:",
              file=sys.stderr)
        print(g.prefix_status(root, cfg.prefix), file=sys.stderr)
        return 1
    g.merge_in(root, cfg.remote, cfg.branch, cfg.prefix,
               f"Merge Overleaf edits into {cfg.prefix}/")
    if cfg.repo_owned:
        print(_c(
            "\nReminder: files matching repo_owned are produced here, so any "
            "edit made to them on Overleaf will be lost the next time they are "
            "regenerated.  Check them before regenerating.", YELLOW))
    return 0


def cmd_check(args) -> int:
    root, cfg = _setup(args)
    if not cfg.checks:
        print("No checks configured.")
        return 0
    failed = 0
    for chk in cfg.checks:
        cwd = (root / cfg.prefix / chk.cwd).resolve()
        print(f"== {chk.name} ==")
        proc = subprocess.run(chk.run, shell=True, cwd=cwd, text=True,
                              capture_output=True)
        if proc.returncode == 0:
            print(f"   ok")
        else:
            failed += 1
            print(_c(f"   FAILED (exit {proc.returncode})", RED), file=sys.stderr)
            tail = (proc.stdout + proc.stderr).strip().splitlines()[-25:]
            for line in tail:
                print(f"     {line}", file=sys.stderr)
    return 1 if failed else 0


def cmd_push(args) -> int:
    root, cfg = _setup(args)

    if g.staged_anywhere(root):
        print("Staged changes present; commit or unstage them first.", file=sys.stderr)
        return 1
    if g.dirty_prefix(root, cfg.prefix):
        print(f"{cfg.prefix}/ has uncommitted changes; commit or stash first:",
              file=sys.stderr)
        print(g.prefix_status(root, cfg.prefix), file=sys.stderr)
        return 1

    if not args.no_check and cmd_check(args) != 0:
        print("\nChecks failed; nothing was pushed.", file=sys.stderr)
        return 1

    g.fetch(root, cfg.remote, cfg.branch)
    ref = cfg.remote_ref

    if not g.remote_is_ancestor(root, ref):
        print("\nOverleaf has commits this branch has not ingested, so publishing\n"
              "would discard them.  Run 'subleaf pull', resolve, re-check, retry.",
              file=sys.stderr)
        return 1

    entries = g.outgoing(root, ref, cfg.prefix)
    if not entries:
        print("Overleaf is already up to date; nothing to push.")
        return 0

    print("\nThis push would change the Overleaf project as follows.\n")
    _print_entries(cfg, entries)
    owned = [e for e in entries if _is_repo_owned(cfg, e.path)
             and not e.status.startswith("A")]
    if owned:
        print(_c(f"\n  {len(owned)} repo-owned file(s) above were edited there and "
                 f"would be replaced.", YELLOW))
    print(f"\n{g.shortstat(root, ref, cfg.prefix)}\n")

    if args.yes:
        reply = "y"
    else:
        while True:
            reply = input("Push to Overleaf? [y/N/d=show full diff] ").strip() or "N"
            if reply.lower() == "d":
                pager = os.environ.get("PAGER", "less -R")
                subprocess.run(f"{pager}", shell=True, text=True,
                               input=g.full_diff(root, ref, cfg.prefix))
                continue
            break
    if reply.lower() != "y":
        print("Aborted; nothing was pushed.")
        return 0

    head = g.git("rev-parse", "--short", "HEAD", cwd=root)
    branch = g.git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    commit = g.publish(root, cfg.remote, cfg.branch, ref, cfg.prefix,
                       f"Sync {cfg.prefix}/ from {head} ({branch})")
    print(f"Published {cfg.prefix}/ to Overleaf as {commit[:7]}.")
    return 0


# ------------------------------------------------------------------- parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="subleaf",
        description="Mirror a subdirectory of this repo onto an Overleaf "
                    "project root, with a review gate before publishing.",
    )
    p.add_argument("-C", "--directory", help="run as if started here")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what is unmerged, and what a push would change")
    sub.add_parser("diff", help="full content diff against the project")
    sub.add_parser("pull", help="merge the project's commits into this repo")
    sub.add_parser("check", help="run the configured checks")

    push = sub.add_parser("push", help="review the outgoing change, then publish")
    push.add_argument("--yes", action="store_true", help="skip the confirmation")
    push.add_argument("--no-check", action="store_true", help="skip the checks")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "status": cmd_status, "diff": cmd_diff, "pull": cmd_pull,
        "check": cmd_check, "push": cmd_push,
    }[args.command]
    try:
        return handler(args)
    except (ConfigError, g.GitError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted; nothing was pushed.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
