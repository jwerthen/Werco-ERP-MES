"""Bounded, deterministic quote nesting using conservative rectangular envelopes.

This is a feasible material estimate, not an optimal irregular nest or an NC program.
Coordinates and spacing are millimetres. Polygons must already represent reviewed flats.
"""

from __future__ import annotations

import math
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Any

EPS = 1e-7
MAX_INSTANCES = 500
MAX_VERTICES = 2000
MAX_CANDIDATE_TESTS = 50000


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0) or number > 1e7:
        raise ValueError(f"{name} is outside the supported range")
    return number


def _quantity(value: Any, name: str) -> int:
    number = _number(value, name)
    if number != int(number) or number > MAX_INSTANCES:
        raise ValueError(f"{name} must be an integer from 0 to {MAX_INSTANCES}")
    return int(number)


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(p, a, b):
    return abs(_cross(a, b, p)) <= EPS and all(min(a[i], b[i]) - EPS <= p[i] <= max(a[i], b[i]) + EPS for i in (0, 1))


def _segments_intersect(a, b, c, d, *, proper=False):
    x, y, z, w = _cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b)
    if ((x > EPS and y < -EPS) or (x < -EPS and y > EPS)) and ((z > EPS and w < -EPS) or (z < -EPS and w > EPS)):
        return True
    return not proper and any(
        (
            _on_segment(c, a, b),
            _on_segment(d, a, b),
            _on_segment(a, c, d),
            _on_segment(b, c, d),
        )
    )


def _inside(p, poly, *, boundary=True):
    inside = False
    for a, b in zip(poly, poly[1:] + poly[:1]):
        if _on_segment(p, a, b):
            return boundary
        if (a[1] > p[1]) != (b[1] > p[1]) and p[0] < (b[0] - a[0]) * (p[1] - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside


def _polygon(raw, name):
    if not isinstance(raw, (list, tuple)) or not 3 <= len(raw) <= MAX_VERTICES:
        raise ValueError(f"{name} must have 3..{MAX_VERTICES} vertices")
    points = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or any(isinstance(v, bool) for v in p):
            raise ValueError(f"{name} vertices must be coordinate pairs")
        try:
            q = [float(p[0]), float(p[1])]
        except (ValueError, TypeError, OverflowError):
            raise ValueError(f"{name} coordinates must be finite") from None
        if not all(math.isfinite(v) and abs(v) <= 1e7 for v in q):
            raise ValueError(f"{name} coordinates must be finite and bounded")
        points.append(q)
    if points[0] == points[-1]:
        points.pop()
    if len(points) < 3 or len(set(map(tuple, points))) != len(points):
        raise ValueError(f"{name} contains repeated vertices")
    edges = list(zip(points, points[1:] + points[:1]))
    area = abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) / 2
    if area <= EPS:
        raise ValueError(f"{name} has zero area")
    for i, (a, b) in enumerate(edges):
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            if _segments_intersect(a, b, *edges[j]):
                raise ValueError(f"{name} is self-intersecting")
    return points


def _rings(item, name):
    outline = item.get("outline")
    if outline is None:
        width = _number(item.get("width_mm"), f"{name}.width_mm", positive=True)
        height = _number(item.get("height_mm"), f"{name}.height_mm", positive=True)
        outline = [[0, 0], [width, 0], [width, height], [0, height]]
    outer = _polygon(outline, name)
    raw_holes = item.get("holes", [])
    if not isinstance(raw_holes, list) or len(raw_holes) > 100:
        raise ValueError(f"{name}.holes is outside the supported range")
    holes = [_polygon(h, f"{name}.hole") for h in raw_holes]
    outer_edges = list(zip(outer, outer[1:] + outer[:1]))
    for i, hole in enumerate(holes):
        if not all(_inside(p, outer, boundary=False) for p in hole):
            raise ValueError(f"{name} has a hole outside its outline")
        edges = list(zip(hole, hole[1:] + hole[:1]))
        if any(_segments_intersect(a, b, c, d) for a, b in edges for c, d in outer_edges):
            raise ValueError(f"{name} has a hole crossing its outline")
        for other in holes[:i]:
            if (
                _inside(hole[0], other)
                or _inside(other[0], hole)
                or any(_segments_intersect(a, b, c, d) for a, b in edges for c, d in zip(other, other[1:] + other[:1]))
            ):
                raise ValueError(f"{name} holes overlap")
    return outer, holes


def _bounds(poly):
    return [
        min(p[0] for p in poly),
        min(p[1] for p in poly),
        max(p[0] for p in poly),
        max(p[1] for p in poly),
    ]


def _rect(bounds):
    x, y, xx, yy = bounds
    return [[x, y], [xx, y], [xx, yy], [x, yy]]


