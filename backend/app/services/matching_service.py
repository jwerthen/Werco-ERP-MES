"""
Fuzzy Matching Service for Vendors and Parts
Matches extracted PO data to existing database records.
"""
import logging
import re
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import or_

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
        suggestions: List[Dict] = None
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
            "suggestions": self.suggestions
        }


def match_vendor(vendor_name: str, db: Session, threshold: int = 70) -> MatchResult:
    """
    Match extracted vendor name to existing vendors.
    Returns best match or suggestions if no confident match found.
    """
    from app.models.purchasing import Vendor
    
    if not vendor_name:
        return MatchResult(matched=False)
    
    vendor_name = vendor_name.strip().upper()
    normalized_vendor_name = re.sub(r"[^A-Z0-9]", "", vendor_name)
    
    # First try exact match (case-insensitive)
    exact = db.query(Vendor).filter(
        Vendor.is_active == True,
        Vendor.name.ilike(vendor_name)
    ).first()
    
    if exact:
        return MatchResult(
            matched=True,
            match_id=exact.id,
            match_name=exact.name,
            confidence=100.0
        )
    
    # Get all active vendors for fuzzy matching
    vendors = db.query(Vendor).filter(Vendor.is_active == True).all()
    
    if not vendors:
        return MatchResult(matched=False)
    
    if FUZZY_LIB is None:
        # No fuzzy library, try normalized contains match
        for v in vendors:
            normalized_db_name = re.sub(r"[^A-Z0-9]", "", v.name.upper())
            if normalized_vendor_name in normalized_db_name or normalized_db_name in normalized_vendor_name:
                return MatchResult(
                    matched=True,
                    match_id=v.id,
                    match_name=v.name,
                    confidence=80.0,
                    suggestions=[]
                )
        return MatchResult(matched=False, suggestions=[
            {"id": v.id, "name": v.name, "code": v.code, "score": 0}
            for v in vendors[:5]
        ])
    
    # Fuzzy match
    vendor_choices = {v.id: v.name for v in vendors}
    matches = process.extract(
        vendor_name, 
        vendor_choices, 
        scorer=fuzz.token_sort_ratio,
        limit=5
    )
    
    suggestions = []
    for match in matches:
        vendor_id = match[2]
        vendor = next((v for v in vendors if v.id == vendor_id), None)
        if vendor:
            suggestions.append({
                "id": vendor.id,
                "name": vendor.name,
                "code": vendor.code,
                "score": match[1]
            })
    
    # Check if best match is above threshold
    if matches and matches[0][1] >= threshold:
        best_id = matches[0][2]
        best_vendor = next((v for v in vendors if v.id == best_id), None)
        return MatchResult(
            matched=True,
            match_id=best_id,
            match_name=best_vendor.name if best_vendor else "",
            confidence=matches[0][1],
            suggestions=suggestions
        )

    # Fallback to contains match when fuzzy score is below threshold
    for vendor in vendors:
        normalized_db_name = re.sub(r"[^A-Z0-9]", "", vendor.name.upper())
        if normalized_vendor_name in normalized_db_name or normalized_db_name in normalized_vendor_name:
            return MatchResult(
                matched=True,
                match_id=vendor.id,
                match_name=vendor.name,
                confidence=80.0,
                suggestions=suggestions
            )
    
    return MatchResult(
        matched=False,
        suggestions=suggestions
    )


def match_part(part_number: str, db: Session, threshold: int = 80) -> MatchResult:
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
    exact = db.query(Part).filter(
        Part.is_active == True,
        or_(
            Part.part_number.ilike(part_number),
            Part.part_number.ilike(clean_pn)
        )
    ).first()
    
    if exact:
        return MatchResult(
            matched=True,
            match_id=exact.id,
            match_name=exact.part_number,
            confidence=100.0
        )
    
    # Get all active parts for fuzzy matching
    parts = db.query(Part).filter(Part.is_active == True).limit(1000).all()
    
    if not parts:
        return MatchResult(matched=False)
    
    if FUZZY_LIB is None:
        # Simple contains match
        for p in parts:
            pn_clean = p.part_number.upper().replace("-", "").replace(" ", "").replace(".", "")
            if clean_pn in pn_clean or pn_clean in clean_pn:
                return MatchResult(
                    matched=True,
                    match_id=p.id,
                    match_name=p.part_number,
                    confidence=85.0
                )
        return MatchResult(matched=False, suggestions=[
            {"id": p.id, "part_number": p.part_number, "name": p.name, "score": 0}
            for p in parts[:5]
        ])
    
    # Fuzzy match on part numbers
    part_choices = {p.id: p.part_number for p in parts}
    matches = process.extract(
        part_number,
        part_choices,
        scorer=fuzz.ratio,  # Stricter matching for part numbers
        limit=5
    )
    
    suggestions = []
    for match in matches:
        part_id = match[2]
        part = next((p for p in parts if p.id == part_id), None)
        if part:
            suggestions.append({
                "id": part.id,
                "part_number": part.part_number,
                "name": part.name,
                "score": match[1]
            })
    
    # Check if best match is above threshold
    if matches and matches[0][1] >= threshold:
        best_id = matches[0][2]
        best_part = next((p for p in parts if p.id == best_id), None)
        return MatchResult(
            matched=True,
            match_id=best_id,
            match_name=best_part.part_number if best_part else "",
            confidence=matches[0][1],
            suggestions=suggestions
        )
    
    return MatchResult(
        matched=False,
        suggestions=suggestions
    )


def match_po_line_items(
    line_items: List[Dict[str, Any]], 
    db: Session
) -> List[Dict[str, Any]]:
    """
    Match all line items to existing parts.
    Returns line items with match info added.
    """
    enhanced_items = []
    
    for item in line_items:
        part_number = item.get("part_number", "")
        match_result = match_part(part_number, db)
        
        enhanced_item = {
            **item,
            "part_match": match_result.to_dict(),
            "matched_part_id": match_result.match_id if match_result.matched else None
        }
        enhanced_items.append(enhanced_item)
    
    return enhanced_items


def check_po_number_exists(po_number: str, db: Session) -> bool:
    """Check if PO number already exists in database."""
    from app.models.purchasing import PurchaseOrder
    
    if not po_number:
        return False
    
    existing = db.query(PurchaseOrder).filter(
        PurchaseOrder.po_number == po_number.strip()
    ).first()
    
    return existing is not None
