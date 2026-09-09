"""Bounded local planning evidence for a pure PDF formatter, never geometry approval."""

import math
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_REPORT_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_REPORT_BYTES + 64 * 1024
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_SHEETS = 300
MAX_VERTICES = 200000
MAX_INCHES = 100000
Number = Annotated[float, Field(ge=-MAX_INCHES, le=MAX_INCHES)]
# Below the native geometry grid, but large enough to keep fit-to-page arithmetic finite.
Dimension = Annotated[float, Field(ge=1e-9, le=MAX_INCHES)]
Allowance = Annotated[float, Field(ge=0, le=100)]
Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r'\S')]
Label = Annotated[str, Field(min_length=1, max_length=1000, pattern=r'\S')]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class Point(StrictModel):
    x: Number
    y: Number


class Polygon(StrictModel):
    type: Literal['poly']
    points: list[Point] = Field(min_length=3, max_length=MAX_VERTICES)


class Circle(StrictModel):
    type: Literal['circle']
    cx: Number
    cy: Number
    r: Dimension


Loop = Annotated[Polygon | Circle, Field(discriminator='type')]


def bounds(loop: Polygon | Circle) -> tuple[float, float, float, float]:
    if isinstance(loop, Circle):
        return loop.cx - loop.r, loop.cy - loop.r, loop.cx + loop.r, loop.cy + loop.r
    return (
        min(p.x for p in loop.points),
        min(p.y for p in loop.points),
        max(p.x for p in loop.points),
        max(p.y for p in loop.points),
    )


def vertices(loop: Polygon | Circle) -> int:
    return 1 if isinstance(loop, Circle) else len(loop.points)


class Exclusion(StrictModel):
    outline: Loop
    clearanceIn: Allowance


class Placement(StrictModel):
    partId: Identifier
    originalInstance: int = Field(ge=0, lt=300)
    loops: list[Loop] = Field(min_length=1, max_length=2000)


class Sheet(StrictModel):
    number: int = Field(ge=1, le=MAX_SHEETS)
    source: Literal['purchase', 'recorded_piece']
    sourceLabel: Label
    widthIn: Dimension
    lengthIn: Dimension
    marginIn: Allowance
    gapIn: Allowance
    outer: Loop
    holes: list[Loop] = Field(max_length=2000)
    exclusions: list[Exclusion] = Field(max_length=16)
    placements: list[Placement] = Field(min_length=1, max_length=300)

    def all_loops(self):
        yield self.outer
        yield from self.holes
        yield from (zone.outline for zone in self.exclusions)
        for placement in self.placements:
            yield from placement.loops

    @model_validator(mode='after')
    def display_bounds(self):
        # This is a structural extent check, not a second topology/clearance engine.
        tolerance = max(1e-9, 1e-12 * max(self.widthIn, self.lengthIn))
        for loop in self.all_loops():
            x0, y0, x1, y1 = bounds(loop)
            if x0 < -tolerance or y0 < -tolerance or x1 > self.lengthIn + tolerance or y1 > self.widthIn + tolerance:
                raise ValueError('Layout geometry lies outside the declared sheet extents')
            if x1 <= x0 or y1 <= y0:
                raise ValueError('Layout contours must have positive two-dimensional extents')
        expected = (0.0, 0.0, self.lengthIn, self.widthIn)
        if any(abs(a - b) > tolerance for a, b in zip(bounds(self.outer), expected)):
            raise ValueError('Sheet outer bounds must match its declared inch dimensions')
        if self.source == 'purchase':
            if self.holes or not isinstance(self.outer, Polygon) or len(self.outer.points) != 4:
                raise ValueError('Purchased stock must be a full rectangular sheet')
            corners = {(0.0, 0.0), (self.lengthIn, 0.0), (self.lengthIn, self.widthIn), (0.0, self.widthIn)}
            actual = {(p.x, p.y) for p in self.outer.points}
            if len(actual) != 4 or any(
                not any(abs(x - a) <= tolerance and abs(y - b) <= tolerance for a, b in corners) for x, y in actual
            ):
                raise ValueError('Purchased stock must have the four declared rectangular corners')
            points = self.outer.points
            for a, b in zip(points, points[1:] + points[:1]):
                if (abs(a.x - b.x) <= tolerance) == (abs(a.y - b.y) <= tolerance):
                    raise ValueError('Purchased sheet corners must follow its rectangular boundary')
        return self


class Requirement(StrictModel):
    id: Identifier
    label: Identifier
    name: Label
    revision: str = Field(max_length=1000)
    quantity: int = Field(ge=1, le=300)


