# overleaf-subtree

Mirror **a subdirectory** of a git repository onto the **root** of an Overleaf
project, and review what a push would do before it reaches your coauthors.

```
myrepo/
  src/            code
  paper/     <->  the whole Overleaf project
  .overleaf-subtree.toml
```

## Bird's-eye view

`subleaf init` connects a subdirectory of your repo to an Overleaf project, once.
After that, two commands cover most of what you will do:

- **`subleaf pull`** gets the latest version from Overleaf and merges it into your
  repo.
- **`subleaf push`** uploads your local version to Overleaf. Before it does, it
  makes sure nothing has changed on Overleaf that you haven't seen. If a
  coauthor has edited the project since your last pull, the push stops and asks
  you to pull first, so their changes get merged in before anything is
  overwritten.

**Setting up with an agent.** In Claude Code or Codex, open your repository and
paste:

```
Set up Overleaf syncing for this repository with subleaf, following
https://raw.githubusercontent.com/tlamadon/overleaf-subtree/main/SETUP.md
```

The agent installs `subleaf`, asks for your Overleaf project link, helps you
create and store an Overleaf git token, and connects the two. After that, ask
it to "pull from Overleaf" or "push to Overleaf". [SETUP.md](SETUP.md) is the
script it follows.

## Why this exists

