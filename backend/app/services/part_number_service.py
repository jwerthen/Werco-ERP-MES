"""
Werco part number generation for raw material and hardware.
"""
import re
import zlib
from typing import Optional, Tuple


def normalize_description(description: str) -> str:
    text = (description or "").upper()
    text = re.sub(r"[\,;:\(\)\[\]]", " ", text)
    text = text.replace("\"", " IN ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _parse_fraction(token: str) -> Optional[float]:
    token = token.strip()
    if not token:
        return None
    if "-" in token and "/" in token:
        whole, frac = token.split("-", 1)
        try:
            return float(whole) + _parse_fraction(frac)
        except Exception:
            return None
    if "/" in token:
        try:
            num, den = token.split("/", 1)
            return float(num) / float(den)
        except Exception:
            return None
    try:
        return float(token)
    except Exception:
        return None


def _format_dim(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _hash_suffix(text: str, length: int = 3) -> str:
    crc = zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out = []
    while crc > 0:
        crc, rem = divmod(crc, 36)
        out.append(alphabet[rem])
    if not out:
        out = ["0"]
    code = "".join(reversed(out))
    if len(code) < length:
        code = code.rjust(length, "0")
    return code[:length]


def _detect_unit(desc: str) -> str:
    if "MM" in desc or "METRIC" in desc:
        return "MM"
    if "IN" in desc or "\"" in desc or re.search(r"\b\d+\s*/\s*\d+\b", desc) or "FT" in desc:
        return "IN"
    return ""


def _find_grade(desc: str) -> str:
    if "6061-T6" in desc or "6061 T6" in desc:
        return "6061T6"
    if "5052-H32" in desc or "5052 H32" in desc:
        return "5052H32"
    er_match = re.search(r"\bER\s*([0-9A-Z\-]+)\b", desc)
    if er_match:
        return f"ER{er_match.group(1)}".replace(" ", "")
    grade_map = ["A36", "1018", "4140", "304", "304L", "316", "316L", "6061", "5052", "7075", "17-4PH", "AR400", "AR500", "G2", "G5", "G8", "A2", "A4"]
    for g in grade_map:
        if g in desc:
            return g
    m = re.search(r"\bGR\s*([0-9]+)\b", desc)
    if m:
        return f"G{m.group(1)}"
    m = re.search(r"\bGRADE\s*([0-9]+)\b", desc)
    if m:
        return f"G{m.group(1)}"
    return ""


def _is_sheet_or_plate(desc: str) -> bool:
    return "SHEET" in desc or "PLATE" in desc


def _find_raw_category(desc: str) -> str:
    if _is_sheet_or_plate(desc):
        return "SM"
    if "FLAT" in desc and "BAR" in desc:
        return "FB"
    if "BAR" in desc:
        return "FB"
    if "ROUND" in desc or "ROD" in desc:
        return "RB"
    if "TUBE" in desc:
        return "TB"
    if "ANGLE" in desc:
        return "ANG"
    if "CHANNEL" in desc:
        return "CHN"
    return "MAT"


def _find_hardware_class(desc: str) -> str:
    if any(k in desc for k in ["BOLT", "SCREW", "NUT", "WASHER", "PIN", "RIVET", "FASTENER"]):
        return "FST"
    return "HDW"


def _find_hardware_type(desc: str) -> str:
    if "SOCKET HEAD CAP" in desc:
        return "SHCS"
    if "HEX HEAD" in desc:
        return "HHCS"
    if "BUTTON HEAD" in desc:
        return "BHCS"
    if "BOLT" in desc:
        return "BOLT"
    if "SCREW" in desc:
        return "SCREW"
    if "NUT" in desc:
        return "NUT"
    if "WASHER" in desc:
        return "WSHR"
    if "PIN" in desc:
        return "PIN"
    if "RIVET" in desc:
        return "RIV"
    return "HDW"


def _find_finish(desc: str) -> str:
    if "BLACK OX" in desc or "BLACKOX" in desc or "BLACK OXIDE" in desc:
        return "BLACKOX"
    if "ZINC" in desc or "ZN" in desc:
        return "ZINC"
    if "GALV" in desc:
        return "GALV"
    if "PHOS" in desc:
        return "PHOS"
    return ""


def _extract_dims(desc: str) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    # thickness, width, length, diameter
    thk = None
    dia = None
    m = re.search(r"(?:THK|THICK|THICKNESS)\s*([0-9./-]+)", desc)
    if m:
        thk = _parse_fraction(m.group(1))
    m = re.search(r"([0-9./-]+)\s*(?:THK|THICK)", desc)
    if m and thk is None:
        thk = _parse_fraction(m.group(1))

    if "DIA" in desc or "DIAM" in desc or "OD" in desc:
        m = re.search(r"(?:DIA|DIAM|OD)\s*([0-9./-]+)", desc)
        if m:
            dia = _parse_fraction(m.group(1))

    # X-separated dims
    m = re.search(r"([0-9./-]+)\s*(?:IN|MM|\")?\s*[Xx]\s*([0-9./-]+)\s*(?:IN|MM|\")?(?:\s*[Xx]\s*([0-9./-]+)\s*(?:IN|MM|\")?)?", desc)
    w = l = None
    if m:
        n1 = _parse_fraction(m.group(1))
        n2 = _parse_fraction(m.group(2))
        n3 = _parse_fraction(m.group(3)) if m.group(3) else None
        if thk is None and n3 is not None:
            thk, w, l = n1, n2, n3
        elif n2 is not None:
            w, l = n1, n2
    return thk, w, l, dia


def generate_werco_part_number(description: str, part_type: str, max_length: int = 30) -> Optional[str]:
    if not description or not part_type:
        return None
    desc = normalize_description(description)
    unit = _detect_unit(desc)

    if part_type not in ["raw_material", "hardware", "consumable"]:
        return None

    if part_type == "raw_material":
        category = _find_raw_category(desc)
        grade = _find_grade(desc)
        thk, w, l, dia = _extract_dims(desc)
        parts = []

        if category == "SM":
            if thk is None or w is None:
                return None
            form = "SHT" if thk <= 0.250 else "PL"
            size = f"{_format_dim(thk)}x{_format_dim(w)}"
            if l is not None:
                size = f"{size}x{_format_dim(l)}"
            parts = ["SM", grade, form, size]
        elif category == "FB":
            if thk is None or w is None:
                return None
            size = f"{_format_dim(thk)}x{_format_dim(w)}"
            if l is not None:
                size = f"{size}x{_format_dim(l)}"
            parts = ["FB", grade, size]
        elif category == "RB":
            if dia is None and w is not None:
                dia = w
            if dia is None:
                return None
            size = _format_dim(dia)
            if l is not None:
                size = f"{size}x{_format_dim(l)}"
            parts = ["RB", grade, size]
        elif category == "TB":
            # OD x WALL from first two dims
            od = w
            wall = thk if thk is not None else l
            if od is None or wall is None:
                return None
            size = f"{_format_dim(od)}x{_format_dim(wall)}"
            parts = ["TB", grade, size]
        elif category in ["ANG", "CHN"]:
            if w is None or l is None:
                return None
            size = f"{_format_dim(w)}x{_format_dim(l)}"
            parts = [category, grade, size]
        else:
            if thk is None or w is None:
                return None
            size = f"{_format_dim(thk)}x{_format_dim(w)}"
            parts = [category, grade, size]

        if unit == "MM":
            parts.append("MM")
        base = "-".join([p for p in parts if p])
    elif part_type == "hardware":
        hw_class = _find_hardware_class(desc)
        hw_type = _find_hardware_type(desc)
        material = _find_grade(desc)
        finish = _find_finish(desc)
        size_str = None

        m = re.search(r"\bM(\d+(?:\.\d+)?)\s*[Xx]\s*(\d+(?:\.\d+)?)\s*(?:[Xx]\s*(\d+(?:\.\d+)?))?\b", desc)
        if m:
            size = _format_dim(float(m.group(1)))
            pitch = _format_dim(float(m.group(2)))
            length = _format_dim(float(m.group(3))) if m.group(3) else None
            size_str = f"M{size}x{pitch}"
            if length:
                size_str = f"{size_str}x{length}"
        else:
            m = re.search(r"([0-9./-]+)\s*-\s*(\d+)\s*(?:[Xx]\s*([0-9./-]+))?", desc)
            if m:
                size_val = _parse_fraction(m.group(1))
                size = _format_dim(size_val)
                pitch = m.group(2)
                length = _format_dim(_parse_fraction(m.group(3))) if m.group(3) else None
                size_str = f"{size}-{pitch}"
                if length:
                    size_str = f"{size_str}x{length}"

        parts = [hw_class, hw_type, size_str, material, finish]
        base = "-".join([p for p in parts if p])
    else:
        # consumable
        material = _find_grade(desc)
        finish = _find_finish(desc)
        c_type = "CNS"
        if "WIRE" in desc and ("WELD" in desc or "FILLER" in desc or "ER" in desc):
            c_type = "WLD"
        elif "ADH" in desc or "EPOXY" in desc or "LOCTITE" in desc:
            c_type = "ADH"
        elif "PAINT" in desc or "COAT" in desc:
            c_type = "PAINT"
        elif "LUBE" in desc or "OIL" in desc or "GREASE" in desc:
            c_type = "LUBE"

        thk, w, l, dia = _extract_dims(desc)
        size = _format_dim(dia) if dia else ""
        parts = [c_type, material, size, finish]
        base = "-".join([p for p in parts if p])

    base = base.replace(" ", "")
    if len(base) <= max_length:
        return base

    suffix = _hash_suffix(desc, 3)
    trimmed = base[: max_length - 4]
    return f"{trimmed}-{suffix}"
