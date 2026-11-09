"""Code extensions: new operations Grok (or a developer) adds without touching the core.

An extension is a small Python package with a manifest `plsi_extension.yaml`:

    id: my_ext            # lowercase id
    version: 0.1.0        # semver; an installed version is immutable
    kind: importer        # importer | deriver | evidence_source
    entrypoint: main:run  # <module file without .py>:<function>
    summary: one line
    monotone: false       # derivers only: true lets censored inputs be bounded through it

Entry-point signatures by kind:
- importer (Life):          run(data: bytes) -> list[dict]  ObservationInput dicts; origin is filled in by ingest
- deriver (Map):            run(inputs: dict[str, float], *, age: float | None, sex: str) -> float
- evidence_source (Policy): run(query: str, limit: int) -> list[dict]  {title, url?, kind?, id?}; kind/id as SourceRef

Rules: extensions ADD operations and never replace core ones. An extension is full-trust code that runs with the
user's permissions: it is installed only with the user's explicit approval naming its source. The static checks are
guard rails, not a sandbox: `install` refuses a package that imports a layer its kind may not use, assigns attributes
of plsi modules, or whose own tests (tests/test_*.py, run with a minimal environment and no keys) fail. Installs live in
<PLSI_HOME>/extensions/<id>/<version>/ and every derived value records extension@version in its provenance.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import Field

from plsi.errors import NotFound, ValidationError, violation
from plsi.files import atomic_write_json, loads_json
from plsi.models import Id, Model, parse
from plsi.paths import plsi_home, safe_join
from plsi.vocab import ClaimLevel, ExtensionKind

MANIFEST = "plsi_extension.yaml"
TEST_TIMEOUT_S = 300
# what an extension's tests see of the environment: enough to start Python, never a key
TEST_ENV = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "APPDATA",
            "LOCALAPPDATA", "LANG", "LC_ALL", "PYTHONIOENCODING")
FORBIDDEN_IMPORTS: dict[ExtensionKind, tuple[str, ...]] = {
    ExtensionKind.IMPORTER: ("plsi.map", "plsi.policy", "plsi.life.store", "plsi.life.index", "plsi.life.observed", "plsi.cli"),
    ExtensionKind.DERIVER: ("plsi.policy", "plsi.life.store", "plsi.life.index", "plsi.life.observed", "plsi.cli"),
    ExtensionKind.EVIDENCE_SOURCE: ("plsi.life", "plsi.cli"),
}


class ExtensionManifest(Model):
    id: Id
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    kind: ExtensionKind
    entrypoint: Annotated[str, Field(pattern=r"^[A-Za-z_]\w*:[A-Za-z_]\w*$")]
    summary: str = Field(min_length=1, max_length=300)
    claim_level: ClaimLevel = ClaimLevel.DESIGNED
    monotone: bool = False


def _root() -> Path:
    return plsi_home() / "extensions"


def _index_path() -> Path:
    return _root() / "index.json"


def _index() -> dict[str, Any]:
    path = _index_path()
    return loads_json(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _read_manifest(directory: Path) -> ExtensionManifest:
    path = directory / MANIFEST
    if not path.is_file():
        raise NotFound(f"{MANIFEST} not found in {directory}", code="EXTENSION_INVALID")
    return parse(ExtensionManifest, yaml.safe_load(path.read_text(encoding="utf-8")), MANIFEST)


def _static_violations(directory: Path, m: ExtensionManifest) -> list[str]:
    """Layering by kind, no patching of plsi modules, entry point defined."""
    out, module, func = [], *m.entrypoint.split(":")
    if not (directory / f"{module}.py").is_file():
        out.append(f"entrypoint module {module}.py is missing")
    for path in sorted(directory.rglob("*.py")):
        rel = path.relative_to(directory).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), rel)
        plsi_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for name in names:
                    if name.startswith(FORBIDDEN_IMPORTS[m.kind]):
                        out.append(f"{rel}: a {m.kind.value} may not import {name}")
                    if name.startswith("plsi"):
                        plsi_names |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("setattr", "delattr"):
                out.append(f"{rel}: {node.func.id}() is not allowed")
        for node in ast.walk(tree):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else []
            for t in targets:
                base = t
                while isinstance(base, ast.Attribute):
                    base = base.value
                if t is not base and isinstance(base, ast.Name) and base.id in plsi_names:
                    out.append(f"{rel}: assigning to {ast.unparse(t)} would patch plsi; extensions add, never replace")
        if rel == f"{module}.py" and not any(isinstance(n, ast.FunctionDef) and n.name == func for n in tree.body):
            out.append(f"entrypoint function {func}() is not defined in {rel}")
    return out


def _run_tests(directory: Path) -> str:
    tests = sorted((directory / "tests").glob("test_*.py"))
    if not tests:
        raise ValidationError("an extension needs tests: tests/test_*.py", code="EXTENSION_INVALID")
    with tempfile.TemporaryDirectory() as home:  # tests never touch real instances
        kept = {k: v for k in TEST_ENV if (v := os.environ.get(k)) is not None}  # no keys or tokens reach the tests
        env = kept | {"PYTHONPATH": str(directory), "PLSI_HOME": home, "PLSI_ENV_FILE": str(Path(home) / "none")}
        proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *map(str, tests)],
                              cwd=directory, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=TEST_TIMEOUT_S)
    last = (proc.stdout.strip().splitlines() or ["no output"])[-1]
    if proc.returncode != 0:
        raise ValidationError(f"extension tests failed: {last}", code="EXTENSION_TESTS_FAILED", hint=proc.stdout[-1500:])
    return last


def install(dir: str) -> dict[str, Any]:  # noqa: A002 - CLI dest name
    """`plsi ext install --dir D`: check, test, then copy to <PLSI_HOME>/extensions/<id>/<version>/ and activate."""
    source = Path(dir).resolve()
    m = _read_manifest(source)
    if bad := _static_violations(source, m):
        raise ValidationError(f"extension {m.id} has {len(bad)} problem(s)", code="EXTENSION_INVALID",
                              violations=[violation(m.id, "extension.static", b) for b in bad])
    target = safe_join(_root(), m.id, m.version)
    if target.exists():
        raise ValidationError(f"{m.id} {m.version} is already installed", code="EXTENSION_EXISTS", hint="bump version")
    index = _index()
    if m.id in index and index[m.id]["kind"] != m.kind.value:
        raise ValidationError(f"{m.id} is already installed as a {index[m.id]['kind']}", code="EXTENSION_INVALID")
    tests = _run_tests(source)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"))
    entry = index.get(m.id, {"versions": []})
    index[m.id] = {**entry, "kind": m.kind.value, "active": m.version, "versions": sorted({*entry["versions"], m.version})}
    atomic_write_json(_index_path(), index)
    _load.cache_clear()
    return {"id": m.id, "version": m.version, "kind": m.kind.value, "tests": tests, "path": str(target), "active": True}


def list_extensions() -> list[dict[str, Any]]:
    return [{"id": k, **v} for k, v in sorted(_index().items())]


def test(id: str) -> dict[str, Any]:  # noqa: A002
    """Re-run the active version's tests."""
    return {"id": id, "tests": _run_tests(_active_dir(id))}


