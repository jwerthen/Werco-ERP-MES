"""
Fuzzy Matching Service for Vendors and Parts
Matches extracted PO data to existing database records.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query

logger = logging.getLogger(__name__)

# Try rapidfuzz first (faster), fall back to fuzzywuzzy
try:
    from rapidfuzz import fuzz, process

    FUZZY_LIB = "rapidfuzz"
except ImportError:
    try:
        from fuzzywuzzy import fuzz, process

        FUZZY_LIB = "fuzzywuzzy"
    except ImportError:
        FUZZY_LIB = None
        logger.warning("No fuzzy matching library available (rapidfuzz or fuzzywuzzy)")


class MatchResult:
    def __init__(
        self,
        matched: bool,
        match_id: Optional[int] = None,
        match_name: str = "",
        confidence: float = 0.0,
        suggestions: List[Dict] = None,
    ):
        self.matched = matched
        self.match_id = match_id
        self.match_name = match_name
        self.confidence = confidence
        self.suggestions = suggestions or []

    def to_dict(self) -> Dict:
        return {
            "matched": self.matched,
            "match_id": self.match_id,
            "match_name": self.match_name,
            "confidence": self.confidence,
            "suggestions": self.suggestions,
        }


def match_vendor(vendor_name: str, db: Session, company_id: int, threshold: int = 70) -> MatchResult:
    """
    Match extracted vendor name to existing vendors.
    Returns best match or suggestions if no confident match found.
    """
    from app.models.purchasing import Vendor

    if not vendor_name:
        return MatchResult(matched=False)

    vendor_name = vendor_name.strip().upper()
    normalized_vendor_name = re.sub(r"[^A-Z0-9]", "", vendor_name)

    # First try exact match (case-insensitive).
    # ``is_deleted`` is filtered alongside ``is_active`` -- tenant_query applies company_id
    # only, and is_active was masking removed vendors incidentally (delete_vendor clears it).
    # An exact hit here returns confidence 100.0 and pre-fills the PO-review screen, so a
    # removed supplier matching by name would be presented to the buyer as certain.
    exact = (
        tenant_query(db, Vendor, company_id)
        .filter(
            Vendor.is_active == True,  # noqa: E712
            Vendor.is_deleted == False,  # noqa: E712
            Vendor.name.ilike(vendor_name),
        )
        .first()
    )

    if exact:
        return MatchResult(matched=True, match_id=exact.id, match_name=exact.name, confidence=100.0)

    # Get all live, active vendors for fuzzy matching. Same reasoning as the exact leg, and
    # this one also DISCLOSES: it returns up to 5 suggestions carrying id, name and code.
    vendors = (
        tenant_query(db, Vendor, company_id)
        .filter(
            Vendor.is_active == True,  # noqa: E712
            Vendor.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    if not vendors:
        return MatchResult(matched=False)

    if FUZZY_LIB is None:
        # No fuzzy library, try normalized contains match
        for v in vendors:
            normalized_db_name = re.sub(r"[^A-Z0-9]", "", v.name.upper())
            if normalized_vendor_name in normalized_db_name or normalized_db_name in normalized_vendor_name:
                return MatchResult(matched=True, match_id=v.id, match_name=v.name, confidence=80.0, suggestions=[])
        return MatchResult(
            matched=False, suggestions=[{"id": v.id, "name": v.name, "code": v.code, "score": 0} for v in vendors[:5]]
        )

    # Fuzzy match
    vendor_choices = {v.id: v.name for v in vendors}
    matches = process.extract(vendor_name, vendor_choices, scorer=fuzz.token_sort_ratio, limit=5)

    suggestions = []
    for match in matches:
        vendor_id = match[2]
        vendor = next((v for v in vendors if v.id == vendor_id), None)
        if vendor:
            suggestions.append({"id": vendor.id, "name": vendor.name, "code": vendor.code, "score": match[1]})

    # Check if best match is above threshold
    if matches and matches[0][1] >= threshold:
        best_id = matches[0][2]
        best_vendor = next((v for v in vendors if v.id == best_id), None)
        return MatchResult(
            matched=True,
            match_id=best_id,
            match_name=best_vendor.name if best_vendor else "",
            confidence=matches[0][1],
            suggestions=suggestions,
        )

    # Fallback to contains match when fuzzy score is below threshold
    for vendor in vendors:
        normalized_db_name = re.sub(r"[^A-Z0-9]", "", vendor.name.upper())
        if normalized_vendor_name in normalized_db_name or normalized_db_name in normalized_vendor_name:
            return MatchResult(
                matched=True, match_id=vendor.id, match_name=vendor.name, confidence=80.0, suggestions=suggestions
            )

    return MatchResult(matched=False, suggestions=suggestions)


def match_part(part_number: str, db: Session, company_id: int, threshold: int = 80) -> MatchResult:
    """
    Match extracted part number to existing parts.
    Part numbers require higher confidence threshold.
    """
    from app.models.part import Part

    if not part_number:
        return MatchResult(matched=False)

    part_number = part_number.strip().upper()

    # Remove common prefixes/suffixes that might cause mismatches
    clean_pn = part_number.replace("-", "").replace(" ", "").replace(".", "")

    # First try exact match
    exact = (
        tenant_query(db, Part, company_id)
        .filter(Part.is_active == True, or_(Part.part_number.ilike(part_number), Part.part_number.ilike(clean_pn)))
        .first()
    )

    if exact:
        return MatchResult(matched=True, match_id=exact.id, match_name=exact.part_number, confidence=100.0)

    # Get all active parts for fuzzy matching
    parts = tenant_query(db, Part, company_id).filter(Part.is_active == True).limit(1000).all()

    if not parts:
        return MatchResult(matched=False)

    if FUZZY_LIB is None:
        # Simple contains match
        for p in parts:
            pn_clean = p.part_number.upper().replace("-", "").replace(" ", "").replace(".", "")
            if clean_pn in pn_clean or pn_clean in clean_pn:
                return MatchResult(matched=True, match_id=p.id, match_name=p.part_number, confidence=85.0)
        return MatchResult(
            matched=False,
            suggestions=[{"id": p.id, "part_number": p.part_number, "name": p.name, "score": 0} for p in parts[:5]],
        )

    # Fuzzy match on part numbers
    part_choices = {p.id: p.part_number for p in parts}
    matches = process.extract(
        part_number, part_choices, scorer=fuzz.ratio, limit=5  # Stricter matching for part numbers
    )

    suggestions = []
    for match in matches:
        part_id = match[2]
        part = next((p for p in parts if p.id == part_id), None)
        if part:
            suggestions.append({"id": part.id, "part_number": part.part_number, "name": part.name, "score": match[1]})

    # Check if best match is above threshold
    if matches and matches[0][1] >= threshold:
        best_id = matches[0][2]
        best_part = next((p for p in parts if p.id == best_id), None)
        return MatchResult(
            matched=True,
            match_id=best_id,
            match_name=best_part.part_number if best_part else "",
            confidence=matches[0][1],
            suggestions=suggestions,
        )

    return MatchResult(matched=False, suggestions=suggestions)


def match_part_by_description(description: str, db: Session, company_id: int, threshold: int = 75) -> MatchResult:
    """
    Match line item description to existing parts by name/description.
    """
    from app.models.part import Part

    if not description:
        return MatchResult(matched=False)

    desc = re.sub(r"\s+", " ", description.strip().upper())

    parts = tenant_query(db, Part, company_id).filter(Part.is_active == True).limit(1000).all()
    if not parts:
        return MatchResult(matched=False)

    if FUZZY_LIB is None:
        for p in parts:
            haystack = f"{p.part_number} {p.name} {p.description or ''}".upper()
            if desc in haystack or haystack in desc:
                return MatchResult(matched=True, match_id=p.id, match_name=p.part_number, confidence=80.0)
        return MatchResult(
            matched=False,
            suggestions=[{"id": p.id, "part_number": p.part_number, "name": p.name, "score": 0} for p in parts[:5]],
        )

    part_choices = {p.id: f"{p.part_number} {p.name} {p.description or ''}" for p in parts}
    matches = process.extract(desc, part_choices, scorer=fuzz.token_set_ratio, limit=5)

    suggestions = []
    for match in matches:
        part_id = match[2]
        part = next((p for p in parts if p.id == part_id), None)
        if part:
            suggestions.append({"id": part.id, "part_number": part.part_number, "name": part.name, "score": match[1]})

    if matches and matches[0][1] >= threshold:
        best_id = matches[0][2]
        best_part = next((p for p in parts if p.id == best_id), None)
        return MatchResult(
            matched=True,
            match_id=best_id,
            match_name=best_part.part_number if best_part else "",
            confidence=matches[0][1],
            suggestions=suggestions,
        )

    return MatchResult(matched=False, suggestions=suggestions)


# Review-time typeahead (GET /po-upload/search-parts). SQL token-AND is the
# fast path; a fuzzy fill covers typos and similar wording the LIKE prefilter
# missed. Caps keep a keystroke off a full table scan.
TYPEAHEAD_SQL_CANDIDATE_CAP = 200
TYPEAHEAD_FUZZY_POOL_CAP = 1000
TYPEAHEAD_FUZZY_MIN_SCORE = 70.0


def _escape_like(term: str) -> str:
    """Backslash-escape SQL LIKE wildcards so user input cannot widen the match."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _part_typeahead_haystack(part: Any) -> str:
    return " ".join(
        piece
        for piece in (
            part.part_number,
            part.name,
            part.description or "",
            part.customer_part_number or "",
        )
        if piece
    )


