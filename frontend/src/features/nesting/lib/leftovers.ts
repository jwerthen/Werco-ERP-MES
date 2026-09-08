import { canonicalJSON } from './provenance';
import * as Clipper from 'clipper-lib';
import { movedOuter } from './contour-packing';
import {
  bounds,
  loopArea,
  partArea,
  validateNest,
  validatePart,
  validateStock,
  type Loop,
  type Nest,
  type Part,
  type Point,
  type Stock,
} from './nesting';

export const LEFTOVER_VERSION = 'werco-leftovers-v1' as const;
/** Engineering approximation profile, not approved shop eligibility or a kerf model. */
export const LEFTOVER_PROFILE = Object.freeze({
  integerGridMm: 0.0001,
  circleRadialExcessMm: 0.0001 * 25.4,
  numericalProtectionMm: 0.0004,
  usableBoundaryInsetMm: 0.0004,
  partGapFraction: 0.5,
  importedCurveToleranceIncluded: true,
  offsetJoin: 'square-tangent' as const,
  maximumConvexJoinRadiusFactor: Math.SQRT2,
  maxInputVertices: 60_000,
  maxOutputVertices: 120_000,
  maxSheetOutputVertices: 30_000,
  maxRegions: 2_000,
  maxCircleVertices: 8_192,
  absoluteAreaToleranceMm2: 1e-7,
  relativeAreaTolerance: 1e-12,
});
const RESERVATION_DESCRIPTION =
  'Half of part gap plus imported curve tolerance and numerical protection around full outer profiles; square tangent joins; inward-protected usable boundary. Not physical kerf.';
const SCALE = 1 / LEFTOVER_PROFILE.integerGridMm;
const squareInches = (area: number) => area / (25.4 * 25.4);
const check = (ok: unknown, message: string): void => {
  if (!ok) throw new Error(`Leftover analysis: ${message}`);
};

export type LeftoverRegion = {
  id: string;
  outer: Point[];
  holes: Point[][];
  area: number;
  bounds: { x: number; y: number; width: number; height: number };
  classification: 'review';
  explanation: string;
  creditUSD: 0;
};
export type SheetLeftoverAnalysis = {
  sheet: number;
  grossArea: number;
  usableArea: number;
  edgeMarginArea: number;
  nominalPartArea: number;
  reservedCutoutArea: number;
  clearanceAndProtectionArea: number;
  remainingArea: number;
  reconciliationResidualArea: number;
  regions: LeftoverRegion[];
};
export type LeftoverAnalysis = {
  inputSignature: string;
  version: typeof LEFTOVER_VERSION;
  status: 'potential_review_only';
  creditUSD: 0;
  assumptions: {
    profile: typeof LEFTOVER_PROFILE;
    reservation: string;
    internalHolesReserved: true;
    boundsAreUsableRectangles: false;
    eligibilityVerified: false;
  };
  sheets: SheetLeftoverAnalysis[];
};

function sum(values: number[]): number {
  let result = 0,
    correction = 0;
  for (const value of values) {
    const next = value - correction,
      total = result + next;
    correction = total - result - next;
    result = total;
  }
  return result;
}

/** Integer-grid ring areas use exact integer products, even near the sheet-size limit. */
function pathArea(path: Clipper.Path): number {
  if (path.length < 3) return 0;
  const origin = path[0];
  let twice = BigInt(0);
  for (let index = 0; index < path.length; index++) {
    const a = path[index],
      b = path[(index + 1) % path.length];
    twice += BigInt(a.X - origin.X) * BigInt(b.Y - origin.Y) - BigInt(b.X - origin.X) * BigInt(a.Y - origin.Y);
  }
  return Number(twice < BigInt(0) ? -twice : twice) / (2 * SCALE * SCALE);
}