def disable(id: str) -> dict[str, Any]:  # noqa: A002
    index = _index()
    if id not in index:
        raise NotFound(f"no extension {id!r}", code="EXTENSION_NOT_FOUND", hint="plsi ext list")
    index[id]["active"] = None
    atomic_write_json(_index_path(), index)
    _load.cache_clear()
    return {"id": id, "active": None}


def installed(kind: ExtensionKind) -> dict[str, str]:
    """Active extensions of a kind -> {id: version}."""
    return {k: v["active"] for k, v in _index().items() if v["kind"] == kind and v.get("active")}


def _active_dir(ext_id: str) -> Path:
    entry = _index().get(ext_id)
    if not entry or not entry.get("active"):
        raise NotFound(f"no active extension {ext_id!r}", code="EXTENSION_NOT_FOUND", hint="plsi ext list")
    return safe_join(_root(), ext_id, entry["active"])


def manifest(ext_id: str) -> ExtensionManifest:
    return _read_manifest(_active_dir(ext_id))


def rule_id(ext_id: str) -> str:
    """Provenance rule of an extension value, e.g. 'extension:my_ext@0.1.0'."""
    return f"extension:{ext_id}@{manifest(ext_id).version}"


@lru_cache(maxsize=64)
def _load(ext_id: str, version: str) -> Callable[..., Any]:
    directory = safe_join(_root(), ext_id, version)
    module, func = _read_manifest(directory).entrypoint.split(":")
    spec = importlib.util.spec_from_file_location(f"plsi_ext_{ext_id}_{version.replace('.', '_')}", directory / f"{module}.py")
    assert spec and spec.loader
    loaded = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(directory))  # the extension may import its own helper modules
    try:
        spec.loader.exec_module(loaded)
    finally:
        sys.path.remove(str(directory))
    return getattr(loaded, func)


def _function(ext_id: str, kind: ExtensionKind) -> Callable[..., Any]:
    m = manifest(ext_id)
    if m.kind != kind:
        raise ValidationError(f"{ext_id} is a {m.kind.value}, not a {kind.value}", code="EXTENSION_KIND")
    return _load(ext_id, m.version)


def importer(ext_id: str) -> Callable[[bytes], list[dict[str, Any]]]:
    return _function(ext_id, ExtensionKind.IMPORTER)


def deriver(ext_id: str) -> Callable[..., float]:
    return _function(ext_id, ExtensionKind.DERIVER)


def evidence_source(ext_id: str) -> Callable[[str, int], list[dict[str, Any]]]:
    return _function(ext_id, ExtensionKind.EVIDENCE_SOURCE)
