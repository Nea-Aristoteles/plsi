"""The deidentified package for the Longevity Mothership, built from published states, recorded life events and
prediction scores, gate-checked (plsi.mothership.gate) into the instance's outbox; plsi.mothership.send sends it.
Coded fields only (WIRE §2): set items and life events go as their code, never their text, and a free or cpg code
only when a shipped or installed map declares it, never an ethnicity, race or occupation code (plsi.codes.NEVER_SHARED:
the subject carries the ethnicity group, sex and a five-year age band); a unit only when it is a map's canonical unit or a form of the
unit table (plsi.life.normalize), else the value stays too. A structured dose goes as {value, unit} only when its
unit is in DOSE_UNITS or a map's canonical unit; the dose as written never leaves.

No age leaves (consent: an age band only). A coordinate computed from chronological age stays on this computer: every
coordinate plsi.map.derive.age_based finds in the state's map (a derive given the age, any value in years such as a
clock's age or age acceleration, and anything derived from those), plus LOCAL_COORDINATES, which give the age back
with another coordinate.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from plsi import codes, files
from plsi.clock import now, ts_filename
from plsi.life.store import EventStore
from plsi.life.normalize import known_unit
from plsi.map import derive, mapper, registry
from plsi.map.models import CoordinateValue
from plsi.models import Code
from plsi.mothership import gate
from plsi.mothership.enroll import active, folder
from plsi.mothership.relay import SCHEMA_VERSION, address
from plsi.policy.score import calibration
from plsi.vocab import ContributionKind, EventType, MothershipMessage, ObservationKind

LIFE_KINDS = frozenset({ObservationKind.CONDITION, ObservationKind.INTERVENTION, ObservationKind.LIFESTYLE,
                        ObservationKind.SYMPTOM, ObservationKind.MEDICATION, ObservationKind.ALLERGY})
LOCAL_COORDINATES = frozenset({"months_since_final_period"})  # with age_at_menopause it gives the age
MIN_SCORES = 5
# UCUM symbols a dose may carry on the wire (annotations for counted forms)
DOSE_UNITS = frozenset({"mg", "g", "ug", "ng", "mL", "L", "uL", "[iU]", "[IU]", "U", "mmol", "umol", "meq", "%",
                        "{tablet}", "{capsule}", "{puff}", "{drop}", "{spray}", "{patch}", "{dose}"})


def _month(t: datetime | None) -> str | None:
    return t.strftime("%Y-%m") if t else None


def _monthly(series: list[Any]) -> list[dict[str, Any]]:
    """One point per month: its mean and `n`, how many recorded values the mean covers."""
    by: dict[str, list[float]] = {}
    for p in series:
        by.setdefault(str(_month(p.t)), []).append(p.value)
    return [{"t": m, "value": float(f"{fmean(vals):.6g}"), "n": len(vals)} for m, vals in sorted(by.items())]


class _Wire:
    """What may leave for this instance: the declared free and cpg codes and the known units."""

    def __init__(self, instance: str) -> None:
        self.declared = registry.declared_codes(instance)
        self.units = registry.canonical_units(instance)

    def code(self, code: Code | None) -> dict[str, str] | None:
        """{system, code} of a code that may leave (plsi.codes.shareable); the display never leaves."""
        return {"system": code.system.value, "code": code.code} if code and codes.shareable(
            code.system.value, code.code, self.declared) else None

    def unit(self, unit: str | None) -> bool:
        return unit is None or unit in self.units or known_unit(unit)

    def dose(self, quantity: float | None, unit: str | None) -> dict[str, Any]:
        """{"dose": {value, unit}} of a structured dose whose unit may leave, else {}."""
        ok = quantity is not None and unit is not None and (unit in DOSE_UNITS or unit in self.units)
        return {"dose": {"value": quantity, "unit": unit}} if ok else {}

    def coordinate(self, v: CoordinateValue) -> dict[str, Any]:
        out: dict[str, Any] = {"status": v.status.value}
        if isinstance(v.value, list):
            out["items"] = [{"code": code} | ({"clinical_status": i.clinical_status.value} if i.clinical_status else {})
                            | self.dose(i.dose_quantity, i.dose_unit) for i in v.value if (code := self.code(i.code))]
        else:
            out |= {k: val for k, val in {"value": v.value, "unit": v.unit,
                                          "comparator": v.comparator.value if v.comparator else None,
                                          "observed": _month(v.observed_at)}.items() if val is not None}
        if v.series:
            out["series"] = _monthly(v.series)
            month = [p["value"] for p in out["series"] if p["t"] == out.get("observed")]
            if "value" in out and v.comparator is None and month:  # the headline value is that month's mean too
                out["value"] = month[0]
        return out

    def states(self, store: EventStore) -> list[dict[str, Any]]:
        out = []
        for doc in mapper.latest_states(store.instance):
            if doc.map_id in mapper.LOCAL_MAPS:
                continue
            local = LOCAL_COORDINATES | derive.age_based(registry.load_snapshot(store.instance, doc.map_id, doc.map_hash).coordinates)
            out.append({"map_id": doc.map_id, "map_version": doc.map_version, "map_hash": doc.map_hash.removeprefix("sha256:"),
                        "as_of": _month(doc.as_of), "gaps": sorted(g.coordinate for g in doc.gaps),
                        "coordinates": {c: self.coordinate(v) for c, v in doc.coordinates.items()
                                        if v.value is not None and c not in local
                                        and (isinstance(v.value, list) or self.unit(v.unit))}})
        return out

    def life_events(self, store: EventStore) -> list[dict[str, Any]]:
        out = []
        for e in store.iter_events(EventType.OBSERVATION, kinds=LIFE_KINDS):
            o = e.payload
            if code := self.code(o.code):
                measured = o.value_quantity is not None and self.unit(o.unit)
                item = {"kind": o.kind.value, "year": o.effective_at.year,
                        "until": o.effective_end.year if o.effective_end else None, "basis": o.effective_basis.value,
                        "code": code, "value": o.value_quantity if measured else None, "unit": o.unit if measured else None,
                        "clinical_status": o.clinical_status.value if o.clinical_status else None}
                out.append({k: v for k, v in item.items() if v is not None} | self.dose(o.dose_quantity, o.dose_unit))
        return sorted(out, key=lambda i: (i["year"], i["kind"], i["code"]["system"], i["code"]["code"]))


def _scores(store: EventStore) -> dict[str, Any]:
    cal = calibration(store.instance)
    cells = {c: s for c, s in cal["by_coordinate"].items() if s["n_scored"] >= MIN_SCORES}
    return {"overall": cal["overall"], "by_coordinate": cells} if cal["overall"]["n_scored"] >= MIN_SCORES else {}


def _age_band(birth_year: int | None, age_group: str | None) -> str | None:
    if age_group:
        return age_group
    if birth_year is None:
        return None
    low = (now().year - birth_year) // 5 * 5
    return f"{low}-{low + 4}"


def pack(instance: str, out: str | None = None) -> dict[str, Any]:
    """`mothership pack`: the deidentified package of the consented kinds, gate-checked; nothing is sent."""
    record = active(instance)
    store = EventStore(instance)
    kinds = set(record["kinds"])
    doc: dict[str, Any] = {"kind": MothershipMessage.CONTRIBUTION, "schema_version": SCHEMA_VERSION,
                           "contributor_id": record["contributor_id"], "consent_version": record["consent_version"],
                           "packed": _month(now()), "kinds": sorted(kinds),
                           "subject": {k: v for k, v in {"sex": store.subject.sex.value,
                                                         "age_band": _age_band(store.subject.birth_year, store.subject.age_group),
                                                         "ethnicity": store.subject.ethnicity}.items() if v}}
    wire = _Wire(instance)
    build = {ContributionKind.STATES: wire.states, ContributionKind.LIFE_EVENTS: wire.life_events,
             ContributionKind.SCORES: _scores}
    doc |= {kind.value: section(store) for kind, section in build.items() if kind in kinds}
    gate.require(doc, instance, what="package")
    path = Path(out) if out else folder(instance) / "outbox" / f"contribution-{ts_filename()}.json"
    files.atomic_write_json(path, doc)
    counts = {"states": len(doc.get("states", [])), "life_events": len(doc.get("life_events", [])),
              "scored_predictions": doc.get("scores", {}).get("overall", {}).get("n_scored", 0)}
    return {"file": str(path), "to": address(),
            "package_hash": files.hash_json(doc), "counts": counts}

