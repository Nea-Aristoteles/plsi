"""Seeded random synthetic history run on whichever `plsi` is first on PYTHONPATH: `python random_history.py SEED OUT.json`.

Each step is a native call (ingest, dry run, replay of an earlier batch, retract with replacement, revise); every result
and the final reads (events, counts, states of several maps, scoring candidates, care context) are written with event ids
replaced by their position in the log. `expected_random.json` holds what the engine at v0.2.1 (17c433c) produced for the
checked-in seeds; the current engine must produce the same. Synthetic data only.
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="plsi-random-"))
os.environ.update(PLSI_HOME=str(home), PLSI_TEST_MODE="1", PLSI_NOW="2026-06-01T00:00:00Z")

from plsi.life import ingest as I, revise, store  # noqa: E402
from plsi.map import mapper, registry  # noqa: E402

seed = int(sys.argv[1])
rng = random.Random(seed)
CODES = [("1751-7", ["g/L", "g/dL"], 3.0, 50.0), ("2160-0", ["umol/L", "mg/dL"], 0.5, 120.0), ("2345-7", ["mmol/L", "mg/dL", "furlongs"], 3.0, 140.0),
         ("1558-6", ["mg/dL"], 60.0, 130.0), ("27873-9", ["u[IU]/mL"], 2.0, 20.0), ("1988-5", ["mg/dL", "mg/L"], 0.1, 5.0), ("736-9", ["%"], 10.0, 50.0),
         ("787-2", ["fL"], 70.0, 100.0), ("788-0", ["%"], 11.0, 16.0), ("6768-6", ["U/L"], 30.0, 150.0), ("6690-2", ["10*3/uL"], 3.0, 11.0),
         ("8302-2", ["m", "cm"], 1.5, 190.0), ("29463-7", ["kg", "[lb_av]"], 50.0, 160.0), ("39156-5", ["kg/m2"], 18.0, 35.0),
         ("93832-4", ["min"], 300.0, 520.0), ("40443-4", ["/min"], 45.0, 80.0), ("5792-7", ["mg/dL"], 0.0, 3.0)]
DAYS = [f"{y}-{m:02d}-{d:02d}" for y, m in ((2025, 9), (2025, 12), (2026, 3), (2026, 5)) for d in (3, 4, 18, 28)] + ["2026-07-01"]
SETS = [("test-drug-a", "rxnorm", "1000", "medication"), ("test-drug-b", "rxnorm", "1001", "medication"), ("test condition", "icd10cm", "I10", "condition"),
        ("test-allergen", "snomed", "99", "allergy"), ("walks daily", None, None, "lifestyle")]
RES: list = []
sent: list = []


def row(n):
    if rng.random() < 0.15:
        text, system, code, kind = rng.choice(SETS)
        r = {"kind": kind, "original_text": text, "effective_at": rng.choice(DAYS), "origin": {"kind": "user_upload", "record_id": f"s{n % 5}"},
             "value_text": rng.choice(["active", "stopped", "x"])}
        if code:
            r["code"] = {"system": system, "code": code}
        if rng.random() < 0.5:
            r["clinical_status"] = rng.choice(["active", "stopped", "resolved"])
        return r
    code, units, lo, hi = rng.choice(CODES)
    r = {"kind": "measurement", "original_text": "metric", "effective_at": rng.choice(DAYS), "unit": rng.choice(units),
         "origin": {"kind": rng.choice(["user_upload", "device", "csv"]), "record_id": f"r{rng.randrange(25)}"}}
    if rng.random() > 0.05:
        r["code"] = {"system": "loinc", "code": code}
    r["value_quantity"] = round(rng.uniform(lo, hi), 2)
    for k, p, v in (("fasting", 0.3, lambda: rng.choice([True, False])), ("value_comparator", 0.1, lambda: rng.choice(["<", ">"])),
                    ("effective_basis", 0.08, lambda: "assumed"), ("repeat", 0.05, lambda: True), ("specimen", 0.05, lambda: "serum")):
        if rng.random() < p:
            r[k] = v()
    if rng.random() < 0.03:
        r["kind"] = "bogus"
    if rng.random() < 0.8:
        r["origin"]["file_sha256"] = rng.choice(["a" * 64, "b" * 64])
    return r


def path(name, rows):
    p = home / name
    p.write_text(json.dumps(rows), encoding="utf-8")
    return str(p)


store.init_instance("r", birth_year=1975, sex="M")
why = path("why.json", {"reason": "synthetic"})
for step in range(14):
    op = rng.random()
    if op < 0.5 or not sent:
        rows = [row(n) for n in range(rng.randrange(1, 80))]
        sent.append(rows)
        RES.append(["ingest", I.ingest("r", file=path("b.json", rows), dry_run=rng.random() < 0.25)])
    elif op < 0.7:
        RES.append(["replay", I.ingest("r", file=path("b.json", rng.choice(sent)), dry_run=rng.random() < 0.3)])
    elif op < 0.85:
        active = [e for e in store.list_events("r")["events"] if e["type"] == "observation" and not e["retracted"]]
        if active:
            e = rng.choice(active)
            rep = path("rep.json", row(1) | {"kind": "measurement"}) if rng.random() < 0.4 else None
            try:
                RES.append(["retract", I.retract("r", e["id"], why, replacement=rep)])
            except Exception as exc:  # noqa: BLE001
                RES.append(["retract-error", type(exc).__name__, getattr(exc, "code", "")])
    else:
        rows = [row(n) | {"origin": {"kind": "user_upload", "record_id": f"d{n}", "document": "doc-1"}} for n in range(rng.randrange(1, 20))]
        try:
            RES.append(["revise", revise.revise("r", document="doc-1", file=path("v.json", rows), dry_run=rng.random() < 0.3)])
        except Exception as exc:  # noqa: BLE001
            RES.append(["revise-error", type(exc).__name__, getattr(exc, "code", "")])

RES.append(["events", store.list_events("r")])
RES.append(["counts", store.event_counts("r")])
RES.append(["select", registry.select("r")])
RES.append(["context", mapper.context("r").dump()])
for map_id in ("blood_phenoage", "cardiometabolic", "sleep_activity", "care_context", "lifestyle_context", "routine_blood"):
    m = registry.get(map_id, "r")
    RES.append([f"state_{map_id}", mapper.build_state("r", m).dump()])
    for c in m.coordinates:
        if c.kind.value != "set":
            RES.append([f"observed_{map_id}_{c.id}", [o.dump() for o in mapper.coordinate_observations("r", m, c.id)]])

order = [json.loads(line)["id"] for line in (home / "r" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
ids = {e: f"E{n}" for n, e in enumerate(order)}
text = re.sub(r"evt_[0-9a-f]{8,32}", lambda mo: ids.get(mo.group(0), "E?"), json.dumps(RES, sort_keys=True, default=str))


def scrub(x):
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if k not in ("state_hash", "subject_id", "path", "created_at", "last_recorded_at")}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    return x


Path(sys.argv[2]).write_text(json.dumps(scrub(json.loads(text)), sort_keys=True) + "\n", encoding="utf-8")
