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
    s = f"{value:.3f}".rstrip("0").rstrip(".")
    s = s.replace(".", "p")
    if s.startswith("0p"):
        s = s[1:]
    return s


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
    grade_map = ["A36", "1018", "4140", "304", "316", "6061", "5052", "7075", "AR400", "AR500", "G2", "G5", "G8", "A2", "A4"]
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


def _find_raw_shape(desc: str) -> str:
    if "PLATE" in desc:
        return "PLT"
    if "SHEET" in desc:
        return "SHT"
    if "TUBE" in desc:
        return "TUB"
    if "ANGLE" in desc:
        return "ANG"
    if "CHANNEL" in desc:
        return "CHN"
    if "FLAT" in desc:
        return "FLT"
    if "BAR" in desc:
        return "BAR"
    if "ROUND" in desc or "ROD" in desc:
        return "ROD"
    return "RM"


def _find_hardware_type(desc: str) -> str:
    if "BOLT" in desc:
        return "BL"
    if "SCREW" in desc:
        return "SC"
    if "NUT" in desc:
        return "NT"
    if "WASHER" in desc:
        return "WS"
    return "HW"


def _find_finish(desc: str) -> str:
    if "ZINC" in desc or "ZN" in desc:
        return "ZN"
    if "GALV" in desc:
        return "GZ"
    if "BLACK" in desc or "BLK" in desc:
        return "BK"
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


def generate_werco_part_number(description: str, part_type: str, max_length: int = 20) -> Optional[str]:
    if not description or not part_type:
        return None
    desc = normalize_description(description)
    unit = _detect_unit(desc)

    if part_type not in ["raw_material", "hardware"]:
        return None

    if part_type == "raw_material":
        shape = _find_raw_shape(desc)
        grade = _find_grade(desc)
        thk, w, l, dia = _extract_dims(desc)
        parts = ["RM", shape]
        if grade:
            parts.append(grade)
        dim_str = ""
        if dia and ("ROD" in desc or "ROUND" in desc):
            dim_str += f"D{_format_dim(dia)}"
        if thk is not None:
            dim_str += f"T{_format_dim(thk)}"
        if w is not None:
            dim_str += f"W{_format_dim(w)}"
        if l is not None:
            dim_str += f"L{_format_dim(l)}"
        if dim_str:
            parts.append(dim_str)
        if unit:
            parts.append(unit)
        base = "".join(parts)
    else:
        hw_type = _find_hardware_type(desc)
        grade = _find_grade(desc)
        finish = _find_finish(desc)
        size = pitch = length = None
        m = re.search(r"\bM(\d+(?:\.\d+)?)\s*[Xx]\s*(\d+(?:\.\d+)?)\b", desc)
        if m:
            size = f"M{_format_dim(float(m.group(1)))}"
            pitch = f"P{_format_dim(float(m.group(2)))}"
        else:
            m = re.search(r"([0-9./-]+)\s*[- ]\s*(\d+)\b", desc)
            if m:
                size_val = _parse_fraction(m.group(1))
                size = f"{_format_dim(size_val)}"
                pitch = f"T{m.group(2)}"
        m = re.search(r"(?:\bL\b|\bLEN\b|\bLENGTH\b)\s*([0-9./-]+)", desc)
        if m:
            length = _format_dim(_parse_fraction(m.group(1)))
        parts = ["HW", hw_type]
        if size:
            parts.append(size)
        if pitch:
            parts.append(pitch)
        if length:
            parts.append(f"L{length}")
        if grade:
            parts.append(grade)
        if finish:
            parts.append(finish)
        base = "".join(parts)

    base = base.replace(" ", "")
    if len(base) <= max_length:
        return base

    suffix = _hash_suffix(desc, 3)
    trimmed = base[: max_length - 4]
    return f"{trimmed}-{suffix}"
