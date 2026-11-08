"""Map registry: shipped maps/*.yaml + <instance>/maps/user/*.yaml, validation, snapshots."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from plsi import codes, errors, files, net, paths, references
from plsi.errors import NotFound, ValidationError, violation
from plsi.life.store import EventStore
from plsi.map import biolearn, derive
from plsi.map.models import Derive, MapContract, MapCoordinate
from plsi.models import SourceRef, parse
from plsi.paths import REPO_ROOT
from plsi.vocab import ClaimLevel, CodeSystem, CoordinateKind, CorrespondenceType, DeriveEngine, Ethnicity, Sex, SourceKind

SHIPPED_DIR = REPO_ROOT / "maps"
LIVE_BATCH = 50
LOAD_CHECKED = frozenset({DeriveEngine.NATIVE, DeriveEngine.EXTENSION})  # biolearn names: maps validate only (slow import)


def _parse(text: str, origin: str) -> MapContract:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"{origin}: not valid YAML: {exc}", code="MAP_INVALID") from None
    return parse(MapContract, data, origin, code="MAP_INVALID")


def _rule_violations(m: MapContract, user: bool, checked: frozenset[DeriveEngine] = LOAD_CHECKED) -> list[dict[str, str]]:
    """Checks beyond the schema; derive model names are checked for the `checked` engines that can list them."""
    out = []
    if user and not m.id.startswith("user_"):
        out.append(violation("id", "map.user_prefix", "user map ids must start with user_", f"user_{m.id}"))
    if user and m.claim_level != ClaimLevel.DESIGNED:
        out.append(violation("claim_level", "map.user_claim_level", "user maps must have claim_level designed", "designed"))
    used = {c.derive.engine for c in m.coordinates if c.derive} & checked
    known = {e: fn() for e in used if (fn := derive.ENGINES[e].models)}
    for i, c in enumerate(m.coordinates):
        d = c.derive
        if d is None or known.get(d.engine) is None or d.model in known[d.engine]:  # type: ignore[operator]
            continue
        rule, message, hint = derive.ENGINES[d.engine].unknown  # type: ignore[misc]
        out.append(violation(f"coordinates.{i}.derive.model", rule, message.format(d.model), hint))
    for i, c in enumerate(m.coordinates):
        if c.ranges and (tie := next(((sex, e) for sex in (Sex.F, Sex.M, Sex.UNKNOWN) for e in (*Ethnicity, None)
                                      if len(c.best_ranges(sex, e)) > 1), None)):
            out.append(violation(f"coordinates.{i}.ranges", "map.range_tie",
                                 f"two {c.id} ranges fit sex {tie[0]}, ethnicity {tie[1]} equally",
                                 "give one range both sex and ethnicity, or drop one"))
    return out


def _taken_violations(m: MapContract) -> list[dict[str, str]]:
    """A user map never accepts a code a shipped coordinate already accepts, except on a copy of that coordinate (same
    id, e.g. a formula's inputs): otherwise one observation would be read twice, or taken from its shipped home."""
    owner = {(code.system, code.code): (sm.id, c.id) for sm in load_all().values() for c in sm.coordinates
             for code in c.accepts.codes}
    out = []
    for i, c in enumerate(m.coordinates):
        for j, code in enumerate(c.accepts.codes):
            if (home := owner.get((code.system, code.code))) and home[1] != c.id:
                out.append(violation(f"coordinates.{i}.accepts.codes.{j}", "map.code_taken",
                                     f"{code.code} is already read by {home[0]}.{home[1]}", f"use {home[0]}.{home[1]}"))
    return out


def _live_violations(m: MapContract) -> list[dict[str, str]]:
    """Every LOINC code must exist in NLM Clinical Tables; accepted codes must not be deprecated."""
    where: dict[str, tuple[str, bool]] = {}
    for i, c in enumerate(m.coordinates):
        for field, accepted in (("codes", True), ("reject_codes", False)):
            for j, code in enumerate(getattr(c.accepts, field)):
                if code.system == CodeSystem.LOINC:
                    where.setdefault(code.code, (f"coordinates.{i}.accepts.{field}.{j}", accepted))
    url = references.api_base("nlm_clinical_tables_loinc") + "/api/loinc_items/v3/search"
    names: dict[str, str] = {}
    todo = sorted(where)
    for k in range(0, len(todo), LIVE_BATCH):
        batch = todo[k:k + LIVE_BATCH]
        reply = net.get_json(url, params={"terms": "", "q": f"LOINC_NUM:({' OR '.join(batch)})",
                                          "df": "LOINC_NUM,LONG_COMMON_NAME", "maxList": 500})
        names |= {num: name for num, name in reply[3]}
    out = []
    for code, (path, accepted) in where.items():
        if code not in names:
            out.append(violation(path, "map.loinc_unknown", f"LOINC {code} does not resolve in NLM Clinical Tables"))
        elif accepted and names[code].startswith("Deprecated"):
            out.append(violation(path, "map.loinc_deprecated", f"LOINC {code} is deprecated: {names[code]}", "move it to reject_codes"))
    return out


def _expanded(m: MapContract) -> MapContract:
    """Shipped/user maps with `expand`: add a derived coordinate per biolearn clock not already declared.
    Snapshots are stored expanded and never re-expanded, so a newer biolearn cannot change an old map hash."""
    if m.expand is None or not biolearn.available():
        return m
    have = {c.id for c in m.coordinates}
    extra = [
        MapCoordinate(id=cid, label=f"{c['name']}: {c['output']} ({c['tissue']}, {c['year']})", kind=CoordinateKind.DERIVED,
                      unit=c["unit"], derive=Derive(engine=DeriveEngine.BIOLEARN, model=c["name"], inputs=[m.expand.input]),
                      correspondence_type=CorrespondenceType.STATISTICAL,
                      sources=[SourceRef(kind=SourceKind.MODEL, id="biolearn", title=c["name"], url=c["source"])])
        for c in biolearn.clocks() if (cid := re.sub(r"[^a-z0-9_]+", "_", c["name"].lower()).strip("_")) not in have
    ]
    return MapContract.model_validate({**m.dump(),
                                       "coordinates": [*m.dump()["coordinates"],
                                                       *(x.dump() for x in extra)]})


@lru_cache(maxsize=32)
def _shipped(path: Path, mtime_ns: int) -> MapContract:
    """A parsed shipped map, re-read only when its file changes."""
    return _parse(path.read_text(encoding="utf-8"), path.name)


def _user_dir(instance: str) -> Path:
    return paths.instance_dir(instance) / "maps" / "user"


def load_all(instance: str | None = None) -> dict[str, MapContract]:
    """All maps by id: shipped maps plus, when instance is given, its maps/user/*.yaml.
    Raises errors.ValidationError (with violations) on an invalid file or a duplicate id."""
    files = [(p, False) for p in sorted(SHIPPED_DIR.glob("*.yaml"))]
    if instance is not None:
        files += [(p, True) for p in sorted(_user_dir(instance).glob("*.yaml"))]
    out: dict[str, MapContract] = {}
    for path, user in files:
        raw = _parse(path.read_text(encoding="utf-8"), path.name) if user else _shipped(path, path.stat().st_mtime_ns)
        m = _expanded(raw)
        if bad := _rule_violations(m, user):
            raise ValidationError(f"{path.name}: {bad[0]['message']}", code="MAP_INVALID", violations=bad)
        if m.id in out:
            raise ValidationError(f"duplicate map id {m.id!r} ({path.name})", code="MAP_DUPLICATE")
        out[m.id] = m
    return out


def rule_ids() -> set[str]:
    """Rule ids `evidence verify --kind rule` resolves: the native models plus the shipped maps' expr formulas."""
    expr = derive.ENGINES[DeriveEngine.EXPR].rule_id
    return derive.rule_ids() | {expr(c.derive) for m in load_all().values() for c in m.coordinates
                                if c.derive and c.derive.engine == DeriveEngine.EXPR}


def get(map_id: str, instance: str | None = None) -> MapContract:
    """One map by id (shipped or the instance's user maps). Raises errors.NotFound (code MAP_NOT_FOUND)."""
    maps = load_all(instance)
    if map_id not in maps:
        raise NotFound(f"unknown map {map_id!r}", code="MAP_NOT_FOUND", hint="plsi maps list")
    return maps[map_id]


def declared_codes(instance: str | None = None) -> set[tuple[str, str]]:
    """(system, code) of every free and cpg code a shipped or installed map accepts: PLSI names that may leave."""
    return {(k.system.value, k.code) for m in load_all(instance).values() for c in m.coordinates for k in c.accepts.codes
            if k.system in codes.LOCAL}


def canonical_units(instance: str | None = None) -> set[str]:
    """The canonical unit of every coordinate of the shipped and installed maps."""
    return {c.unit for m in load_all(instance).values() for c in m.coordinates if c.unit}


def snapshot_path(instance: str, map_id: str, map_hash: str) -> Path:
    return paths.safe_join(paths.instance_dir(instance), "maps", f"{map_id}@{files.hash12(map_hash)}.json")


def snapshot(instance: str, m: MapContract) -> Path:
    """Write-once <instance>/maps/<map_id>@<hash12>.json; an existing snapshot is never rewritten."""
    path = snapshot_path(instance, m.id, m.compute_hash())
    if not path.exists():
        files.atomic_write_json(path, m.dump())
    return path


def load_snapshot(instance: str, map_id: str, map_hash: str) -> MapContract:
    """The exact map a plan/state was made with (scoring resolves through it). Raises errors.NotFound."""
    path = snapshot_path(instance, map_id, map_hash)
    if not path.is_file():
        raise NotFound(f"no snapshot of {map_id} {files.hash12(map_hash)}", code="SNAPSHOT_NOT_FOUND")
    m = _parse(path.read_text(encoding="utf-8"), path.name)
    if m.compute_hash() != map_hash:
        raise ValidationError(f"snapshot {path.name} does not match its hash", code="SNAPSHOT_CORRUPT")
    return m


def _validate(text: str, origin: str, user: bool, live: bool) -> dict[str, Any]:
    m = _expanded(_parse(text, origin))
    if not biolearn.available() and any(c.derive and c.derive.engine == DeriveEngine.BIOLEARN for c in m.coordinates):
        errors.warn("DEPENDENCY_MISSING", f"biolearn model names not checked; {biolearn.HINT}")
    bad = _rule_violations(m, user, frozenset(DeriveEngine)) + (_taken_violations(m) if user else [])
    bad += _live_violations(m) if live else []
    if bad:
        raise ValidationError(f"map {m.id} has {len(bad)} violation(s)", code="MAP_INVALID", violations=bad)
    return {"map_id": m.id, "map_version": m.map_version, "map_hash": m.compute_hash(),
            "coordinates": len(m.coordinates), "user_map": user, "live_checked": live}


def validate_file(file: str, live: bool = False) -> dict[str, Any]:
    """`plsi maps validate FILE [--live]`: schema, derive models, user-map rules and (live) LOINC resolution.
    Collects ALL violations and raises errors.ValidationError. Files outside SHIPPED_DIR are user maps."""
    user = file == "-" or Path(file).resolve().parent != SHIPPED_DIR.resolve()
    return _validate(files.read_input_text(file), Path(file).name, user, live)


def install(instance: str, file: str) -> dict[str, Any]:
    """`plsi maps install --instance N --file F`: validate as a user map, then copy to <instance>/maps/user/<id>.yaml."""
    text = files.read_input_text(file)
    result = _validate(text, Path(file).name, user=True, live=False)
    if result["map_id"] in load_all():
        raise ValidationError(f"{result['map_id']!r} clashes with a shipped map", code="MAP_DUPLICATE")
    path = paths.safe_join(_user_dir(instance), f"{result['map_id']}.yaml")
    with files.lock(paths.instance_dir(instance)):
        files.atomic_write_text(path, text)
    return {"map_id": result["map_id"], "map_hash": result["map_hash"], "path": str(path)}


def list_maps(instance: str | None = None) -> list[dict[str, Any]]:
    """`plsi maps list`: [{id, title, map_version, claim_level, tags, coordinates, user_map, map_hash}] sorted by id."""
    return [
        {"id": m.id, "title": m.title, "map_version": m.map_version, "claim_level": m.claim_level.value, "tags": m.tags,
         "coordinates": len(m.coordinates), "user_map": m.id.startswith("user_"), "map_hash": m.compute_hash()}
        for m in sorted(load_all(instance).values(), key=lambda m: m.id)
    ]


def show(map_id: str, instance: str | None = None) -> dict[str, Any]:
    """`plsi maps show ID`: the full map plus map_hash."""
    m = get(map_id, instance)
    return {**m.dump(), "map_hash": m.compute_hash()}


def select(instance: str) -> list[dict[str, Any]]:
    """`plsi maps select --instance N`: per map {map_id, title, present, missing, ratio, tags}, sorted by ratio desc
    then id. Coverage only (no clock is computed); Grok chooses; nothing is written."""
    from plsi.map import mapper

    store = EventStore(instance)
    out = []
    for m in load_all(instance).values():
        present = mapper.coverage(instance, m, store)
        missing = [c.id for c in m.coordinates if c.id not in present]
        out.append({"map_id": m.id, "title": m.title, "present": present, "missing": missing,
                    "ratio": round(len(present) / len(m.coordinates), 3), "tags": m.tags})
    return sorted(out, key=lambda x: (-x["ratio"], x["map_id"]))