def _typeahead_similarity(query: str, haystack: str) -> float:
    if FUZZY_LIB:
        return float(fuzz.token_set_ratio(query, haystack))
    q = query.upper()
    h = haystack.upper()
    if q == h:
        return 100.0
    if q in h:
        return 90.0
    tokens = [t for t in q.split() if t]
    if tokens and all(t in h for t in tokens):
        return 80.0
    return 0.0


def _typeahead_rank_key(query: str, part: Any) -> tuple:
    """Exact part-number, exact name, then phrase-in-name, then fuzzy score."""
    q = query.strip().upper()
    name = (part.name or "").strip().upper()
    pn = (part.part_number or "").strip().upper()
    score = _typeahead_similarity(query.strip(), _part_typeahead_haystack(part))
    return (
        0 if q == pn else 1,
        0 if q == name else 1,
        0 if q and q in name else 1,
        -score,
        pn,
    )


def _serialize_typeahead_part(part: Any, score: float) -> Dict[str, Any]:
    return {
        "id": part.id,
        "part_number": part.part_number,
        "name": part.name,
        "description": part.description,
        "score": round(float(score), 1),
    }


def search_parts_for_typeahead(query: str, db: Session, company_id: int, limit: int = 10) -> List[Dict[str, Any]]:
    """
    Ranked part typeahead for PO-upload review.

    Every whitespace-separated term must appear in part number, name,
    description, or customer part number (same rule as the frontend ComboBox).
    Remaining slots are filled from a fuzzy similar-word pass so a query like
    "raw material" surfaces "A36 Raw Material Sheet" ahead of unrelated hits.
    """
    from app.models.part import Part

    q = (query or "").strip()
    if not q:
        return []

    tokens = [t for t in re.split(r"\s+", q) if t]
    if not tokens:
        return []

    # Exact number/name first so a catalog hit cannot be dropped by the
    # unordered SQL candidate cap (a tenant with >200 "sheet" parts would
    # otherwise never see the part whose number or name is the query).
    q_lower = q.lower()
    exact_hits = (
        tenant_query(db, Part, company_id)
        .filter(
            Part.is_active == True,
            Part.is_deleted == False,
            or_(func.lower(Part.part_number) == q_lower, func.lower(Part.name) == q_lower),
        )
        .all()
    )
    ranked_parts = list(exact_hits)
    seen_ids = {p.id for p in ranked_parts}

    # Two independent queries: Query.filter() mutates in place in SA 1.4, so
    # chaining tokens onto a shared `active` object would also constrain the
    # fuzzy pool (and `.limit()` on one would leak onto the other).
    sql_query = tenant_query(db, Part, company_id).filter(Part.is_active == True, Part.is_deleted == False)
    for token in tokens:
        like = f"%{_escape_like(token)}%"
        sql_query = sql_query.filter(
            or_(
                Part.part_number.ilike(like, escape="\\"),
                Part.name.ilike(like, escape="\\"),
                Part.description.ilike(like, escape="\\"),
                Part.customer_part_number.ilike(like, escape="\\"),
            )
        )
    if seen_ids:
        sql_query = sql_query.filter(~Part.id.in_(seen_ids))
    sql_hits = sql_query.limit(TYPEAHEAD_SQL_CANDIDATE_CAP).all()
    ranked_parts.extend(sql_hits)
    seen_ids.update(p.id for p in sql_hits)

    if len(ranked_parts) < limit and FUZZY_LIB:
        pool = (
            tenant_query(db, Part, company_id)
            .filter(Part.is_active == True, Part.is_deleted == False)
            .limit(TYPEAHEAD_FUZZY_POOL_CAP)
            .all()
        )
        choices = {p.id: _part_typeahead_haystack(p) for p in pool if p.id not in seen_ids}
        if choices:
            extras = process.extract(q, choices, scorer=fuzz.token_set_ratio, limit=limit)
            by_id = {p.id: p for p in pool}
            for match in extras:
                score = match[1]
                if score < TYPEAHEAD_FUZZY_MIN_SCORE:
                    continue
                part = by_id.get(match[2])
                if part is not None and part.id not in seen_ids:
                    ranked_parts.append(part)
                    seen_ids.add(part.id)

    ranked_parts.sort(key=lambda p: _typeahead_rank_key(q, p))
    out: List[Dict[str, Any]] = []
    for part in ranked_parts[:limit]:
        score = _typeahead_similarity(q, _part_typeahead_haystack(part))
        out.append(_serialize_typeahead_part(part, score))
    return out