function canonicalPath(path: Clipper.Path, positive?: boolean): Clipper.Path {
  const points = path.map(point => ({ X: point.X, Y: point.Y }));
  if (positive !== undefined && Clipper.Clipper.Orientation(points) !== positive) points.reverse();
  let start = 0;
  for (let index = 1; index < points.length; index++)
    if (points[index].X < points[start].X || (points[index].X === points[start].X && points[index].Y < points[start].Y))
      start = index;
  return [...points.slice(start), ...points.slice(0, start)];
}
const pathKey = (path: Clipper.Path) => path.map(point => `${point.X},${point.Y}`).join(';');
const compareText = (left: string, right: string) => (left < right ? -1 : left > right ? 1 : 0);
const pointPath = (path: Clipper.Path) => path.map(point => ({ x: point.X / SCALE, y: point.Y / SCALE }));
const sortedPaths = (paths: Clipper.Paths) =>
  paths
    .map(path => canonicalPath(path))
    .sort((a, b) => {
      const left = pathKey(a),
        right = pathKey(b);
      return left < right ? -1 : left > right ? 1 : 0;
    });

function circleSegments(radius: number): number {
  const angle = Math.acos(1 / (1 + LEFTOVER_PROFILE.circleRadialExcessMm / radius));
  const count = Math.max(16, Math.ceil(Math.PI / angle / 4) * 4);
  check(
    Number.isFinite(count) && count <= LEFTOVER_PROFILE.maxCircleVertices,
    'circle tessellation exceeds the numerical budget.'
  );
  return count;
}
function integerOuter(loop: Loop): Clipper.Path {
  let points: Point[];
  if (loop.type === 'poly') points = loop.points;
  else {
    const count = circleSegments(loop.r),
      radius = loop.r / Math.cos(Math.PI / count);
    // Mid-step vertices give tangent sides at the exact axial circle extrema.
    points = Array.from({ length: count }, (_, index) => {
      const angle = ((2 * index + 1) * Math.PI) / count;
      return { x: loop.cx + radius * Math.cos(angle), y: loop.cy + radius * Math.sin(angle) };
    });
  }
  const path = points.map(point => ({ X: Math.round(point.x * SCALE), Y: Math.round(point.y * SCALE) }));
  check(
    path.every(point => Number.isSafeInteger(point.X) && Number.isSafeInteger(point.Y)),
    'coordinates exceed integer precision.'
  );
  check(pathArea(path) > 0, 'an outer contour collapses at the analysis grid precision.');
  return canonicalPath(path, true);
}

function inwardSheet(stock: Stock): Clipper.Path | null {
  const inset = LEFTOVER_PROFILE.usableBoundaryInsetMm;
  const x0 = Math.ceil((stock.margin + inset) * SCALE),
    y0 = x0;
  const x1 = Math.floor((stock.width - stock.margin - inset) * SCALE);
  const y1 = Math.floor((stock.height - stock.margin - inset) * SCALE);
  return x1 > x0 && y1 > y0
    ? [
        { X: x0, Y: y0 },
        { X: x1, Y: y0 },
        { X: x1, Y: y1 },
        { X: x0, Y: y1 },
      ]
    : null;
}

/** Canonical worker inputs bind a cached report to its exact geometry and placements.
 * This is equality evidence, not a cryptographic approval or an inventory identifier. */
function inputSignature(parts: Part[], stock: Stock, nest: Nest): string {
  return canonicalJSON({
    version: 'werco-leftover-inputs-v1',
    stock: [
      stock.width,
      stock.height,
      stock.margin,
      stock.gap,
      stock.maxSheets,
      stock.bedWidth,
      stock.bedHeight,
      stock.grainAxis ?? null,
    ],
    parts: [...parts]
      .sort((a, b) => compareText(a.id, b.id))
      .map(part => ({
        id: part.id,
        quantity: part.quantity,
        loops: part.loops,
        referencePaths: part.referencePaths ?? [],
        geometryToleranceMm: part.geometryToleranceMm ?? 0,
        rotate: part.rotate,
        rotationMode: part.rotationMode ?? null,
        grainAxis: part.grainAxis ?? null,
      })),
    sheets: nest.sheets,
    placements: [...nest.placements]
      .sort((a, b) => a.sheet - b.sheet || compareText(a.partId, b.partId) || a.instance - b.instance)
      .map(placement => [
        placement.partId,
        placement.instance,
        placement.sheet,
        placement.x,
        placement.y,
        placement.width,
        placement.height,
        placement.rotation,
      ]),
    unplaced: [...nest.unplaced].sort((a, b) => compareText(a.partId, b.partId)).map(item => [item.partId, item.count]),
  });
}

