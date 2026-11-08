"""Deterministic synthetic scenario run against whichever `plsi` is first on PYTHONPATH.

`python tests/golden/scenario.py OUT.json [--legacy DIR | --keep DIR]` writes the normalized outputs of ingest, retraction, revision, events,
projection, coverage, scoring candidates and care context for one small synthetic history. The checked-in
`expected.json` was produced by the engine at v0.2.1 (17c433c) before the event index existed; the equivalence test
re-runs this script on the current engine and compares. `--keep DIR` copies the finished instance (log written by the
engine that ran) to DIR; `--legacy DIR` skips every write, copies such an instance in and runs only the reads, which is
how a log written by v0.2.1 is checked against the current engine. Event ids are random, so they are replaced by their position
in the log; clocks are pinned with PLSI_TEST_MODE / PLSI_NOW. Synthetic data only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp(prefix="plsi-golden-"))
os.environ.update(PLSI_HOME=str(home), PLSI_TEST_MODE="1", PLSI_NOW="2026-06-01T00:00:00Z")

from plsi import clock, files  # noqa: E402
from plsi.life import ingest as I, revise, store  # noqa: E402
from plsi.map import mapper, registry  # noqa: E402

PHENO = {  # coordinate -> (LOINC, unit, value)
    "albumin": ("1751-7", "g/L", 45.0), "creatinine": ("2160-0", "umol/L", 80.0), "glucose": ("2345-7", "mmol/L", 5.0),
    "crp": ("1988-5", "mg/dL", 0.2), "lymphocyte_pct": ("736-9", "%", 30.0), "mcv": ("787-2", "fL", 90.0),
    "rdw": ("788-0", "%", 13.0), "alp": ("6768-6", "U/L", 70.0), "wbc": ("6690-2", "10*3/uL", 6.0),
}
RES: dict[str, object] = {}


def obs(text, code=None, value=1.0, unit=None, day="2026-03-01", kind="measurement", origin="user_upload", record=None, sha=None, system="loinc", **more):
    row = {"kind": kind, "original_text": text, "effective_at": day, "origin": {"kind": origin}, **more}
    if code:
        row["code"] = {"system": system, "code": code}
    if isinstance(value, bool):
        row["value_bool"] = value
    elif isinstance(value, str):
        row["value_text"] = value
    else:
        row["value_quantity"] = value
    if unit:
        row["unit"] = unit
    if record:
        row["origin"]["record_id"] = record
    if sha:
        row["origin"]["file_sha256"] = sha
    return row


def write(name, rows):
    path = home / f"{name}.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return str(path)


def pheno(day, scale=1.0, tag="a"):
    return [obs(k, c, v * scale, u, day, record=f"{tag}-{k}") for k, (c, u, v) in PHENO.items()]


def run(name, **kw):
    RES[name] = I.ingest("g", **kw)


import shutil  # noqa: E402

args = sys.argv[2:]
legacy = Path(args[args.index("--legacy") + 1]) if "--legacy" in args else None
keep = Path(args[args.index("--keep") + 1]) if "--keep" in args else None
WRITES = ("cardio", "pheno1", "pheno1_replay", "pheno2", "pheno2_dry", "glucose", "glucose_replay", "monthly1", "monthly2", "sets", "sets_csv",
          "retract", "retract_replace", "reingest_after_retract", "revise1", "revise2")


def writes():
    global events
    store.init_instance("g", birth_year=1970, sex="F")
    SHA = "a" * 64
    run("pheno1", file=write("p1", pheno("2026-01-10") + [obs("rejected", None, "x", day="2026-01-10", kind="bogus")]))
    run("pheno1_replay", file=write("p1", pheno("2026-01-10")))
    run("pheno2", file=write("p2", pheno("2026-04-20", 1.1, "b")))
    RES["pheno2_dry"] = I.ingest("g", file=write("p3", pheno("2026-05-10", 0.9, "c")), dry_run=True)
    glu = [obs("glucose", "2345-7", 5.0, "mmol/L", "2026-05-15T08:00:00Z", record=f"ord-{i}", sha=SHA) for i in range(40)]
    glu += [obs("glucose", "2345-7", 5.0, "mmol/L", "2026-05-15T08:00:00Z", record="ord-1", sha=SHA)]  # same file repeat
    glu += [obs("glucose", "2345-7", 5.0, "mmol/L", "2026-05-15T08:00:00Z", record="again", repeat=True, origin="device"),
            obs("glucose", "2345-7", 5.0, "mmol/L", "2026-05-15T08:00:00Z", record="again", repeat=True, origin="device")]
    glu += [obs("glucose", "2345-7", 90.0, "mg/dL", "2026-05-16T08:00:00Z", record="mg"),
            obs("glucose", "2345-7", 5.5, "mmol/L", "2026-05-17T08:00:00Z", fasting=True, record="f"),
            obs("glucose", "2345-7", 7.0, "mmol/L", "2026-05-17T08:00:00Z", fasting=False, record="nf"),
            obs("glucose", "2345-7", 4.0, "mmol/L", "2026-05-18T08:00:00Z", value_comparator="<", record="cmp"),
            obs("glucose", "2345-7", 6.0, "furlongs", "2026-05-19T08:00:00Z", record="bad-unit"),
            obs("glucose", "2345-7", "text", None, "2026-05-19T09:00:00Z", record="txt"),
            obs("glucose", "2345-7", 5.9, "mmol/L", "2026-05-20", record="assumed", effective_basis="assumed")]
    cardio = [obs("Height", "8302-2", 1.75, "m", "2025-06-01", record="h1"), obs("Height", "8302-2", 176.0, "cm", "2026-02-01", record="h2"),
              obs("Weight", "29463-7", 70.0, "kg", "2026-01-05", record="w1"), obs("Weight", "29463-7", 72.0, "kg", "2026-03-05", record="w2"),
              obs("BMI", "39156-5", 23.5, "kg/m2", "2026-03-05", record="b1"), obs("Weight", "29463-7", 154.0, "[lb_av]", "2026-05-05", record="w3"),
              obs("Fasting glucose", "1558-6", 95.0, "mg/dL", "2026-03-05", record="fg1"),
              obs("Fasting glucose", "1558-6", 99.0, "mg/dL", "2026-04-05", fasting=True, record="fg2"),
              obs("Fasting glucose", "1558-6", 140.0, "mg/dL", "2026-04-06", fasting=False, record="fg3"),
              obs("Fasting insulin", "27873-9", 6.0, "u[IU]/mL", "2026-03-05", record="fi1"),
              obs("Fasting insulin", "27873-9", 8.0, "u[IU]/mL", "2026-04-05", record="fi2")]
    run("cardio", file=write("cm", cardio))
    run("glucose", file=write("g", glu))
    run("glucose_replay", file=write("g", glu))
    month = lambda m, v, rec, o="device": obs("Sleep duration", "93832-4", v, "min", f"2026-{m}-28", origin=o, record=rec)  # noqa: E731
    run("monthly1", file=write("m1", [month("02", 420.0, "m-02"), month("03", 410.0, "m-03")]))
    run("monthly2", file=write("m2", [month("03", 430.0, "m-03b"), month("03", 431.0, "m-03c", "csv"), month("04", 400.0, "m-04"),
                                       month("04", 401.0, "m-04b", "user_upload")]))
    sets = [obs("test-drug-a", "1000", "text", None, "2026-02-01", kind="medication", clinical_status="active", dose="1 unit", system="rxnorm"),
            obs("test-drug-b", None, "text", None, "2026-02-02", kind="medication", clinical_status="active"),
            obs("test-drug-b", None, "stopped", None, "2026-03-02", kind="medication", clinical_status="stopped"),
            obs("Test Condition", "I10", "text", None, "2026-02-03", kind="condition", clinical_status="active", system="icd10cm"),
            obs("test-allergen", "99", "text", None, "2026-02-04", kind="allergy", system="snomed"),
            obs("walks daily", None, "yes", None, "2026-02-05", kind="lifestyle"),
            obs("unmapped metric", "5792-7", 1.0, "mg/dL", "2026-02-06")]
    run("sets", file=write("s", sets))
    (home / "c.csv").write_text("effective_at,text,value,unit,code_system,code\n2026-05-01,Albumin,40,g/L,loinc,1751-7\n2026-05-01,Albumin,40,g/L,loinc,1751-7\n", encoding="utf-8")
    run("sets_csv", csv=str(home / "c.csv"))
    events = store.list_events("g")["events"]
    victim = next(e["id"] for e in events if e["payload"].get("original_text") == "unmapped metric")
    (home / "why.json").write_text(json.dumps({"reason": "synthetic test"}), encoding="utf-8")
    RES["retract"] = I.retract("g", victim, str(home / "why.json"))
    victim2 = next(e["id"] for e in events if e["payload"].get("origin", {}).get("record_id") == "ord-3")
    RES["retract_replace"] = I.retract("g", victim2, str(home / "why.json"), replacement=write("rep", obs("glucose", "2345-7", 8.0, "mmol/L", "2026-05-15T08:00:00Z", record="ord-3")))
    RES["reingest_after_retract"] = I.ingest("g", file=write("reingest", [obs("unmapped metric", "5792-7", 1.0, "mg/dL", "2026-02-06")]))
    docrow = lambda v: [obs("Albumin", "1751-7", v, "g/L", "2026-05-02", origin="user_upload", record="d1")]  # noqa: E731
    RES["revise1"] = revise.revise("g", document="doc-1", file=write("rv1", docrow(41.0)))
    RES["revise2"] = revise.revise("g", document="doc-1", file=write("rv2", docrow(42.0)))



if legacy is None:
    writes()
    if keep:
        shutil.copytree(home / "g", keep, dirs_exist_ok=True)
else:
    shutil.copytree(legacy, home / "g")
events = store.list_events("g")["events"]

RES["events_all"] = store.list_events("g")
RES["events_last5"] = store.list_events("g", limit=5)
RES["events_obs"] = store.list_events("g", type="observation", kind="medication")
RES["events_retraction"] = store.list_events("g", type="retraction")
RES["counts"] = store.event_counts("g")
RES["instances"] = store.list_instances()
RES["select"] = registry.select("g")
RES["world_change"] = str(mapper.last_world_change("g"))
RES["context"] = mapper.context("g").dump()
for map_id in ("blood_phenoage", "sleep_activity", "cardiometabolic", "care_context", "lifestyle_context"):
    RES[f"project_{map_id}"] = mapper.build_state("g", registry.get(map_id, "g")).dump()
m = registry.get("blood_phenoage", "g")
for cid in ("glucose", "albumin", "phenoage"):
    RES[f"observed_{cid}"] = [o.dump() for o in mapper.coordinate_observations("g", m, cid)]
mc = registry.get("cardiometabolic", "g")
for cid in ("bmi", "homa_ir", "fasting_glucose", "weight"):
    RES[f"observed_cardio_{cid}"] = [o.dump() for o in mapper.coordinate_observations("g", mc, cid)]
RES["single_event"] = store.EventStore("g").get(events[0]["id"]).payload.dump()
RES["retracted_ids"] = sorted(store.EventStore("g").retracted_ids())

order = [json.loads(line)["id"] for line in (home / "g" / "events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
ids = {e: f"E{n}" for n, e in enumerate(order)}
text = json.dumps(RES, sort_keys=True, default=str, indent=1)
text = re.sub(r"evt_[0-9a-f]{8,32}", lambda mo: ids.get(mo.group(0), "E?"), text)
doc = json.loads(text)


def scrub(x):
    if isinstance(x, dict):
        return {k: scrub(v) for k, v in x.items() if k not in ("state_hash", "subject_id", "path", "created_at", "last_recorded_at")}
    if isinstance(x, list):
        return [scrub(v) for v in x]
    return x


doc = scrub(doc)
doc["retracted_ids"] = sorted(doc["retracted_ids"], key=lambda s: int(s[1:]))
doc["_log_lines"] = len(order)
if legacy is not None:
    doc = {k: v for k, v in doc.items() if k not in WRITES}
Path(sys.argv[1]).write_text(json.dumps(doc, sort_keys=True, indent=1) + "\n", encoding="utf-8")
