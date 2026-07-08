"""Pure majority-vote + anomaly rules for Estimate Workbench PDF assist (Phase 4).

No SQLAlchemy / FastAPI / LLM imports — pass extraction pass dicts in, get
confidence + winning values out. Unit-testable without network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ConfidenceLevel string values (keep this module free of SQLAlchemy models)
CONFIRMED = "confirmed"
MAJORITY = "majority"
REVIEW = "review"

# Fields compared across fab extraction passes
FAB_VOTE_FIELDS: Tuple[str, ...] = (
    "material",
    "thickness_in",
    "width_in",
    "length_in",
    "cut_length_in",
    "pierce_count",
    "bend_count",
    "weld_length_in",
    "qty",
)

BUYOUT_VOTE_FIELDS: Tuple[str, ...] = (
    "part_number",
    "description",
    "qty",
    "unit_cost",
    "category",
    "vendor",
)

# Known material substrings — anything else is an anomaly → Review
_KNOWN_MATERIAL_TOKENS = (
    "steel",
    "stain",
    "alum",
    "a36",
    "1018",
    "1020",
    "1045",
    "304",
    "316",
    "5052",
    "6061",
    "crs",
    "hrs",
    "mild",
    "carbon",
    "copper",
    "brass",
    "bronze",
)

# Canonical mild-steel gauge decimals from Excel defaults (for anomaly check)
_CANONICAL_GAUGES_MILD = (
    0.0239,
    0.0299,
    0.0359,
    0.0478,
    0.0598,
    0.0747,
    0.075,  # shop-canonical 14 ga
    0.1046,
    0.1196,
    0.1345,
    0.1793,
    0.1875,
    0.250,
    0.313,
    0.375,
    0.500,
    0.625,
    0.750,
    1.000,
)


@dataclass
class FieldVote:
    field: str
    value: Any
    agreement: int  # 0–3 (or N for N passes)
    total_passes: int
    confidence: str
    values_seen: List[Any] = field(default_factory=list)
    anomaly_reasons: List[str] = field(default_factory=list)


@dataclass
class LineVoteResult:
    """One fab or buyout line after majority vote across passes."""

    line_type: str  # fab | buyout
    values: Dict[str, Any]
    field_votes: Dict[str, FieldVote]
    confidence: str
    verification_note: Optional[str] = None
    pass_count: int = 0


def _norm_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _norm_num(value: Any, *, ndigits: int = 4) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if not (n == n):  # NaN
        return None
    return round(n, ndigits)


def _norm_int(value: Any) -> Optional[int]:
    n = _norm_num(value, ndigits=0)
    if n is None:
        return None
    return int(round(n))


def normalize_field_value(field: str, value: Any) -> Any:
    """Canonicalize a field so near-equal extractions vote together."""
    if field in {"material", "description", "category", "vendor", "part_number", "detail_name"}:
        text = _norm_str(value)
        if text is None:
            return None
        if field == "material":
            return " ".join(text.lower().split())
        return text
    if field in {"thickness_in", "width_in", "length_in", "cut_length_in", "weld_length_in", "unit_cost"}:
        return _norm_num(value, ndigits=4)
    if field in {"pierce_count", "bend_count", "qty"}:
        # qty on buyout may be float; keep int when whole
        n = _norm_num(value, ndigits=4)
        if n is None:
            return None
        if abs(n - round(n)) < 1e-9:
            return int(round(n))
        return n
    return value


def confidence_from_agreement(agreement: int, total: int) -> str:
    """Map vote tally to Confirmed / Majority / Review."""
    if total <= 0 or agreement <= 0:
        return REVIEW
    if agreement >= 3 or (total >= 3 and agreement == total):
        return CONFIRMED
    if agreement >= 2:
        return MAJORITY
    return REVIEW


def material_is_known(material: Optional[str]) -> bool:
    if not material:
        return False
    lower = material.lower()
    return any(token in lower for token in _KNOWN_MATERIAL_TOKENS)


def thickness_is_nonstandard(thickness_in: Optional[float], *, tol: float = 0.03) -> bool:
    """True when thickness is far from any common gauge / plate band."""
    if thickness_in is None or thickness_in <= 0:
        return True
    for canonical in _CANONICAL_GAUGES_MILD:
        if abs(thickness_in - canonical) / canonical <= tol:
            return False
    # Allow exact plate sizes already in the list; anything else is suspicious
    return True


def detect_field_anomalies(field: str, value: Any) -> List[str]:
    reasons: List[str] = []
    if field == "material":
        if value is None:
            reasons.append("missing material")
        elif not material_is_known(str(value)):
            reasons.append(f"unknown material '{value}'")
    if field == "thickness_in":
        if value is None:
            reasons.append("missing thickness")
        elif thickness_is_nonstandard(float(value)):
            reasons.append(f"non-standard thickness {value}\"")
    if field == "bend_count" and value is not None:
        try:
            if int(value) < 0:
                reasons.append("negative bend count")
            elif int(value) > 80:
                reasons.append(f"unusually high bend count ({value})")
        except (TypeError, ValueError):
            reasons.append("invalid bend count")
    return reasons


def vote_field(field: str, pass_values: Sequence[Any]) -> FieldVote:
    """Majority vote one field across independent extraction passes."""
    normalized = [normalize_field_value(field, v) for v in pass_values]
    present = [v for v in normalized if v is not None]
    total = len(pass_values)

    if not present:
        return FieldVote(
            field=field,
            value=None,
            agreement=0,
            total_passes=total,
            confidence=REVIEW,
            values_seen=[],
            anomaly_reasons=["no value extracted in any pass"],
        )

    counts: Dict[Any, int] = {}
    for v in present:
        counts[v] = counts.get(v, 0) + 1
    winner, agreement = max(counts.items(), key=lambda kv: (kv[1], str(kv[0])))
    conf = confidence_from_agreement(agreement, total)
    anomalies = detect_field_anomalies(field, winner)
    if anomalies:
        conf = REVIEW

    return FieldVote(
        field=field,
        value=winner,
        agreement=agreement,
        total_passes=total,
        confidence=conf,
        values_seen=list(counts.keys()),
        anomaly_reasons=anomalies,
    )


def _line_confidence(field_votes: Dict[str, FieldVote], critical: Iterable[str]) -> Tuple[str, Optional[str]]:
    """Roll field votes up to a line-level confidence + note."""
    notes: List[str] = []
    worst = CONFIRMED
    rank = {CONFIRMED: 3, MAJORITY: 2, REVIEW: 1}

    for name in critical:
        fv = field_votes.get(name)
        if not fv:
            worst = REVIEW
            notes.append(f"{name}: missing vote")
            continue
        if rank[fv.confidence] < rank[worst]:
            worst = fv.confidence
        if fv.anomaly_reasons:
            notes.extend(f"{name}: {r}" for r in fv.anomaly_reasons)
        elif fv.confidence != CONFIRMED:
            notes.append(
                f"{name}: {fv.agreement}/{fv.total_passes} agree"
                + (f" (saw {fv.values_seen})" if len(fv.values_seen) > 1 else "")
            )

    # Any Review among critical fields forces Review
    for name in critical:
        fv = field_votes.get(name)
        if fv and fv.confidence == REVIEW:
            worst = REVIEW

    note = "; ".join(notes) if notes else None
    return worst, note


def vote_fab_line(passes: Sequence[Dict[str, Any]]) -> LineVoteResult:
    """Merge N fab-line extraction dicts into one voted line."""
    field_votes: Dict[str, FieldVote] = {}
    values: Dict[str, Any] = {}
    for name in FAB_VOTE_FIELDS:
        fv = vote_field(name, [p.get(name) for p in passes])
        field_votes[name] = fv
        values[name] = fv.value

    # Carry through non-voted identity fields from first non-empty pass
    for key in ("detail_name", "part_number", "source_file", "drawing_number", "revision"):
        for p in passes:
            if p.get(key) not in (None, ""):
                values[key] = p[key]
                break

    # Prefer a human detail name
    if not values.get("detail_name"):
        values["detail_name"] = values.get("part_number") or "Extracted detail"

    critical = ("material", "thickness_in", "bend_count", "qty")
    conf, note = _line_confidence(field_votes, critical)

    # Missing thickness with laser/brake intent → Review
    if values.get("thickness_in") is None:
        conf = REVIEW
        note = (note + "; " if note else "") + "thickness required for laser/brake lookups"

    return LineVoteResult(
        line_type="fab",
        values=values,
        field_votes=field_votes,
        confidence=conf,
        verification_note=note,
        pass_count=len(passes),
    )


def vote_buyout_line(passes: Sequence[Dict[str, Any]]) -> LineVoteResult:
    field_votes: Dict[str, FieldVote] = {}
    values: Dict[str, Any] = {}
    for name in BUYOUT_VOTE_FIELDS:
        fv = vote_field(name, [p.get(name) for p in passes])
        field_votes[name] = fv
        values[name] = fv.value

    for key in ("price_source", "source_file"):
        for p in passes:
            if p.get(key) not in (None, ""):
                values[key] = p[key]
                break

    if not values.get("description"):
        values["description"] = values.get("part_number") or "Buyout item"

    # Buyouts without a real unit cost → Review + placeholder note
    unit = values.get("unit_cost")
    if unit is None or float(unit) <= 0:
        conf = REVIEW
        note = "Placeholder — quote required (no unit cost from drawing)"
        values["unit_cost"] = float(unit or 0)
        values.setdefault("price_source", "Placeholder")
    else:
        critical = ("description", "qty", "unit_cost")
        conf, note = _line_confidence(field_votes, critical)

    return LineVoteResult(
        line_type="buyout",
        values=values,
        field_votes=field_votes,
        confidence=conf,
        verification_note=note,
        pass_count=len(passes),
    )


def align_and_vote_fab_passes(
    pass_lists: Sequence[Sequence[Dict[str, Any]]],
) -> List[LineVoteResult]:
    """Align fab lines across passes by part_number / detail_name, then vote.

    Pass lists are ordered (pass0, pass1, pass2). Lines are matched by a
    normalized key; unmatched lines from any pass still appear (as Review).
    """
    keys_order: List[str] = []
    buckets: Dict[str, List[Optional[Dict[str, Any]]]] = {}
    n = len(pass_lists)

    def key_for(item: Dict[str, Any]) -> str:
        raw = item.get("part_number") or item.get("detail_name") or item.get("drawing_number")
        if raw:
            return "".join(ch for ch in str(raw).lower() if ch.isalnum())
        # Stable fallback by index within pass — handled by caller index
        return ""

    for pass_idx, lines in enumerate(pass_lists):
        used: Dict[str, int] = {}
        for line_idx, item in enumerate(lines):
            base = key_for(item) or f"anon-{line_idx}"
            # Disambiguate duplicate keys within a pass
            count = used.get(base, 0)
            used[base] = count + 1
            k = base if count == 0 else f"{base}#{count}"
            if k not in buckets:
                buckets[k] = [None] * n
                keys_order.append(k)
            buckets[k][pass_idx] = item

    results: List[LineVoteResult] = []
    for k in keys_order:
        slots = buckets[k]
        present = [s for s in slots if s is not None]
        # Pad missing passes with empty dicts so agreement counts stay honest
        padded = [s if s is not None else {} for s in slots]
        voted = vote_fab_line(padded if len(present) == n else padded)
        if len(present) < n:
            # Force Review when a line only appeared in some passes
            voted.confidence = REVIEW
            extra = f"present in {len(present)}/{n} passes"
            voted.verification_note = (
                f"{voted.verification_note}; {extra}" if voted.verification_note else extra
            )
        results.append(voted)
    return results


def align_and_vote_buyout_passes(
    pass_lists: Sequence[Sequence[Dict[str, Any]]],
) -> List[LineVoteResult]:
    keys_order: List[str] = []
    buckets: Dict[str, List[Optional[Dict[str, Any]]]] = {}
    n = len(pass_lists)

    def key_for(item: Dict[str, Any]) -> str:
        raw = item.get("part_number") or item.get("description")
        if raw:
            return "".join(ch for ch in str(raw).lower() if ch.isalnum())
        return ""

    for pass_idx, lines in enumerate(pass_lists):
        used: Dict[str, int] = {}
        for line_idx, item in enumerate(lines):
            base = key_for(item) or f"buy-{line_idx}"
            count = used.get(base, 0)
            used[base] = count + 1
            k = base if count == 0 else f"{base}#{count}"
            if k not in buckets:
                buckets[k] = [None] * n
                keys_order.append(k)
            buckets[k][pass_idx] = item

    results: List[LineVoteResult] = []
    for k in keys_order:
        slots = buckets[k]
        present = [s for s in slots if s is not None]
        padded = [s if s is not None else {} for s in slots]
        voted = vote_buyout_line(padded)
        if len(present) < n:
            voted.confidence = REVIEW
            extra = f"present in {len(present)}/{n} passes"
            voted.verification_note = (
                f"{voted.verification_note}; {extra}" if voted.verification_note else extra
            )
        results.append(voted)
    return results


def line_vote_to_fab_draft(voted: LineVoteResult) -> Dict[str, Any]:
    v = voted.values
    qty = v.get("qty")
    return {
        "detail_name": v.get("detail_name") or "Extracted detail",
        "part_number": v.get("part_number"),
        "material": v.get("material") or "",
        "qty": int(qty or 1),
        "thickness_in": v.get("thickness_in"),
        "width_in": v.get("width_in"),
        "length_in": v.get("length_in"),
        "cut_length_in": v.get("cut_length_in"),
        "pierce_count": int(v.get("pierce_count") or 0),
        "bend_count": int(v.get("bend_count") or 0),
        "weld_length_in": v.get("weld_length_in"),
        "include_material": True,
        "include_laser": True,
        "include_brake": True,
        "include_weld": bool(v.get("weld_length_in")),
        "confidence": voted.confidence,
        "verification_note": voted.verification_note,
        "field_confidence": {
            name: {
                "value": fv.value,
                "agreement": fv.agreement,
                "total_passes": fv.total_passes,
                "confidence": fv.confidence,
                "anomaly_reasons": fv.anomaly_reasons,
            }
            for name, fv in voted.field_votes.items()
        },
    }


def line_vote_to_buyout_draft(voted: LineVoteResult) -> Dict[str, Any]:
    v = voted.values
    return {
        "description": v.get("description") or "Buyout item",
        "part_number": v.get("part_number"),
        "category": v.get("category") or "hardware",
        "vendor": v.get("vendor"),
        "qty": float(v.get("qty") or 1),
        "unit_cost": float(v.get("unit_cost") or 0),
        "price_source": v.get("price_source") or (
            "Placeholder" if voted.confidence == REVIEW else "Drawing extract"
        ),
        "confidence": voted.confidence,
        "verification_note": voted.verification_note,
        "field_confidence": {
            name: {
                "value": fv.value,
                "agreement": fv.agreement,
                "total_passes": fv.total_passes,
                "confidence": fv.confidence,
                "anomaly_reasons": fv.anomaly_reasons,
            }
            for name, fv in voted.field_votes.items()
        },
    }