def _point_distance(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    t = max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _fits_stock(bounds, stock, margin):
    rect = _rect(bounds)
    edges = list(zip(rect, rect[1:] + rect[:1]))
    if not all(_inside(p, stock["outline"]) for p in rect):
        return False
    for ring_index, ring in enumerate([stock["outline"]] + stock["holes"]):
        ring_edges = list(zip(ring, ring[1:] + ring[:1]))
        if any(_segments_intersect(a, b, c, d, proper=True) for a, b in edges for c, d in ring_edges):
            return False
        # A concave notch or hole can meet rectangle corners without a proper crossing.
        if any(_inside([(a[0] + b[0]) / 2, (a[1] + b[1]) / 2], rect, boundary=False) for a, b in ring_edges):
            return False
        if ring_index and (any(_inside(p, ring) for p in rect) or any(_inside(p, rect) for p in ring)):
            return False
        if margin > 0 and any(
            min(
                _point_distance(a, c, d),
                _point_distance(b, c, d),
                _point_distance(c, a, b),
                _point_distance(d, a, b),
            )
            < margin - EPS
            for a, b in edges
            for c, d in ring_edges
        ):
            return False
    return True


def _separated(a, b, spacing):
    return (
        a[2] + spacing <= b[0] + EPS
        or b[2] + spacing <= a[0] + EPS
        or a[3] + spacing <= b[1] + EPS
        or b[3] + spacing <= a[1] + EPS
    )


def nest_parts(payload: dict) -> dict:
    """Return a fully checked conservative nest or explicit unplaced demand.

    Bad requests raise ValueError; solver failure never silently changes constraints.
    Stock quantity is finite; price is per whole sheet and returned as Decimal text.
    """
    if not isinstance(payload, dict):
        raise ValueError("nest request must be an object")
    spacing = _number(payload.get("spacing_mm", 0), "spacing_mm")
    margin = _number(payload.get("edge_margin_mm", 0), "edge_margin_mm")
    parts, stocks = payload.get("parts", []), payload.get("stocks", [])
    if (
        not isinstance(parts, list)
        or not isinstance(stocks, list)
        or len(parts) > MAX_INSTANCES
        or len(stocks) > MAX_INSTANCES
    ):
        raise ValueError("parts and stocks must be bounded lists")
    if any(not isinstance(item, dict) for item in parts + stocks):
        raise ValueError("parts and stocks must contain objects")
    instances, inventory = [], []
    for group, target, label in (
        (parts, instances, "part"),
        (stocks, inventory, "stock"),
    ):
        seen = set()
        for item in group:
            ident = str(item.get("id", ""))
            if not ident or ident in seen:
                raise ValueError(f"{label} IDs must be nonempty and unique")
            seen.add(ident)
            if item.get("units", "mm") != "mm":
                raise ValueError("nest coordinates must be explicitly normalized to mm")
            material = item.get("material")
            if not isinstance(material, str) or not material.strip():
                raise ValueError(f"{label} {ident} requires material identity")
            thickness = _number(item.get("thickness_mm"), "thickness_mm", positive=True)
            outline, holes = _rings(item, f"{label} {ident}")
            qty = _quantity(item.get("quantity", 1), "quantity")
            normalized = dict(
                item,
                id=ident,
                outline=outline,
                holes=holes,
                thickness_mm=thickness,
                bounds=_bounds(outline),
            )
            if label == "part":
                rotations = item.get("allowed_rotations", [0])
                if not isinstance(rotations, list) or not 1 <= len(rotations) <= 36:
                    raise ValueError("allowed_rotations must contain 1..36 angles")
                if item.get("mirror", False):
                    raise ValueError("mirroring is unsupported; supply a reviewed handed flat")
                angles = []
                for angle in rotations:
                    try:
                        angle = float(angle)
                    except (ValueError, TypeError):
                        raise ValueError("rotation must be finite") from None
                    if not math.isfinite(angle):
                        raise ValueError("rotation must be finite")
                    angles.append(angle % 360)
                normalized["allowed_rotations"] = sorted(set(angles))
            if label == "stock" and item.get("price") is not None:
                try:
                    price = Decimal(str(item["price"]))
                except InvalidOperation:
                    raise ValueError("stock price must be a decimal") from None
                if not price.is_finite() or price < 0 or not item.get("currency"):
                    raise ValueError("stock price requires a nonnegative value and currency")
                normalized["price"] = str(price)
            target.extend(dict(normalized, instance=i + 1, placements=[]) for i in range(qty))
            if len(target) > MAX_INSTANCES:
                raise ValueError(f"maximum {MAX_INSTANCES} {label} instances per request")
    instances.sort(
        key=lambda p: (
            -(p["bounds"][2] - p["bounds"][0]) * (p["bounds"][3] - p["bounds"][1]),
            p["id"],
            p["instance"],
        )
    )
    placements, missing = [], Counter()
    candidate_tests = 0
    for part in instances:
        found = False
        for stock in inventory:
            if candidate_tests >= MAX_CANDIDATE_TESTS:
                break
            if stock["material"] != part["material"] or stock["thickness_mm"] != part["thickness_mm"]:
                continue
            for angle in part["allowed_rotations"]:
                if candidate_tests >= MAX_CANDIDATE_TESTS:
                    break
                radians = math.radians(angle)
                cosine, sine = math.cos(radians), math.sin(radians)
                rotated = [[x * cosine - y * sine, x * sine + y * cosine] for x, y in part["outline"]]
                pb = _bounds(rotated)
                width, height = pb[2] - pb[0], pb[3] - pb[1]
                sb = stock["bounds"]
                if width > sb[2] - sb[0] - 2 * margin + EPS or height > sb[3] - sb[1] - 2 * margin + EPS:
                    continue
                xs = sorted(
                    {sb[0] + margin}
                    | {p["envelope_mm"][2] + spacing for p in stock["placements"]}
                    | {p[0] + margin for p in stock["outline"]}
                )
                ys = sorted(
                    {sb[1] + margin}
                    | {p["envelope_mm"][3] + spacing for p in stock["placements"]}
                    | {p[1] + margin for p in stock["outline"]}
                )
                for y in ys:
                    if found or candidate_tests >= MAX_CANDIDATE_TESTS:
                        break
                    for x in xs:
                        if candidate_tests >= MAX_CANDIDATE_TESTS:
                            break
                        candidate_tests += 1
                        box = [x, y, x + width, y + height]
                        if not _fits_stock(box, stock, margin) or any(
                            not _separated(box, p["envelope_mm"], spacing) for p in stock["placements"]
                        ):
                            continue
                        tx, ty = x - pb[0], y - pb[1]
                        placement = {
                            "part_id": part["id"],
                            "part_instance": part["instance"],
                            "stock_id": stock["id"],
                            "stock_instance": stock["instance"],
                            "rotation_degrees": angle,
                            "translation_mm": [tx, ty],
                            "envelope_mm": box,
                            "outline_mm": [[p[0] + tx, p[1] + ty] for p in rotated],
                            "material": part["material"],
                            "thickness_mm": part["thickness_mm"],
                        }
                        placements.append(placement)
                        stock["placements"].append(placement)
                        found = True
                        break
                if found:
                    break
            if found:
                break
        if not found:
            missing[part["id"]] += 1
    used = []
    costs: dict[str, Decimal] = {}
    fully_priced = True
    for stock in inventory:
        if not stock["placements"]:
            continue
        for i, p in enumerate(stock["placements"]):
            if not _fits_stock(p["envelope_mm"], stock, margin) or any(
                not _separated(p["envelope_mm"], q["envelope_mm"], spacing) for q in stock["placements"][:i]
            ):
                raise RuntimeError("independent final envelope validation failed")
        sheet = {
            "stock_id": stock["id"],
            "stock_instance": stock["instance"],
            "part_count": len(stock["placements"]),
        }
        if stock.get("price") is not None:
            sheet.update(price=stock["price"], currency=stock["currency"])
            costs[stock["currency"]] = costs.get(stock["currency"], Decimal(0)) + Decimal(stock["price"])
        else:
            fully_priced = False
        used.append(sheet)
    return {
        "status": "partial" if missing else "complete",
        "solver": "conservative-envelope-first-fit-v1",
        "optimal": False,
        "validated": True,
        "units": "mm",
        "spacing_mm": spacing,
        "edge_margin_mm": margin,
        "placements": placements,
        "unplaced": [
            {
                "part_id": ident,
                "quantity": qty,
                "reason": (
                    "Search budget reached"
                    if candidate_tests >= MAX_CANDIDATE_TESTS
                    else "No feasible placement found on eligible available stock by conservative fallback"
                ),
            }
            for ident, qty in sorted(missing.items())
        ],
        "sheets_used": used,
        "sheet_count": len(used),
        "cost_by_currency": {c: str(v) for c, v in costs.items()},
        "fully_priced": bool(fully_priced and not missing),
        "candidate_tests": candidate_tests,
        "search_budget_exhausted": candidate_tests >= MAX_CANDIDATE_TESTS,
        "issues": [
            {
                "code": "conservative_nest",
                "severity": "info",
                "message": "Envelopes may overestimate stock; no part-in-hole or common-line savings. This is not an optimality proof or NC program.",
            }
        ],
    }
