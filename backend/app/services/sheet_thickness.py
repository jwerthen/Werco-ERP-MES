"""Shared sheet stock thickness parsing; no pricing or process defaults."""

import re
from typing import Dict, Optional

GAUGE_TO_INCHES: Dict[str, float] = {
    "24ga": 0.0239,
    "22ga": 0.0299,
    "20ga": 0.0359,
    "18ga": 0.0478,
    "16ga": 0.0598,
    "14ga": 0.0747,
    "12ga": 0.1046,
    "11ga": 0.1196,
    "10ga": 0.1345,
    "7ga": 0.1793,
}


def parse_thickness_to_inches(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().lower().replace(",", "")
    if not text:
        return None

    gauge_match = re.search(r"\b(\d{1,2})\s*(?:ga|gauge)\b", text)
    if gauge_match:
        return GAUGE_TO_INCHES.get(f"{gauge_match.group(1)}ga")

    mixed_fraction_match = re.search(r"\b(\d+)\s+(\d+)\s*/\s*(\d+)\b", text)
    if mixed_fraction_match:
        whole = float(mixed_fraction_match.group(1))
        numerator = float(mixed_fraction_match.group(2))
        denominator = float(mixed_fraction_match.group(3))
        if denominator:
            return whole + (numerator / denominator)

    fraction_match = re.search(r"\b(\d+)\s*/\s*(\d+)\b", text)
    if fraction_match:
        numerator = float(fraction_match.group(1))
        denominator = float(fraction_match.group(2))
        if denominator:
            return numerator / denominator

    mm_match = re.search(r"(\d*\.?\d+)\s*mm\b", text)
    if mm_match:
        return float(mm_match.group(1)) / 25.4

    inch_match = re.search(r"(\d*\.?\d+)\s*(?:in|inch|inches|\")?\b", text)
    if inch_match:
        return float(inch_match.group(1))

    return None
