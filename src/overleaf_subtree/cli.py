"""Command line interface."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import agents
from . import gitops as g
from . import report
from .config import CONFIG_NAME, Config, ConfigError, find_root, load, read_raw, render

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


AUTH_HELP = """\
Check the project link first.  Overleaf's git access needs a plan that
includes git integration, and a git authentication token, which you create
under Account Settings > Git Integration.  Git asks for it as the password
(the username is 'git').  To store it, make sure a credential helper is set
(git config --global credential.helper), then run this once in a terminal
and paste the token when asked:

  git ls-remote {url}"""


def _project_url(project: str) -> str:
    """Accept a project ID, its editor link, or its git URL."""
    project = project.strip().rstrip("/")
    m = re.fullmatch(r"https?://(?:www\.)?overleaf\.com/project/([0-9a-zA-Z]+)", project)
    if m:
        project = m.group(1)
    if "://" in project or project.startswith("git@") or os.path.exists(project):
        return project
    return f"https://git@git.overleaf.com/{project}"


def _ask(question: str, default: str | None = None) -> str:
    hint = f" [{default}]" if default else ""
    while True:
        try:
            reply = input(f"{question}{hint}: ").strip()
        except EOFError:
            raise ConfigError(
                "no terminal to ask on.  Pass the answers as flags instead:\n"
                "  subleaf init --project <link or ID> --prefix <subdirectory>"
            ) from None
        if reply or default:
            return reply or default


# ------------------------------------------------------------------ commands

def cmd_init(args) -> int:
    root = g.toplevel(Path(args.directory or "."))
    config_path = root / CONFIG_NAME
    raw = read_raw(root) if config_path.is_file() else {}
    if raw:
        print(f"Using the existing {CONFIG_NAME}.")

    if g.staged_anywhere(root):
        print("Staged changes present; commit or unstage them first.", file=sys.stderr)
        return 1

    # Gather and validate everything before changing anything.
    remote = args.remote or raw.get("remote") or "overleaf"
    add_remote = not g.has_remote(root, remote)
    if not add_remote:
        url = g.remote_url(root, remote)
        if args.project and _project_url(args.project) != url:
            raise ConfigError(f"remote '{remote}' already points at {url}")
        print(f"Remote '{remote}' already set: {url}")
    else:
        url = _project_url(args.project or _ask(
            "Overleaf project (its link, or the ID from the address bar)"))

    prefix = (args.prefix or raw.get("prefix")
              or _ask("Subdirectory of this repo that holds the paper", "paper"))
    prefix = prefix.strip().strip("/")
    if prefix in ("", ".") or prefix.startswith("..") or os.path.isabs(prefix):
        raise ConfigError(f"'{prefix}' must be a subdirectory inside the repository")

    print("Contacting Overleaf...")
    try:
        detected = g.default_branch(root, url, prompt=sys.stdin.isatty())
    except g.GitError as exc:
        raise ConfigError(f"could not reach the Overleaf project.\n{exc}\n\n"
                          + AUTH_HELP.format(url=url)) from None
    branch = args.branch or raw.get("branch") or detected

    if add_remote:
        g.git("remote", "add", remote, url, cwd=root)
        print(f"Added remote '{remote}': {url}")
    g.fetch(root, remote, branch)
    ref = f"{remote}/{branch}"

    if not raw:
        config_path.write_text(render(prefix, remote, branch))
        g.git("add", CONFIG_NAME, cwd=root)
        g.git("commit", "-q", "-m", "Add overleaf-subtree config", "--", CONFIG_NAME,
              cwd=root)
        print(f"Wrote and committed {CONFIG_NAME}.")

    if g.shares_history(root, ref):
        print(f"{prefix}/ is already joined to the Overleaf project.")
    elif not g.tracked_in_prefix(root, prefix):
        g.import_subtree(root, ref, prefix, f"Import Overleaf project into {prefix}/")
        print(f"Imported the Overleaf project into {prefix}/.")
    else:
        if g.dirty_prefix(root, prefix):
            print(f"{prefix}/ has uncommitted changes; commit or stash first:",
                  file=sys.stderr)
            print(g.prefix_status(root, prefix), file=sys.stderr)
            return 1
        print(f"{prefix}/ already has files; merging the Overleaf project into it.")
        try:
            g.join_subtree(root, ref, prefix, f"Join Overleaf project into {prefix}/")
        except g.GitError:
            then = "'subleaf agent-setup'" if args.agents else "'subleaf status'"
            print("\nFiles that differ on the two sides conflict.  Resolve them, "
                  f"'git commit', then run {then}.", file=sys.stderr)
            return 1

    print("\nDone.  'subleaf pull' brings in Overleaf edits; 'subleaf push' publishes "
          f"yours.\nTo run checks before every push, edit {CONFIG_NAME}.")

    if args.agents or (sys.stdin.isatty() and _ask(
            "\nLet Claude Code and Codex see the Overleaf status at the start of "
            "each session (y/n)", "y").lower().startswith("y")):
        print()
        args.statusline = False
        return cmd_agent_setup(args)
    print("To let coding agents see the Overleaf status, run 'subleaf agent-setup'.")
    return 0


def cmd_status(args) -> int:
    if args.short or args.json:
        return _status_for_machines(args)
    root, cfg = _setup(args)
    if not args.cached:
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


def _status_for_machines(args) -> int:
    """Run from hooks and status bars: never prompts, never fails loudly,
    and prints nothing at all outside a repo set up for subleaf."""
    try:
        root, cfg = _setup(args)
    except (ConfigError, g.GitError):
        return 0
    r = report.gather(root, cfg, fetch=not args.cached)
    print(json.dumps(r, indent=2) if args.json else r["summary"])
    return 0


def cmd_agent_setup(args) -> int:
    root = find_root(Path(args.directory) if args.directory else None)
    cfg = load(root)
    print("Connecting coding agents to subleaf:")
    for line in agents.setup(root, cfg.prefix, statusline=args.statusline):
        print(line)
    print("\nCommit these files so your coauthors' agents pick them up too.  "
          "Codex asks once\nto trust the project's hooks; the hook runs "
          f"'{agents.HOOK_COMMAND}'.")
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
    g.fetch(root, cfg.remote, cfg.branch)
    if not g.shares_history(root, cfg.remote_ref):
        print(f"{cfg.prefix}/ has never been joined to the Overleaf project; "
              "run 'subleaf init' first.", file=sys.stderr)
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

    if args.dry_run:
        print("Dry run; nothing was pushed.  Rerun with --yes to publish.")
        return 0
    if args.yes:
        reply = "y"
    else:
        while True:
            try:
                reply = input("Push to Overleaf? [y/N/d=show full diff] ").strip() or "N"
            except EOFError:
                print("\nNo terminal to confirm on; nothing was pushed.  Review with "
                      "--dry-run, then publish with --yes.", file=sys.stderr)
                return 1
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
    g.record_published(root, commit, f"Record publishing {cfg.prefix}/ to Overleaf")
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

    init = sub.add_parser("init", help="connect this repo to an Overleaf project")
    init.add_argument("--project", help="the project's link, ID, or git URL")
    init.add_argument("--prefix", help="subdirectory that maps onto the project")
    init.add_argument("--remote", help="git remote name (default: overleaf)")
    init.add_argument("--branch", help="remote branch (default: detected)")
    init.add_argument("--agents", action="store_true",
                      help="also run agent-setup, without asking")

    status = sub.add_parser("status", help="what is unmerged, and what a push would change")
    status.add_argument("--short", action="store_true",
                        help="one line, for an agent's context or a status bar")
    status.add_argument("--json", action="store_true", help="everything, as JSON")
    status.add_argument("--cached", action="store_true",
                        help="don't contact Overleaf; use the last fetch")

    agent = sub.add_parser("agent-setup",
                           help="show Claude Code and Codex the Overleaf status")
    agent.add_argument("--statusline", action="store_true",
                       help="also show it in Claude Code's status line")

    sub.add_parser("diff", help="full content diff against the project")
    sub.add_parser("pull", help="merge the project's commits into this repo")
    sub.add_parser("check", help="run the configured checks")

    push = sub.add_parser("push", help="review the outgoing change, then publish")
    push.add_argument("--yes", action="store_true", help="skip the confirmation")
    push.add_argument("--no-check", action="store_true", help="skip the checks")
    push.add_argument("--dry-run", action="store_true",
                      help="run the checks and show the review, then stop")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "init": cmd_init, "status": cmd_status, "agent-setup": cmd_agent_setup,
        "diff": cmd_diff, "pull": cmd_pull,
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
