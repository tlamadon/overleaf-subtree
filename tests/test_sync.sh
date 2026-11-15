#!/usr/bin/env bash
# End-to-end test of the Overleaf-wins workflow (src/overleaf_subtree/sync.py).
#
# Self-contained: it never talks to Overleaf and needs no real project.  Under
# a temp directory it builds
#
#   overleaf.git   a bare repo standing in for the Overleaf project: a small
#                  manuscript, a beamer deck with its PDF and figures, a
#                  coauthor's note
#   work/          a repo joined to it with `subleaf init --prefix paper`, with
#                  local-only notes, a generator, and the deck's working copy
#                  in slides/
#   coauthor/      a clone of the bare repo that commits "Update on Overleaf."
#                  under other people's names
#
#   tests/test_sync.sh          fast (fake latexmk and check command)
#   FULL=1 tests/test_sync.sh   also real latexmk and latexdiff
#   KEEP=1 ...                  keep the sandbox for inspection
set -uo pipefail

SRC=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d "${TMPDIR:-/tmp}/subleaf-test.XXXXXX")
O=$T/overleaf.git
W=$T/work
C=$T/coauthor
if [ -z "${KEEP:-}" ]; then trap 'rm -rf "$T"' EXIT; else echo "sandbox: $T"; fi

export OVERLEAF_SYNC_BACKUP_DIR=$T/backups
export OVERLEAF_SYNC_SETTLE=0
export GIT_CONFIG_GLOBAL=/dev/null     # no user hooks, helpers or aliases in here
export GIT_AUTHOR_NAME="Sandbox Tester" GIT_AUTHOR_EMAIL="sandbox@example.invalid"
export GIT_COMMITTER_NAME="Sandbox Tester" GIT_COMMITTER_EMAIL="sandbox@example.invalid"

# The code under test, as it is in this checkout.
mkdir "$T/bin"
cat > "$T/bin/subleaf" << EOF
#!/bin/sh
PYTHONPATH="$SRC/src" exec python3 -m overleaf_subtree.cli "\$@"
EOF
cat > "$T/bin/publish-tree" << EOF
#!/bin/sh
PYTHONPATH="$SRC/src" exec python3 -c "
from overleaf_subtree import sync
ctx = sync.Ctx('.')
print(sync.publish_tree(ctx, ctx.git.rev(ctx.cfg.tracking)))"
EOF
chmod +x "$T/bin/subleaf" "$T/bin/publish-tree"
PATH="$T/bin:$PATH"

pass=0
failed=0
ok() { pass=$((pass + 1)); printf '  ok    %s\n' "$1"; }
bad() {
    failed=$((failed + 1))
    printf '  FAIL  %s\n' "$1"
    if [ -n "${2:-}" ]; then printf '%s\n' "$2" | tail -30 | sed 's/^/        | /'; fi
}
section() { printf '\n== %s ==\n' "$1"; }
# run CMD...: capture combined output in OUT and the exit status in RC.
run() { OUT=$("$@" 2>&1); RC=$?; }
rc_is() { if [ "$RC" -eq "$1" ]; then ok "$2"; else bad "$2 (exit $RC, wanted $1)" "$OUT"; fi; }
has() { if grep -qF -- "$1" <<< "$OUT"; then ok "$2"; else bad "$2 (output lacks: $1)" "$OUT"; fi; }
hasnt() { if grep -qF -- "$1" <<< "$OUT"; then bad "$2 (output has: $1)" "$OUT"; else ok "$2"; fi; }
is() { if "${@:2}" > /dev/null 2>&1; then ok "$1"; else bad "$1"; fi; }
isnt() { if "${@:2}" > /dev/null 2>&1; then bad "$1"; else ok "$1"; fi; }
same() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "got:    $2
wanted: $3"; fi; }

tip() { git -C "$O" rev-parse main; }
ncommits() { git -C "$O" rev-list --count main; }
blob() { git -C "$W" hash-object "$1"; }
# The mirror equals Overleaf, repo-only (added) paths aside.
mirror_equal() {
    git -C "$W" fetch -q overleaf main
    [ -z "$(git -C "$W" diff --no-renames --name-status overleaf/main HEAD:paper | grep -v '^A')" ]
}
co_sync() { git -C "$C" fetch -q origin && git -C "$C" reset -q --hard origin/main; }
co_push() {  # co_push "Author Name"
    git -C "$C" add -A
    GIT_AUTHOR_NAME="$1" GIT_AUTHOR_EMAIL="coauthor@example.invalid" \
        git -C "$C" commit -q -m "Update on Overleaf."
    git -C "$C" push -q origin HEAD:main
}
wcommit() { git -C "$W" add -A -- "${@:2}" && git -C "$W" commit -q -m "$1" -- "${@:2}"; }

