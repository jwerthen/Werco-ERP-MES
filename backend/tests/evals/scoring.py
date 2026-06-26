"""Deterministic scorers for extraction evals.

Scores compare an extraction result (stored golden output in offline mode, a
fresh model response in live mode) against hand-checked ground truth. All
scores are 0.0-1.0.
"""

from typing import Any, Dict, List, Optional, Tuple


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, float) and value == int(value):
        return int(value)
    return value


def _field_matches(expected: Any, actual: Any) -> bool:
    return _normalize_scalar(expected) == _normalize_scalar(actual)


def score_scalar_fields(expected: Dict[str, Any], actual: Dict[str, Any], fields: List[str]) -> float:
    """Fraction of the listed scalar fields extracted exactly (case/space-insensitive)."""
    if not fields:
        return 1.0
    hits = sum(1 for field in fields if _field_matches(expected.get(field), (actual or {}).get(field)))
    return hits / len(fields)


def _index_items(items: List[Dict[str, Any]], key: str) -> Dict[Any, Dict[str, Any]]:
    indexed = {}
    for item in items or []:
        item_key = _normalize_scalar(item.get(key))
        if item_key is not None and item_key not in indexed:
            indexed[item_key] = item
    return indexed


def score_line_items(
    expected_items: List[Dict[str, Any]],
    actual_items: List[Dict[str, Any]],
    *,
    key: str = "part_number",
    compare_fields: Tuple[str, ...] = (),
) -> Dict[str, float]:
    """Recall/precision of line items matched by ``key``, plus field accuracy
    across the matched pairs."""
    expected_index = _index_items(expected_items, key)
    actual_index = _index_items(actual_items, key)

    if not expected_index:
        return {"recall": 1.0, "precision": 1.0 if not actual_index else 0.0, "field_accuracy": 1.0}

    matched_keys = set(expected_index) & set(actual_index)
    recall = len(matched_keys) / len(expected_index)
    precision = len(matched_keys) / len(actual_index) if actual_index else 0.0

    if not matched_keys or not compare_fields:
        field_accuracy = 1.0 if matched_keys else 0.0
    else:
        comparisons = 0
        hits = 0
        for item_key in matched_keys:
            for field in compare_fields:
                comparisons += 1
                if _field_matches(expected_index[item_key].get(field), actual_index[item_key].get(field)):
                    hits += 1
        field_accuracy = hits / comparisons if comparisons else 1.0

    return {"recall": recall, "precision": precision, "field_accuracy": field_accuracy}


def score_po_extraction(expected: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, float]:
    scalar_fields = ["document_type", "po_number", "quote_number", "total_amount"]
    scores = {
        "header_accuracy": score_scalar_fields(expected, actual, scalar_fields),
        "vendor_match": (
            1.0
            if _field_matches(
                (expected.get("vendor") or {}).get("name"), ((actual or {}).get("vendor") or {}).get("name")
            )
            else 0.0
        ),
    }
    line_scores = score_line_items(
        expected.get("line_items") or [],
        (actual or {}).get("line_items") or [],
        key="part_number",
        compare_fields=("qty_ordered", "unit_price"),
    )
    scores["line_item_recall"] = line_scores["recall"]
    scores["line_item_precision"] = line_scores["precision"]
    scores["line_item_field_accuracy"] = line_scores["field_accuracy"]
    return scores


def score_bom_extraction(expected: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, float]:
    expected_assembly = expected.get("assembly") or {}
    actual_assembly = (actual or {}).get("assembly") or {}
    scores = {
        "document_type_match": (
            1.0 if _field_matches(expected.get("document_type"), (actual or {}).get("document_type")) else 0.0
        ),
        "assembly_accuracy": score_scalar_fields(expected_assembly, actual_assembly, ["part_number", "revision"]),
    }
    item_scores = score_line_items(
        expected.get("items") or [],
        (actual or {}).get("items") or [],
        key="part_number",
        compare_fields=("quantity", "item_type"),
    )
    scores["item_recall"] = item_scores["recall"]
    scores["item_precision"] = item_scores["precision"]
    scores["item_field_accuracy"] = item_scores["field_accuracy"]
    return scores


def score_laser_nest_extraction(expected: Dict[str, Any], actual: Dict[str, Any]) -> Dict[str, float]:
    """Per-field accuracy for the laser-nest extraction (the metadata fields callers
    depend on). Each field scores 1.0/0.0 independently and ``field_accuracy`` is
    the mean over the four primary fields, so a single missed field is visible.
    """
    fields = ["cnc_number", "material", "thickness", "sheet_size"]
    scores: Dict[str, float] = {
        f"{field}_match": (1.0 if _field_matches(expected.get(field), (actual or {}).get(field)) else 0.0)
        for field in fields
    }
    scores["field_accuracy"] = score_scalar_fields(expected, actual, fields)
    return scores


def assert_thresholds(scores: Dict[str, float], thresholds: Dict[str, float], case_id: Optional[str] = None) -> None:
    """Raise AssertionError listing every score below its threshold."""
    failures = [
        f"{name}: {scores.get(name, 0.0):.3f} < {minimum:.3f}"
        for name, minimum in thresholds.items()
        if scores.get(name, 0.0) < minimum
    ]
    assert not failures, f"Eval case {case_id or '?'} below thresholds: {'; '.join(failures)} (scores={scores})"
