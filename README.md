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

Two commands cover most of what you will do:

- **`subleaf pull`** gets the latest version from Overleaf and merges it into your
  repo.
- **`subleaf push`** uploads your local version to Overleaf. Before it does, it
  makes sure nothing has changed on Overleaf that you haven't seen. If a
  coauthor has edited the project since your last pull, the push stops and asks
  you to pull first, so their changes get merged in before anything is
  overwritten.

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

**Plain git underneath.** No web API, no scraping, no cookie jar, and no
dependencies beyond the standard library. Authentication is whatever your git
credential helper already does, and every command is one you could have typed
yourself.

## Install

```sh
uv tool install git+https://github.com/tlamadon/overleaf-subtree
```

This installs the `subleaf` command (sub*tree* + Over*leaf*). Then, once per
project:

```sh
git remote add overleaf https://git@git.overleaf.com/<project-id>
```

Authentication is plain git — a credential helper, or the literal username
`git` with an Overleaf git token as the password. Requires Overleaf's git
integration.

## Configure

`.overleaf-subtree.toml` at the repository root:

```toml
prefix = "paper"          # the subdirectory that maps onto the project root
remote = "overleaf"       # git remote name
branch = "main"           # branch on that remote

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

Nothing in the tool knows about LaTeX. The checks are yours.

## Use

```sh
subleaf status    # what is unmerged, and what a push would change
subleaf diff      # full content diff against the project
subleaf pull      # merge the project's commits into this repo
subleaf check     # run the checks, no network
subleaf push      # review the outgoing change, confirm, publish
```

`push` shows:

```
This push would change the Overleaf project as follows.

  add     notes/appendix.tex
  MODIFY  paper.tex                    (there: your.coauthor, 2 hours ago)
  DELETE  graphs/old-figure.tex        (there: never touched there)

 3 files changed, 41 insertions(+), 12 deletions(-)

Push to Overleaf? [y/N/d=show full diff]
```

## Design: merge history in, publish trees out

The two directions are deliberately asymmetric.

**Pull is a real merge.** `git merge -Xsubtree=<prefix>` brings the coauthors'
commits into your history with their authorship, and git does the conflict
detection. That history is also what makes the "who last touched this" column
possible.

**Push sends one commit built from the prefix's tree**, parented on the
project's current tip, via `git commit-tree`. It is fast-forward by
construction.

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
  not merge unattended.
