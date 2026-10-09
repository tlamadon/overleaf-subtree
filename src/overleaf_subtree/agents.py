"""Wire subleaf into coding agents (Claude Code, Codex).

Both read a session-start hook whose stdout becomes context for the model,
and both use the same hooks layout, so one command serves both.  Every edit
here merges into what is already there and is safe to repeat.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from .config import ConfigError

HOOK_COMMAND = "subleaf status --short"
STATUSLINE_COMMAND = "subleaf status --short --cached"

BEGIN, END = "<!-- subleaf:begin -->", "<!-- subleaf:end -->"

INSTRUCTIONS = """\
{begin}
## Overleaf

`{prefix}/` is synced with an Overleaf project using `subleaf` (overleaf-subtree).
At the start of a session, a line beginning `Overleaf:` reports what is waiting
to be pulled or pushed. If there are commits to pull, tell the user before
editing anything under `{prefix}/`. For the details, run `subleaf status --json`.

- "Pull from Overleaf": run `subleaf pull`. Resolve any merge conflicts with git,
  then summarize what came in.
- "Push to Overleaf": commit the work under `{prefix}/` first. Run
  `subleaf push --dry-run` and show the user the whole output. Run
  `subleaf push --yes` only after the user approves that list. If push reports
  unmerged Overleaf commits, pull first, then start again from the dry run.
- Never run `subleaf push --yes` before the user has seen the dry run.
{end}
"""


def _hook_entry() -> dict:
    return {"matcher": "startup|resume",
            "hooks": [{"type": "command", "command": HOOK_COMMAND}]}


def _add_session_hook(path: Path) -> bool:
    """Add the hook to a Claude- or Codex-style hooks file.  True if changed."""
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}); fix it and rerun") from None
    groups = data.setdefault("hooks", {}).setdefault("SessionStart", [])
    if any("subleaf status" in h.get("command", "")
           for grp in groups for h in grp.get("hooks", [])):
        return False
    groups.append(_hook_entry())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _set_statusline(path: Path) -> bool:
    data = json.loads(path.read_text()) if path.exists() else {}
    if "statusLine" in data:
        return False
    data["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return True


def _enable_codex_hooks(path: Path) -> bool:
    text = path.read_text() if path.exists() else ""
    features = tomllib.loads(text).get("features", {})
    if features.get("hooks") or features.get("codex_hooks"):
        return False
    if "hooks" in features:
        raise ConfigError(f"{path} sets features.hooks = false; set it to true "
                          "to let Codex run the subleaf hook")
    if re.search(r"(?m)^\[features\]\s*$", text):
        text = re.sub(r"(?m)^\[features\]\s*$", "[features]\nhooks = true", text, count=1)
    else:
        text += ("\n" if text and not text.endswith("\n") else "") \
            + ("\n" if text else "") + "[features]\nhooks = true\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return True


def _write_instructions(path: Path, prefix: str) -> bool:
    block = INSTRUCTIONS.format(begin=BEGIN, end=END, prefix=prefix)
    text = path.read_text() if path.exists() else ""
    if BEGIN in text and END in text:
        start, stop = text.index(BEGIN), text.index(END) + len(END) + 1
        new = text[:start] + block + text[stop:]
    else:
        new = text + ("\n" if text and not text.endswith("\n\n") else "") + block
        new = new.replace("\n\n\n", "\n\n")
    if new == text:
        return False
    path.write_text(new)
    return True


def setup(root: Path, prefix: str, *, statusline: bool) -> list[str]:
    """Returns a line per file, saying what was done to it."""
    done = []

    def note(changed: bool, path: Path, what: str) -> None:
        rel = path.relative_to(root)
        done.append(f"  {'updated' if changed else 'already set':<11}  {rel}  ({what})")

    claude = root / ".claude" / "settings.json"
    note(_add_session_hook(claude), claude, "Claude Code session-start hook")
    if statusline:
        changed = _set_statusline(claude)
        if not changed and json.loads(claude.read_text())["statusLine"].get(
                "command") != STATUSLINE_COMMAND:
            done.append(f"  {'skipped':<11}  {claude.relative_to(root)}  "
                        "(a statusLine is already configured; left alone)")
        else:
            note(changed, claude, "Claude Code status line")

    codex_hooks = root / ".codex" / "hooks.json"
    note(_add_session_hook(codex_hooks), codex_hooks, "Codex session-start hook")
    codex_config = root / ".codex" / "config.toml"
    note(_enable_codex_hooks(codex_config), codex_config, "Codex hooks enabled")

    for name in ("CLAUDE.md", "AGENTS.md"):
        path = root / name
        note(_write_instructions(path, prefix), path, "instructions for the agent")
    return done