/** Analyze only validated placements. Every region is a potential leftover with zero credit. */
export function analyzeLeftovers(parts: Part[], stock: Stock, nest: Nest): LeftoverAnalysis {
  validateStock(stock);
  check(
    parts.length <= 300 && new Set(parts.map(part => part.id)).size === parts.length,
    'invalid part count or identity.'
  );
  parts.forEach(validatePart);
  check(
    parts.reduce((count, part) => count + part.quantity, 0) <= 300,
    'part quantities exceed the 300-instance limit.'
  );
  check(
    parts.reduce(
      (count, part) =>
        count +
        part.loops.reduce((vertices, loop) => vertices + (loop.type === 'circle' ? 1 : loop.points.length), 0) +
        (part.referencePaths?.reduce((vertices, path) => vertices + path.length, 0) ?? 0),
      0
    ) <= 20000,
    'source geometry exceeds the 20,000-vertex limit.'
  );
  validateNest(parts, stock, nest);
  const byId = new Map(parts.map(part => [part.id, part]));
  const placements = [...nest.placements].sort(
    (a, b) => a.sheet - b.sheet || compareText(a.partId, b.partId) || a.instance - b.instance
  );
  let inputVertices = 0;
  for (const placement of nest.placements) {
    const outer = byId.get(placement.partId)!.loops[0];
    inputVertices += outer.type === 'poly' ? outer.points.length : circleSegments(outer.r);
    check(
      inputVertices <= LEFTOVER_PROFILE.maxInputVertices,
      'placed geometry exceeds the 60,000 input-vertex budget.'
    );
  }
  const usableBoundary = inwardSheet(stock);
  const grossArea = stock.width * stock.height;
  const usableArea = (stock.width - 2 * stock.margin) * (stock.height - 2 * stock.margin);
  const edgeMarginArea = grossArea - usableArea;
  let outputVertices = 0,
    regionCount = 0;
  const sheets: SheetLeftoverAnalysis[] = [];
  for (let sheet = 0; sheet < nest.sheets; sheet++) {
    const placed = placements.filter(placement => placement.sheet === sheet);
    const nominalPartArea = sum(placed.map(placement => partArea(byId.get(placement.partId)!)));
    const reservedCutoutArea = sum(
      placed.map(placement => sum(byId.get(placement.partId)!.loops.slice(1).map(loopArea)))
    );
    const clipPaths: Clipper.Paths = [];
    let offsetVertices = 0;
    if (usableBoundary) {
      for (const placement of placed) {
        const part = byId.get(placement.partId)!;
        const path = integerOuter(movedOuter(part, placement));
        const reserve =
          stock.gap * LEFTOVER_PROFILE.partGapFraction +
          (part.geometryToleranceMm ?? 0) +
          LEFTOVER_PROFILE.numericalProtectionMm;
        check(Number.isFinite(reserve) && reserve <= 40_000, 'reserve distance exceeds the bounded offset range.');
        // Square joins place a tangent outside the circular offset. The extra
        // four grid cells cover input/output rounding and Boolean intersections.
        const offset = new Clipper.ClipperOffset();
        offset.AddPath(path, Clipper.JoinType.jtSquare, Clipper.EndType.etClosedPolygon);
        const envelopes: Clipper.Paths = [];
        offset.Execute(envelopes, Math.ceil(reserve * SCALE));
        check(envelopes.length > 0, 'offset did not preserve a placed outer contour.');
        offsetVertices += envelopes.reduce((count, ring) => count + ring.length, 0);
        check(
          offsetVertices <= LEFTOVER_PROFILE.maxOutputVertices,
          'guarded envelopes exceed the offset-vertex budget.'
        );
        clipPaths.push(...envelopes);
      }
    }
    const tree = new Clipper.PolyTree();
    if (usableBoundary) {
      const clip = new Clipper.Clipper();
      clip.StrictlySimple = true;
      check(clip.AddPath(usableBoundary, Clipper.PolyType.ptSubject, true), 'usable sheet polygon is invalid.');
      if (clipPaths.length)
        check(clip.AddPaths(sortedPaths(clipPaths), Clipper.PolyType.ptClip, true), 'guarded envelopes are invalid.');
      check(
        clip.Execute(
          Clipper.ClipType.ctDifference,
          tree,
          Clipper.PolyFillType.pftNonZero,
          Clipper.PolyFillType.pftNonZero
        ),
        'polygon difference failed.'
      );
    }
    const regions: LeftoverRegion[] = [];
    let sheetVertices = 0;
    const visit = (node: Clipper.PolyNode) => {
      for (const child of node.Childs()) {
        if (!child.IsHole()) {
          const outer = canonicalPath(child.Contour(), true);
          const holes = child
            .Childs()
            .filter(hole => hole.IsHole())
            .map(hole => canonicalPath(hole.Contour(), false));
          holes.sort((a, b) => compareText(pathKey(a), pathKey(b)));
          sheetVertices += outer.length + holes.reduce((count, hole) => count + hole.length, 0);
          check(
            sheetVertices <= LEFTOVER_PROFILE.maxSheetOutputVertices,
            'a sheet exceeds the 30,000 output-vertex display budget.'
          );
          const area = pathArea(outer) - sum(holes.map(pathArea));
          check(Number.isFinite(area) && area > 0, 'a connected region has invalid area.');
          const points = pointPath(outer);
          regions.push({
            id: '',
            outer: points,
            holes: holes.map(pointPath),
            area,
            bounds: bounds({ type: 'poly', points }),
            classification: 'review',
            creditUSD: 0,
            explanation:
              'Potential leftover only. Review actual cuts, access, handling and material condition. Bounds are extents, not a guaranteed usable rectangle; no inventory eligibility or credit is established.',
          });
        }
        visit(child);
      }
    };
    visit(tree);
    outputVertices += sheetVertices;
    regionCount += regions.length;
    check(outputVertices <= LEFTOVER_PROFILE.maxOutputVertices, 'regions exceed the 120,000 output-vertex budget.');
    check(regionCount <= LEFTOVER_PROFILE.maxRegions, 'regions exceed the 2,000-region budget.');
    regions.sort(
      (a, b) =>
        b.area - a.area ||
        a.bounds.x - b.bounds.x ||
        a.bounds.y - b.bounds.y ||
        compareText(JSON.stringify(a.outer), JSON.stringify(b.outer))
    );
    regions.forEach((region, index) => {
      region.id = `leftover-v1-sheet-${sheet + 1}-region-${index + 1}`;
    });
    const remainingArea = sum(regions.map(region => region.area));
    const allowance = usableArea - remainingArea - nominalPartArea - reservedCutoutArea;
    const tolerance = Math.max(
      LEFTOVER_PROFILE.absoluteAreaToleranceMm2,
      grossArea * LEFTOVER_PROFILE.relativeAreaTolerance
    );
    check(allowance >= -tolerance, 'remaining regions overstate usable material; the area ledger is negative.');
    const clearanceAndProtectionArea = Math.max(0, allowance);
    const reconciliationResidualArea =
      grossArea - sum([edgeMarginArea, nominalPartArea, reservedCutoutArea, clearanceAndProtectionArea, remainingArea]);
    check(Math.abs(reconciliationResidualArea) <= tolerance, 'the area ledger does not reconcile.');
    sheets.push({
      sheet,
      grossArea,
      usableArea,
      edgeMarginArea,
      nominalPartArea,
      reservedCutoutArea,
      clearanceAndProtectionArea,
      remainingArea,
      reconciliationResidualArea,
      regions,
    });
  }
  return {
    inputSignature: inputSignature(parts, stock, nest),
    version: LEFTOVER_VERSION,
    status: 'potential_review_only',
    creditUSD: 0,
    assumptions: {
      profile: LEFTOVER_PROFILE,
      reservation: RESERVATION_DESCRIPTION,
      internalHolesReserved: true,
      boundsAreUsableRectangles: false,
      eligibilityVerified: false,
    },
    sheets,
  };
}