def match_po_line_items(line_items: List[Dict[str, Any]], db: Session, company_id: int) -> List[Dict[str, Any]]:
    """
    Match all line items to existing parts.
    Returns line items with match info added.
    """
    enhanced_items = []

    for item in line_items:
        part_number = item.get("part_number", "")
        description = item.get("description", "")
        match_result = match_part(part_number, db, company_id)
        if not match_result.matched and description:
            match_result = match_part_by_description(description, db, company_id)

        enhanced_item = {
            **item,
            "part_match": match_result.to_dict(),
            "matched_part_id": match_result.match_id if match_result.matched else None,
        }
        enhanced_items.append(enhanced_item)

    return enhanced_items


def suggest_part_type(description: str, uom: str = "") -> str:
    """
    Heuristic classification of line items into part types.
    Returns: purchased, raw_material, hardware, consumable.
    """
    text = f"{description or ''} {uom or ''}".upper()

    hardware_keywords = [
        "BOLT",
        "SCREW",
        "NUT",
        "WASHER",
        "FASTENER",
        "RIVET",
        "PIN",
        "CLIP",
        "HINGE",
        "SPRING",
        "BRACKET",
        "STUD",
        "ANCHOR",
        "INSERT",
    ]
    if any(k in text for k in hardware_keywords):
        return "hardware"

    raw_material_keywords = [
        "SHEET",
        "PLATE",
        "BAR",
        "ROD",
        "TUBE",
        "PIPE",
        "ANGLE",
        "CHANNEL",
        "BEAM",
        "FLAT",
        "ROUND",
        "A36",
        "1018",
        "4140",
        "6061",
        "ALUMINUM",
        "STEEL",
        "STAINLESS",
        "BRASS",
        "COPPER",
        "THK",
        "GA",
        "GAUGE",
        "FT",
        "IN",
        "LB/FT",
    ]
    if any(k in text for k in raw_material_keywords):
        return "raw_material"

    consumable_keywords = [
        "TAPE",
        "GLUE",
        "ADHESIVE",
        "SEALANT",
        "SILICONE",
        "LOCTITE",
        "OIL",
        "GREASE",
        "COOLANT",
        "ABRASIVE",
        "SANDPAPER",
        "DISC",
        "WHEEL",
        "WIRE",
        "GAS",
        "PAINT",
        "PRIMER",
        "SOLVENT",
        "CLEANER",
        "RAGS",
        "WIPES",
    ]
    if any(k in text for k in consumable_keywords):
        return "consumable"

    return "purchased"


def check_po_number_exists(po_number: str, db: Session, company_id: int) -> bool:
    """Check if PO number already exists in database."""
    from app.models.purchasing import PurchaseOrder

    if not po_number:
        return False

    existing = tenant_query(db, PurchaseOrder, company_id).filter(PurchaseOrder.po_number == po_number.strip()).first()

    return existing is not None
