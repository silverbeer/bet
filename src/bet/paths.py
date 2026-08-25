"""XDG path resolution and the git-work-tree safety rule.

BET's data is a complete personal financial record and this repository is
public. Keeping the two apart is structural: no code may place data inside a
git work tree, and `bet init` refuses to start one there.
"""

from __future__ import annotations

import os
from pathlib import Path

from bet.errors import DataLocationError

APP_NAME = "bet"


def _xdg(var: str, fallback: str) -> Path:
    """Resolve an XDG base directory, honouring the environment variable.

    An empty or relative value is treated as unset, per the XDG specification.
    """
    raw = os.environ.get(var, "")
    if raw and Path(raw).is_absolute():
        return Path(raw)
    return Path.home() / fallback


def xdg_data_home() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share")


def xdg_config_home() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config")


def xdg_state_home() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state")


def default_data_dir() -> Path:
    return xdg_data_home() / APP_NAME


def default_config_path() -> Path:
    return xdg_config_home() / APP_NAME / "config.toml"


def default_state_dir() -> Path:
    return xdg_state_home() / APP_NAME


def find_git_work_tree(path: Path) -> Path | None:
    """Return the root of the git work tree containing ``path``, if any.

    Walks upward from the fully resolved path. ``.git`` may be a directory or,
    for linked worktrees and submodules, a file — both count.

    The path need not exist: resolution is symlink-aware and ``..`` is
    collapsed, so a path that escapes the repository and re-enters through a
    symlink is still caught.
    """
    resolved = path.expanduser().resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def assert_outside_git(path: Path, *, field: str) -> Path:
    """Return the resolved path, or raise if it lands inside a git work tree.

    This is a hard requirement, not a warning. There is no override flag: an
    override would exist precisely to be used on the day it should not be.
    """
    resolved = path.expanduser().resolve()
    work_tree = find_git_work_tree(resolved)
    if work_tree is None:
        return resolved

    raise DataLocationError(
        f"{field} resolves inside a git work tree: {resolved}",
        remediation=(
            f"The git work tree is {work_tree}.\n"
            "BET data is a complete personal financial record and git history is\n"
            "permanent — a single commit discloses it irreversibly, and forks\n"
            "cannot be recalled.\n\n"
            f"Point {field} somewhere outside any repository, for example:\n"
            f"    {default_data_dir()}"
        ),
    )
