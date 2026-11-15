"""The Overleaf-is-the-source-of-truth workflow.

Everything the plain ``pull`` / ``push`` pair does not cover:

    pull --theirs   take Overleaf's version of every file it has
    push PATH...    publish the named files only, then merge back and verify
    pairs           working copies kept outside the prefix (a talk deck)
    check --report  what got worse since the previous sync
    accept          strip \\new{...} review wrappers with balanced braces
    latexdiff       diff PDF between two versions

Configuration is extra keys in ``.overleaf-subtree.toml`` (``local_only``,
``artifacts``, ``[[pair]]``, ``[pdf]``, ``[post_pull]``); all optional.  The
bookkeeping that has to survive between runs -- the last-synced blob of every
pair, the previous check results -- lives in a committed state file,
``.overleaf-state.json`` by default.

Nothing here prints a credential: git authenticates through its own
credential helper, and git output that is echoed goes through redact().
"""

from __future__ import annotations

import argparse
import collections
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib

ZERO = "0" * 40
STATE_VERSION = 1


# ── small utilities ─────────────────────────────────────────────────────


class SyncError(Exception):
    """A refusal or failure that should end the run with a message."""


def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def redact(text: str) -> str:
    """Hide anything shaped like credentials in a URL."""
    return re.sub(r"(https?://)[^/@\s]+@", r"\1***@", text)


def glob_to_re(pattern: str) -> re.Pattern:
    """Path glob: `**` crosses directories, `*` and `?` do not."""
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


class Globs:
    def __init__(self, patterns):
        self.patterns = list(patterns)
        self._res = [glob_to_re(p) for p in self.patterns]

    def match(self, path: str) -> bool:
        return any(r.match(path) for r in self._res)

    def which(self, path: str):
        for p, r in zip(self.patterns, self._res):
            if r.match(path):
                return p
        return None


# ── git plumbing ────────────────────────────────────────────────────────


