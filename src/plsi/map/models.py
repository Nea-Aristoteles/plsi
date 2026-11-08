"""Map models: the State Map contract and the state document it produces."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from plsi.files import hash_json
from plsi.models import Code, EventId, Id, Model, PrefixedHash, SourceRef, UtcTime
from plsi.vocab import (
    ClaimLevel,
    ClinicalStatus,
    Comparator,
    CoordinateKind,
    CorrespondenceType,
    DeriveEngine,
    Ethnicity,
    EvidenceClass,
    MatchKind,
    ObservationKind,
    Sex,
    TimeBasis,
    ValueStatus,
)

SERIES_CAP = 20  # most recent active values kept in CoordinateValue.series
UNMAPPED_CAP = 1000  # unmapped observation ids listed in a StateDocument; the rest are only counted


class Accepts(Model):
    codes: list[Code] = []
    reject_codes: list[Code] = []
    requires_fasting: bool = False


class Conversion(Model):
    from_unit: str
    factor: float = Field(gt=0)
    source: SourceRef


class Range(Model):
    low: float | None = None  # inclusive
    high: float | None = None  # exclusive
    sex: Sex | None = None  # F or M
    ethnicity: list[Ethnicity] = []
    source: SourceRef

    @model_validator(mode="after")
    def _bounds(self) -> Range:
        if self.sex not in (None, Sex.F, Sex.M):
            raise ValueError("a range's sex is F or M")
        if self.low is None and self.high is None:
            raise ValueError("a range needs low or high")
        if self.low is not None and self.high is not None and self.low >= self.high:
            raise ValueError("low must be below high")
        return self


class Derive(Model):
    engine: DeriveEngine
    model: str = Field(min_length=1)  # native/biolearn model, extension id, or the score's name for expr
    inputs: list[Id] = []
    formula: str | None = None  # engine expr only, e.g. "fasting_glucose * fasting_insulin / 405"
    carry_forward: list[Id] = []  # inputs whose latest value on or before the day is used (adult height)

    @model_validator(mode="after")
    def _formula(self) -> Derive:
        if (self.engine == DeriveEngine.EXPR) != (self.formula is not None):
            raise ValueError("formula is required for engine expr and forbidden otherwise")
        if self.formula is not None:
            from plsi.map import formula

            names = formula.variables(self.formula) - formula.CONTEXT
            if names != set(self.inputs):
                raise ValueError(f"inputs must be exactly the formula's variables {sorted(names)} (age, female, male are implicit)")
        if self.carry_forward and not set(self.carry_forward) < set(self.inputs):
            raise ValueError("carry_forward must name some, not all, of the inputs")
        return self


class ScoreWindow(Model):
    before: int = Field(default=14, ge=0)
    after: int = Field(default=28, ge=0)


class MapCoordinate(Model):
    id: Id
    label: str
    kind: CoordinateKind
    unit: str | None = None  # canonical UCUM; None for set and artifact coordinates
    collects: ObservationKind | None = None  # set coordinates: the observation kind whose active items they hold
    accepts: Accepts = Accepts()
    conversions: list[Conversion] = []
    derive: Derive | None = None
    recency_days: int = Field(default=365, ge=1)
    predictable: bool = True
    score_window_days: ScoreWindow = ScoreWindow()
    ranges: list[Range] | None = None  # None, not [], so maps without ranges keep their hash
    correspondence_type: CorrespondenceType
    sources: list[SourceRef] = []

    @model_validator(mode="after")
    def _structure(self) -> MapCoordinate:
        # measured may opt into derive (hybrid: accepts + derive); derived still requires derive; set forbids it.
        if self.kind == CoordinateKind.DERIVED and self.derive is None:
            raise ValueError("derive is required for derived coordinates")
        if self.derive is not None and self.kind not in (CoordinateKind.DERIVED, CoordinateKind.MEASURED):
            raise ValueError("derive is only allowed on measured or derived coordinates")
        if (self.kind == CoordinateKind.SET) != (self.collects is not None):
            raise ValueError("collects is required for set coordinates and forbidden otherwise")
        both = {(c.system, c.code) for c in self.accepts.codes} & {(c.system, c.code) for c in self.accepts.reject_codes}
        if both:
            raise ValueError(f"codes both accepted and rejected: {sorted(c for _, c in both)}")
        return self

    def best_ranges(self, sex: Sex, ethnicity: Ethnicity | None) -> list[Range]:
        """The ranges that fit the subject with the most specific match (sex, ethnicity); more than one is a tie."""
        fits = [r for r in self.ranges or [] if r.sex in (None, sex) and (not r.ethnicity or ethnicity in r.ethnicity)]
        top = max(((r.sex is not None) + bool(r.ethnicity) for r in fits), default=0)
        return [r for r in fits if (r.sex is not None) + bool(r.ethnicity) == top]


class ClockExpansion(Model):
    """Adds one derived coordinate per biolearn aging clock (plsi.map.biolearn.clocks()), fed by `input`."""

    engine: Literal[DeriveEngine.BIOLEARN] = DeriveEngine.BIOLEARN
    input: Id


class InferenceBoundaries(Model):
    supports: list[str] = []
    does_not_support: list[str] = []
    min_evidence_class_for_actions: EvidenceClass


class MapContract(Model):
    """A State Map. User maps (instance maps/user/) must have an id starting `user_` and claim_level designed;
    that rule depends on where the file lives and is enforced by plsi.map.registry."""

    kind: Literal["plsi_map"] = "plsi_map"
    schema_version: Literal[1] = 1
    id: Id
    title: str
    map_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    claim_level: ClaimLevel
    summary: str
    tags: list[str] = []
    coordinates: list[MapCoordinate] = Field(min_length=1)
    inference_boundaries: InferenceBoundaries
    expand: ClockExpansion | None = None
    sources: list[SourceRef] = []

    @model_validator(mode="after")
    def _graph(self) -> MapContract:
        ids = [c.id for c in self.coordinates]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate coordinate ids")
        if self.expand and self.expand.input not in ids:
            raise ValueError(f"expand.input {self.expand.input!r} is not a coordinate")
        deps = {c.id: (c.derive.inputs if c.derive else []) for c in self.coordinates}
        for cid, inputs in deps.items():
            if missing := [i for i in inputs if i not in deps]:
                raise ValueError(f"{cid}: unknown derive inputs {missing}")
        state: dict[str, int] = {}

        def visit(node: str) -> None:
            if state.get(node) == 1:
                raise ValueError(f"derive cycle through {node}")
            if state.get(node) != 2:
                state[node] = 1
                for dep in deps[node]:
                    visit(dep)
                state[node] = 2

        for cid in deps:
            visit(cid)
        return self

    def coordinate(self, coordinate_id: str) -> MapCoordinate | None:
        return next((c for c in self.coordinates if c.id == coordinate_id), None)

    def compute_hash(self) -> str:
        return hash_json(self.dump())


class Provenance(Model):
    match: MatchKind
    rule: str
    conversion: dict[str, Any] | None = None  # {from_unit, factor}
    derivation: dict[str, Any] | None = None  # {engine, model, computed_at, usage_note?, censored_inputs?}


class SetItem(Model):
    """One active item of a set coordinate (care_context)."""

    text: str
    code: Code | None = None
    dose: str | None = None
    dose_quantity: float | None = None
    dose_unit: str | None = None
    clinical_status: ClinicalStatus | None = None
    event_id: EventId


class SeriesPoint(Model):
    t: UtcTime
    value: float
    comparator: Comparator | None = None
    event_ids: list[EventId] = []


class CoordinateValue(Model):
    value: float | list[SetItem] | None = None
    unit: str | None = None
    comparator: Comparator | None = None
    observed_at: UtcTime | None = None
    event_ids: list[EventId] = []
    status: ValueStatus
    note: str | None = None
    provenance: Provenance | None = None
    series: list[SeriesPoint] = Field(default=[], max_length=SERIES_CAP)  # active values oldest-first


class Gap(Model):
    coordinate: Id
    reason: str
    suggestion: str


class StateDocument(Model):
    kind: Literal["plsi_state"] = "plsi_state"
    schema_version: Literal[1] = 1
    subject_id: str
    map_id: Id
    map_version: str
    map_hash: PrefixedHash
    as_of: UtcTime
    generated_at: UtcTime
    coordinates: dict[str, CoordinateValue]
    gaps: list[Gap] = []
    unmapped_observations: list[EventId] = Field(default=[], max_length=UNMAPPED_CAP)  # first UNMAPPED_CAP, log order
    unmapped_count: int | None = None  # all unmapped observations, only when that is more than listed
    state_hash: str = ""  # set by sealed(); computed over the document minus generated_at and state_hash

    def compute_hash(self) -> str:
        return hash_json(self.model_dump(mode="json", exclude_none=True, exclude={"generated_at", "state_hash"}))

    def sealed(self) -> StateDocument:
        return self.model_copy(update={"state_hash": self.compute_hash()})

    def verify_hash(self) -> bool:
        return self.state_hash == self.compute_hash()


class ObservedValue(Model):
    """One candidate value of a coordinate for scoring (plsi.map.mapper.coordinate_observations)."""

    event_ids: list[EventId]
    effective_at: UtcTime
    recorded_at: UtcTime
    effective_basis: TimeBasis
    value: CoordinateValue


class CareContext(Model):
    """mapper.context(): Policy's only view of meds, conditions and allergies."""

    medications: list[SetItem] = []
    conditions: list[SetItem] = []
    allergies: list[SetItem] = []


class Estimate(Model):
    """A calculator input with its interval (plsi calc course, survival); a bare number is the expected value."""

    expected: float
    low: float | None = None
    high: float | None = None

    @classmethod
    def of(cls, v: float | Self) -> Self:
        return v if isinstance(v, cls) else cls(expected=v)
