import {
  canonicalPath,
  circleSegments,
  compareText,
  guardedOuter,
  inwardSheet,
  pathArea,
  pathKey,
  pointPath,
  sortedPaths,
  SCALE,
  intersectPaths,
  filledArea,
  GUARDED_GEOMETRY_PROFILE,
} from './guarded-geometry';
import { EXCLUSION_PROFILE, exclusionVertexCount, prepareExclusions } from './stock-exclusions';
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
  type Nest,
  type Part,
  type Point,
  type Stock,
} from './nesting';

export const LEFTOVER_VERSION = 'werco-leftovers-v1' as const;
export const LEFTOVER_EXCLUSION_VERSION = 'werco-leftovers-v2' as const;
/** Engineering approximation profile, not approved shop eligibility or a kerf model. */
export const LEFTOVER_PROFILE = Object.freeze({
  integerGridMm: GUARDED_GEOMETRY_PROFILE.integerGridMm,
  circleRadialExcessMm: GUARDED_GEOMETRY_PROFILE.circleRadialExcessMm,
  numericalProtectionMm: GUARDED_GEOMETRY_PROFILE.numericalProtectionMm,
  usableBoundaryInsetMm: GUARDED_GEOMETRY_PROFILE.usableBoundaryInsetMm,
  partGapFraction: GUARDED_GEOMETRY_PROFILE.partGapFraction,
  importedCurveToleranceIncluded: true,
  offsetJoin: GUARDED_GEOMETRY_PROFILE.offsetJoin,
  maximumConvexJoinRadiusFactor: GUARDED_GEOMETRY_PROFILE.maximumConvexJoinRadiusFactor,
  maxInputVertices: 60_000,
  maxOutputVertices: 120_000,
  maxSheetOutputVertices: 30_000,
  maxRegions: 2_000,
  maxCircleVertices: GUARDED_GEOMETRY_PROFILE.maxCircleVertices,
  absoluteAreaToleranceMm2: 1e-7,
  relativeAreaTolerance: 1e-12,
});
export const LEFTOVER_EXCLUSION_PROFILE = Object.freeze({ ...LEFTOVER_PROFILE, exclusions: EXCLUSION_PROFILE });
const RESERVATION_DESCRIPTION =
  'Half of part gap plus imported curve tolerance and numerical protection around full outer profiles; square tangent joins; inward-protected usable boundary. Not physical kerf.';
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
  excludedArea?: number;
  nominalPartArea: number;
  reservedCutoutArea: number;
  clearanceAndProtectionArea: number;
  remainingArea: number;
  reconciliationResidualArea: number;
  regions: LeftoverRegion[];
};
export type LeftoverAnalysis = {
  inputSignature: string;
  version: typeof LEFTOVER_VERSION | typeof LEFTOVER_EXCLUSION_VERSION;
  status: 'potential_review_only';
  creditUSD: 0;
  assumptions: {
    profile: typeof LEFTOVER_PROFILE | typeof LEFTOVER_EXCLUSION_PROFILE;
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

/** Canonical worker inputs bind a cached report to its exact geometry and placements.
 * This is equality evidence, not a cryptographic approval or an inventory identifier. */
function inputSignature(parts: Part[], stock: Stock, nest: Nest): string {
  return canonicalJSON({
    version: stock.exclusions?.length ? 'werco-leftover-inputs-v2' : 'werco-leftover-inputs-v1',
    ...(stock.exclusions?.length ? { exclusions: stock.exclusions } : {}),
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
    ) +
      exclusionVertexCount(stock.exclusions ?? []) <=
      20000,
    'source geometry exceeds the 20,000-vertex limit.'
  );
  validateNest(parts, stock, nest);
  const byId = new Map(parts.map(part => [part.id, part]));
  const placements = [...nest.placements].sort(
    (a, b) => a.sheet - b.sheet || compareText(a.partId, b.partId) || a.instance - b.instance
  );
  const exclusions = prepareExclusions(stock);
  const exclusionPaths = exclusions.flatMap(region => region.paths);
  const withExclusions = exclusions.length > 0;
  let inputVertices = exclusionPaths.reduce((count, path) => count + path.length, 0);
  for (const placement of nest.placements) {
    const outer = byId.get(placement.partId)!.loops[0];
    inputVertices += outer.type === 'poly' ? outer.points.length : circleSegments(outer.r);
    check(
      inputVertices <= LEFTOVER_PROFILE.maxInputVertices,
      'placed geometry exceeds the 60,000 input-vertex budget.'
    );
  }
  const usableBoundary = inwardSheet(stock);
  const excludedArea =
    usableBoundary && withExclusions ? filledArea(intersectPaths([usableBoundary], exclusionPaths)) : 0;
  check(Number.isFinite(excludedArea) && excludedArea >= 0, 'invalid guarded exclusion area.');
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
    const clipPaths: Clipper.Paths = [...exclusionPaths];
    let offsetVertices = exclusionPaths.reduce((count, path) => count + path.length, 0);
    if (usableBoundary) {
      for (const placement of placed) {
        const part = byId.get(placement.partId)!;
        const envelopes = guardedOuter(
          movedOuter(part, placement),
          stock.gap * LEFTOVER_PROFILE.partGapFraction + (part.geometryToleranceMm ?? 0)
        );
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
      region.id = `leftover-${withExclusions ? 'v2' : 'v1'}-sheet-${sheet + 1}-region-${index + 1}`;
    });
    const remainingArea = sum(regions.map(region => region.area));
    const allowance = usableArea - excludedArea - remainingArea - nominalPartArea - reservedCutoutArea;
    const tolerance = Math.max(
      LEFTOVER_PROFILE.absoluteAreaToleranceMm2,
      grossArea * LEFTOVER_PROFILE.relativeAreaTolerance
    );
    check(allowance >= -tolerance, 'remaining regions overstate usable material; the area ledger is negative.');
    const clearanceAndProtectionArea = Math.max(0, allowance);
    const reconciliationResidualArea =
      grossArea -
      sum([
        edgeMarginArea,
        excludedArea,
        nominalPartArea,
        reservedCutoutArea,
        clearanceAndProtectionArea,
        remainingArea,
      ]);
    check(Math.abs(reconciliationResidualArea) <= tolerance, 'the area ledger does not reconcile.');
    sheets.push({
      sheet,
      grossArea,
      usableArea,
      edgeMarginArea,
      ...(withExclusions ? { excludedArea } : {}),
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
    version: withExclusions ? LEFTOVER_EXCLUSION_VERSION : LEFTOVER_VERSION,
    status: 'potential_review_only',
    creditUSD: 0,
    assumptions: {
      profile: withExclusions ? LEFTOVER_EXCLUSION_PROFILE : LEFTOVER_PROFILE,
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
  const withExclusions = analysis.version === LEFTOVER_EXCLUSION_VERSION;
  check(
    (analysis.version === LEFTOVER_VERSION || withExclusions) &&
      analysis.status === 'potential_review_only' &&
      analysis.creditUSD === 0,
    'invalid review status.'
  );
  check(
    JSON.stringify(analysis.assumptions.profile) ===
      JSON.stringify(withExclusions ? LEFTOVER_EXCLUSION_PROFILE : LEFTOVER_PROFILE),
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
    check(
      withExclusions === Boolean(context.stock.exclusions?.length),
      'report exclusion version does not match the stock.'
    );
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
        ...(withExclusions ? { excludedArea: sheet.excludedArea! } : {}),
        nominalPartArea: sheet.nominalPartArea,
        reservedCutoutArea: sheet.reservedCutoutArea,
        clearanceAndProtectionArea: sheet.clearanceAndProtectionArea,
        remainingArea: sheet.remainingArea,
      };
      check(
        withExclusions ? typeof sheet.excludedArea === 'number' : sheet.excludedArea === undefined,
        'invalid exclusion ledger version.'
      );
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
              sheet.excludedArea ?? 0,
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