# ── sandbox ─────────────────────────────────────────────────────────────
section "sandbox"
git init -q --bare -b main "$O"
git clone -q "$O" "$C" 2> /dev/null
git -C "$C" checkout -q -b main
mkdir -p "$C/talk/graphs" "$C/notes"
cat > "$C/paper.tex" << 'EOF'
\documentclass{article}
\usepackage{xcolor}
\newcommand{\new}[1]{\textcolor{red}{#1}}
%\renewcommand{\new}[1]{#1}
\begin{document}
\title{A paper}\maketitle

\section{Introduction}

Opening words.

\subsection{Motivation}

Why this matters.

\subsection{Contribution}

What is new.

\section{Model}

\subsection{Setup}

The setup.

More of the setup.

\subsection{Results}

The results, with a note \textcolor{red}{[TO BE UPDATED.]} from a coauthor.

\appendix
\section{Proofs}

The proofs.

\end{document}
EOF
cat > "$C/talk/deck.tex" << 'EOF'
\documentclass{beamer}
\begin{document}

\begin{frame}{Title slide}
Hello.
\end{frame}

\begin{frame}{Motivation}
Why.
\end{frame}

\begin{frame}
\frametitle{What we do}
This.
\end{frame}

\begin{frame}{Main result}
\begin{itemize}
  \item First point
  \item Second point
\end{itemize}
\end{frame}

\begin{frame}{Conclusion}
Done.
\end{frame}

\end{document}
EOF
printf '\\def\\DeckNotes{}\n\\input{deck}\n' > "$C/talk/deck_notes.tex"
printf '%%PDF-original-deck\n' > "$C/talk/deck.pdf"
printf '%%PDF-figure-one\n' > "$C/talk/graphs/fig_one.pdf"
printf '%%PDF-figure-two\n' > "$C/talk/graphs/fig_two.pdf"
printf '%%PDF-overview\n' > "$C/overview.pdf"
printf '@article{key, title={A title}}\n' > "$C/ref.bib"
printf '# Structure\n' > "$C/STRUCTURE.md"
printf 'A note the coauthors keep on Overleaf.\n' > "$C/notes/shared.tex"
printf '*.aux\n*.log\n*.fls\n*.fdb_latexmk\n*.out\n*.nav\n*.snm\n*.toc\n/paper.pdf\n' > "$C/.gitignore"
co_push "Alice Sandbox"

mkdir -p "$W/src"
git -C "$W" init -q -b main
echo "print('analysis')" > "$W/src/analysis.py"
git -C "$W" add -A && git -C "$W" commit -q -m "Code"
cd "$W" || exit 1
run subleaf init --project "$O" --prefix paper
rc_is 0 "subleaf init joins the project into paper/"
is "... the manuscript is there" test -f paper/paper.tex

# What a real project adds by hand: files that stay here, the deck's working
# copy, and the optional config keys.
mkdir -p paper/generator slides/graphs
echo "my private note one" > paper/notes/private-one.tex
echo "my private note two" > paper/notes/private-two.tex
echo "print('figures')" > paper/generator/make.py
cp paper/talk/deck.tex paper/talk/deck_notes.tex slides/
cp paper/talk/graphs/*.pdf slides/graphs/
cat >> .overleaf-subtree.toml << 'EOF'

local_only = ["notes/**", "generator/**"]
artifacts = ["**/*.pdf"]

[pdf]
policy = "rebuild"
build = "latexmk -pdf -interaction=nonstopmode -halt-on-error"

[post_pull]
check = "latexmk -pdf -interaction=nonstopmode -halt-on-error -cd paper/paper.tex"

[[pair]]
local = "slides/deck.tex"
remote = "talk/deck.tex"
pdf = "talk/deck.pdf"

[[pair]]
local = "slides/deck_notes.tex"
remote = "talk/deck_notes.tex"

[[pair]]
local_dir = "slides/graphs"
remote_dir = "talk/graphs"
files = ["fig_one.pdf", "fig_two.pdf"]
EOF
cp .overleaf-subtree.toml "$T/real-config.toml"
git add -A && git commit -q -m "Local-only files, the deck's working copy, sync config"

same "the sandbox's only remote is the throwaway bare repo" "$(git remote -v | awk '{print $2}' | sort -u)" "$O"
run subleaf pairs
hasnt "no-base" "freshly copied pairs are recognised as identical"

# Fakes for the fast run: a check command and a `latexmk` that write a log and
# a PDF from files the test controls.
cat > "$T/fakecheck.sh" << EOF
#!/bin/sh
cp "$T/paper.log.in" paper/paper.log
exit \$(cat "$T/check.rc")
EOF
cat > "$T/fakelatexmk.sh" << EOF
#!/bin/sh
for a in "\$@"; do
    case "\$a" in -outdir=*) out=\${a#-outdir=} ;; *.tex) name=\${a%.tex} ;; esac
done
if [ -n "\$(cat "$T/deck.fail" 2>/dev/null)" ]; then
    echo '! Undefined control sequence.' > "\$out/\$name.log"; exit 12
fi
cp "$T/deck.log.in" "\$out/\$name.log"
printf '%%PDF-fake built from %s\n' "\$(git hash-object "\$name.tex")" > "\$out/\$name.pdf"
EOF
chmod +x "$T/fakecheck.sh" "$T/fakelatexmk.sh"
printf 'This is a fake log.\n' > "$T/paper.log.in"
printf 'This is a fake deck log.\n' > "$T/deck.log.in"
echo 0 > "$T/check.rc"
: > "$T/deck.fail"
sed -i "s|^check = .*|check = \"sh $T/fakecheck.sh\"|; s|^build = .*|build = \"sh $T/fakelatexmk.sh\"|" \
    .overleaf-subtree.toml
wcommit "sandbox: fake check and build commands" .overleaf-subtree.toml

DECK=slides/deck.tex
RDECK=talk/deck.tex
RPDF=talk/deck.pdf

# ── 1. Overleaf-wins pull ───────────────────────────────────────────────
section "1. Overleaf-wins pull: local-only files, dirty PDFs, section report"
co_sync
# Coauthor A edits the manuscript just below a \subsection ...
subline=$(grep -n '^\\subsection{[A-Za-z ,-]*}$' "$C/paper.tex" | sed -n 3p)
subno=${subline%%:*}
subtitle=$(sed -e 's/^[0-9]*:\\subsection{//' -e 's/}$//' <<< "$subline")
sed -i "$((subno + 2))i A sentence added by a coauthor on Overleaf." "$C/paper.tex"
co_push "Alice Sandbox"
# ... coauthor B edits the deck, replaces its PDF, deletes a file, adds one.
co_sync
frameline=$(grep -n '\\begin{frame}' "$C/$RDECK" | sed -n 4p | cut -d: -f1)
sed -i "$((frameline + 2))i % coauthor edit inside the fourth frame" "$C/$RDECK"
printf '%%PDF-coauthor\n' > "$C/$RPDF"
git -C "$C" rm -q STRUCTURE.md
echo "new on Overleaf" > "$C/added-on-overleaf.txt"
co_push "Bob Sandbox"

# Local state the pull has to cope with:
printf '%%PDF-local-editor-build\n' > "paper/$RPDF"              # dirty artifact Overleaf changed
printf '%%PDF-local-other\n' > paper/overview.pdf    # dirty artifact Overleaf did not
echo "untracked plan" > paper/PLAN.md                             # untracked, repo-only
echo "% local commit that was never pushed" >> paper/ref.bib
wcommit "local: unpushed edit to ref.bib" paper/ref.bib
notes_before=$(git ls-files paper/notes | wc -l)
# Overleaf has notes of its own; ours sit next to them and must stay here.
shared_notes=$(git -C "$O" ls-tree -r --name-only main | grep -c '^notes/\|^generator/')
head_before=$(git rev-parse HEAD)

run subleaf pull --theirs
rc_is 0 "pull succeeds with dirty PDFs and an untracked file present"
is "mirror equals Overleaf afterwards (only repo-only paths differ)" mirror_equal
same "one merge commit with two parents: old HEAD and Overleaf's tip" \
    "$(git rev-list --parents -n1 HEAD | cut -d' ' -f2-)" "$head_before $(tip)"
same "local-only notes/ are all still tracked" "$(git ls-files paper/notes | wc -l)" "$notes_before"
is "local-only files exist that Overleaf lacks" test "$notes_before" -gt "$shared_notes"
has "first run; recorded as the baseline" "the first post-pull check records a baseline"
same "dirty PDF that Overleaf changed now holds Overleaf's version" "$(cat "paper/$RPDF")" "%PDF-coauthor"
bak=$(find "$T/backups" -path "*paper/$RPDF" | head -1)
same "... and the local build was backed up outside the repo" "$(cat "${bak:-/dev/null}")" "%PDF-local-editor-build"
same "dirty PDF that Overleaf did not change is left alone" "$(cat paper/overview.pdf)" "%PDF-local-other"
same "untracked paper/PLAN.md untouched" "$(cat paper/PLAN.md)" "untracked plan"
isnt "file deleted on Overleaf is deleted here" test -e paper/STRUCTURE.md
is "file added on Overleaf arrives" test -f paper/added-on-overleaf.txt
hasnt "never pushed" "unpushed local commit to ref.bib is overwritten (Overleaf wins)"
has "Local changes overwritten" "... and that is reported"
is "... with ref.bib named" grep -qx '    ref.bib' <<< "$OUT"
isnt "ref.bib no longer has the local line" grep -q 'never pushed' paper/ref.bib
msg=$(git log -1 --format=%B)
OUT=$msg
has "Merge Overleaf edits into paper/" "merge message keeps its subject"
has "Alice Sandbox" "merge message names the first author"
has "Bob Sandbox" "merge message names the second author"
has "M paper.tex" "merge message lists the files"
has "D STRUCTURE.md" "merge message lists the deletion"
has "> $subtitle" "merge message maps the paper.tex hunk to its \\subsection ($subtitle)"
has "frame: " "merge message maps the deck hunk to its frame"
same "paired deck tex was fast-forwarded to Overleaf's" "$(blob $DECK)" "$(git rev-parse "overleaf/main:$RDECK")"
is "... and committed with the merge" git diff --quiet HEAD -- "$DECK"
is "state file is committed" git cat-file -e HEAD:.overleaf-state.json
same "state records the pair's synced blob" \
    "$(python3 -c "import json;print(json.load(open('.overleaf-state.json'))['pairs']['$RDECK']['blob'])")" \
    "$(blob $DECK)"
same "state records the Overleaf tip" \
    "$(python3 -c "import json;print(json.load(open('.overleaf-state.json'))['overleaf_tip'])")" "$(tip)"
git checkout -q -- paper/overview.pdf
git checkout -q -- "paper/$RPDF" 2> /dev/null   # the fake build may have refreshed it

run subleaf pull --theirs
rc_is 0 "a second pull is a no-op"
has "Already up to date" "... and says so"

section "1b. dirty source files"
co_sync
echo "% coauthor line" >> "$C/ref.bib"
co_push "Alice Sandbox"
echo "% my uncommitted edit" >> paper/ref.bib
head_before=$(git rev-parse HEAD)
run subleaf pull --theirs --no-check
rc_is 1 "pull refuses a dirty .bib that differs from the incoming version"
has "paper/ref.bib" "... naming the file"
same "... and changes nothing" "$(git rev-parse HEAD)" "$head_before"
is "... the edit is still there" grep -q 'my uncommitted edit' paper/ref.bib
git show overleaf/main:ref.bib > paper/ref.bib
run subleaf pull --theirs --no-check
rc_is 0 "pull accepts a dirty source file whose content already equals the incoming version"
is "mirror equals Overleaf" mirror_equal
is "... and the file is clean afterwards" git diff --quiet HEAD -- paper/ref.bib
echo "% edit to a file Overleaf will not touch" >> paper/paper.tex
co_sync; echo "x" >> "$C/added-on-overleaf.txt"; co_push "Bob Sandbox"
run subleaf pull --theirs --no-check
rc_is 1 "pull refuses a dirty .tex even when Overleaf did not change that file"
git checkout -q -- paper/paper.tex
run subleaf pull --theirs --no-check
rc_is 0 "... and goes through once it is restored"
echo "staged" >> src/analysis.py; git add src/analysis.py
co_sync; echo "y" >> "$C/added-on-overleaf.txt"; co_push "Bob Sandbox"
run subleaf pull --theirs --no-check
rc_is 1 "pull refuses when anything is staged"
git reset -q; git checkout -q -- src/analysis.py
run subleaf pull --theirs --no-check
rc_is 0 "(back in sync)"

# ── 2. targeted push ────────────────────────────────────────────────────
section "2. targeted push of named files"
echo "% pending local work, committed, not part of this push" >> paper/paper.tex
wcommit "local: pending paper.tex edit" paper/paper.tex
echo "% entry added locally" >> paper/ref.bib            # uncommitted: pushed from disk
old=$(tip)
run subleaf push --dry-run --diff ref.bib
rc_is 0 "dry run succeeds"
has "MODIFY  ref.bib" "dry run shows the name-status"
has "(Overleaf: Alice Sandbox" "... and who last touched the file on Overleaf"
has "1 file changed, 1 insertion(+)" "... and the shortstat"
has "+% entry added locally" "... and, with --diff, the full diff"
same "dry run pushes nothing" "$(tip)" "$old"
run bash -c 'echo n | subleaf push ref.bib'
rc_is 0 "answering n aborts cleanly"
has "Aborted; nothing was pushed." "... and says so"
same "... and pushes nothing" "$(tip)" "$old"
run bash -c 'printf "d\ny\n" | subleaf push paper/ref.bib'
rc_is 0 "answering d then y shows the diff and pushes"
has "+% entry added locally" "... the diff was shown"
same "Overleaf got exactly one commit, parented on its previous tip" "$(git -C "$O" rev-parse main^)" "$old"
same "that commit touches exactly the named file" "$(git -C "$O" diff --name-status "$old" main)" "M	ref.bib"
same "the pushed blob equals the local file" "$(git -C "$O" rev-parse main:ref.bib)" "$(blob paper/ref.bib)"
same "no local-only path reached Overleaf" \
    "$(git -C "$O" ls-tree -r --name-only main | grep -c '^notes/\|^generator/')" "$shared_notes"
has "mirror verified" "push merges back and verifies the mirror"
has "Still unpublished" "the pending paper.tex commit is reported as unpublished"
is "... and survives the merge-back" grep -q 'pending local work' paper/paper.tex
isnt "... without having been published" git -C "$O" grep -q 'pending local work' main -- paper.tex
is "ref.bib is committed after the merge-back" git diff --quiet HEAD -- paper/ref.bib
same "HEAD contains Overleaf's tip" "$(git merge-base HEAD overleaf/main)" "$(tip)"
old=$(tip)
run subleaf push --yes notes/private-one.tex
rc_is 1 "a local_only file is refused by name"
has "local_only" "... with the reason"
run subleaf push --yes ref.bib
rc_is 0 "pushing a file that is already identical is a no-op"
same "... nothing is pushed" "$(tip)" "$old"
run subleaf push --yes --delete added-on-overleaf.txt
rc_is 1 "--delete refuses while the file still exists locally"
rm paper/added-on-overleaf.txt
run subleaf push --yes added-on-overleaf.txt
rc_is 1 "a vanished file is not deleted on Overleaf without --delete"
run subleaf push --yes --delete added-on-overleaf.txt
rc_is 0 "--delete removes it from Overleaf"
same "... as a single deletion" "$(git -C "$O" diff --name-status "$old" main)" "D	added-on-overleaf.txt"
# paper.tex is still pending, so compare everything else.
same "mirror differs from Overleaf only by the pending file" \
    "$(git diff --no-renames --name-status overleaf/main HEAD:paper | grep -v '^A')" "M	paper.tex"
run subleaf push --yes paper.tex
rc_is 0 "the pending paper.tex is pushed in turn"
is "mirror equals Overleaf" mirror_equal

# ── 3. local_only ───────────────────────────────────────────────────────
section "3. local_only: a whole-tree push never adds them; status lists them apart"
echo "% another local edit" >> paper/ref.bib
wcommit "local: ref.bib" paper/ref.bib
echo "% edit to a local-only note" >> paper/notes/private-one.tex
wcommit "local: note" paper/notes/private-one.tex
tree=$(publish-tree)
same "the tree a whole-tree push would publish differs from Overleaf by ref.bib alone" \
    "$(git diff --name-status overleaf/main "$tree")" "M	ref.bib"
same "that tree's notes/ is exactly Overleaf's: nothing of ours added" \
    "$(git ls-tree -r "$tree" -- notes)" "$(git ls-tree -r overleaf/main -- notes)"
is "(HEAD:paper has more notes than that)" test "$(git ls-tree -r --name-only HEAD:paper -- notes | wc -l)" -gt "$shared_notes"
run subleaf status
rc_is 0 "status runs"
has "Outgoing: 0 added, 1 modified, 0 deleted" "status counts only publishable changes as outgoing"
has "Local-only: " "status lists local-only files separately"
has "notes/**" "... by pattern"
hasnt "notes/private-one.tex" "... without naming every file by default"
has "Pairs (working copy <-> Overleaf copy):" "status shows the pairs"
run subleaf status --all
has "notes/private-one.tex" "'status all' names every local-only file"
has "already on Overleaf" "... and says which matching files are shared instead"
run subleaf push --dry-run --no-check
rc_is 0 "a whole-tree push dry run works"
has "ref.bib" "... and would publish ref.bib"
hasnt "private-one" "... but none of the local-only notes"
hasnt "generator/" "... nor the generator"
run subleaf status --short --cached
has "1 file to push (ref.bib)" "the one-line status does not count local-only files as outgoing"
run subleaf push --yes ref.bib
rc_is 0 "(ref.bib published)"
shared=$(git ls-tree -r --name-only overleaf/main -- notes | grep '\.tex$' | grep -v "[' ]" | head -1)
if [ -n "$shared" ]; then
    echo "% edit to a note the coauthors also have" >> "paper/$shared"
    run subleaf push --yes "$shared"
    rc_is 0 "a notes/ file that Overleaf already has is shared, and can be pushed"
fi

# ── 4. pairs ────────────────────────────────────────────────────────────
section "4a. pair: only Overleaf changed -> local copy fast-forwarded"
co_sync
sed -i '1i % remote-only edit at the top of the deck' "$C/$RDECK"
co_push "Alice Sandbox"
run subleaf pull --theirs --no-check
rc_is 0 "pull succeeds"
has "updated   " "the pair is reported as updated"
same "local copy equals Overleaf's" "$(blob $DECK)" "$(git rev-parse "overleaf/main:$RDECK")"
is "local copy is committed" git diff --quiet HEAD -- "$DECK"

section "4b. pair: only the local copy changed -> stays, then pushed in reverse"
echo "% local-only edit at the bottom of the deck" >> "$DECK"
co_sync; echo "z" > "$C/another.txt"; co_push "Bob Sandbox"
run subleaf pull --theirs --no-check
rc_is 0 "pull succeeds"
has "local edit" "the pair is reported as having local edits"
is "local edit survives the pull" grep -q 'local-only edit at the bottom' "$DECK"
run subleaf status
has "LOCAL AHEAD" "status shows the pair as ahead"
old=$(tip)
run subleaf push --yes --no-rebuild "$DECK"
rc_is 0 "pushing the pair by its local name succeeds"
same "Overleaf's copy now equals the local one" "$(git -C "$O" rev-parse "main:$RDECK")" "$(blob $DECK)"
same "... and only that path changed" "$(git -C "$O" diff --name-status "$old" main)" "M	$RDECK"
same "mirror copy equals it too" "$(blob "paper/$RDECK")" "$(blob $DECK)"
is "local working copy is committed along" git diff --quiet HEAD -- "$DECK"
run subleaf pairs
hasnt "local-ahead" "pair is back in sync"

section "4c. pair: both changed, no overlap -> merged with git merge-file"
co_sync
sed -i '1i % second remote edit at the top' "$C/$RDECK"
co_push "Alice Sandbox"
echo "% second local edit at the bottom" >> "$DECK"
run subleaf pull --theirs --no-check
rc_is 0 "pull succeeds without stopping"
has "merged    " "the pair is reported as merged"
is "merged copy has Overleaf's edit" grep -q 'second remote edit at the top' "$DECK"
is "merged copy has the local edit" grep -q 'second local edit at the bottom' "$DECK"
isnt "no conflict markers" grep -q '^<<<<<<<' "$DECK"
run subleaf push --yes --no-rebuild --pairs
rc_is 0 "'--pairs' pushes the merged copy back"
same "Overleaf has the merged deck" "$(git -C "$O" rev-parse "main:$RDECK")" "$(blob $DECK)"
is "mirror equals Overleaf" mirror_equal

section "4d. pair: both changed the same line -> real conflict stops for a decision"
co_sync
sed -i '1s/.*/% the coauthor rewrote line one/' "$C/$RDECK"
co_push "Bob Sandbox"
sed -i '1s/.*/% I rewrote line one differently/' "$DECK"
base_before=$(python3 -c "import json;print(json.load(open('.overleaf-state.json'))['pairs']['$RDECK']['blob'])")
run subleaf pull --theirs --no-check
rc_is 2 "pull exits 2: complete, but a pair needs a decision"
has "CONFLICT" "the conflict is reported"
is "the mirror itself was still merged and equals Overleaf" mirror_equal
is "conflict markers are in the working copy" grep -q '^<<<<<<< local' "$DECK"
bak=$(find "$T/backups" -path "*/$DECK" | sort | tail -1)
is "the pre-merge working copy was backed up" grep -q 'I rewrote line one differently' "${bak:-/dev/null}"
same "the recorded base did not move" \
    "$(python3 -c "import json;print(json.load(open('.overleaf-state.json'))['pairs']['$RDECK']['blob'])")" "$base_before"
old=$(tip)
run subleaf push --yes --no-rebuild "$DECK"
rc_is 1 "pushing the conflicted pair is refused"
same "... nothing pushed" "$(tip)" "$old"
run subleaf pairs resolved "$DECK"
rc_is 1 "'resolved' refuses while markers remain"
cp "$bak" "$DECK"                                        # resolve: keep mine
run subleaf pairs resolved "$DECK"
rc_is 0 "'resolved' records the decision"
run subleaf push --yes --no-rebuild "$DECK"
rc_is 0 "the resolved copy is pushed"
same "Overleaf has the resolved line" "$(git -C "$O" show "main:$RDECK" | head -1)" "% I rewrote line one differently"
is "mirror equals Overleaf" mirror_equal
co_sync
sed -i '1s/.*/% coauthor again/' "$C/$RDECK"
co_push "Bob Sandbox"
sed -i '1s/.*/% me again/' "$DECK"
run subleaf pull --theirs --no-check
rc_is 2 "(a second conflict)"
run subleaf pairs take-remote "$DECK"
rc_is 0 "'take-remote' overwrites the working copy with Overleaf's"
same "... local equals Overleaf" "$(blob $DECK)" "$(git rev-parse "overleaf/main:$RDECK")"
run subleaf pull --theirs --no-check
rc_is 0 "and the next pull is quiet"

section "4e. pair: binary figure changed on Overleaf"
FIG=graphs/fig_one.pdf
co_sync
cp "$C/talk/graphs/fig_two.pdf" "$C/talk/$FIG"   # a real PDF, so the deck still builds
co_push "Alice Sandbox"
run subleaf pull --theirs --no-check
rc_is 0 "pull succeeds"
same "the local figure was fast-forwarded" "$(blob "slides/$FIG")" "$(git rev-parse "overleaf/main:talk/$FIG")"
is "... and committed" git diff --quiet HEAD -- "slides/$FIG"

# ── 5. divergence ───────────────────────────────────────────────────────
section "5. divergence refusal"
co_sync; echo "% coauthor got there first" >> "$C/ref.bib"; co_push "Alice Sandbox"
echo "% my competing edit" >> paper/ref.bib
old=$(tip)
run subleaf push --yes ref.bib
rc_is 1 "push is refused when Overleaf has commits we have not ingested"
has "pull --theirs" "... pointing at the pull"
same "... nothing pushed" "$(tip)" "$old"
git checkout -q -- paper/ref.bib
run subleaf pull --theirs --no-check
rc_is 0 "(pulled)"

echo "% edit under review" >> paper/ref.bib
mkfifo "$T/answer"
(subleaf push ref.bib < "$T/answer" > "$T/review.out" 2>&1; echo $? > "$T/review.rc") &
exec 9> "$T/answer"
for _ in $(seq 100); do grep -q 'Push to Overleaf' "$T/review.out" 2> /dev/null && break; sleep 0.2; done
co_sync; echo "% landed during the review" >> "$C/ref.bib"; co_push "Bob Sandbox"
old=$(tip)
echo y >&9
exec 9>&-
wait
OUT=$(cat "$T/review.out"); RC=$(cat "$T/review.rc")
rc_is 1 "push stops if Overleaf moves between the review and the push"
has "Overleaf moved while this push was being reviewed" "... and says why"
same "... nothing pushed" "$(tip)" "$old"
git checkout -q -- paper/ref.bib
run subleaf pull --theirs --no-check
rc_is 0 "(pulled)"

# ── 6. the timeout quirk ────────────────────────────────────────────────
section "6. push reports a timeout (simulated with a git shim)"
REAL_GIT=$(command -v git)
mkdir "$T/shim"
cat > "$T/shim/git" << EOF
#!/bin/sh
# Stand-in for Overleaf's "Request timed out" on push.
#   applied  the push goes through, but git reports the rejection
#   dropped  the first FAKE_DROPS pushes are lost, later ones work
if [ "\$1" = push ] && [ -n "\${FAKE_PUSH:-}" ]; then
    n=\$(cat "$T/pushes" 2>/dev/null || echo 0); echo \$((n + 1)) > "$T/pushes"
    reject() {
        echo "To overleaf" >&2
        echo " ! [remote rejected] main -> main (Request timed out)" >&2
        echo "error: failed to push some refs" >&2
        exit 1
    }
    case "\$FAKE_PUSH" in
        applied) "$REAL_GIT" "\$@" > /dev/null 2>&1; reject ;;
        dropped) [ "\$n" -lt "\${FAKE_DROPS:-1}" ] && reject ;;
    esac
fi
exec "$REAL_GIT" "\$@"
EOF
chmod +x "$T/shim/git"

echo "% timeout case one" >> paper/ref.bib
before=$(ncommits); echo 0 > "$T/pushes"
run env PATH="$T/shim:$PATH" FAKE_PUSH=applied subleaf push --yes ref.bib
rc_is 0 "timeout but applied: the push is treated as done"
has "Request timed out" "... the error is shown"
has "Overleaf applied the push despite the error" "... and recognised by re-fetching and comparing trees"
same "... git push ran exactly once (no blind retry)" "$(cat "$T/pushes")" "1"
same "... Overleaf has exactly one new commit" "$(ncommits)" "$((before + 1))"
is "... and the mirror was merged back and equals Overleaf" mirror_equal

echo "% timeout case two" >> paper/ref.bib
before=$(ncommits); echo 0 > "$T/pushes"
run env PATH="$T/shim:$PATH" FAKE_PUSH=dropped FAKE_DROPS=1 subleaf push --yes ref.bib
rc_is 0 "timeout and not applied: retried after confirming Overleaf is unchanged"
has "Overleaf is unchanged; retrying" "... says so"
same "... two push attempts" "$(cat "$T/pushes")" "2"
same "... one new commit on Overleaf" "$(ncommits)" "$((before + 1))"
is "... mirror equals Overleaf" mirror_equal

echo "% timeout case three" >> paper/ref.bib
before=$(ncommits); echo 0 > "$T/pushes"; head_before=$(git rev-parse HEAD)
run env PATH="$T/shim:$PATH" FAKE_PUSH=dropped FAKE_DROPS=99 subleaf push --yes ref.bib
rc_is 1 "timeout every time: gives up"
has "Nothing was published" "... and says nothing was published"
same "... Overleaf unchanged" "$(ncommits)" "$before"
same "... local history unchanged" "$(git rev-parse HEAD)" "$head_before"
is "... the edit is still on disk" grep -q 'timeout case three' paper/ref.bib
run subleaf push --yes ref.bib
rc_is 0 "(published normally afterwards)"

# ── 7. post-pull check and PDF policy ───────────────────────────────────
section "7. post-pull check: only what got worse; stale deck PDF rebuilt"
cat > "$T/paper.log.in" << 'EOF'
LaTeX Warning: Reference `eq:old' on page 3 undefined on input line 10.
Overfull \hbox (12.5pt too wide) in paragraph at lines 100--104
\OT1/cmr/m/n/10 an old overfull paragraph that was always there
EOF
run subleaf check --report
rc_is 0 "check report runs (and records these as the baseline)"
cat > "$T/paper.log.in" << 'EOF'
LaTeX Warning: Reference `eq:old' on page 3 undefined on input line 12.
LaTeX Warning: Reference `eq:fresh' on page 4 undefined on input line 50.
LaTeX Warning: Citation `nobody2026' on page 4 undefined on input line 51.
Overfull \hbox (12.5pt too wide) in paragraph at lines 102--106
\OT1/cmr/m/n/10 an old overfull paragraph that was always there
Overfull \hbox (3.1pt too wide) in paragraph at lines 200--201
\OT1/cmr/m/n/10 a brand new overfull paragraph
! Undefined control sequence.
l.1128 the estimand \DIDs
EOF
echo 1 > "$T/check.rc"
co_sync
sed -i '1s/.*/% coauthor edit that will stale the PDF/' "$C/$RDECK"
echo "% and one to the manuscript" >> "$C/paper.tex"
co_push "Alice Sandbox"
run subleaf pull --theirs
rc_is 0 "a failing check does not block the pull"
is "the pull itself is complete" git merge-base --is-ancestor overleaf/main HEAD
has "WORSE than at the previous sync" "regressions are reported"
has "now FAILS" "... the check newly failing"
has "new undefined: reference eq:fresh" "... the new undefined reference"
has "new undefined: citation nobody2026" "... the new undefined citation"
has "new error: Undefined control sequence." "... the new error"
has '\DIDs' "... with the offending source line"
has "new overfull: hbox: a brand new overfull paragraph" "... the new overfull box"
hasnt "eq:old" "an old undefined reference is not repeated (though its line moved)"
hasnt "an old overfull paragraph" "an old overfull box is not repeated (though its lines moved)"
has "Rebuilt paper/$RPDF" "the deck PDF is rebuilt because its tex changed on Overleaf"
same "the mirror PDF is the fresh build of Overleaf's tex" \
    "$(cat "paper/$RPDF")" "%PDF-fake built from $(git rev-parse "overleaf/main:$RDECK")"
has "subleaf push '$RPDF'" "... and the push is offered"
is "the check baseline is committed with the merge" git diff --quiet HEAD -- .overleaf-state.json
old=$(tip)
run subleaf push --yes "$RPDF"
rc_is 0 "the rebuilt PDF is pushed"
same "... as that one file" "$(git -C "$O" diff --name-status "$old" main)" "M	$RPDF"
run subleaf check --report
has "Nothing got worse" "an unchanged second run reports nothing"
has "still failing" "... while noting the check still fails"
echo 0 > "$T/check.rc"; printf 'clean\n' > "$T/paper.log.in"
run subleaf check --report
has "Nothing got worse" "improvements are not reported as regressions"
git add .overleaf-state.json; git commit -q -m "sandbox: baseline" -- .overleaf-state.json

echo "% local deck edit that needs a new PDF" >> "$DECK"
old=$(tip)
run subleaf push --yes "$DECK"
rc_is 0 "pushing the deck tex rebuilds its PDF and sends both"
same "... tex and PDF in one commit" "$(git -C "$O" diff --name-status "$old" main | sort)" "M	$RPDF
M	$RDECK"
same "... the PDF was built from the pushed tex" \
    "$(git -C "$O" show "main:$RPDF")" "%PDF-fake built from $(git -C "$O" rev-parse "main:$RDECK")"
is "mirror equals Overleaf" mirror_equal

echo yes > "$T/deck.fail"
echo "% edit that breaks the deck" >> "$DECK"
old=$(tip)
run subleaf push --yes "$DECK"
rc_is 0 "a deck that does not build is still pushed"
has "does not build" "... with a warning"
same "... without a PDF" "$(git -C "$O" diff --name-status "$old" main)" "M	$RDECK"
: > "$T/deck.fail"

section "7b. PDF policy \"never\""
sed -i 's/^policy = "rebuild"/policy = "never"/' .overleaf-subtree.toml
printf '%%PDF-local-build\n' > "paper/$RPDF"
old=$(tip)
run subleaf push --yes "$RPDF"
rc_is 1 "the compiled PDF is refused by name"
wcommit "local: commit the PDF" "paper/$RPDF"
tree=$(publish-tree)
same "... and held back from a whole-tree push" "$(git diff --name-status overleaf/main "$tree")" ""
echo "% deck edit under policy never" >> "$DECK"
run subleaf push --yes "$DECK"
rc_is 0 "pushing the deck tex works"
same "... and sends the tex only" "$(git -C "$O" diff --name-status "$old" main)" "M	$RDECK"
sed -i 's/^policy = "never"/policy = "rebuild"/' .overleaf-subtree.toml
git show "overleaf/main:$RPDF" > "paper/$RPDF"
wcommit "local: back to Overleaf's PDF" "paper/$RPDF"

# ── 8. review markup ────────────────────────────────────────────────────
section "8. accept"
cat > paper/accept-test.tex << 'EOF'
\newcommand{\new}[1]{\textcolor{red}{#1}}
%\renewcommand{\new}[1]{#1}
Plain \new{accepted text} here.
Coauthor note \textcolor{red}{[TO BE UPDATED.]} stays.
Nested \new{outer \textbf{bold {deep}} and \new{inner} done}.
Escapes \new{50\% of \{braces\} kept} fine.
Multi \new{line one
line two} end.
Math $x^\new{ab} + y_\new{c}$ and $\alpha\new{b}$ and \alpha\new{$b$}.
Spaced \new {with a space}. \newline \newcommand{\newer}{x} \newer{untouched}
% a comment with \new{left alone} and an unbalanced {
Comment inside \new{kept % } not a brace
text} end.
Unbalanced \new{never closes
EOF
cat > "$T/accept-expected.tex" << 'EOF'
\newcommand{\new}[1]{\textcolor{red}{#1}}
%\renewcommand{\new}[1]{#1}
Plain accepted text here.
Coauthor note \textcolor{red}{[TO BE UPDATED.]} stays.
Nested outer \textbf{bold {deep}} and inner done.
Escapes 50\% of \{braces\} kept fine.
Multi line one
line two end.
Math $x^{ab} + y_{c}$ and $\alpha b$ and \alpha$b$.
Spaced with a space. \newline \newcommand{\newer}{x} \newer{untouched}
% a comment with \new{left alone} and an unbalanced {
Comment inside kept % } not a brace
text end.
Unbalanced \new{never closes
EOF
cp paper/accept-test.tex "$T/accept-before.tex"
run subleaf accept --dry-run paper/accept-test.tex
rc_is 0 "dry run succeeds"
has "11 \\new{...} wrapper(s) would be accepted" "... and counts the wrappers"
is "... and changes nothing" cmp -s paper/accept-test.tex "$T/accept-before.tex"
run subleaf accept --lines 3:3 paper/accept-test.tex
has "1 \\new{...} wrapper(s) accepted" "--lines restricts to a range"
run subleaf accept paper/accept-test.tex
rc_is 0 "accept succeeds"
if diff -u "$T/accept-expected.tex" paper/accept-test.tex > "$T/accept.diff"; then
    ok "wrappers stripped with balanced braces; \\textcolor, \\newcommand, comments untouched"
else
    bad "accept output differs from the expectation" "$(cat "$T/accept.diff")"
fi
rm paper/accept-test.tex
run subleaf accept --dry-run
rc_is 0 "with no file named, accept finds the main document"
has "0 \\new{...} wrapper(s) would be accepted" "... which has nothing to accept yet"
# A copy of the manuscript, which defines \new itself.
git show overleaf/main:paper.tex > paper/accept-real.tex
if grep -q '^\\newcommand{\\new}\[1\]{\\textcolor{red}{#1}}' paper/accept-real.tex; then
    ok "paper.tex defines \\new"
    is "... with the one-line switch that turns it off" \
        grep -q '^%\\renewcommand{\\new}\[1\]{#1}' paper/accept-real.tex
    sed -i 's/^\\section{Proofs}$/&\n\\new{A proposed sentence with a \\textcolor{red}{coauthor note} inside.}/' paper/accept-real.tex
    before=$(grep -c 'textcolor' paper/accept-real.tex)
    run subleaf accept paper/accept-real.tex
    has "1 \\new{...} wrapper(s) accepted" "accepting in paper.tex strips the one wrapper"
    same "... and leaves the definition and every \\textcolor alone" \
        "$(grep -c 'textcolor' paper/accept-real.tex)" "$before"
    is "... the sentence is now plain text" \
        grep -q '^A proposed sentence with a \\textcolor{red}{coauthor note} inside\.$' paper/accept-real.tex
else
    bad "paper.tex defines \\new"
fi
rm -f paper/accept-real.tex

# ── 9. the real toolchain (slow) ────────────────────────────────────────
if [ -n "${FULL:-}" ]; then
    section "9. FULL: real latexmk for the check and the deck, real latexdiff"
    cp "$T/real-config.toml" .overleaf-subtree.toml
    wcommit "sandbox: real check and build commands" .overleaf-subtree.toml
    run subleaf check --report
    rc_is 0 "the real check report runs"
    echo "$OUT" | sed 's/^/        | /'
    has "manuscript: ok" "... and the real manuscript passes the checks"
    has "deck $RDECK: ok" "... and the real deck builds from the mirror"
    co_sync
    fl=$(grep -n '\\begin{frame}' "$C/$RDECK" | sed -n 4p | cut -d: -f1)
    {
        head -n "$((fl + 1))" "$C/$RDECK"
        printf '%s\n' '\undefinedmacroforthetest'
        tail -n "+$((fl + 2))" "$C/$RDECK"
    } > "$T/deck.tmp"
    cp "$T/deck.tmp" "$C/$RDECK"
    co_push "Alice Sandbox"
    run subleaf pull --theirs --no-offer
    rc_is 0 "a pull whose deck no longer compiles still completes"
    echo "$OUT" | sed -n '/post-pull/,$p' | sed 's/^/        | /'
    has "new error: Undefined control sequence." "... and the broken deck is reported as a new error"
    hasnt "Rebuilt paper/$RPDF" "... and its PDF is not replaced by a failed build"
    run subleaf latexdiff overleaf/main~3 overleaf/main
    rc_is 0 "latexdiff produces a PDF"
    is "... on disk" test -s "$(ls paper-latexdiff-*.pdf | head -1)"
fi

printf '\n%d passed, %d failed\n' "$pass" "$failed"
[ "$failed" -eq 0 ]
