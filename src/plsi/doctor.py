"""`plsi doctor`: versions, extras, PLSI_HOME, keys (booleans + what each unlocks) and fixes; --online checks
that every reference's api_base answers."""

from __future__ import annotations

import importlib.metadata
import platform
from typing import Any

from plsi import __version__, errors, keys, net, paths, references


def _version(dist: str) -> str | None:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def _sqlite() -> str | None:
    try:
        import sqlite3
    except ImportError:
        return None
    return sqlite3.sqlite_version


def _reachable(url: str) -> str:
    try:
        net.request("GET", url, timeout=15)
        return "ok"
    except errors.NetworkError as exc:
        return "ok" if exc.status is not None else "unreachable"


def doctor(online: bool = False) -> dict[str, Any]:
    """`plsi doctor`."""
    versions = {"plsi": __version__, "python": platform.python_version(), "pydantic": _version("pydantic"),
                "pyyaml": _version("PyYAML"), "biolearn": _version("biolearn"), "sqlite": _sqlite()}
    home = paths.plsi_home()
    fixes: list[str] = []
    if versions["sqlite"] is None:
        fixes.append("the event index needs Python's sqlite3 module: install a Python build that includes SQLite")
    if versions["biolearn"] is None:
        fixes.append('methylation clocks: python -m pip install -e "<repo>[clocks]"')
    if not keys.env_file().is_file():
        fixes.append("optional keys: copy .env.example to .env and fill only what you use")
    try:
        refs = references.load()
    except errors.PlsiError as exc:
        refs = {}
        fixes.append(f"mothership/references/ is invalid: {exc.message}")
    status = keys.status()
    out: dict[str, Any] = {
        "versions": versions,
        "extras": {"clocks": versions["biolearn"] is not None},
        "plsi_home": str(home),
        "plsi_home_exists": home.is_dir(),
        "keys": {name: {"set": is_set, "unlocks": keys.UNLOCKS[name]} for name, is_set in status.items()},
        "references": len(refs),
        "maps": sorted(p.stem for p in (paths.REPO_ROOT / "maps").glob("*.yaml")),
        "fixes": fixes,
    }
    if online:
        out["reachability"] = {ref.id: _reachable(ref.api_base) for ref in refs.values() if ref.api_base}
    return out