/** Export a bounded worker result as unapproved review evidence, never as inventory. */
export function leftoversToFile(analysis: LeftoverAnalysis, context?: { parts: Part[]; stock: Stock; nest: Nest }) {
  check(
    analysis.version === LEFTOVER_VERSION && analysis.status === 'potential_review_only' && analysis.creditUSD === 0,
    'invalid review status.'
  );
  check(
    JSON.stringify(analysis.assumptions.profile) === JSON.stringify(LEFTOVER_PROFILE),
    'unknown numerical profile.'
  );
  check(Array.isArray(analysis.sheets) && analysis.sheets.length <= 300, 'invalid sheet count.');
  check(
    analysis.assumptions.reservation === RESERVATION_DESCRIPTION &&
      analysis.assumptions.internalHolesReserved === true &&
      analysis.assumptions.boundsAreUsableRectangles === false &&
      analysis.assumptions.eligibilityVerified === false,
    'invalid review assumptions.'
  );
  check(
    new Set(analysis.sheets.map(sheet => sheet.sheet)).size === analysis.sheets.length &&
      analysis.sheets.every(
        sheet => Number.isInteger(sheet.sheet) && sheet.sheet >= 0 && sheet.sheet < analysis.sheets.length
      ),
    'invalid sheet identities.'
  );
  check(
    typeof analysis.inputSignature === 'string' &&
      analysis.inputSignature.length > 0 &&
      analysis.inputSignature.length <= 5_000_000,
    'missing or oversized worker input binding.'
  );
  if (context) {
    check(analysis.sheets.length === context.nest.sheets, 'report sheet count does not match the validated nest.');
    check(
      analysis.inputSignature === inputSignature(context.parts, context.stock, context.nest),
      'report input binding does not match the current geometry and placements.'
    );
  }
  const exportBoundary = context ? inwardSheet(context.stock) : undefined;
  let vertices = 0,
    regions = 0;
  const regionIds = new Set<string>();
  return {
    inputBinding: context ? 'matched_current_layout' : 'not_checked',
    version: analysis.version,
    status: analysis.status,
    units: 'in',
    areaUnits: 'in2',
    creditUSD: 0,
    assumptions: analysis.assumptions,
    sheets: analysis.sheets.map(sheet => {
      const areas = {
        grossArea: sheet.grossArea,
        usableArea: sheet.usableArea,
        edgeMarginArea: sheet.edgeMarginArea,
        nominalPartArea: sheet.nominalPartArea,
        reservedCutoutArea: sheet.reservedCutoutArea,
        clearanceAndProtectionArea: sheet.clearanceAndProtectionArea,
        remainingArea: sheet.remainingArea,
      };
      check(
        Object.values(areas).every(area => Number.isFinite(area) && area >= 0),
        'invalid area ledger.'
      );
      const tolerance = Math.max(
        LEFTOVER_PROFILE.absoluteAreaToleranceMm2,
        sheet.grossArea * LEFTOVER_PROFILE.relativeAreaTolerance
      );
      check(Array.isArray(sheet.regions), 'invalid regions.');
      let sheetVertexCount = 0;
      if (context) {
        const { parts, stock, nest } = context;
        const byId = new Map(parts.map(part => [part.id, part]));
        const placed = nest.placements.filter(placement => placement.sheet === sheet.sheet);
        const nominal = sum(placed.map(placement => partArea(byId.get(placement.partId)!)));
        const cutouts = sum(placed.map(placement => sum(byId.get(placement.partId)!.loops.slice(1).map(loopArea))));
        check(
          Math.abs(sheet.grossArea - stock.width * stock.height) <= tolerance &&
            Math.abs(sheet.usableArea - (stock.width - 2 * stock.margin) * (stock.height - 2 * stock.margin)) <=
              tolerance &&
            Math.abs(sheet.nominalPartArea - nominal) <= tolerance &&
            Math.abs(sheet.reservedCutoutArea - cutouts) <= tolerance,
          'report areas do not match the validated placements and sheet.'
        );
      }
      check(
        Math.abs(sheet.edgeMarginArea + sheet.usableArea - sheet.grossArea) <= tolerance,
        'usable and margin areas do not match the sheet.'
      );
      const remaining = sum(sheet.regions.map(region => region.area));
      check(Math.abs(remaining - sheet.remainingArea) <= tolerance, 'region areas do not match the ledger.');
      check(
        Math.abs(
          sheet.grossArea -
            sum([
              sheet.edgeMarginArea,
              sheet.nominalPartArea,
              sheet.reservedCutoutArea,
              sheet.clearanceAndProtectionArea,
              sheet.remainingArea,
            ]) -
            sheet.reconciliationResidualArea
        ) <= tolerance && Math.abs(sheet.reconciliationResidualArea) <= tolerance,
        'the exported area ledger does not reconcile.'
      );
      return {
        sheet: sheet.sheet,
        ...Object.fromEntries(Object.entries(areas).map(([key, value]) => [key + 'In2', squareInches(value)])),
        reconciliationResidualAreaIn2: squareInches(sheet.reconciliationResidualArea),
        regions: sheet.regions.map(region => {
          regions++;
          check(
            regions <= LEFTOVER_PROFILE.maxRegions && region.classification === 'review' && region.creditUSD === 0,
            'invalid region classification or count.'
          );
          check(
            typeof region.id === 'string' &&
              region.id.length > 0 &&
              region.id.length < 200 &&
              typeof region.explanation === 'string' &&
              region.explanation.length <= 2000 &&
              Array.isArray(region.outer) &&
              Array.isArray(region.holes),
            'invalid region metadata.'
          );
          check(!regionIds.has(region.id), 'duplicate region IDs.');
          regionIds.add(region.id);
          const rings = [region.outer, ...region.holes];
          check(rings.every(Array.isArray), 'invalid region rings.');
          const ringVertices = rings.reduce((count, ring) => count + ring.length, 0);
          vertices += ringVertices;
          sheetVertexCount += ringVertices;
          check(
            sheetVertexCount <= LEFTOVER_PROFILE.maxSheetOutputVertices,
            'sheet geometry exceeds the display budget.'
          );
          check(
            vertices <= LEFTOVER_PROFILE.maxOutputVertices &&
              rings.every(
                ring =>
                  ring.length >= 3 &&
                  ring.every(
                    point =>
                      point &&
                      Number.isFinite(point.x) &&
                      Number.isFinite(point.y) &&
                      Math.abs(point.x) <= 20000 &&
                      Math.abs(point.y) <= 20000
                  )
              ),
            'invalid region geometry.'
          );
          const integerRings = rings.map(ring =>
            ring.map(point => {
              const X = Math.round(point.x * SCALE),
                Y = Math.round(point.y * SCALE);
              const onGrid = (coordinate: number, rounded: number) =>
                Math.abs(coordinate * SCALE - rounded) <= 4 * Number.EPSILON * Math.max(1, Math.abs(rounded));
              check(onGrid(point.x, X) && onGrid(point.y, Y), 'cached region does not use the declared integer grid.');
              return { X, Y };
            })
          );
          if (context)
            check(
              exportBoundary &&
                integerRings.every(ring =>
                  ring.every(
                    point =>
                      point.X >= exportBoundary[0].X &&
                      point.Y >= exportBoundary[0].Y &&
                      point.X <= exportBoundary[2].X &&
                      point.Y <= exportBoundary[2].Y
                  )
                ),
              'region extends outside the inward-protected usable sheet.'
            );
          const area = pathArea(integerRings[0]) - sum(integerRings.slice(1).map(pathArea));
          check(
            Number.isFinite(area) && area > 0 && Math.abs(area - region.area) <= tolerance,
            'region geometry does not match its area.'
          );
          const box = bounds({ type: 'poly', points: region.outer });
          check(
            ['x', 'y', 'width', 'height'].every(
              key =>
                Math.abs(box[key as keyof typeof box] - region.bounds[key as keyof typeof box]) <
                LEFTOVER_PROFILE.integerGridMm
            ),
            'region bounds do not match its geometry.'
          );
          const toIn = (point: Point) => ({ x: point.x / 25.4, y: point.y / 25.4 });
          return {
            id: region.id,
            outer: region.outer.map(toIn),
            holes: region.holes.map(ring => ring.map(toIn)),
            areaIn2: squareInches(region.area),
            bounds: { x: box.x / 25.4, y: box.y / 25.4, width: box.width / 25.4, height: box.height / 25.4 },
            classification: 'review',
            explanation: region.explanation,
            creditUSD: 0,
          };
        }),
      };
    }),
  };
}