class PurchaseCount(StrictModel):
    widthIn: Dimension
    lengthIn: Dimension
    quantity: int = Field(ge=1, le=MAX_SHEETS)


class Group(StrictModel):
    id: Identifier
    name: Label
    material: Label
    materialDescription: Label
    thicknessIn: Dimension
    selectionKind: Literal['full_sheet', 'recorded_piece']
    partRequirements: list[Requirement] = Field(min_length=1, max_length=300)
    sheets: list[Sheet] = Field(min_length=1, max_length=MAX_SHEETS)
    baselinePurchaseSheets: list[PurchaseCount] = Field(max_length=12)

    @model_validator(mode='after')
    def exact_instances(self):
        requirements = {p.id: p for p in self.partRequirements}
        if len(requirements) != len(self.partRequirements):
            raise ValueError('Part requirement IDs must be unique')
        if len({p.label for p in self.partRequirements}) != len(self.partRequirements):
            raise ValueError('Part labels must be unique within a group')
        if [s.number for s in self.sheets] != list(range(1, len(self.sheets) + 1)):
            raise ValueError('Sheet numbers must be sequential within each group')
        if sum(part.quantity for part in self.partRequirements) > 300:
            raise ValueError('A material group exceeds 300 original instances')
        expected = {(part.id, i) for part in self.partRequirements for i in range(part.quantity)}
        actual = [(part.partId, part.originalInstance) for sheet in self.sheets for part in sheet.placements]
        if len(actual) != len(set(actual)) or set(actual) != expected:
            raise ValueError('Selected layouts must place every original part instance exactly once')
        if self.selectionKind == 'full_sheet' and any(s.source != 'purchase' for s in self.sheets):
            raise ValueError('A full-sheet selection cannot contain a recorded piece')
        if self.selectionKind == 'recorded_piece' and not self.baselinePurchaseSheets:
            raise ValueError('A conditional selection requires its full-sheet purchase fallback')
        if sum(s.quantity for s in self.baselinePurchaseSheets) > MAX_SHEETS:
            raise ValueError('Full-sheet fallback exceeds the sheet budget')
        if len({(s.widthIn, s.lengthIn) for s in self.baselinePurchaseSheets}) != len(self.baselinePurchaseSheets):
            raise ValueError('Full-sheet fallback dimensions must be unique')
        return self


class BuyerPdfReport(StrictModel):
    version: Literal[1]
    units: Literal['in']
    expectedCompanyId: int = Field(ge=1, le=2147483647)
    projectName: str = Field(min_length=1, max_length=200, pattern=r'\S')
    notes: str = Field(max_length=2000)
    inputSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    solverVersion: Literal['werco-contour-v7']
    groups: list[Group] = Field(min_length=1, max_length=300)

    @model_validator(mode='before')
    @classmethod
    def bounded_tree(cls, value: Any):
        stack = [(value, 0)]
        nodes = 0
        while stack:
            item, depth = stack.pop()
            nodes += 1
            if nodes > 900000 or depth > 24:
                raise ValueError('Report structure exceeds the parsing budget')
            if isinstance(item, dict):
                if any(not isinstance(key, str) or len(key) > 100 for key in item):
                    raise ValueError('Report field name is invalid')
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
            elif isinstance(item, str):
                if len(item) > 2000 or any(ord(c) < 32 and c not in '\n\r\t' for c in item):
                    raise ValueError('Report text is too long or contains unsupported control characters')
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                if not math.isfinite(item):
                    raise ValueError('Report numbers must be finite')
        if isinstance(value, dict) and type(value.get('version')) is not int:
            raise ValueError('Report version must be integer 1')
        return value

    @model_validator(mode='after')
    def project_budget(self):
        if len({g.id for g in self.groups}) != len(self.groups):
            raise ValueError('Material group IDs must be unique')
        parts = [p for g in self.groups for p in g.partRequirements]
        if len({p.id for p in parts}) != len(parts) or sum(p.quantity for p in parts) > 300:
            raise ValueError('Reports require unique part IDs and at most 300 original instances')
        sheets = [s for g in self.groups for s in g.sheets]
        if (
            len(sheets) > MAX_SHEETS
            or sum(vertices(loop) for sheet in sheets for loop in sheet.all_loops()) > MAX_VERTICES
        ):
            raise ValueError('Report exceeds 300 sheets or 200000 aggregate geometry vertices')
        if (
            sum(s.source == 'recorded_piece' for s in sheets) > 1
            or sum(g.selectionKind == 'recorded_piece' for g in self.groups) > 1
        ):
            raise ValueError('A report can use at most one recorded piece for one material group')
        return self
