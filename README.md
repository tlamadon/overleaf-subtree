# overleaf-subtree

Mirror **a subdirectory** of a git repository onto the **root** of an Overleaf
project, and review what a push would do before it reaches your coauthors.

```
myrepo/
  src/            code
  paper/     <->  the whole Overleaf project
  .overleaf-subtree.toml
```

## Why this exists

Every other Overleaf sync tool assumes the repository root *is* the project
root. That excludes the common case where a manuscript lives inside a code
repository, next to the scripts that generate its numbers and figures — where
you want the paper versioned alongside the code that produces it.

The second thing it does is refuse to publish blind. A push overwrites whatever
your coauthors currently have, so `push` prints the additions, modifications and
deletions first, annotates each modification and deletion with **who last
touched that file on Overleaf and when**, and asks.

## Install

```sh
uv tool install git+https://github.com/<you>/overleaf-subtree
```

Then, once per project:

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
olsub status    # what is unmerged, and what a push would change
olsub diff      # full content diff against the project
olsub pull      # merge the project's commits into this repo
olsub check     # run the checks, no network
olsub push      # review the outgoing change, confirm, publish
```

`push` shows:

```
This push would change the Overleaf project as follows.

  add     notes/appendix.tex
  MODIFY  paper.tex                    (there: magne.mogstad, 2 hours ago)
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

## Limits

- Overleaf's git integration is a paid feature; there is no web-API backend.
  If you need one, see [olsync](https://github.com/moritzgloeckl/overleaf-sync)
  or [overleaf-git-sync](https://github.com/genggng/overleaf-git-sync), neither
  of which supports subdirectory mapping.
- Untracked files under the prefix are never published. `status` lists them.
- Conflicts are resolved by you, in the working tree, with git. The tool will
  not merge unattended.
