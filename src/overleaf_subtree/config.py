"""Per-project configuration.

Lives at the repository root as ``.overleaf-subtree.toml``.  Everything that
differs between projects belongs here; nothing in this package knows about
LaTeX, generators, or any particular manuscript.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_NAME = ".overleaf-subtree.toml"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Check:
    """A command run before publishing.  Non-zero exit blocks the push."""

    name: str
    run: str
    # Relative to the prefix directory, so checks read naturally as if the
    # manuscript were the whole project -- which, on Overleaf, it is.
    cwd: str = "."


@dataclass(frozen=True)
class Config:
    #: subdirectory of this repo that maps onto the Overleaf project root
    prefix: str
    #: git remote name for the Overleaf project
    remote: str = "overleaf"
    #: branch on that remote
    branch: str = "main"
    #: paths (glob, relative to the prefix) this repo generates.  A remote
    #: edit to one of these is reported loudly on pull: it will be lost the
    #: next time the generator runs, so it is a warning rather than a merge.
    repo_owned: tuple[str, ...] = ()
    checks: tuple[Check, ...] = field(default=())

    @property
    def remote_ref(self) -> str:
        return f"{self.remote}/{self.branch}"


def find_root(start: Path | None = None) -> Path:
    """Nearest ancestor holding a config file."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / CONFIG_NAME).is_file():
            return candidate
    raise ConfigError(
        f"no {CONFIG_NAME} found in {here} or any parent.\n"
        f"Create one at the repository root; see the README for the schema."
    )


def load(root: Path) -> Config:
    path = root / CONFIG_NAME
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    prefix = raw.get("prefix")
    if not prefix:
        raise ConfigError(f"{path}: 'prefix' is required (the subdirectory to mirror)")
    prefix = prefix.rstrip("/")
    if not (root / prefix).is_dir():
        raise ConfigError(f"{path}: prefix '{prefix}' is not a directory in {root}")

    checks = tuple(
        Check(
            name=c.get("name") or f"check-{i + 1}",
            run=c["run"],
            cwd=c.get("cwd", "."),
        )
        for i, c in enumerate(raw.get("checks", []))
        if c.get("run")
    )

    return Config(
        prefix=prefix,
        remote=raw.get("remote", "overleaf"),
        branch=raw.get("branch", "main"),
        repo_owned=tuple(raw.get("repo_owned", ())),
        checks=checks,
    )