class Git:
    def __init__(self, root: str):
        self.root = root

    def run(self, *args, env=None, check=True, input=None, binary=False):
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=full_env,
            input=input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if check and proc.returncode != 0:
            err = redact(proc.stderr.decode("utf-8", "replace").strip())
            raise SyncError(f"git {' '.join(args[:3])} ... failed:\n{err}")
        return proc.stdout if binary else proc.stdout.decode("utf-8", "replace")

    def proc(self, *args, env=None, input=None):
        """Run without raising; returns (rc, stdout, stderr) as text."""
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        p = subprocess.run(
            ["git", *args], cwd=self.root, env=full_env, input=input,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return (
            p.returncode,
            p.stdout.decode("utf-8", "replace"),
            redact(p.stderr.decode("utf-8", "replace")),
        )

    def out(self, *args, **kw) -> str:
        return self.run(*args, **kw).strip()

    def ok(self, *args) -> bool:
        return self.proc(*args)[0] == 0

    def rev(self, name: str) -> str:
        return self.out("rev-parse", "--verify", "-q", name + "^{commit}")

    def ls_tree(self, treeish: str) -> dict:
        """path -> (mode, blob) for every file in a tree."""
        rc, out, _ = self.proc("ls-tree", "-r", "-z", "--full-tree", treeish)
        if rc != 0:
            return {}
        entries = {}
        for rec in out.split("\0"):
            if not rec:
                continue
            meta, path = rec.split("\t", 1)
            mode, kind, sha = meta.split()
            if kind == "blob":
                entries[path] = (mode, sha)
        return entries

    def hash_file(self, abspath: str, write: bool = False):
        """Blob id of a working-tree file, or None if it is not a file."""
        if os.path.islink(abspath) or not os.path.isfile(abspath):
            return None
        args = ["hash-object", "--no-filters"]
        if write:
            args.append("-w")
        return self.out(*args, "--", abspath)

    def blob(self, sha: str) -> bytes:
        return self.run("cat-file", "blob", sha, binary=True)

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", a, b)


def repo_root() -> str:
    p = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        raise SyncError("not inside a git repository")
    return p.stdout.decode().strip()


# ── configuration and state ─────────────────────────────────────────────


class Pair:
    def __init__(self, local: str, remote: str, pdf: str | None = None):
        self.local = local      # repo-relative working copy
        self.remote = remote    # path inside the Overleaf project
        self.pdf = pdf          # compiled product stored on Overleaf, if any


class Config:
    def __init__(self, root: str):
        self.root = root
        path = os.path.join(root, ".overleaf-subtree.toml")
        if not os.path.isfile(path):
            raise SyncError(f"{path} not found")
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        self.prefix = raw.get("prefix", "paper").strip("/")
        self.remote = raw.get("remote", "overleaf")
        self.branch = raw.get("branch", "master")
        self.main = raw.get("main")
        self.checks = [c for c in raw.get("checks", []) if c.get("run")]
        self.artifacts = Globs(raw.get("artifacts", []))
        self.local_only = Globs(raw.get("local_only", []))
        self.state_file = raw.get("state_file", ".overleaf-state.json")
        default_backup = os.path.join(
            os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
            "subleaf", os.path.basename(root),
        )
        self.backup_dir = os.path.expanduser(
            os.environ.get("OVERLEAF_SYNC_BACKUP_DIR")
            or raw.get("backup_dir", default_backup)
        )
        pdf = raw.get("pdf", {})
        self.pdf_policy = pdf.get("policy", "rebuild")
        if self.pdf_policy not in ("rebuild", "never"):
            raise SyncError("[pdf] policy must be \"rebuild\" or \"never\"")
        self.pdf_build = pdf.get(
            "build", "latexmk -pdf -interaction=nonstopmode -halt-on-error"
        )
        post = raw.get("post_pull", {})
        self.check_cmd = post.get("check")   # None: run the [[checks]]
        self.check_log = post.get("log")     # None: the main document's log
        self.pairs: list[Pair] = []
        for entry in raw.get("pair", []):
            if "files" in entry:
                for name in entry["files"]:
                    self.pairs.append(Pair(
                        f"{entry['local_dir'].rstrip('/')}/{name}",
                        f"{entry['remote_dir'].rstrip('/')}/{name}",
                    ))
            else:
                self.pairs.append(Pair(entry["local"], entry["remote"], entry.get("pdf")))
        seen = set()
        for pr in self.pairs:
            if pr.remote in seen:
                raise SyncError(f"pair remote declared twice: {pr.remote}")
            seen.add(pr.remote)
            if self.local_only.match(pr.remote):
                raise SyncError(f"pair remote {pr.remote} is also local_only")

    @property
    def tracking(self) -> str:
        return f"{self.remote}/{self.branch}"

    def mirror(self, remote_path: str) -> str:
        """Repo-relative path of an Overleaf path."""
        return f"{self.prefix}/{remote_path}"

    def abs(self, repo_path: str) -> str:
        return os.path.join(self.root, repo_path)

    def pdf_products(self) -> list[Pair]:
        return [p for p in self.pairs if p.pdf]

    def is_local_only(self, remote_path: str, theirs: dict) -> bool:
        """Matches local_only and is not on Overleaf: never to be added there.

        A matching path that Overleaf already has is a shared file (the
        coauthors' own notes/ live next to ours) and syncs like any other.
        """
        return self.local_only.match(remote_path) and remote_path not in theirs

    def held_back(self, remote_path: str, theirs: dict) -> bool:
        """A whole-tree push leaves this path exactly as Overleaf has it."""
        if self.is_local_only(remote_path, theirs):
            return True
        if self.pdf_policy == "never":
            return any(p.pdf == remote_path for p in self.pairs)
        return False


class State:
    def __init__(self, cfg: Config):
        self.path = cfg.abs(cfg.state_file)
        self.rel = cfg.state_file
        self.data = {"version": STATE_VERSION, "overleaf_tip": None, "pairs": {}, "checks": {}}
        if os.path.isfile(self.path):
            with open(self.path) as fh:
                loaded = json.load(fh)
            if loaded.get("version") != STATE_VERSION:
                raise SyncError(f"{self.rel}: unknown version {loaded.get('version')!r}")
            self.data.update(loaded)
        self._before = json.dumps(self.data, sort_keys=True)

    def base(self, pair: Pair):
        entry = self.data["pairs"].get(pair.remote)
        return entry.get("blob") if entry else None

    def set_base(self, pair: Pair, blob: str) -> None:
        self.data["pairs"][pair.remote] = {"local": pair.local, "blob": blob}

    def save(self) -> bool:
        now = json.dumps(self.data, sort_keys=True)
        if now == self._before and os.path.isfile(self.path):
            return False
        with open(self.path, "w") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        self._before = now
        return True


class Ctx:
    def __init__(self, root=None):
        self.root = str(root) if root else repo_root()
        self.git = Git(self.root)
        self.cfg = Config(self.root)
        self.state = State(self.cfg)
        self.stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # -- shared preconditions ------------------------------------------

    def require_remote(self) -> None:
        if not self.git.ok("remote", "get-url", self.cfg.remote):
            raise SyncError(
                f"no git remote named '{self.cfg.remote}'; run 'subleaf init' first.")

    def require_nothing_staged(self) -> None:
        rc, out, _ = self.git.proc("diff-index", "--cached", "--name-only", "HEAD", "--")
        if out.strip():
            raise SyncError(
                "Staged changes present; commit or unstage them first:\n"
                + "\n".join("    " + l for l in out.strip().splitlines())
            )

    def fetch(self) -> str:
        cfg = self.cfg
        rc, _, err = self.git.proc(
            "fetch", "-q", cfg.remote,
            f"+refs/heads/{cfg.branch}:refs/remotes/{cfg.remote}/{cfg.branch}",
        )
        if rc != 0:
            raise SyncError(f"git fetch {cfg.remote} failed:\n{err.strip()}")
        return self.git.rev(cfg.tracking)

    def backup(self, repo_path: str) -> str:
        dest = os.path.join(self.cfg.backup_dir, self.stamp, repo_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(self.cfg.abs(repo_path), dest)
        return dest

    def write_blob(self, sha: str, repo_path: str) -> None:
        dest = self.cfg.abs(repo_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(self.git.blob(sha))

    def temp_index(self):
        fd, path = tempfile.mkstemp(prefix="overleaf-index-")
        os.close(fd)
        os.unlink(path)  # git wants to create it itself
        return path


# ── LaTeX structure: which sections does a change touch? ────────────────

_HEAD_RE = re.compile(r"\\(section|subsection)\*?\s*(?:\[[^\]]*\]\s*)?\{")
_FRAME_RE = re.compile(r"\\begin\{frame\}(?:<[^>]*>)?(?:\[[^\]]*\])?\s*")
_FRAMETITLE_RE = re.compile(r"\\frametitle\s*(?:<[^>]*>)?\s*\{")


def _uncomment(line: str) -> str:
    """The part of a TeX line before an unescaped %."""
    m = re.search(r"(?<!\\)%", line)
    return line[: m.start()] if m else line


def _braced(lines: list[str], row: int, col: int) -> str:
    """Text of the {...} group opening at lines[row][col], across up to 4 lines."""
    depth = 0
    buf = []
    for r in range(row, min(row + 4, len(lines))):
        text = _uncomment(lines[r])
        start = col if r == row else 0
        i = start
        while i < len(text):
            ch = text[i]
            if ch == "\\":
                buf.append(text[i:i + 2])
                i += 2
                continue
            if ch == "{":
                depth += 1
                if depth == 1:
                    i += 1
                    continue
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return _clean_title("".join(buf))
            buf.append(ch)
            i += 1
        buf.append(" ")
    return _clean_title("".join(buf))


def _clean_title(title: str) -> str:
    title = re.sub(r"\\label\{[^}]*\}", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title if len(title) <= 70 else title[:67] + "..."


def section_labels(text: str) -> list[str]:
    """For every line of a TeX file, the heading it sits under.

    Tracks \\section and \\subsection (and, in a beamer deck, the frame
    title), so a diff hunk's line number maps to the nearest preceding one.
    """
    lines = text.split("\n")
    labels = []
    sec = sub = frame = None
    in_doc = False
    appendix = False
    for row, raw in enumerate(lines):
        line = _uncomment(raw)
        if "\\begin{document}" in line:
            in_doc = True
        if re.match(r"\s*\\appendix\b", line):
            appendix = True
        m = _HEAD_RE.search(line)
        if m:
            title = _braced(lines, row, m.end() - 1)
            if m.group(1) == "section":
                sec = ("Appendix: " if appendix else "") + title
                sub = None
            else:
                sub = title
            frame = None
        fm = _FRAME_RE.search(line)
        if fm:
            frame = None
            rest = line[fm.end():]
            if rest.startswith("{"):
                frame = _braced(lines, row, fm.end())
            else:
                for ahead in range(row, min(row + 6, len(lines))):
                    tm = _FRAMETITLE_RE.search(_uncomment(lines[ahead]))
                    if tm:
                        frame = _braced(lines, ahead, tm.end() - 1)
                        break
            frame = frame or "(untitled frame)"
        parts = [p for p in (sec, sub, f"frame: {frame}" if frame else None) if p]
        if parts:
            labels.append(" > ".join(parts))
        else:
            labels.append("(before first section)" if in_doc else "(preamble)")
        if "\\end{frame}" in line:
            frame = None
    return labels


def sections_touched(git: Git, old: str | None, new: str, path: str) -> list[str]:
    """Headings of `path` (at `new`) under which `old..new` changed lines."""
    rc, text, _ = git.proc("show", f"{new}:{path}")
    if rc != 0:
        return []
    labels = section_labels(text)
    if not labels:
        return []
    if not old:
        return []
    rc, diff, _ = git.proc("diff", "-U0", "--no-renames", "--no-color", old, new, "--", path)
    seen: dict[str, None] = {}
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        start = int(m.group(1))
        count = 1 if m.group(2) is None else int(m.group(2))
        rows = range(start, start + count) if count else [max(start, 1)]
        for row in rows:
            idx = min(max(row, 1), len(labels)) - 1
            seen.setdefault(labels[idx])
    return list(seen)


def describe_commits(git: Git, base: str, tip: str) -> list[str]:
    """Human-readable account of base..tip: who, when, which files, which sections."""
    commits = git.out("rev-list", "--reverse", f"{base}..{tip}").split()
    if not commits:
        return []
    lines = []
    authors: collections.Counter = collections.Counter()
    for c in commits:
        meta = git.out(
            "show", "-s", "--date=format-local:%Y-%m-%d %H:%M",
            "--format=%h%x00%an%x00%ad%x00%s%x00%P", c,
        ).split("\0")
        short, author, when, subject, parents = meta
        authors[author] += 1
        parent = parents.split()[0] if parents.split() else None
        lines.append(f"{short}  {when}  {author}  --  {subject}")
        if not parent:
            lines.append("    (root commit)")
            continue
        status = {}
        raw = git.run("diff-tree", "-r", "--no-renames", "--name-status", "-z", parent, c)
        toks = [t for t in raw.split("\0") if t]
        for st, path in zip(toks[0::2], toks[1::2]):
            status[path] = st
        numstat = {}
        raw = git.run("diff-tree", "-r", "--no-renames", "--numstat", "-z", parent, c)
        for rec in raw.split("\0"):
            if rec.count("\t") >= 2:
                a, d, path = rec.split("\t", 2)
                numstat[path] = (a, d)
        for path in sorted(status):
            a, d = numstat.get(path, ("?", "?"))
            size = "binary" if a == "-" else f"+{a} -{d}"
            lines.append(f"    {status[path]} {path}  ({size})")
            if path.endswith(".tex") and status[path] == "M":
                for label in sections_touched(git, parent, c, path):
                    lines.append(f"        {label}")
    who = ", ".join(f"{a} ({n})" for a, n in authors.most_common())
    lines.append("")
    lines.append(f"Authors: {who}")
    return lines


# ── pairs ───────────────────────────────────────────────────────────────


def _is_binary(*chunks: bytes) -> bool:
    return any(b"\0" in c for c in chunks)


def three_way(git: Git, ours: bytes, base: bytes, theirs: bytes, labels):
    """git merge-file.  Returns (merged bytes or None if binary, conflicts)."""
    if _is_binary(ours, base, theirs):
        return None, 1
    with tempfile.TemporaryDirectory(prefix="overleaf-merge-") as tmp:
        names = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            p = os.path.join(tmp, name)
            with open(p, "wb") as fh:
                fh.write(data)
            names.append(p)
        proc = subprocess.run(
            ["git", "merge-file", "-p", "--diff3",
             "-L", labels[0], "-L", labels[1], "-L", labels[2], *names],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    if proc.returncode < 0 or proc.returncode > 127:
        return None, 1
    return proc.stdout, proc.returncode


class PairResult:
    def __init__(self, pair: Pair, status: str, note: str = ""):
        self.pair = pair
        self.status = status  # in-sync | fast-forwarded | merged | conflict | local-ahead | no-base | missing
        self.note = note
        self.wrote_local = False
        self.local_was_clean = False


def pair_status(ctx: Ctx, pair: Pair, remote_blob: str | None):
    """Classify a pair against its recorded base.  Returns (status, local blob)."""
    local = ctx.git.hash_file(ctx.cfg.abs(pair.local))
    base = ctx.state.base(pair)
    if local is None or remote_blob is None:
        return "missing", local
    if local == remote_blob:
        return "in-sync", local
    if base is None:
        return "no-base", local
    if remote_blob == base:
        return "local-ahead", local
    if local == base:
        return "remote-ahead", local
    return "diverged", local


def sync_pairs_from_remote(ctx: Ctx, remote_tree: dict,
                           old_mirror: dict | None = None) -> list[PairResult]:
    """After a pull: bring each pair's local copy up to Overleaf's version.

    remote-only change  -> fast-forward the local copy
    both changed        -> git merge-file against the recorded base
    real conflict       -> markers in the local copy (original backed up),
                           base left alone, reported as a conflict
    """
    git, cfg, state = ctx.git, ctx.cfg, ctx.state
    head_local = git.ls_tree("HEAD")
    results = []
    for pair in cfg.pairs:
        remote_blob = remote_tree.get(pair.remote, (None, None))[1]
        if state.base(pair) is None and old_mirror:
            # Never synced under this scheme.  If the working copy equals the
            # mirror as it stood before this pull, that is the common version.
            before = old_mirror.get(pair.remote, (None, None))[1]
            if before and before == git.hash_file(cfg.abs(pair.local)):
                state.set_base(pair, before)
        status, local = pair_status(ctx, pair, remote_blob)
        res = PairResult(pair, status)
        res.local_was_clean = (
            local is not None and head_local.get(pair.local, (None, None))[1] == local
        )
        if status == "missing":
            res.note = ("not on Overleaf" if remote_blob is None else "no local copy")
        elif status == "in-sync":
            state.set_base(pair, remote_blob)
        elif status == "no-base":
            res.note = "the two copies differ and no synced version is recorded"
        elif status == "local-ahead":
            res.note = "local edits not on Overleaf yet"
        elif status == "remote-ahead":
            ctx.write_blob(remote_blob, pair.local)
            state.set_base(pair, remote_blob)
            res.status, res.wrote_local = "fast-forwarded", True
        else:  # diverged
            base = state.base(pair)
            with open(cfg.abs(pair.local), "rb") as fh:
                ours = fh.read()
            merged, conflicts = three_way(
                git, ours, git.blob(base), git.blob(remote_blob),
                (f"local {pair.local}", "last synced", f"Overleaf {pair.remote}"),
            )
            if conflicts == 0:
                with open(cfg.abs(pair.local), "wb") as fh:
                    fh.write(merged)
                state.set_base(pair, remote_blob)
                res.status, res.wrote_local = "merged", True
                res.note = "local edits kept on top of Overleaf's; not on Overleaf yet"
            else:
                saved = ctx.backup(pair.local)
                res.status = "conflict"
                if merged is None:
                    res.note = f"binary file changed on both sides (local copy saved to {saved})"
                else:
                    with open(cfg.abs(pair.local), "wb") as fh:
                        fh.write(merged)
                    res.note = (f"{conflicts} conflict(s) marked in {pair.local} "
                                f"(your version saved to {saved})")
        results.append(res)
    return results


def report_pairs(results: list[PairResult]) -> list[str]:
    lines = []
    for r in results:
        if r.status == "in-sync":
            continue
        label = {
            "fast-forwarded": "updated   ",
            "merged": "merged    ",
            "conflict": "CONFLICT  ",
            "local-ahead": "local edit",
            "no-base": "UNSYNCED  ",
            "missing": "missing   ",
        }[r.status]
        line = f"    {label}  {r.pair.local}  <-  {r.pair.remote}"
        if r.note:
            line += f"\n                {r.note}"
        lines.append(line)
    return lines


# ── Overleaf-wins pull ──────────────────────────────────────────────────


class PullResult:
    def __init__(self):
        self.tip = None
        self.old_head = None
        self.commit = None            # merge commit created, if any
        self.changed: list[str] = []  # Overleaf paths the pull changed locally
        self.discarded: list[str] = []
        self.backups: list[str] = []
        self.pending: list[str] = []  # unpublished local changes left in place
        self.base = None


def plan_pull(ctx: Ctx, head: str, tip: str, keep_local: bool = False):
    """Work out the mirror tree an Overleaf-wins pull must produce.

    keep_local is for the merge-back after a targeted push: a file changed
    here since the last sync that Overleaf has NOT changed stays as it is
    (still unpublished) instead of being overwritten.  Returns those paths
    as `pending`.
    """
    git, cfg = ctx.git, ctx.cfg
    rc, base, _ = git.proc("merge-base", head, tip)
    base = base.strip().split("\n")[0] if rc == 0 else ""
    if not base:
        raise SyncError(
            f"{cfg.tracking} shares no history with HEAD; this is not a mirror "
            "that has been pulled before."
        )
    theirs = git.ls_tree(tip)
    ours = git.ls_tree(f"{head}:{cfg.prefix}")
    base_tree = git.ls_tree(base)
    target = dict(theirs)
    for path, entry in ours.items():
        if path in theirs:
            continue
        # Absent on Overleaf.  Keep it if it is ours alone; drop it if Overleaf
        # had it at the last sync and a coauthor has since deleted it.
        if cfg.local_only.match(path) or path not in base_tree:
            target[path] = entry
    pending = set()
    if keep_local:
        for path in set(ours) | set(base_tree):
            if cfg.is_local_only(path, theirs):
                continue
            if theirs.get(path) == base_tree.get(path) != ours.get(path):
                pending.add(path)
                if path in ours:
                    target[path] = ours[path]
                else:
                    target.pop(path, None)
    changes = {}
    for path in set(ours) | set(target):
        if ours.get(path) != target.get(path):
            changes[path] = target.get(path)
    discarded = sorted(
        p for p in changes
        if p in ours and ours[p] != base_tree.get(p)
    )
    return base, theirs, ours, target, changes, discarded, pending


def pull_theirs(ctx: Ctx, quiet_if_current: bool = False,
                keep_local: bool = False) -> PullResult:
    """Fetch, then make the mirror equal Overleaf (repo-only paths aside)."""
    git, cfg = ctx.git, ctx.cfg
    res = PullResult()
    ctx.require_remote()
    ctx.require_nothing_staged()
    res.tip = tip = ctx.fetch()
    res.old_head = head = git.rev("HEAD")

    if git.is_ancestor(tip, head):
        if not quiet_if_current:
            say(f"Already up to date with Overleaf ({tip[:7]}).")
        return res

    base, theirs, ours, target, changes, discarded, pending = plan_pull(
        ctx, head, tip, keep_local)
    res.base, res.discarded, res.pending = base, discarded, sorted(pending)

    # -- preflight: what is dirty, and may we touch it? -----------------
    git.proc("update-index", "-q", "--refresh")
    rc, out, _ = git.proc("diff-files", "--name-only", "-z", "--", cfg.prefix)
    dirty = {}
    for repo_path in filter(None, out.split("\0")):
        rel = repo_path[len(cfg.prefix) + 1:]
        w = git.hash_file(cfg.abs(repo_path))
        if rel in ours and w != ours[rel][1]:
            dirty[rel] = w
    refused, to_backup = [], []
    for rel, w in sorted(dirty.items()):
        if rel not in theirs and rel not in changes:
            continue                      # repo-only and untouched: not our business
        incoming = target.get(rel, (None, None))[1]
        if w == incoming:
            continue                      # already what Overleaf has
        if w is None:
            continue                      # deleted locally; nothing to lose
        if cfg.artifacts.match(rel):
            if rel in changes:
                to_backup.append(rel)     # Overleaf changed it: save ours, take theirs
            continue                      # Overleaf did not: leave it alone
        refused.append(rel)
    for rel, entry in sorted(changes.items()):
        if rel in ours or entry is None:
            continue
        w = git.hash_file(cfg.abs(cfg.mirror(rel)))
        if w is None and os.path.lexists(cfg.abs(cfg.mirror(rel))):
            refused.append(rel)
        elif w is not None and w != entry[1]:
            # Overleaf adds a path where an untracked file already sits.
            (to_backup if cfg.artifacts.match(rel) else refused).append(rel)
    if refused:
        raise SyncError(
            "Uncommitted edits to source files in the mirror differ from what "
            "Overleaf has.\nOverleaf wins on a pull, so these would be lost; "
            "commit, stash or discard them first:\n"
            + "\n".join(f"    {cfg.mirror(p)}" for p in refused)
        )

    # -- build the merged tree in a scratch index -----------------------
    records = []
    for rel, entry in sorted(changes.items()):
        mode, sha = entry if entry else ("0", ZERO)
        records.append(f"{mode} {sha}\t{cfg.mirror(rel)}")
    payload = ("\0".join(records) + "\0").encode()
    scratch = ctx.temp_index()
    try:
        env = {"GIT_INDEX_FILE": scratch}
        git.run("read-tree", head, env=env)
        if records:
            git.run("update-index", "-z", "--index-info", env=env, input=payload)
        tree = git.out("write-tree", env=env)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)

    # -- verify before anything moves: only repo-only paths may differ --
    verify_mirror(ctx, tip, f"{tree}:{cfg.prefix}", allowed=pending)

    # -- message ---------------------------------------------------------
    n = len(git.out("rev-list", f"{base}..{tip}").split())
    msg = [f"Merge Overleaf edits into {cfg.prefix}/", ""]
    msg.append(f"{n} commit{'s' if n != 1 else ''} from Overleaf ({base[:7]}..{tip[:7]}):")
    msg.append("")
    msg += describe_commits(git, base, tip)
    if discarded:
        msg += ["", "Local changes overwritten (Overleaf wins); recover with",
                f"`git show {head[:7]}:{cfg.prefix}/<path>`:"]
        msg += [f"    {p}" for p in discarded]
    commit = git.out("commit-tree", tree, "-p", head, "-p", tip,
                     input="\n".join(msg).encode() + b"\n")

    # -- apply: backups, index, working tree, then HEAD ------------------
    for rel in to_backup:
        res.backups.append(ctx.backup(cfg.mirror(rel)))
    if records:
        git.run("update-index", "-z", "--index-info", input=payload)
    checkout = []
    for rel, entry in sorted(changes.items()):
        path = cfg.abs(cfg.mirror(rel))
        if entry is None:
            if os.path.lexists(path):
                os.unlink(path)
                try:
                    os.removedirs(os.path.dirname(path))
                except OSError:
                    pass
        elif git.hash_file(path) != entry[1]:
            checkout.append(cfg.mirror(rel))
    if checkout:
        git.run("checkout-index", "-f", "-z", "--stdin",
                input=("\0".join(checkout) + "\0").encode())
    git.run("update-ref", "-m", "overleaf: Overleaf-wins pull", "HEAD", commit, head)
    git.proc("update-index", "-q", "--refresh")

    res.commit = commit
    res.changed = sorted(changes)
    return res


def verify_mirror(ctx: Ctx, tip: str, mirror_tree: str, allowed=()) -> list[str]:
    """Fail unless the mirror differs from Overleaf only by repo-only paths
    (and, after a targeted push, by the `allowed` still-unpublished ones).

    Returns the repo-only paths (those present here and absent on Overleaf).
    """
    git = ctx.git
    raw = git.run("diff", "--no-renames", "--name-status", "-z", tip, mirror_tree)
    toks = [t for t in raw.split("\0") if t]
    bad, extra = [], []
    for st, path in zip(toks[0::2], toks[1::2]):
        if path in allowed:
            continue
        (extra if st == "A" else bad).append((st, path))
    if bad:
        raise SyncError(
            "The mirror does not match Overleaf:\n"
            + "\n".join(f"    {st} {p}" for st, p in bad[:20])
        )
    return [p for _, p in extra]


# ── folding bookkeeping into the commit ─────────────────────────────────


def record(ctx: Ctx, paths: list[str], created: str | None, subject: str,
           notes: list[str] | None = None) -> str | None:
    """Commit the given paths (state file, pair copies).

    If this run created the merge commit that is still HEAD, the paths are
    folded into it, so a pull stays one commit; otherwise they get a small
    commit of their own.
    """
    git = ctx.git
    paths = [p for p in dict.fromkeys(paths) if os.path.lexists(ctx.cfg.abs(p))]
    if not paths:
        return None
    rc, out, _ = git.proc("status", "--porcelain", "-z", "--", *paths)
    if not out.strip("\0"):
        return None
    git.run("add", "--", *paths)
    tree = git.out("write-tree")
    head = git.rev("HEAD")
    if created and head == created:
        parents = git.out("rev-list", "--parents", "-n", "1", head).split()[1:]
        message = git.run("log", "-1", "--format=%B", head).rstrip("\n")
        if notes:
            message += "\n\n" + "\n".join(notes)
        env = {"GIT_AUTHOR_DATE": git.out("log", "-1", "--format=%aI", head)}
    else:
        parents = [head]
        message = subject + ("\n\n" + "\n".join(notes) if notes else "")
        env = None
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    new = git.out(*args, env=env, input=message.encode() + b"\n")
    git.run("update-ref", "-m", "overleaf: record sync state", "HEAD", new, head)
    return new


# ── post-pull check: report only what got worse ─────────────────────────


def parse_tex_log(path: str) -> dict:
    """Errors, undefined references and overfull boxes from a LaTeX log."""
    result = {"errors": [], "undefined": [], "overfull": []}
    if not os.path.isfile(path):
        return result
    with open(path, errors="replace") as fh:
        raw = fh.read().split("\n")
    # TeX wraps log lines at 79 columns; rejoin so long labels match.
    lines, buf = [], ""
    for line in raw:
        buf += line
        if len(line) == 79:
            continue
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    for i, line in enumerate(lines):
        if line.startswith("! ") and "==>" not in line:
            where = ""
            for nxt in lines[i + 1:i + 8]:
                m = re.match(r"l\.(\d+)\s*(.*)", nxt)
                if m:
                    where = f"  [near: {m.group(2).strip()[-50:]}]"
                    break
            result["errors"].append(line[2:].strip() + where)
            continue
        m = re.search(r"Label `([^']+)' multiply defined", line)
        if m:
            result["errors"].append(f"multiply-defined label {m.group(1)}")
            continue
        m = re.search(r"(Reference|Citation) `([^']+)' on page \S+ undefined", line)
        if m:
            result["undefined"].append(f"{m.group(1).lower()} {m.group(2)}")
            continue
        m = re.match(r"Overfull \\([hv])box \(([\d.]+)pt too (?:wide|high)\)", line)
        if m:
            snippet = ""
            if m.group(1) == "h" and i + 1 < len(lines):
                # The offending text; stable when lines above it shift.
                snippet = re.sub(r"\\[A-Za-z0-9/@.+]+ ", "", lines[i + 1])
                snippet = re.sub(r"\s+", " ", snippet).strip()[:60]
            result["overfull"].append(f"{m.group(1)}box: {snippet}" if snippet
                                      else f"{m.group(1)}box")
    result["undefined"] = sorted(set(result["undefined"]))
    result["errors"] = sorted(set(result["errors"]))
    result["overfull"] = sorted(result["overfull"])
    return result


def worse(prev: dict | None, now: dict) -> list[str]:
    """Lines describing what `now` has that `prev` did not."""
    out = []
    if prev is None:
        return out
    if prev.get("ok", True) and not now.get("ok", True):
        out.append("    now FAILS (it passed at the previous sync)")
    for key, title in (("errors", "new error"), ("undefined", "new undefined"),
                       ("overfull", "new overfull")):
        fresh = collections.Counter(now.get(key, [])) - collections.Counter(prev.get(key, []))
        for item, n in sorted(fresh.items()):
            out.append(f"    {title}: {item}" + (f"  (x{n})" if n > 1 else ""))
    return out


def summarize(m: dict) -> str:
    return (f"{'ok' if m.get('ok') else 'FAILING'}; {len(m['errors'])} error(s), "
            f"{len(m['undefined'])} undefined, {len(m['overfull'])} overfull")


def build_deck(ctx: Ctx, pair: Pair):
    """Compile a paired deck the way Overleaf does: from the mirror, with the
    mirror's own figures.  Output goes to a scratch directory so the tracked
    PDF in the mirror is not disturbed.  Returns (metrics, scratch pdf path,
    scratch dir)."""
    cfg = ctx.cfg
    src = cfg.abs(cfg.mirror(pair.remote))
    out = tempfile.mkdtemp(prefix="overleaf-deck-")
    name = os.path.splitext(os.path.basename(src))[0]
    cmd = f"{cfg.pdf_build} -outdir={shell_quote(out)} {shell_quote(os.path.basename(src))}"
    proc = subprocess.run(cmd, shell=True, cwd=os.path.dirname(src),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    metrics = parse_tex_log(os.path.join(out, name + ".log"))
    pdf = os.path.join(out, name + ".pdf")
    metrics["ok"] = proc.returncode == 0 and os.path.isfile(pdf)
    if not metrics["ok"] and not metrics["errors"]:
        tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1:]
        metrics["errors"] = [("build failed: " + tail[0]) if tail else "build failed"]
    return metrics, pdf, out


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main_tex(ctx: Ctx):
    """The manuscript's root file, relative to the prefix: `main` in the
    config, else the only top-level .tex with a \\documentclass."""
    cfg = ctx.cfg
    if cfg.main:
        return cfg.main
    found = []
    top = cfg.abs(cfg.prefix)
    for name in sorted(os.listdir(top)) if os.path.isdir(top) else []:
        if not name.endswith(".tex"):
            continue
        try:
            with open(os.path.join(top, name), errors="replace") as fh:
                head = fh.read(20000)
        except OSError:
            continue
        if re.search(r"^[^%\n]*\\documentclass", head, re.M):
            found.append(name)
    return found[0] if len(found) == 1 else None


def run_manuscript_check(ctx: Ctx):
    """Run `[post_pull] check`, or failing that the configured [[checks]].
    Returns (metrics, last output lines), or (None, []) with nothing to run."""
    cfg = ctx.cfg
    if cfg.check_cmd:
        say(f"== post-pull check: {cfg.check_cmd} ==")
        runs = [(cfg.check_cmd, ctx.root)]
    elif cfg.checks:
        say("== post-pull check: the configured checks ==")
        runs = [(c["run"], os.path.join(ctx.root, cfg.prefix, c.get("cwd", ".")))
                for c in cfg.checks]
    else:
        say("== post-pull check ==")
        say("   no [[checks]] or [post_pull] check configured; manuscript not checked")
        return None, []
    ok, tail = True, []
    for cmd, cwd in runs:
        try:
            proc = subprocess.run(cmd, shell=True, cwd=cwd,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        except OSError as exc:
            ok, tail = False, [str(exc)]
            break
        if proc.returncode != 0:
            ok = False
            tail = [f"$ {cmd}"] + proc.stdout.decode("utf-8", "replace").strip().splitlines()[-12:]
            break
    log = cfg.check_log
    if not log:
        main = main_tex(ctx)
        log = os.path.splitext(main)[0] + ".log" if main else None
    metrics = parse_tex_log(cfg.abs(cfg.mirror(log))) if log else \
        {"errors": [], "undefined": [], "overfull": []}
    metrics["ok"] = ok
    return metrics, tail


def run_checks(ctx: Ctx, rebuild_for: set[str] | None = None, store: bool = True):
    """Run the manuscript check and compile each paired deck, without
    blocking: failures are reported, never raised.  Compares against the
    results stored at the previous sync and prints only what got worse.

    rebuild_for: Overleaf paths changed by the pull.  Under the "rebuild" PDF
    policy, a deck whose directory is among them gets its fresh PDF copied
    over the mirror's stale one.  Returns the Overleaf paths of rebuilt PDFs.
    """
    cfg, state = ctx.cfg, ctx.state
    checks = state.data.setdefault("checks", {})
    rebuilt = []
    worse_lines = []

    metrics, tail = run_manuscript_check(ctx)
    if metrics is not None:
        name = "manuscript"
        prev = checks.get(name)
        say(f"   {name}: {summarize(metrics)}"
            + ("" if prev else "  (first run; recorded as the baseline)"))
        diff = worse(prev, metrics)
        if diff:
            worse_lines += [f"  {name}:"] + diff
            if not metrics["ok"]:
                worse_lines += ["    last lines of the check:"] + [f"      {l}" for l in tail]
        elif not metrics["ok"]:
            say("   (still failing, as at the previous sync)")
        if store:
            checks[name] = metrics

    for pair in cfg.pdf_products():
        src = cfg.abs(cfg.mirror(pair.remote))
        if not os.path.isfile(src):
            continue
        name = f"deck {pair.remote}"
        if not shutil.which(cfg.pdf_build.split()[0]):
            say(f"   {name}: skipped ({cfg.pdf_build.split()[0]} not found)")
            continue
        metrics, pdf, scratch = build_deck(ctx, pair)
        try:
            prev = checks.get(name)
            say(f"   {name}: {summarize(metrics)}"
                + ("" if prev else "  (first run; recorded as the baseline)"))
            diff = worse(prev, metrics)
            if diff:
                worse_lines += [f"  {name}:"] + diff
            if store:
                checks[name] = metrics
            folder = os.path.dirname(pair.pdf) + "/"
            stale = rebuild_for and any(
                p.startswith(folder) and p != pair.pdf for p in rebuild_for
            ) and pair.pdf not in rebuild_for
            if stale and cfg.pdf_policy == "rebuild":
                if metrics["ok"]:
                    dest = cfg.abs(cfg.mirror(pair.pdf))
                    shutil.copyfile(pdf, dest)
                    rebuilt.append(pair.pdf)
                else:
                    say(f"   {pair.pdf} is stale (its sources changed on Overleaf) "
                        "but the deck does not build; not rebuilt.")
            elif stale:
                say(f"   {pair.pdf} on Overleaf is now older than its sources "
                    "(PDF policy \"never\": not rebuilt).")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    say()
    if worse_lines:
        say("WORSE than at the previous sync:")
        for line in worse_lines:
            say(line)
    else:
        say("Nothing got worse since the previous sync.")
    return rebuilt


# ── commands ────────────────────────────────────────────────────────────


def finish_pull(ctx: Ctx, res: PullResult, results: list[PairResult],
                extra_paths: list[str], subject: str) -> None:
    """Save state and commit it with the pair copies that were clean before."""
    ctx.state.data["overleaf_tip"] = res.tip
    ctx.state.save()
    paths = [ctx.cfg.state_file] + extra_paths
    paths += [r.pair.local for r in results
              if r.wrote_local and r.local_was_clean and r.status != "conflict"]
    notes = []
    synced = [r for r in results if r.status in ("fast-forwarded", "merged")]
    if synced:
        notes.append("Paired copies:")
        notes += [f"    {r.status}  {r.pair.local}  <-  {r.pair.remote}" for r in synced]
    record(ctx, paths, res.commit, subject, notes)


def cmd_pull_theirs(ctx: Ctx, args) -> int:
    git, cfg = ctx.git, ctx.cfg
    res = pull_theirs(ctx)
    if res.commit:
        say(f"Merged Overleaf {res.base[:7]}..{res.tip[:7]} into {cfg.prefix}/ "
            f"as {res.commit[:7]}.")
        say()
        for line in describe_commits(git, res.base, res.tip):
            say("  " + line)
        say()
        if res.discarded:
            say("Local changes overwritten (Overleaf wins); recover any of them with")
            say(f"`git show {res.old_head[:7]}:{cfg.prefix}/<path>`:")
            for p in res.discarded:
                say(f"    {p}")
            say()
        for b in res.backups:
            say(f"Saved your copy of a build artifact Overleaf replaced: {b}")
        if res.backups:
            say()

    results = sync_pairs_from_remote(
        ctx, git.ls_tree(res.tip),
        git.ls_tree(f"{res.old_head}:{cfg.prefix}") if res.commit else None)
    lines = report_pairs(results)
    if lines:
        say("Pairs:")
        for line in lines:
            say(line)
        say()

    rebuilt = []
    if res.commit and not args.no_check:
        rebuilt = run_checks(ctx, rebuild_for=set(res.changed))
        say()
    finish_pull(ctx, res, results, [], "Overleaf sync: record pair state")

    extra = verify_mirror(ctx, res.tip, f"HEAD:{cfg.prefix}") if res.commit else None
    if extra is not None:
        say(f"Mirror verified: {cfg.prefix}/ equals Overleaf {res.tip[:7]} "
            f"({len(extra)} repo-only file(s) aside).")

    ahead = [r for r in results if r.status in ("local-ahead", "merged")]
    if ahead:
        say("To publish the local pair edits:  subleaf push "
            + " ".join(shell_quote(r.pair.local) for r in ahead))
    for pdf in rebuilt:
        say(f"Rebuilt {cfg.mirror(pdf)} (its sources changed on Overleaf).")
        if sys.stdin.isatty() and not args.no_offer:
            say()
            push_files(ctx, [pdf], assume_yes=False, rebuild=False)
        else:
            say(f"  publish it with:  subleaf push {shell_quote(pdf)}")
    conflicts = [r for r in results if r.status in ("conflict", "no-base")]
    if conflicts:
        warn("")
        warn("Pairs needing a decision (the pull itself is complete):")
        for r in conflicts:
            warn(f"    {r.pair.local}: resolve it, then `subleaf pairs resolved "
                 f"{r.pair.local}`,")
            warn(f"        or take Overleaf's copy with `subleaf pairs take-remote "
                 f"{r.pair.local}`")
        return 2
    return 0


# -- targeted push ---------------------------------------------------------


class Outgoing:
    def __init__(self, remote: str):
        self.remote = remote          # Overleaf path
        self.blob = None              # None = delete
        self.mode = "100644"
        self.source = None            # repo-relative file the content came from
        self.pair: Pair | None = None
        self.merged = False
        self.note = ""


def resolve_targets(ctx: Ctx, names: list[str], delete: bool, all_pairs: bool,
                    tip_tree: dict) -> list[Outgoing]:
    git, cfg = ctx.git, ctx.cfg
    by_local = {p.local: p for p in cfg.pairs}
    by_remote = {p.remote: p for p in cfg.pairs}
    targets: dict[str, Outgoing] = {}

    def add_pair(pair: Pair):
        out = Outgoing(pair.remote)
        out.pair = pair
        targets[pair.remote] = out

    if all_pairs:
        for pair in cfg.pairs:
            st, _ = pair_status(ctx, pair, tip_tree.get(pair.remote, (None, None))[1])
            if st in ("local-ahead", "diverged"):
                add_pair(pair)
    for name in names:
        name = os.path.normpath(name)
        if os.path.isabs(name):
            name = os.path.relpath(name, ctx.root)
        if name in by_local:
            add_pair(by_local[name])
            continue
        if name == cfg.prefix or name.startswith(cfg.prefix + "/"):
            rel = name[len(cfg.prefix) + 1:]
        else:
            rel = name
        if not rel or rel.startswith(".."):
            raise SyncError(f"{name}: not a path inside {cfg.prefix}/")
        if rel in by_remote:
            add_pair(by_remote[rel])
            continue
        path = cfg.abs(cfg.mirror(rel))
        if os.path.isdir(path):
            raise SyncError(f"{name}: is a directory; name the files to push")
        out = Outgoing(rel)
        if os.path.isfile(path):
            if delete:
                raise SyncError(f"{name}: --delete given but {cfg.mirror(rel)} still exists")
            out.source = cfg.mirror(rel)
        elif rel in tip_tree:
            if not delete:
                raise SyncError(
                    f"{cfg.mirror(rel)} does not exist locally; to remove it from "
                    "Overleaf pass --delete")
        else:
            raise SyncError(f"{name}: no such file in {cfg.prefix}/ or on Overleaf")
        targets[rel] = out

    for rel, out in targets.items():
        if cfg.is_local_only(rel, tip_tree):
            raise SyncError(
                f"{rel} is local_only ({cfg.local_only.which(rel)}); it never goes to Overleaf")
        if cfg.held_back(rel, tip_tree):
            raise SyncError(f"{rel} is a compiled PDF and the PDF policy is \"never\"")
    return list(targets.values())


def prepare_outgoing(ctx: Ctx, out: Outgoing, tip_tree: dict) -> None:
    """Fill in the blob to publish.  Pairs are reconciled here, in reverse:
    local-only change -> publish the local copy; both changed -> merge-file."""
    git, cfg = ctx.git, ctx.cfg
    if out.pair is None:
        if out.source is None:
            return
        path = cfg.abs(out.source)
        out.blob = git.hash_file(path, write=True)
        out.mode = "100755" if os.access(path, os.X_OK) else "100644"
        return
    pair = out.pair
    local = git.hash_file(cfg.abs(pair.local), write=True)
    mirror = git.hash_file(cfg.abs(cfg.mirror(pair.remote)), write=True)
    base = ctx.state.base(pair)
    if local is None:
        raise SyncError(f"pair {pair.local}: no local copy to push")
    out.source = pair.local
    if mirror is None or local == mirror or mirror == base:
        out.blob = local
    elif base is None:
        raise SyncError(
            f"pair {pair.local}: differs from {cfg.mirror(pair.remote)} and no synced "
            "version is recorded.\nSettle it first with `subleaf pairs resolved "
            f"{pair.local}` (keep local) or `take-remote`.")
    elif local == base:
        out.blob = mirror
        out.source = cfg.mirror(pair.remote)
        out.note = "mirror copy is newer; the local copy will be updated to match"
        out.merged = True
    else:
        with open(cfg.abs(pair.local), "rb") as fh:
            ours = fh.read()
        merged, conflicts = three_way(
            git, git.blob(mirror), git.blob(base), ours,
            (f"mirror {cfg.mirror(pair.remote)}", "last synced", f"local {pair.local}"),
        )
        if conflicts:
            raise SyncError(
                f"pair {pair.local}: the local copy and {cfg.mirror(pair.remote)} both "
                "changed since the last sync and do not merge cleanly.\n"
                "Nothing was pushed.  Reconcile the two files, then run "
                f"`subleaf pairs resolved {pair.local}`.")
        out.blob = git.out("hash-object", "-w", "--stdin", input=merged)
        out.merged = True
        out.note = "merged with edits made directly in the mirror"


def last_author(git: Git, tip: str, path: str) -> str:
    who = git.out("log", "-1", "--format=%an, %ar", tip, "--", path)
    return who or "never touched"


def push_files(ctx: Ctx, names: list[str], assume_yes: bool = False,
               dry_run: bool = False, show_diff: bool = False, delete: bool = False,
               all_pairs: bool = False, rebuild: bool = True) -> int:
    git, cfg = ctx.git, ctx.cfg
    ctx.require_remote()
    ctx.require_nothing_staged()
    tip = ctx.fetch()
    head = git.rev("HEAD")
    if not git.is_ancestor(tip, head):
        raise SyncError(
            "Overleaf has commits this branch has not ingested, so a push could "
            "overwrite them.\nRun 'subleaf pull --theirs', then try again.")
    tip_tree = git.ls_tree(tip)

    targets = resolve_targets(ctx, names, delete, all_pairs, tip_tree)
    if not targets:
        say("Nothing to push." if all_pairs else "No files named; nothing to push.")
        return 0
    for out in targets:
        prepare_outgoing(ctx, out, tip_tree)

    # A deck whose sources go out makes its stored PDF stale: rebuild and send
    # it along (policy "rebuild"), unless the PDF was named explicitly.
    scratch_dirs = []
    named = {o.remote for o in targets}
    try:
        if rebuild and cfg.pdf_policy == "rebuild":
            for pair in cfg.pdf_products():
                folder = os.path.dirname(pair.pdf) + "/"
                moving = [o for o in targets if o.remote.startswith(folder)
                          and o.blob != tip_tree.get(o.remote, (None, None))[1]]
                if not moving or pair.pdf in named:
                    continue
                if not shutil.which(cfg.pdf_build.split()[0]):
                    say(f"({cfg.pdf_build.split()[0]} not found; {pair.pdf} not rebuilt)")
                    continue
                say(f"Rebuilding {pair.pdf} from the outgoing sources ...")
                metrics, pdf, scratch = build_deck_with(ctx, pair, moving)
                scratch_dirs.append(scratch)
                if not metrics["ok"]:
                    say("  the deck does not build with these sources:")
                    for e in metrics["errors"][:5]:
                        say(f"    {e}")
                    say(f"  {pair.pdf} is left as it is on Overleaf.")
                    continue
                extra = Outgoing(pair.pdf)
                extra.blob = git.out("hash-object", "-w", "--", pdf)
                extra.note = "rebuilt from the outgoing sources"
                extra.merged = True     # content does not come from a tracked file
                targets.append(extra)

        return _push_prepared(ctx, targets, tip, tip_tree, assume_yes, dry_run, show_diff)
    finally:
        for d in scratch_dirs:
            shutil.rmtree(d, ignore_errors=True)


def build_deck_with(ctx: Ctx, pair: Pair, moving: list[Outgoing]):
    """Build a deck from a scratch copy of its mirror folder with the outgoing
    blobs laid over it, i.e. exactly what Overleaf will hold after the push."""
    git, cfg = ctx.git, ctx.cfg
    folder = os.path.dirname(pair.remote)
    scratch = tempfile.mkdtemp(prefix="overleaf-deck-")
    work = os.path.join(scratch, "src")
    shutil.copytree(cfg.abs(cfg.mirror(folder)), work)
    for o in moving:
        dest = os.path.join(work, os.path.relpath(o.remote, folder))
        if o.blob is None:
            if os.path.exists(dest):
                os.unlink(dest)
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(git.blob(o.blob))
    name = os.path.splitext(os.path.basename(pair.remote))[0]
    out = os.path.join(scratch, "out")
    os.makedirs(out)
    cmd = f"{cfg.pdf_build} -outdir={shell_quote(out)} {shell_quote(name + '.tex')}"
    proc = subprocess.run(cmd, shell=True, cwd=work,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    metrics = parse_tex_log(os.path.join(out, name + ".log"))
    pdf = os.path.join(out, name + ".pdf")
    metrics["ok"] = proc.returncode == 0 and os.path.isfile(pdf)
    if not metrics["ok"] and not metrics["errors"]:
        metrics["errors"] = ["build failed"]
    return metrics, pdf, scratch


def _push_prepared(ctx: Ctx, targets: list[Outgoing], tip: str, tip_tree: dict,
                   assume_yes: bool, dry_run: bool, show_diff: bool) -> int:
    git, cfg = ctx.git, ctx.cfg

    # -- the commit: Overleaf's tree with exactly these paths replaced ----
    live = []
    for out in targets:
        if out.blob == tip_tree.get(out.remote, (None, None))[1]:
            say(f"  unchanged, skipped: {out.remote}")
        else:
            live.append(out)
    if not live:
        say("Overleaf already has this content; nothing to push.")
        _settle_pairs_after_push(ctx, targets, tip, created=None)
        return 0
    records = [
        f"{o.mode if o.blob else '0'} {o.blob or ZERO}\t{o.remote}" for o in live
    ]
    scratch = ctx.temp_index()
    try:
        env = {"GIT_INDEX_FILE": scratch}
        git.run("read-tree", tip, env=env)
        git.run("update-index", "-z", "--add", "--index-info", env=env,
                input=("\0".join(records) + "\0").encode())
        tree = git.out("write-tree", env=env)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)

    # -- verify: the diff is exactly the intended paths, each blob is its source
    raw = git.run("diff", "--no-renames", "--name-status", "-z", tip, tree)
    toks = [t for t in raw.split("\0") if t]
    status = dict(zip(toks[1::2], toks[0::2]))
    if set(status) != {o.remote for o in live}:
        raise SyncError("internal check failed: the push would touch "
                        f"{sorted(set(status) ^ {o.remote for o in live})} unexpectedly")
    new_tree = git.ls_tree(tree)
    for o in live:
        if o.blob and new_tree[o.remote][1] != o.blob:
            raise SyncError(f"internal check failed: wrong blob staged for {o.remote}")
        if o.blob and o.source and not o.merged:
            if git.hash_file(cfg.abs(o.source)) != o.blob:
                raise SyncError(f"{o.source} changed while the push was being prepared")

    # -- review -----------------------------------------------------------
    say()
    say("This push would change the Overleaf project as follows.")
    say()
    for o in sorted(live, key=lambda o: o.remote):
        st = status[o.remote]
        if st == "A":
            line = f"  add     {o.remote}"
        else:
            verb = "MODIFY" if st == "M" else "DELETE"
            line = f"  {verb}  {o.remote:<52} (Overleaf: {last_author(git, tip, o.remote)})"
        say(line)
        if o.pair is not None:
            say(f"            from {o.source}" + (f"; {o.note}" if o.note else ""))
        elif o.note:
            say(f"            {o.note}")
    say()
    say(" " + git.out("diff", "--shortstat", tip, tree))
    say()
    if show_diff:
        say(git.run("--no-pager", "diff", "--no-renames", tip, tree))
    if dry_run:
        say("Dry run; nothing was pushed.")
        return 0
    if not assume_yes:
        while True:
            try:
                sys.stdout.write("Push to Overleaf? [y/N/d=show full diff] ")
                sys.stdout.flush()
                reply = sys.stdin.readline()
            except KeyboardInterrupt:
                reply = ""
            reply = reply.strip().lower()
            if reply == "y":
                break
            if reply == "d":
                diff = git.run("--no-pager", "diff", "--no-renames", "--color=always"
                               if sys.stdout.isatty() else "--no-color", tip, tree)
                pager = os.environ.get("PAGER", "less -R")
                if sys.stdout.isatty():
                    subprocess.run(pager, shell=True, input=diff.encode())
                else:
                    say(diff)
                continue
            say("Aborted; nothing was pushed.")
            return 0

    # -- push: re-fetch first, and stop if Overleaf moved -----------------
    if ctx.fetch() != tip:
        raise SyncError(
            "Overleaf moved while this push was being reviewed.  Nothing was "
            "pushed.\nRun 'subleaf pull --theirs' and try again.")
    head = git.rev("HEAD")
    names = ", ".join(sorted(o.remote for o in live))
    subject = (f"{names} from {head[:7]}" if len(live) <= 3 else
               f"{len(live)} files from {head[:7]}")
    body = "\n".join(f"{status[o.remote]} {o.remote}" for o in sorted(live, key=lambda o: o.remote))
    commit = git.out("commit-tree", tree, "-p", tip,
                     input=f"{subject}\n\n{body}\n".encode())
    push_with_recovery(ctx, commit, tree, tip)
    say(f"Published to Overleaf as {commit[:7]}.")

    # -- merge back, then verify the mirror equals Overleaf ---------------
    # Lay the published content over the mirror (and over a pair's local copy
    # when the content was merged) so the pull finds it already in place.
    for o in live:
        dest = cfg.abs(cfg.mirror(o.remote))
        if o.blob is None:
            continue
        if git.hash_file(dest) != o.blob:
            ctx.write_blob(o.blob, cfg.mirror(o.remote))
        if o.pair is not None and git.hash_file(cfg.abs(o.pair.local)) != o.blob:
            ctx.write_blob(o.blob, o.pair.local)
    res = pull_theirs(ctx, quiet_if_current=True, keep_local=True)
    _settle_pairs_after_push(ctx, targets, res.tip, created=res.commit)
    extra = verify_mirror(ctx, res.tip, f"HEAD:{cfg.prefix}", allowed=set(res.pending))
    say(f"Merged back as {git.rev('HEAD')[:7]}; mirror verified: {cfg.prefix}/ equals "
        f"Overleaf {res.tip[:7]} ({len(extra)} repo-only file(s) aside).")
    if res.pending:
        say(f"Still unpublished ({len(res.pending)} committed local change(s) this push "
            "did not include):")
        for p in res.pending:
            say(f"    {p}")
    if res.discarded:
        say("Overwritten by a coauthor's edit that arrived with the merge "
            f"(recover with `git show {res.old_head[:7]}:{cfg.prefix}/<path>`):")
        for p in res.discarded:
            say(f"    {p}")
    if res.tip != commit:
        say("Note: Overleaf's tip is not the commit that was pushed "
            f"({res.tip[:7]} vs {commit[:7]}); a coauthor's edit arrived with the merge.")
    return 0


def _settle_pairs_after_push(ctx: Ctx, targets: list[Outgoing], tip: str, created) -> None:
    git, cfg, state = ctx.git, ctx.cfg, ctx.state
    tip_tree = git.ls_tree(tip)
    paths = [cfg.state_file]
    for o in targets:
        if o.pair is None:
            continue
        remote_blob = tip_tree.get(o.pair.remote, (None, None))[1]
        if remote_blob and git.hash_file(cfg.abs(o.pair.local)) == remote_blob:
            state.set_base(o.pair, remote_blob)
            paths.append(o.pair.local)   # now public: commit the local source too
    state.data["overleaf_tip"] = tip
    state.save()
    record(ctx, paths, created, "Overleaf sync: record pair state")


def push_with_recovery(ctx: Ctx, commit: str, tree: str, old_tip: str) -> None:
    """git push, fast-forward only, by object id.

    Overleaf sometimes answers a large push with "remote rejected ... Request
    timed out" and applies it anyway.  So a failed push is never retried
    blind: re-fetch, compare trees, and only push again if Overleaf is
    verifiably still where it was.
    """
    git, cfg = ctx.git, ctx.cfg
    settle = float(os.environ.get("OVERLEAF_SYNC_SETTLE", "10"))
    polls = int(os.environ.get("OVERLEAF_SYNC_POLLS", "3"))
    retries = int(os.environ.get("OVERLEAF_SYNC_RETRIES", "2"))
    attempt = 0
    while True:
        attempt += 1
        rc, out, err = git.proc("push", cfg.remote, f"{commit}:refs/heads/{cfg.branch}")
        if rc == 0:
            now = ctx.fetch()
            if now != commit and git.out("rev-parse", f"{now}^{{tree}}") != tree:
                raise SyncError(
                    f"The push succeeded but Overleaf's tip is {now[:7]}, not "
                    f"{commit[:7]}.  Run 'subleaf pull --theirs' and check.")
            return
        message = (err.strip() or out.strip())
        timed_out = "timed out" in message.lower()
        warn("git push reported a failure:")
        for line in message.splitlines()[-4:]:
            warn(f"    {line}")
        warn("Re-fetching to see what Overleaf actually did ...")
        now = old_tip
        for i in range(polls if timed_out else 1):
            if i:
                time.sleep(settle)
            now = ctx.fetch()
            if now != old_tip:
                break
        if now == commit or git.out("rev-parse", f"{now}^{{tree}}") == tree:
            warn("Overleaf applied the push despite the error; carrying on.")
            return
        if now != old_tip:
            raise SyncError(
                f"Overleaf moved to {now[:7]}, which is not this push.  Nothing "
                "more was sent.\nRun 'subleaf pull --theirs' and try again.")
        if attempt > retries:
            raise SyncError(
                f"The push failed {attempt} times and Overleaf is unchanged "
                f"({old_tip[:7]}).  Nothing was published.")
        warn(f"Overleaf is unchanged; retrying (attempt {attempt + 1} of {retries + 1}) ...")
        time.sleep(settle)


def cmd_push_files(ctx: Ctx, args) -> int:
    return push_files(ctx, args.paths, assume_yes=args.yes, dry_run=args.dry_run,
                      show_diff=args.diff, delete=args.delete, all_pairs=args.pairs,
                      rebuild=not args.no_rebuild)


# -- publish-tree: what a whole-tree `push` may send --------------------------------


def publish_tree(ctx: Ctx, tip: str) -> str:
    """HEAD's mirror tree with every held-back path (local_only, and compiled
    PDFs under policy "never") set to whatever Overleaf already has: a
    whole-tree push then never adds, changes or deletes them."""
    git, cfg = ctx.git, ctx.cfg
    ours = git.ls_tree(f"HEAD:{cfg.prefix}")
    theirs = git.ls_tree(tip)
    records = []
    for path in set(ours) | set(theirs):
        if not cfg.held_back(path, theirs):
            continue
        if ours.get(path) == theirs.get(path):
            continue
        mode, sha = theirs.get(path, ("0", ZERO))
        records.append(f"{mode} {sha}\t{path}")
    if not records:
        return git.out("rev-parse", f"HEAD:{cfg.prefix}")
    scratch = ctx.temp_index()
    try:
        env = {"GIT_INDEX_FILE": scratch}
        git.run("read-tree", f"HEAD:{cfg.prefix}", env=env)
        git.run("update-index", "-z", "--add", "--index-info", env=env,
                input=("\0".join(sorted(records)) + "\0").encode())
        return git.out("write-tree", env=env)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def cmd_publish_tree(ctx: Ctx, args) -> int:
    say(publish_tree(ctx, ctx.git.rev(ctx.cfg.tracking)))
    return 0


# -- status-extra --------------------------------------------------------------


def cmd_status_extra(ctx: Ctx, args) -> int:
    git, cfg = ctx.git, ctx.cfg
    tip = git.rev(cfg.tracking)
    ours = git.ls_tree(f"HEAD:{cfg.prefix}")
    theirs = git.ls_tree(tip)

    if not git.is_ancestor(tip, "HEAD"):
        rc, base, _ = git.proc("merge-base", "HEAD", tip)
        if rc == 0 and base.strip():
            say()
            say("What the incoming commits touch:")
            for line in describe_commits(git, base.strip().split("\n")[0], tip):
                say("    " + line)

    held = sorted(p for p in ours if cfg.is_local_only(p, theirs))
    if cfg.local_only.patterns:
        say()
        say(f"Local-only: {len(held)} file(s) in {cfg.prefix}/ that are never published "
            "(local_only in .overleaf-subtree.toml):")
        counts = collections.Counter(cfg.local_only.which(p) for p in held)
        for pattern in cfg.local_only.patterns:
            say(f"    {counts.get(pattern, 0):>4}  {pattern}")
        if args.all:
            for p in held:
                say(f"          {p}")
        else:
            say("    ('subleaf status --all' names every file)")
        shared = sorted(p for p in theirs if cfg.local_only.match(p))
        if shared:
            say(f"    {len(shared)} more match those patterns but are already on Overleaf; "
                "they are shared")
            say("    files and sync normally.")
            if args.all:
                for p in shared:
                    say(f"          {p}")

    if cfg.pairs:
        say()
        say("Pairs (working copy <-> Overleaf copy):")
        hints = []
        for pair in cfg.pairs:
            mirror_blob = git.hash_file(cfg.abs(cfg.mirror(pair.remote)))
            st, _ = pair_status(ctx, pair, mirror_blob)
            label = {
                "in-sync": "in sync      ",
                "local-ahead": "LOCAL AHEAD  ",
                "remote-ahead": "MIRROR AHEAD ",
                "diverged": "BOTH CHANGED ",
                "no-base": "UNSYNCED     ",
                "missing": "MISSING      ",
            }[st]
            say(f"    {label} {pair.local}  <->  {pair.remote}")
            if st == "local-ahead":
                hints.append(f"subleaf push {shell_quote(pair.local)}")
            elif st in ("remote-ahead", "diverged"):
                hints.append("subleaf pull --theirs    (reconciles the pairs)")
            elif st == "no-base":
                hints.append(f"subleaf pairs take-remote|resolved {shell_quote(pair.local)}")
        for h in dict.fromkeys(hints):
            say(f"      -> {h}")

    git.proc("update-index", "-q", "--refresh")
    rc, out, _ = git.proc("diff-files", "--name-only", "-z", "--", cfg.prefix)
    dirty = [p[len(cfg.prefix) + 1:] for p in out.split("\0") if p]
    art = [p for p in dirty if cfg.artifacts.match(p)]
    if art:
        say()
        say("Dirty build artifacts in the mirror (tolerated by pull --theirs and")
        say("targeted push; backed up before Overleaf's version replaces them):")
        for p in art:
            say(f"    {cfg.mirror(p)}")
    if cfg.pdf_policy == "never" and cfg.pdf_products():
        say()
        say("PDF policy \"never\": these compiled PDFs are not pushed:")
        for pair in cfg.pdf_products():
            say(f"    {pair.pdf}")
    return 0


# -- pairs --------------------------------------------------------------------


def cmd_pairs(ctx: Ctx, args) -> int:
    git, cfg, state = ctx.git, ctx.cfg, ctx.state
    if not args.action:
        if not cfg.pairs:
            say("No [[pair]] declared in .overleaf-subtree.toml.")
            return 0
        for pair in cfg.pairs:
            mirror_blob = git.hash_file(cfg.abs(cfg.mirror(pair.remote)))
            st, local = pair_status(ctx, pair, mirror_blob)
            say(f"{st:<13} {pair.local}  <->  {cfg.mirror(pair.remote)}")
            say(f"              local {str(local)[:7]}  mirror {str(mirror_blob)[:7]}  "
                f"last synced {str(state.base(pair))[:7]}")
        return 0
    ctx.require_nothing_staged()
    touched = [cfg.state_file]
    if args.action == "init":
        # Record every pair whose two copies are identical right now.
        for pair in cfg.pairs:
            mirror_blob = git.hash_file(cfg.abs(cfg.mirror(pair.remote)))
            if mirror_blob and mirror_blob == git.hash_file(cfg.abs(pair.local)):
                state.set_base(pair, mirror_blob)
                say(f"in sync    {pair.local}")
            else:
                say(f"DIFFERS    {pair.local}  (settle with take-remote or resolved)")
        if not args.no_commit:
            state.save()
            record(ctx, touched, None, "Overleaf sync: record pair state")
        else:
            state.save()
        return 0
    if not args.paths:
        raise SyncError(f"subleaf pairs {args.action}: name at least one pair")
    for name in args.paths:
        name = os.path.normpath(name)
        pair = next((p for p in cfg.pairs
                     if name in (p.local, p.remote, cfg.mirror(p.remote))), None)
        if pair is None:
            raise SyncError(f"{name}: not a declared pair")
        mirror_path = cfg.abs(cfg.mirror(pair.remote))
        mirror_blob = git.hash_file(mirror_path, write=True)
        if mirror_blob is None:
            raise SyncError(f"{cfg.mirror(pair.remote)} does not exist")
        if args.action == "take-remote":
            if os.path.isfile(cfg.abs(pair.local)) and \
                    git.hash_file(cfg.abs(pair.local)) != mirror_blob:
                say(f"saved your copy to {ctx.backup(pair.local)}")
            ctx.write_blob(mirror_blob, pair.local)
            state.set_base(pair, mirror_blob)
            say(f"{pair.local} now equals {cfg.mirror(pair.remote)}.")
        else:  # resolved
            with open(cfg.abs(pair.local), "rb") as fh:
                data = fh.read()
            if re.search(rb"^(<<<<<<<|>>>>>>>|\|\|\|\|\|\|\|) ", data, re.M):
                raise SyncError(f"{pair.local} still contains conflict markers")
            state.set_base(pair, mirror_blob)
            say(f"{pair.local}: recorded as containing Overleaf's version "
                f"({mirror_blob[:7]}).")
            if git.hash_file(cfg.abs(pair.local)) != mirror_blob:
                say(f"  publish it with:  subleaf push {shell_quote(pair.local)}")
    state.save()
    record(ctx, touched, None, "Overleaf sync: record pair state")
    return 0


# -- check / incoming --------------------------------------------------------


def cmd_check(ctx: Ctx, args) -> int:
    run_checks(ctx, rebuild_for=None, store=not args.no_store)
    if not args.no_store:
        ctx.state.save()
    return 0


def cmd_incoming(ctx: Ctx, args) -> int:
    git, cfg = ctx.git, ctx.cfg
    if args.range:
        base, tip = args.range
    else:
        tip = git.rev(cfg.tracking)
        base = git.out("merge-base", "HEAD", tip)
    lines = describe_commits(git, base, tip)
    for line in lines or ["Nothing incoming."]:
        say(line)
    return 0


# -- accept: strip \new{...} --------------------------------------------------


def _match_brace(text: str, start: int):
    """Index of the } closing the { at text[start], honouring \\-escapes and
    %-comments; None if it never closes."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "%":
            nl = text.find("\n", i)
            if nl < 0:
                return None
            i = nl + 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def strip_wrappers(text: str, macro: str = "new", lines: tuple | None = None,
                   _line0: int = 1):
    """Replace every \\macro{X} by X.  Returns (new text, [(line, preview)]).

    Only the exact control word is matched (\\newcommand, \\newline and the
    like are different tokens), braces are balanced the way TeX balances
    them, and occurrences inside comments are left alone.  Where removing
    the braces would change the meaning, they are kept or a space is added:
    after ^ or _ (the group was the script), and after a control word that
    would otherwise fuse with the first letter of X.
    """
    token = "\\" + macro
    out = []
    found = []
    i = 0
    n = len(text)
    line = _line0
    while i < n:
        ch = text[i]
        if ch == "%":
            nl = text.find("\n", i)
            nl = n if nl < 0 else nl
            out.append(text[i:nl])
            i = nl
            continue
        if ch != "\\":
            if ch == "\n":
                line += 1
            out.append(ch)
            i += 1
            continue
        j = i + len(token)
        is_macro = text.startswith(token, i) and not (
            j < n and (text[j].isalpha() or text[j] == "@"))
        if is_macro:
            k = j
            while k < n and text[k] in " \t":
                k += 1
            end = _match_brace(text, k) if k < n and text[k] == "{" else None
            in_range = lines is None or lines[0] <= line <= lines[1]
            if end is not None and in_range:
                inner, sub = strip_wrappers(text[k + 1:end], macro, None, line)
                preview = re.sub(r"\s+", " ", text[k + 1:end]).strip()
                found.append((line, preview[:60] + ("..." if len(preview) > 60 else "")))
                found += sub
                before = "".join(out[-40:])
                if re.search(r"[\^_]\s*\Z", before):
                    inner = "{" + inner + "}"
                elif re.search(r"\\[A-Za-z@]+\Z", before) and inner[:1].isalpha():
                    inner = " " + inner
                out.append(inner)
                line += text.count("\n", i, end + 1)
                i = end + 1
                continue
        # Any other control sequence: copy the backslash and the next
        # character together, so \% \{ \} \\ are never misread.
        chunk = text[i:i + 2]
        line += chunk.count("\n")
        out.append(chunk)
        i += 2
    return "".join(out), found


def cmd_accept(ctx: Ctx, args) -> int:
    cfg = ctx.cfg
    files = args.files
    if not files:
        main = main_tex(ctx)
        if not main:
            raise SyncError("name the .tex file(s) to accept in, or set `main` in "
                            ".overleaf-subtree.toml")
        files = [cfg.mirror(main)]
    span = None
    if args.lines:
        m = re.fullmatch(r"(\d+)[:-](\d+)", args.lines)
        if not m:
            raise SyncError("--lines wants START:END")
        span = (int(m.group(1)), int(m.group(2)))
    total = 0
    for name in files:
        path = name if os.path.isabs(name) else cfg.abs(name)
        if not os.path.isfile(path):
            path = cfg.abs(cfg.mirror(name))
        if not os.path.isfile(path):
            raise SyncError(f"{name}: no such file")
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        new, found = strip_wrappers(text, args.macro, span)
        rel = os.path.relpath(path, ctx.root)
        for line, preview in found:
            say(f"  {rel}:{line}: \\{args.macro}{{{preview}}}")
        total += len(found)
        if found and not args.dry_run:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(new)
    verb = "would be accepted" if args.dry_run else "accepted"
    say(f"{total} \\{args.macro}{{...}} wrapper(s) {verb}.")
    if total and not args.dry_run:
        say("Review with `git diff`, then publish:  subleaf push "
            + " ".join(shell_quote(f) for f in files))
    return 0


# -- latexdiff ----------------------------------------------------------------


def cmd_latexdiff(ctx: Ctx, args) -> int:
    """A latexdiff PDF between two versions.  Each revision may be an Overleaf
    commit (project at its root) or a commit of this repo (its prefix is used)."""
    git, cfg = ctx.git, ctx.cfg
    name = args.file or main_tex(ctx)
    if not name:
        raise SyncError("name the .tex file to diff, or set `main` in .overleaf-subtree.toml")
    for tool in ("latexdiff", "latexmk"):
        if not shutil.which(tool):
            raise SyncError(f"{tool} not found on PATH")
    scratch = tempfile.mkdtemp(prefix="subleaf-latexdiff-")
    try:
        for side, rev in (("old", args.rev1), ("new", args.rev2)):
            if git.ok("cat-file", "-e", f"{rev}:{cfg.prefix}/{name}"):
                tree = f"{rev}:{cfg.prefix}"
            elif git.ok("cat-file", "-e", f"{rev}:{name}"):
                tree = f"{rev}^{{tree}}"
            else:
                raise SyncError(f"{rev}: no {name} in that version")
            dest = os.path.join(scratch, side)
            os.makedirs(dest)
            archive = git.run("archive", tree, binary=True)
            subprocess.run(["tar", "-x", "-C", dest], input=archive, check=True)
        new = os.path.join(scratch, "new")
        with open(os.path.join(new, "subleafdiff.tex"), "wb") as fh:
            proc = subprocess.run(
                ["latexdiff", "--flatten", os.path.join("..", "old", name), name],
                cwd=new, stdout=fh, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            raise SyncError("latexdiff failed:\n" + proc.stderr.decode("utf-8", "replace")[-800:])
        # A diff document rarely compiles without complaint: force it through,
        # bounded in time, and judge by whether a PDF came out.
        try:
            build = subprocess.run(
                ["latexmk", "-pdf", "-f", "-interaction=nonstopmode", "subleafdiff.tex"],
                cwd=new, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
            tail = build.stdout.decode("utf-8", "replace").strip().splitlines()[-25:]
        except subprocess.TimeoutExpired:
            tail = ["latexmk did not finish within 10 minutes"]
        pdf = os.path.join(new, "subleafdiff.pdf")
        if not os.path.isfile(pdf) or not os.path.getsize(pdf):
            raise SyncError("the latexdiff document did not compile:\n" + "\n".join(tail))
        stem = os.path.splitext(os.path.basename(name))[0]
        out = args.output or (f"{stem}-latexdiff-{git.out('rev-parse', '--short', args.rev1)}"
                              f"-{git.out('rev-parse', '--short', args.rev2)}.pdf")
        shutil.copyfile(pdf, os.path.join(ctx.root, out) if not os.path.isabs(out) else out)
        say(f"wrote {out}")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
