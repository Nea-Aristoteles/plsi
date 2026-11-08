"""Modality importers -> Imported (observations, rejections, warnings).

No network calls. No plsi.map / plsi.policy imports.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pydantic

from plsi.life.models import ORIGINAL_TEXT_MAX, ObservationInput
from plsi.life.deid import contains_identifier, patient_identifiers, patient_subject_fields
from plsi.clock import now
from plsi.errors import DependencyMissing, ValidationError
from plsi.files import atomic_write_bytes, sha256_hex
from plsi.models import violations_from
from plsi.vocab import ClinicalStatus, CodeSystem, Comparator, ObservationKind, OriginKind, TimeBasis

CSV_COLUMNS = ("effective_at", "text", "value", "unit", "code_system", "code", "specimen", "kind", "fasting", "dose_quantity", "dose_unit")
CSV_REQUIRED = CSV_COLUMNS[:4]
FHIR_SYSTEMS = {
    "http://loinc.org": CodeSystem.LOINC,
    "http://www.nlm.nih.gov/research/umls/rxnorm": CodeSystem.RXNORM,
    "http://snomed.info/sct": CodeSystem.SNOMED,
    "http://hl7.org/fhir/sid/icd-10-cm": CodeSystem.ICD10CM,
}
UCUM_SYSTEM = "http://unitsofmeasure.org"
_STATUS = {  # FHIR status / clinicalStatus codes -> ClinicalStatus
    "active": ClinicalStatus.ACTIVE, "recurrence": ClinicalStatus.ACTIVE, "relapse": ClinicalStatus.ACTIVE,
    "completed": ClinicalStatus.STOPPED, "stopped": ClinicalStatus.STOPPED, "cancelled": ClinicalStatus.STOPPED,
    "inactive": ClinicalStatus.RESOLVED, "remission": ClinicalStatus.RESOLVED, "resolved": ClinicalStatus.RESOLVED,
}
_KINDS = {
    "MedicationStatement": ObservationKind.MEDICATION, "MedicationRequest": ObservationKind.MEDICATION,
    "Condition": ObservationKind.CONDITION, "AllergyIntolerance": ObservationKind.ALLERGY,
}
_NUMBER_RE = re.compile(r"(<=|>=|<|>)?\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)")
METHYLATION_HINT = 'pip install -e ".[clocks]"'


@dataclass
class Imported:
    """Result of one importer run, consumed by plsi.life.ingest.ingest."""

    observations: list[ObservationInput] = field(default_factory=list)
    indexes: list[int] = field(default_factory=list)  # source index (entry, row, list item) of each observation
    rejected: list[dict[str, Any]] = field(default_factory=list)  # [{index, error}] (error = code: message)
    warnings: list[str] = field(default_factory=list)
    dropped_columns: list[str] = field(default_factory=list)  # CSV only; names only, never values
    subject: dict[str, Any] = field(default_factory=dict)  # FHIR Patient -> {birth_year?, sex?}

    @classmethod
    def from_items(cls, items: list[Any]) -> Imported:
        out = cls()
        for i in range(len(items)):
            out.add(i, items[i])
            items[i] = None  # each row is released as soon as it is validated: a big file is never held twice
        return out

    def add(self, index: int, data: dict[str, Any]) -> None:
        try:
            self.observations.append(ObservationInput.model_validate(data))
            self.indexes.append(index)
        except pydantic.ValidationError as exc:
            v = violations_from(exc)[0]
            where = "" if v["path"] == "$" else f"{v['path']}: "
            self.rejected.append({"index": index, "error": f"VALIDATION_ERROR: {where}{v['message']}"})


def observation_items(data: Any) -> list[Any]:
    """The items of a JSON ObservationInput list, bare or wrapped as {"observations": [...]}."""
    items = data.get("observations") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValidationError("expected a JSON list of ObservationInput (or {\"observations\": [...]})")
    return items


# ---------------------------------------------------------------- FHIR

def _code(concept: dict[str, Any] | None) -> tuple[dict[str, str] | None, str | None]:
    """CodeableConcept -> (first mappable coding as Code dict, label)."""
    concept = concept or {}
    code, label = None, concept.get("text")
    for coding in concept.get("coding") or []:
        label = label or coding.get("display")
        system = FHIR_SYSTEMS.get(coding.get("system", ""))
        if code is None and system and coding.get("code"):
            code = {"system": system.value, "code": str(coding["code"]), **({"display": coding["display"]} if coding.get("display") else {})}
    return code, label or (code or {}).get("code")


def _effective(res: dict[str, Any], *prefixes: str) -> tuple[str | None, str | None]:
    for p in prefixes:
        if res.get(f"{p}DateTime") or res.get(f"{p}Instant"):
            return res.get(f"{p}DateTime") or res.get(f"{p}Instant"), None
        if period := res.get(f"{p}Period"):
            return period.get("start"), period.get("end")
    for key in ("issued", "authoredOn", "recordedDate", "dateAsserted"):
        if res.get(key):
            return res[key], None
    return None, None


def _value(item: dict[str, Any]) -> dict[str, Any] | None:
    """value[x] -> ObservationInput value fields, or None when absent/unsupported."""
    if (q := item.get("valueQuantity")) is not None:
        unit = q.get("code") if q.get("system") == UCUM_SYSTEM and q.get("code") else q.get("unit") or q.get("code")
        out: dict[str, Any] = {"value_quantity": q.get("value"), "unit": unit}
        if q.get("comparator") in {c.value for c in Comparator}:
            out["value_comparator"] = q["comparator"]
        return out
    if "valueInteger" in item:
        return {"value_quantity": item["valueInteger"]}
    if "valueBoolean" in item:
        return {"value_bool": item["valueBoolean"]}
    if "valueString" in item:
        return {"value_text": item["valueString"]}
    if (cc := item.get("valueCodeableConcept")) is not None:
        return {"value_text": _code(cc)[1]}
    return None


def _dose(dosage: dict[str, Any]) -> dict[str, Any]:
    """dosage[0].doseAndRate[0].doseQuantity -> {dose_quantity, dose_unit} (UCUM code, else the printed unit), or {}."""
    q = ((dosage.get("doseAndRate") or [{}])[0] or {}).get("doseQuantity") or {}
    value, unit = q.get("value"), q.get("code") if q.get("system") == UCUM_SYSTEM and q.get("code") else q.get("unit")
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 and isinstance(unit, str) and unit.strip()
    return {"dose_quantity": value, "dose_unit": unit.strip()} if ok else {}


def _status(res: dict[str, Any]) -> ClinicalStatus:
    cs = res.get("clinicalStatus")
    raw = (cs.get("coding") or [{}])[0].get("code") if isinstance(cs, dict) else res.get("status")
    return _STATUS.get(str(raw or "").lower(), ClinicalStatus.UNKNOWN)


def _fhir_items(res: dict[str, Any]) -> list[dict[str, Any]]:
    """Allowlisted fields of one resource -> partial ObservationInput dicts (no origin)."""
    rtype = res.get("resourceType")
    if rtype == "Observation":
        start, end = _effective(res, "effective")
        base = {"kind": ObservationKind.MEASUREMENT.value, "effective_at": start, "effective_end": end}
        parts = [(res.get("code"), res)] if _value(res) is not None or not res.get("component") else []
        parts += [(c.get("code"), c) for c in res.get("component") or []]
        out = []
        for concept, holder in parts:
            code, label = _code(concept)
            out.append({**base, "code": code, "original_text": label, **(_value(holder) or {"__error": "NO_VALUE: no supported value[x]"})})
        return out
    kind = _KINDS[rtype]
    if kind == ObservationKind.MEDICATION:
        code, label = _code(res.get("medicationCodeableConcept"))
        dosage = (res.get("dosage") or res.get("dosageInstruction") or [{}])[0]
        start, end = _effective(res, "effective")
        extra = {"dose": dosage.get("text"), **_dose(dosage)}
    else:
        code, label = _code(res.get("code"))
        start, end = _effective(res, "onset")
        end = end or res.get("abatementDateTime")
        extra = {}
    # Medications, conditions and allergies are presence assertions: value_bool true.
    return [{"kind": kind.value, "code": code, "original_text": label, "value_bool": True, "effective_at": start,
             "effective_end": end, "clinical_status": _status(res).value, **extra}]


def from_fhir(bundle: dict[str, Any], file_sha256: str) -> Imported:
    """FHIR R4 Bundle (or single resource) -> observations, applying the deid allowlist in plsi.life.deid.

    Observation (component[] unpacked), MedicationStatement/MedicationRequest, Condition, AllergyIntolerance.
    origin = {kind: fhir, file_sha256, record_id: entry index[.component]}. Text containing a Patient identifier
    -> IDENTIFIER_IN_TEXT. Patient fields -> Imported.subject. Other resource types are skipped with a warning.
    """
    if not isinstance(bundle, dict):
        raise ValidationError("FHIR input must be a JSON object (Bundle or resource)")
    if bundle.get("resourceType") == "Bundle":
        resources = [e.get("resource") or {} for e in bundle.get("entry") or []]
    else:
        resources = [bundle]
    out = Imported()
    identifiers: set[str] = set()
    for res in resources:
        if res.get("resourceType") == "Patient":
            identifiers |= patient_identifiers(res)
            out.subject = out.subject or {k: v for k, v in patient_subject_fields(res).items() if v is not None}
    skipped: dict[str, int] = {}
    for index, res in enumerate(resources):
        rtype = res.get("resourceType", "?")
        if rtype == "Patient":
            continue
        if rtype != "Observation" and rtype not in _KINDS:
            skipped[rtype] = skipped.get(rtype, 0) + 1
            continue
        items = _fhir_items(res)
        for j, item in enumerate(items):
            item = {k: v for k, v in item.items() if v is not None}
            if (text := item.get("original_text")) and len(text) > ORIGINAL_TEXT_MAX:
                item["original_text"] = text[:ORIGINAL_TEXT_MAX]
            texts = [str(item.get(k)) for k in ("original_text", "value_text", "dose", "dose_unit", "unit") if item.get(k)]
            if error := item.pop("__error", None):
                out.rejected.append({"index": index, "error": error})
            elif any(contains_identifier(t, identifiers) for t in texts):
                out.rejected.append({"index": index, "error": "IDENTIFIER_IN_TEXT: text contains a patient identifier"})
            elif not item.get("effective_at"):
                out.rejected.append({"index": index, "error": "MISSING_DATE: no effective[x], onset[x] or recorded date"})
            else:
                record_id = f"{index}.{j}" if len(items) > 1 else str(index)
                out.add(index, {**item, "origin": {"kind": OriginKind.FHIR.value, "file_sha256": file_sha256, "record_id": record_id}})
    out.warnings += [f"SKIPPED_RESOURCE: {t} x{n}" for t, n in sorted(skipped.items())]
    return out


# ---------------------------------------------------------------- CSV

def from_csv(text: str, file_sha256: str) -> Imported:
    """Canonical long CSV, header required: CSV_COLUMNS, the first four (CSV_REQUIRED) present.

    A numeric value (optionally prefixed by <, <=, >, >=) -> value_quantity (+ value_comparator); otherwise value_text.
    kind defaults to measurement. Other columns -> dropped_columns (names only). origin record_id = data row index.
    """
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in reader.fieldnames or []]
    if missing := [c for c in CSV_REQUIRED if c not in header]:
        raise ValidationError(f"CSV header is missing columns {missing}", code="CSV_HEADER", hint="header: " + ",".join(CSV_COLUMNS))
    reader.fieldnames = header
    out = Imported(dropped_columns=[h for h in header if h not in CSV_COLUMNS])
    for i, row in enumerate(reader):
        cell = {k: (row.get(k) or "").strip() for k in CSV_COLUMNS}
        system, code = cell["code_system"].lower(), cell["code"]
        if bool(system) != bool(code):
            out.rejected.append({"index": i, "error": "CODE_INCOMPLETE: code_system and code go together"})
            continue
        if m := _NUMBER_RE.fullmatch(cell["value"]):
            value: dict[str, Any] = {"value_quantity": float(m[2]), "value_comparator": m[1]}
        else:
            value = {"value_text": cell["value"] or None}
        fasting = {"true": True, "yes": True, "1": True, "false": False, "no": False, "0": False}.get(cell["fasting"].lower())
        data = {
            "kind": cell["kind"] or ObservationKind.MEASUREMENT.value, "original_text": cell["text"],
            "code": {"system": system, "code": code} if code else None, "unit": cell["unit"] or None,
            "effective_at": cell["effective_at"], "specimen": cell["specimen"] or None, "fasting": fasting,
            "dose_quantity": cell["dose_quantity"] or None, "dose_unit": cell["dose_unit"] or None,
            "origin": {"kind": OriginKind.CSV.value, "file_sha256": file_sha256, "record_id": str(i)}, **value,
        }
        out.add(i, {k: v for k, v in data.items() if v is not None})
    return out


# ---------------------------------------------------------------- methylation

def from_methylation(path: Path, instance_dir: Path, sample: str | None, file_sha256: str, effective_at: datetime | None) -> Imported:
    """Methylation beta matrix CSV (first column CpG id, then one column per sample). Requires pandas (clocks extra).

    Streams the file; several sample columns require `sample`. Drops rs* and ch.* probes; sample names are not stored.
    Writes artifacts/methylation/<sha256>.csv.gz (columns: cpg,beta) and returns ONE artifact observation whose
    value_text is the artifact sha256. Without effective_at the time is now() with basis assumed (never scores).
    """
    try:
        import pandas as pd
    except ImportError:
        raise DependencyMissing("methylation import needs pandas from the clocks extra", hint=METHYLATION_HINT) from None
    columns = list(pd.read_csv(path, nrows=0).columns)
    samples = columns[1:]
    if not samples:
        raise ValidationError("methylation file needs a CpG id column and at least one sample column", code="METHYLATION_FORMAT")
    if sample is None and len(samples) > 1:
        raise ValidationError(f"the file has {len(samples)} sample columns", code="SAMPLE_REQUIRED", hint="pass --sample COL")
    column = sample if sample is not None else samples[0]
    if column not in samples:
        raise ValidationError("--sample does not name a sample column", code="SAMPLE_NOT_FOUND")
    buf, kept, dropped = io.StringIO(), 0, 0
    buf.write("cpg,beta\n")
    try:
        for chunk in pd.read_csv(path, usecols=[columns[0], column], index_col=0, dtype={column: "float64"}, chunksize=200_000):
            betas = chunk[column].dropna()
            probe = betas.index.astype(str)
            keep = ~(probe.str.startswith("rs") | probe.str.startswith("ch."))
            dropped += int((~keep).sum())
            betas = betas[keep]
            if ((betas < 0) | (betas > 1)).any():
                raise ValidationError("beta values must lie in [0, 1] (M-values are not supported)", code="METHYLATION_FORMAT")
            betas.to_csv(buf, header=False, lineterminator="\n")
            kept += len(betas)
    except ValueError as exc:
        raise ValidationError(f"unreadable methylation matrix: {exc}", code="METHYLATION_FORMAT") from None
    if not kept:
        raise ValidationError("no CpG beta values left after dropping rs*/ch.* probes", code="METHYLATION_FORMAT")
    data = gzip.compress(buf.getvalue().encode("utf-8"), mtime=0)
    artifact = sha256_hex(data)
    atomic_write_bytes(instance_dir / "artifacts" / "methylation" / f"{artifact}.csv.gz", data)
    out = Imported(warnings=[f"DROPPED_PROBES: {dropped}"] if dropped else [])
    out.add(0, {
        "kind": ObservationKind.ARTIFACT.value, "code": {"system": CodeSystem.CPG.value, "code": "methylation_betas"},
        "original_text": "DNA methylation beta values", "value_text": artifact,
        "effective_at": effective_at or now(),
        "effective_basis": (TimeBasis.USER_STATED if effective_at else TimeBasis.ASSUMED).value,
        "origin": {"kind": OriginKind.METHYLATION.value, "file_sha256": file_sha256},
    })
    return out
