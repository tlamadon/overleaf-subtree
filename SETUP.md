# Setting up subleaf: instructions for a coding agent

A user has asked you to connect their repository to an Overleaf project with
`subleaf` (package `overleaf-subtree`). They may not know git or the command
line well, so do the work yourself, ask one plain question at a time, and
explain what you are doing in a sentence, not a lecture. Never ask them to
edit a file by hand.

`subleaf` mirrors one subdirectory of the repository (the *prefix*, usually
`paper/`) onto the root of an Overleaf project. It needs Overleaf's git
integration, which is part of Overleaf's paid and institutional plans.

Rules for the whole setup:

- Do not push to Overleaf during setup. Setup only brings Overleaf's version in.
- Ask before installing software or changing the user's global git config.
- Never write the Overleaf token into a file, a URL, or a commit.

## 1. Check the basics

Run `git rev-parse --show-toplevel`. If this is not a git repository, ask
whether to create one here with `git init`.

Run `subleaf --help`. If the command is missing, ask to install it, then use the
first of these that is available:

```sh
uv tool install git+https://github.com/tlamadon/overleaf-subtree
pipx install git+https://github.com/tlamadon/overleaf-subtree
```

It needs Python 3.11 or newer. If neither `uv` nor `pipx` is installed, offer
to install `uv` (https://docs.astral.sh/uv/) rather than using `pip` directly.

If `.overleaf-subtree.toml` already exists at the repository root, the setup
was started before: skip to step 5, `subleaf init` picks up from there.

## 2. Ask for the Overleaf project

Ask the user to open the project on overleaf.com and paste the address from
the browser's address bar. It looks like
`https://www.overleaf.com/project/<24 characters>`. `subleaf` accepts that link
as is.

## 3. Choose the subdirectory

Look for an existing manuscript: `.tex` files containing `\documentclass`,
outside build and dependency directories. Then propose one, in plain words:

- Found one in, say, `paper/`: "Your paper seems to be in `paper/`. Should that
  folder be the one that mirrors the Overleaf project?"
- Found none: "Overleaf's files will go into a new `paper/` folder. Is that
  name fine?"

If the folder already has files, warn the user: files that differ between it
and Overleaf will need to be reconciled once, in step 5.

## 4. Make sure git can log in to Overleaf

Test access without letting git prompt (the project ID is the last part of
the link):

```sh
GIT_TERMINAL_PROMPT=0 GIT_ASKPASS= git ls-remote https://git@git.overleaf.com/<project-id>
```

If it lists refs, credentials are already stored: go to step 5.

If it fails with "Repository not found" or "does not exist", the link is wrong
or the account has no git integration; check with the user before going on.

If it fails asking for a password, the user needs an Overleaf git token. One
token works for every project they own, for a year. Explain:

> Overleaf lets git in with a token instead of your password. On overleaf.com,
> open Account Settings, find "Git Integration", and generate a token.

Then git needs a place to keep it. Run `git config --global credential.helper`.
If it prints nothing, propose the usual helper for the platform and set it only
after the user agrees:

| Platform | Command |
|---|---|
| macOS | `git config --global credential.helper osxkeychain` |
| Windows | `git config --global credential.helper manager` |
| Linux | `git config --global credential.https://git.overleaf.com.helper store` (keeps the token in `~/.git-credentials`, readable only by the user) |

Then store the token. Recommend the first way:

1. **The token never enters this chat.** Ask the user to run this one command
   in a terminal of their own, and paste the token when it asks for a password:

   ```sh
   git ls-remote https://git@git.overleaf.com/<project-id>
   ```

   (In Claude Code they can type `! ` followed by the command in the prompt,
   but a password prompt may not work there; a separate terminal always does.)

2. **The user pastes the token into the chat.** Tell them first that the token
   will then pass through this conversation. If they accept, store it with:

   ```sh
   printf 'protocol=https\nhost=git.overleaf.com\nusername=git\npassword=%s\n' '<token>' | git credential approve
   ```

Re-run the test above. Continue only once it lists refs.

## 5. Connect

```sh
subleaf init --project <link> --prefix <folder> --agents
```

This adds the git remote, writes and commits `.overleaf-subtree.toml`, brings
the Overleaf project into the folder, and (`--agents`) sets up Claude Code and
Codex to see the Overleaf status at the start of each session.

If it reports conflicts, the folder and Overleaf disagree on some files. For
each conflicted file, show the user the two versions in plain words and ask
which to keep, or merge them if the user wants both changes. Then
`git add` the files, `git commit --no-edit`, and run `subleaf agent-setup`,
the step `init` stopped before.

## 6. Finish

Run `subleaf status --short` and tell the user what it says.

`--agents` wrote `.claude/settings.json`, `.codex/hooks.json`,
`.codex/config.toml`, `CLAUDE.md` and `AGENTS.md`. Show the user that list and
offer to commit them, so coauthors' agents get the same setup. The hook takes
effect in the next session; Codex asks once to trust it.

Close by telling the user how to use it from now on, in their words, not
commands:

- "Pull from Overleaf" brings their coauthors' edits in.
- "Push to Overleaf" shows them exactly what would change on Overleaf, and
  publishes only after they say yes.