The existing sync tools — Overleaf's own integrations included, see
[Alternatives](#alternatives) — assume the repository root *is* the project
root. That excludes the common case where a manuscript lives inside a code
repository, next to the scripts that generate its numbers and figures, where you
want the paper versioned alongside the code that produces it. Publishing from
there means a push can quietly overwrite a coauthor who was editing in the
browser an hour ago.

What this tool does about that:

**Overleaf as a subtree.** One `prefix` in the config maps a subdirectory onto
the project root. The paper stays inside your repo, versioned in the same
history as the code that generates it; Overleaf still sees a project whose root
is `paper.tex`. Nothing about your repo layout has to change to suit the editor.

**A history that stays clean.** Pull is a real `git merge -Xsubtree`, so your
coauthors' commits enter your history under their own names and git does the
conflict detection. Push sends a *single* commit built from the prefix's tree
with `git commit-tree`, parented on the project's current tip — fast-forward by
construction. The tempting alternative, publishing a rewritten history with
`git subtree split`, poisons the repository permanently; see
[Design](#design-merge-history-in-publish-trees-out) for the failure mode that
prompted this tool.

**Verify before publishing.** A push overwrites whatever your coauthors
currently have, so `push` prints the additions, modifications and deletions
first, annotates each modification and deletion with **who last touched that
file on Overleaf and when**, and asks — with `d` to page through the full diff
before deciding.

**Checks that gate the push.** Configure any commands you like — compile the
document, grep the log for undefined references, run a linter. A non-zero exit
blocks publishing, so a manuscript that does not build never reaches your
coauthors. `subleaf check` runs them without touching the network.

**Generated files are called out by name.** Mark the paths your repo produces as
`repo_owned`. When someone edits one in the browser, that edit is doomed — the
next generator run overwrites it — so `status` and `push` list them apart from
ordinary content, and `pull` reminds you to look before regenerating.

**It refuses to act when the situation is ambiguous.** Push aborts if Overleaf
has commits you have not merged, rather than discarding them. Both `pull` and
`push` refuse to run against a dirty prefix or staged changes. Untracked files
are never published, and `status` lists them so their absence is not a
surprise.

**For projects where Overleaf is the source of truth.** `pull --theirs` takes
Overleaf's version of everything it has, tolerates the PDFs your editor
rebuilds in place, and tells you who changed which sections and what got worse
in the build. `push FILE...` publishes only the files you name. `local_only`
keeps your own notes and scripts out of the project, and *pairs* keep a working
copy elsewhere in the repo (a talk deck) in step with its copy on Overleaf. See
[When Overleaf is the source of truth](#when-overleaf-is-the-source-of-truth).

**Plain git underneath.** No web API, no scraping, no cookie jar, and no
dependencies beyond the standard library. Authentication is whatever your git
credential helper already does, and every command is one you could have typed
yourself.

## Install

```sh
uv tool install git+https://github.com/tlamadon/overleaf-subtree
```

This installs the `subleaf` command (sub*tree* + Over*leaf*). Then, once per
project, from inside your repository (or let an agent do all of this; see
[Setting up with an agent](#birds-eye-view)):

```sh
subleaf init
```

It asks for the Overleaf project (paste its link from the browser) and the
subdirectory that holds the paper, then adds the git remote, writes and commits
`.overleaf-subtree.toml`, and brings the Overleaf project into that
subdirectory. If the subdirectory already has files, they are merged, and any
file that differs on the two sides is left as a conflict for you to resolve.
The answers can also be given as flags, `--project` and `--prefix`.

Authentication is plain git — a credential helper, or the literal username
`git` with an Overleaf git token as the password. Requires Overleaf's git
integration.

## Configure

`subleaf init` writes a minimal `.overleaf-subtree.toml` at the repository root.
Everything it can hold:

```toml
prefix = "paper"          # the subdirectory that maps onto the project root
remote = "overleaf"       # git remote name
branch = "master"         # branch on that remote; Overleaf's is master

# Paths this repo generates (globs, relative to the prefix).  An edit made to
# one of these in the Overleaf editor is lost the next time the generator runs,
# so status and push call them out instead of treating them as ordinary files.
repo_owned = ["graphs/**/generated/*.tex"]

# Run before publishing.  Non-zero exit blocks the push.  cwd is relative to
# the prefix, so checks read as if the manuscript were the whole project.
[[checks]]
name = "compile"
run = "latexmk -pdf -interaction=nonstopmode -halt-on-error paper.tex"

[[checks]]
name = "no multiply-defined labels"
run = "! grep -q 'multiply defined' paper.log"
```

The core knows nothing about LaTeX; the checks are yours. The optional keys
below, all for the [Overleaf-wins workflow](#when-overleaf-is-the-source-of-truth),
are where LaTeX comes in (reading the log, rebuilding a deck). Top-level keys go
above the first `[[checks]]`.

```toml
main = "paper.tex"        # root document; found automatically if there is only one

# Never added to Overleaf.  A matching file Overleaf already has is shared and
# syncs normally.
local_only = ["notes/**", "scripts/**"]

# Tracked build products an editor may rewrite in place.  Anything not matched
# here is source, and a dirty source file stops 'pull --theirs'.
artifacts = ["**/*.pdf"]

# backup_dir = "~/.local/state/subleaf/<repo>"   # where replaced artifacts are saved
# state_file = ".overleaf-state.json"            # committed bookkeeping

[pdf]
policy = "rebuild"        # or "never": do not push compiled PDFs of pairs
build = "latexmk -pdf -interaction=nonstopmode -halt-on-error"

[post_pull]
# check = "make check"    # default: the [[checks]] above
# log = "paper.log"       # default: the main document's log

[[pair]]
local = "slides/talk.tex"            # relative to the repository
remote = "slides/talk.tex"           # relative to the Overleaf project
pdf = "slides/talk.pdf"              # its compiled copy in the project, if any

[[pair]]                             # several files with the same names
local_dir = "slides/graphs"
remote_dir = "slides/graphs"
files = ["fig1.pdf", "fig2.pdf"]
```

## Use

```sh
subleaf status    # what is unmerged, and what a push would change
                  #   --short: one line; --json: everything; --cached: no network
subleaf diff      # full content diff against the project
subleaf pull      # merge the project's commits into this repo
subleaf check     # run the checks, no network
subleaf push      # review the outgoing change, confirm, publish

subleaf pull --theirs      # Overleaf wins: its version of every file it has
subleaf push FILE...       # publish only these files, then merge back
subleaf pairs              # working copies kept outside the prefix
subleaf check --report     # only what got worse since the previous sync
subleaf accept             # strip \new{...} review markup
subleaf latexdiff A B      # diff PDF between two versions
```

`push --dry-run` runs the checks and prints the review, then stops. `push
--yes` skips the confirmation.

`push` shows:

```
This push would change the Overleaf project as follows.

  add     notes/appendix.tex
  MODIFY  paper.tex                    (there: your.coauthor, 2 hours ago)
  DELETE  graphs/old-figure.tex        (there: never touched there)

 3 files changed, 41 insertions(+), 12 deletions(-)

Push to Overleaf? [y/N/d=show full diff]
```

## When Overleaf is the source of truth

`pull` and `push` treat the two sides as equals: a 3-way merge in, the whole
prefix out. Many collaborations are not like that. The coauthors work on
Overleaf, their version is the reference, and only a few files ever go back.

```sh
subleaf pull --theirs          # Overleaf's version of everything it has
# ... edit ...
subleaf push paper.tex         # publish exactly this, then merge back
```

**`pull --theirs`** makes the prefix equal the project for every path Overleaf
has, in one merge commit, verified before anything moves.

- Local commits to Overleaf's files that were never pushed are overwritten.
  They are listed, with the `git show` that recovers them. Publish before you
  pull to keep an edit.
- Files that exist only here are kept: `local_only`, and files added but not
  pushed yet. A file a coauthor deleted is deleted here.
- A dirty file matching `artifacts` does not block it. It is left alone if
  Overleaf did not change it; otherwise it is saved under `backup_dir` and
  replaced. A dirty source file stops the pull unless it already equals the
  incoming version.
- The merge message replaces "Update on Overleaf." with each incoming commit's
  author, time and files, and for `.tex` files the sections (or beamer frames)
  the change touched.
- Afterwards the checks run without blocking, and only what got **worse** since
  the previous sync is printed: the checks newly failing, new errors, new
  undefined references, new overfull boxes. `subleaf check --report` does this
  on its own.

**`push FILE...`** publishes Overleaf's current tree with the named paths
replaced, so nothing else can ride along. Paths are relative to the project
(`paper.tex`) or the repo (`paper/paper.tex`). The content is what is on disk,
committed or not. It shows the same review as `push`, re-fetches before pushing
and stops if Overleaf moved, then merges back and verifies the prefix against
the project. `--dry-run` and `--yes` work as for `push`; `--diff` prints the
full diff; `--delete` removes a named file from the project.

Overleaf sometimes answers a large push with `Request timed out` and applies it
anyway. A failed push is never retried blind: `subleaf` re-fetches and compares
trees, treats an applied push as done, and retries only if the project is
verifiably unchanged.

**Pairs** are for a file edited outside the prefix with a copy inside the
project, such as a talk deck built in `slides/`. On `pull --theirs`, a change
on Overleaf fast-forwards the working copy; if both changed they are merged
with `git merge-file` against the last-synced version; a real conflict leaves
markers in the working copy (the original is backed up), the pull completes
with exit status 2, and `subleaf pairs resolved PATH` or
`subleaf pairs take-remote PATH` settles it. `subleaf push slides/talk.tex`
publishes a pair by its local name, and `push --pairs` every pair that is
ahead. With `pdf` set and `[pdf] policy = "rebuild"`, the compiled PDF stored
in the project is rebuilt when its sources change in either direction.

**Review markup.** Define `\newcommand{\new}[1]{\textcolor{red}{#1}}` in the
manuscript and mark proposed edits with it. `subleaf accept` replaces every
`\new{X}` by `X` with balanced braces and leaves any other `\textcolor` alone
(`--dry-run` lists them, `--lines A:B` restricts the range).
`subleaf latexdiff REV1 REV2` builds a diff PDF between two versions; each may
be an Overleaf commit or a commit of this repo.

`.overleaf-state.json` holds what has to survive between runs: per pair, the
blob both copies last agreed on, and the previous check results. It is
committed, folded into the pull's merge commit.

`tests/test_sync.sh` runs all of this against a throwaway stand-in for the
Overleaf project.

## Using it from a coding agent

Claude Code and Codex can both drive `subleaf`, and see where the paper stands
at the start of every session. Set that up once per repository:

```sh
subleaf agent-setup
```

(`subleaf init` offers to do this at the end.) It writes:

- **A session-start hook** in `.claude/settings.json` and `.codex/hooks.json`
  that runs `subleaf status --short`. Its output goes into the agent's context,
  so before touching the paper the agent already knows something like

  ```
  Overleaf: 2 new commits to pull (jane, 3h ago: main.tex, intro.tex) · 1 file to push (appendix.tex) · both sides changed, pull before pushing
  ```

  and can tell you, rather than editing a stale copy. The hook is silent in
  repositories without `subleaf`, never prompts for credentials, and gives up on
  the network after ten seconds, falling back to the last fetch. Codex also
  needs `[features] hooks = true` in `.codex/config.toml`, which this adds, and
  asks once to trust the project's hooks.
- **Instructions** in `CLAUDE.md` and `AGENTS.md`, between `subleaf` markers, on
  how to pull and push. An agent cannot answer push's `y/N` prompt (without a
  terminal, `push` stops with nothing published), so it shows you the review
  with `subleaf push --dry-run` and publishes with `--yes` only after you approve.

Existing settings and instructions are added to, never replaced, and rerunning
it is safe. Commit the files so your coauthors' agents get them too.

`--statusline` also shows the line under the prompt in Claude Code's status line
(`subleaf status --short --cached`, which reads the last fetch rather than
contacting Overleaf on every refresh). It leaves an existing status line alone.
Codex's status line takes only built-in items, so there the hook is all there is.

For anything beyond one line, `subleaf status --json` reports the incoming
commits and files, the outgoing changes with who last touched each file on
Overleaf, and the warnings, as structured data.

## Design: merge history in, publish trees out

The two directions are deliberately asymmetric.

**Pull is a real merge.** `git merge -Xsubtree=<prefix>` brings the coauthors'
commits into your history with their authorship, and git does the conflict
detection. That history is also what makes the "who last touched this" column
possible.

**Push sends one commit built from the prefix's tree**, parented on the
project's current tip, via `git commit-tree`. It is fast-forward by
construction. Afterwards that commit is merged back into your branch with
`git merge -s ours`, which leaves your files untouched but records that
Overleaf now holds your version. Without that record, the next pull would
compare against the project as it was before your push, and a coauthor's edit
next to lines you had just published would conflict with your own change.

The obvious alternative is to publish a rewritten history with `git subtree
split`. Don't. Its output travels to Overleaf and returns on your next pull as
synthesized twins of your own commits — same trees, same `git-subtree-split`
trailers. Two commits carrying the same `git-subtree-mainline` is enough to make
every later split abort with

```
fatal: cache for <sha> already exists!
```

and clearing the cache does not help, because the twins are in the history.
This tool exists partly because that happened.

## Alternatives

This is a small tool in a crowded space, and for most people one of these is the
better answer. The distinction that matters is **what it talks to**: Overleaf's
git remote (needs a paid plan, but it is real git) or the web API behind the
editor (works on a free account, but is unofficial and breaks when the site
changes).

**Start with Overleaf's own integrations.** The
[git integration](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization/git-integration)
gives the project a git remote, and
[GitHub synchronization](https://docs.overleaf.com/integrations-and-add-ons/git-integration-and-github-synchronization)
links it to a repo without any local tooling. Both are paid. If the repo root
*is* the project root and you don't need a review step, you need nothing else.

Over the git remote:

| | |
|---|---|
| [overleaf-git-sync](https://github.com/genggng/overleaf-git-sync) | The closest thing to this tool, and more mature. Imports the project into a managed branch, so git does the conflict detection, and `push --dry-run` previews before writing. Assumes repo root = project root. |
| [overleaf_sync_with_git](https://github.com/subhamX/overleaf_sync_with_git) | Aimed at automated backup and CI rather than interactive coauthoring; also works with self-hosted instances. |
| [olgitbridge](https://github.com/chazeon/olgitbridge) · [overleaf-gitbridge](https://github.com/camillemndn/overleaf-gitbridge) | For self-hosted Overleaf CE, which has no git bridge of its own. |

Over the web API, no paid plan needed:

| | |
|---|---|
| [overleaf-sync](https://github.com/moritzgloeckl/overleaf-sync) | The established one (~390 stars). Cookie auth via a browser popup, whole-project sync with `.olignore`. Last released 2024. |
| [LocalLeaf](https://github.com/jazielloureiro/LocalLeaf) | A maintained fork of the above, if it has stopped working for you. |
| [overleaf-sync-rs](https://github.com/katzper-michno/overleaf-sync-rs) | Same idea in Rust. |

**Why this one exists.** None of the above map a *subdirectory* onto the project
root, so none of them fit a manuscript that lives inside a code repository next
to the scripts generating its figures. That is the whole reason for this tool;
if your paper is its own repo, you do not need it. The push review is a second,
smaller reason — `overleaf-git-sync` also confirms before writing, but here each
modified and deleted file is annotated with who last touched it on Overleaf, and
`repo_owned` marks the generated files where a coauthor's edit is about to be
overwritten.

## Limits

- Overleaf's git integration is a paid feature; this tool has no web-API
  backend and will not get one. If you need to work from a free account, use
  one of the web-API tools above.
- Untracked files under the prefix are never published. `status` lists them.
- Conflicts are resolved by you, in the working tree, with git. The tool will
  not merge unattended. (`pull --theirs` does not merge the prefix at all; it
  takes Overleaf's version.)
- `check --report`, `accept` and `latexdiff` assume LaTeX. Section names in
  merge messages come from `\section`, `\subsection` and beamer frame titles.
