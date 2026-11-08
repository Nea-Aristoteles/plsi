"""The identity of an observation: the key that makes a replayed input a duplicate."""

from __future__ import annotations

from plsi.files import canonical_json, sha256_hex
from plsi.life.models import ObservationInput
from plsi.life.normalize import normalize_unit


def dedupe_key(obs: ObservationInput) -> str:
    """sha256 hex of canonical_json({kind, code or lower(original_text), effective_at, value fields, normalized unit,
    specimen, fasting, origin.file_sha256, origin.record_id, clinical_status, effective_end}); ingest recomputes it
    from stored payloads, so a change here applies to existing events too."""
    d = obs.model_dump(mode="json")
    return sha256_hex(canonical_json({
        "kind": d["kind"],
        "code": {"system": d["code"]["system"], "code": d["code"]["code"]} if obs.code else obs.original_text.lower(),
        **{k: d[k] for k in ("effective_at", "value_quantity", "value_text", "value_bool", "value_comparator", "specimen", "fasting")},
        "unit": normalize_unit(obs.unit),
        "file_sha256": obs.origin.file_sha256,
        "record_id": obs.origin.record_id,
        "clinical_status": d.get("clinical_status"),
        "effective_end": d.get("effective_end"),
    }))
