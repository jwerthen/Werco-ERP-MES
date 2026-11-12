/**
 * The drag planner: the geometric rule table, the worked fixtures from the spec, the
 * counter-examples the property loops found against the bare geometric rule (now named
 * fixtures), and the property loops themselves — every pair and triple of panel sizes at
 * xl / lg / base and KPI strips at 5 / 3 / 2 columns, laid out by an independent CSS-grid
 * simulator (`test-utils/gridFlow`), with a pointer on every point of a 10 px lattice.
 */
import {
  flowGrid,
  rectOf,
  type FlowBreakpoint,
  type FlowItem,
  type FlowResult,
  type KpiColumns,
  type PanelBreakpoint,
} from '../../../test-utils/gridFlow';
import { PANEL_SIZES, type PanelSize } from '../widgetTypes';
import {
  FULL_ROW_RATIO,
  GHOST_MAX,
  POSITION_ANNOUNCE_DEBOUNCE_MS,
  REORDER_HYSTERESIS_PX,
  ROW_TOLERANCE_PX,
  ZONE_HIT,
  breakpointOf,
  ghostBox,
  hitTestZone,
  inferGap,
  liveReorderTarget,
  movedEnough,
  planOrder,
  predictZoneLayout,
  rowBands,
  stepTarget,
  type LiveReorderInput,
  type PlanPoint,
} from './dragPlanning';

/** xl content width with the 288 px sidebar and lg:p-8 padding at a 1280 px viewport is 928; use a round 960. */
const XL_WIDTH = 960;

const panel = (id: string, size: PanelSize, height: number): FlowItem => ({ id, size, height });

/** The planner's input for a pointer over a flowed layout, with the hit the collision probe would report. */
function inputFor(flow: FlowResult, order: readonly string[], activeId: string, pointer: PlanPoint): LiveReorderInput {
  return {
    order,
    activeId,
    hitId: hitTestZone(order, flow.rects, flow.zoneRect, pointer),
    pointer,
    rects: flow.rects,
    zoneRect: flow.zoneRect,
  };
}

/** The order the stable rule produces for a pointer over `items` laid out at `breakpoint`. */
function plannedOrder(
  items: FlowItem[],
  breakpoint: FlowBreakpoint,
  activeId: string,
  pointer: PlanPoint,
  width?: number
) {
  const order = items.map(item => item.id);
  const flow = flowGrid(items, breakpoint, width ? { width } : {});
  return planOrder(order, activeId, liveReorderTarget(inputFor(flow, order, activeId, pointer)));
}

describe('constants', () => {
  it('match the spec', () => {
    expect(FULL_ROW_RATIO).toBe(0.95);
    expect(REORDER_HYSTERESIS_PX).toBe(8);
    expect(ROW_TOLERANCE_PX).toBe(2);
    expect(GHOST_MAX).toEqual({ width: 480, height: 280 });
    expect(POSITION_ANNOUNCE_DEBOUNCE_MS).toBe(500);
  });
});

describe('stepTarget — the geometric rule table', () => {
  // One xl row: A third | B third | C third (each 309.33 wide, gap 16).
  const row = flowGrid([panel('a', 'third', 100), panel('b', 'third', 100), panel('c', 'third', 100)], 'xl', {
    width: XL_WIDTH,
  });
  const order = ['a', 'b', 'c'];
  const at = (activeId: string, pointer: PlanPoint) => stepTarget(inputFor(row, order, activeId, pointer));

  it('is null over the active cell itself', () => {
    expect(at('a', { x: 10, y: 10 })).toBeNull();
  });

  it('decides before/after by the horizontal midline of a cell narrower than the zone', () => {
    const c = row.rects.get('c')!;
    expect(at('a', { x: c.left + 10, y: 50 })).toEqual({ kind: 'before', id: 'c' });
    expect(at('a', { x: c.right - 10, y: 50 })).toEqual({ kind: 'after', id: 'c' });
    // Exactly on the midline counts as past it.
    expect(at('a', { x: c.left + c.width / 2, y: 50 })).toEqual({ kind: 'after', id: 'c' });
  });

  it('decides before/after by the vertical midline of a full-width cell', () => {
    const stacked = flowGrid([panel('a', 'third', 80), panel('b', 'full', 200)], 'xl', { width: XL_WIDTH });
    const b = stacked.rects.get('b')!;
    const target = (pointer: PlanPoint) => stepTarget(inputFor(stacked, ['a', 'b'], 'a', pointer));
    // Far right but in the top half: still "before" — a full-width cell is read top to bottom.
    expect(target({ x: b.right - 5, y: b.top + 20 })).toEqual({ kind: 'before', id: 'b' });
    expect(target({ x: b.left + 5, y: b.bottom - 20 })).toEqual({ kind: 'after', id: 'b' });
  });

  it(`switches axis at ${FULL_ROW_RATIO} of the zone width`, () => {
    const zoneRect = rectOf(0, 0, 1000, 400);
    const input = (width: number, pointer: PlanPoint): LiveReorderInput => ({
      order: ['a', 'o'],
      activeId: 'a',
      hitId: 'o',
      pointer,
      rects: new Map([
        ['a', rectOf(0, 0, 100, 100)],
        ['o', rectOf(0, 116, width, 200)],
      ]),
      zoneRect,
    });
    // Left half, bottom half: the axis decides.
    const pointer = { x: 20, y: 300 };
    expect(stepTarget(input(950, pointer))).toEqual({ kind: 'after', id: 'o' });
    expect(stepTarget(input(949, pointer))).toEqual({ kind: 'before', id: 'o' });
  });

  it('a zone hit takes the row band and goes after the last cell left of the pointer', () => {
    // Row 1: A half (h100) | B third (h300); row 2: C half. The pointer under A is in row 1's band.
    const flow = flowGrid([panel('a', 'half', 100), panel('b', 'third', 300), panel('c', 'half', 80)], 'xl', {
      width: XL_WIDTH,
    });
    const pointer = { x: 200, y: 200 };
    expect(hitTestZone(['a', 'b', 'c'], flow.rects, flow.zoneRect, pointer)).toBe(ZONE_HIT);
    expect(stepTarget(inputFor(flow, ['a', 'b', 'c'], 'c', pointer))).toEqual({ kind: 'after', id: 'a' });
    // The row-end hole right of B (row 1) is in row 1's band too: after B.
    const hole = { x: flow.rects.get('b')!.right + 40, y: 50 };
    expect(stepTarget(inputFor(flow, ['a', 'b', 'c'], 'c', hole))).toEqual({ kind: 'after', id: 'b' });
  });

  it('a zone hit left of a row’s first cell goes before it', () => {
    const zoneRect = rectOf(0, 0, 600, 200);
    const input: LiveReorderInput = {
      order: ['a', 'b'],
      activeId: 'b',
      hitId: ZONE_HIT,
      pointer: { x: 5, y: 50 },
      rects: new Map([
        ['a', rectOf(20, 0, 200, 100)],
        ['b', rectOf(20, 116, 200, 80)],
      ]),
      zoneRect,
    };
    expect(stepTarget(input)).toEqual({ kind: 'before', id: 'a' });
  });

  it('a zone hit whose row cell is the active one is null', () => {
    const flow = flowGrid([panel('a', 'third', 100), panel('b', 'third', 60)], 'xl', { width: XL_WIDTH });
    // Below B (shorter than A) in row 1's band: L = B = active.
    const pointer = { x: flow.rects.get('b')!.left + 20, y: 90 };
    expect(hitTestZone(['a', 'b'], flow.rects, flow.zoneRect, pointer)).toBe(ZONE_HIT);
    expect(stepTarget(inputFor(flow, ['a', 'b'], 'b', pointer))).toBeNull();
  });

  it('a zone hit above the first row, or below the zone, is null', () => {
    const rects = new Map([
      ['a', rectOf(0, 10, 200, 100)],
      ['b', rectOf(216, 10, 200, 100)],
    ]);
    const base = { order: ['a', 'b'], activeId: 'a', hitId: ZONE_HIT, rects, zoneRect: rectOf(0, 0, 600, 110) };
    expect(stepTarget({ ...base, pointer: { x: 300, y: 5 } })).toBeNull();
    expect(stepTarget({ ...base, pointer: { x: 300, y: 115 } })).toBeNull();
    expect(stepTarget({ ...base, pointer: { x: 300, y: 50 } })).toEqual({ kind: 'after', id: 'b' });
  });

  it('only cells of the active zone count (zone filter); no hit or no pointer is null', () => {
    const c = row.rects.get('c')!;
    const pointer = { x: c.left + 10, y: 50 };
    expect(stepTarget({ ...inputFor(row, order, 'a', pointer), hitId: 'kpi.overdue' })).toBeNull();
    expect(stepTarget({ ...inputFor(row, ['a', 'b'], 'a', pointer), hitId: 'c' })).toBeNull();
    expect(stepTarget({ ...inputFor(row, order, 'a', pointer), hitId: null })).toBeNull();
    expect(stepTarget({ ...inputFor(row, order, 'a', pointer), pointer: null })).toBeNull();
    // An active id that is not in this zone's order never plans anything.
    expect(stepTarget(inputFor(row, order, 'kpi.overdue', pointer))).toBeNull();
  });

  it('agrees with the stable rule wherever a move cannot re-wrap a row', () => {
    const c = row.rects.get('c')!;
    const input = inputFor(row, order, 'a', { x: c.right - 10, y: 50 });
    expect(liveReorderTarget(input)).toEqual(stepTarget(input));
  });
});

describe('hitTestZone', () => {
  it('prefers a cell, then the zone (half-open edges), else null', () => {
    const rects = new Map([['a', rectOf(0, 0, 100, 100)]]);
    const zone = rectOf(0, 0, 300, 100);
    expect(hitTestZone(['a'], rects, zone, { x: 0, y: 0 })).toBe('a');
    expect(hitTestZone(['a'], rects, zone, { x: 99.5, y: 99.5 })).toBe('a');
    expect(hitTestZone(['a'], rects, zone, { x: 100, y: 50 })).toBe(ZONE_HIT);
    expect(hitTestZone(['a'], rects, zone, { x: 300, y: 50 })).toBeNull();
    expect(hitTestZone(['a'], rects, zone, { x: 50, y: 100 })).toBeNull();
    expect(hitTestZone(['b'], rects, zone, { x: 50, y: 50 })).toBe(ZONE_HIT);
    expect(hitTestZone(['a'], rects, zone, null)).toBeNull();
  });
});

describe('rowBands', () => {
  it('groups by top within the tolerance, sorts each row by left and spans bands to the next row', () => {
    const bands = rowBands(
      [
        { id: 'c', rect: rectOf(0, 120, 100, 50) },
        { id: 'b', rect: rectOf(300, 1.5, 100, 40) },
        { id: 'a', rect: rectOf(0, 0, 100, 100) },
        { id: 'd', rect: rectOf(200, 121.5, 100, 50) },
      ],
      300
    );
    expect(bands.map(band => [band.top, band.bottom, band.cells.map(cell => cell.id)])).toEqual([
      [0, 120, ['a', 'b']],
      [120, 300, ['c', 'd']],
    ]);
    // Beyond the tolerance is a new row.
    expect(
      rowBands(
        [
          { id: 'a', rect: rectOf(0, 0, 100, 100) },
          { id: 'b', rect: rectOf(200, ROW_TOLERANCE_PX + 0.5, 100, 100) },
        ],
        200
      ).map(band => band.cells.map(cell => cell.id))
    ).toEqual([['a'], ['b']]);
    expect(rowBands([], 100)).toEqual([]);
  });
});

describe('planOrder', () => {
  const order = ['a', 'b', 'c', 'd'];

  it('moves the active id before or after the target', () => {
    expect(planOrder(order, 'a', { kind: 'after', id: 'c' })).toEqual(['b', 'c', 'a', 'd']);
    expect(planOrder(order, 'd', { kind: 'before', id: 'b' })).toEqual(['a', 'd', 'b', 'c']);
    expect(planOrder(order, 'b', { kind: 'after', id: 'd' })).toEqual(['a', 'c', 'd', 'b']);
    expect(planOrder(order, 'c', { kind: 'before', id: 'a' })).toEqual(['c', 'a', 'b', 'd']);
  });

  it('returns the same array when nothing changes', () => {
    expect(planOrder(order, 'a', null)).toBe(order);
    expect(planOrder(order, 'a', { kind: 'before', id: 'b' })).toBe(order);
    expect(planOrder(order, 'b', { kind: 'after', id: 'a' })).toBe(order);
    expect(planOrder(order, 'b', { kind: 'after', id: 'b' })).toBe(order);
    expect(planOrder(order, 'x', { kind: 'after', id: 'a' })).toBe(order);
    expect(planOrder(order, 'a', { kind: 'after', id: 'x' })).toBe(order);
  });
});

describe('movedEnough', () => {
  it('always allows the first reorder, then needs the hysteresis distance', () => {
    expect(movedEnough(null, { x: 0, y: 0 })).toBe(true);
    expect(movedEnough({ x: 0, y: 0 }, { x: 5, y: 6 })).toBe(false);
    expect(movedEnough({ x: 0, y: 0 }, { x: 0, y: 7.99 })).toBe(false);
    expect(movedEnough({ x: 0, y: 0 }, { x: 8, y: 0 })).toBe(true);
    expect(movedEnough({ x: 10, y: 10 }, { x: 16, y: 18 })).toBe(true);
    expect(movedEnough({ x: 0, y: 0 }, { x: 3, y: 0 }, 2)).toBe(true);
  });
});

describe('ghostBox', () => {
  it('caps a large cell and keeps the grab point at the same fraction of the ghost', () => {
    const box = ghostBox({ width: 800, height: 600 }, { x: 400, y: 150 });
    expect(box).toEqual({ width: 480, height: 280, left: 160, top: 80 });
    // The grab point sits at the same fraction of the ghost as of the cell.
    expect((400 - box.left) / box.width).toBeCloseTo(400 / 800);
    expect((150 - box.top) / box.height).toBeCloseTo(150 / 600);
  });

  it('keeps a small cell at full size with no offset', () => {
    expect(ghostBox({ width: 200, height: 72 }, { x: 150, y: 30 })).toEqual({
      width: 200,
      height: 72,
      left: 0,
      top: 0,
    });
  });

  it('copes with an empty cell', () => {
    expect(ghostBox({ width: 0, height: 0 }, { x: 0, y: 0 })).toEqual({ width: 0, height: 0, left: 0, top: 0 });
  });
});

describe('breakpointOf', () => {
  it('maps viewport widths to the Tailwind breakpoints', () => {
    const cases: [number, string][] = [
      [320, 'base'],
      [639, 'base'],
      [640, 'sm'],
      [767, 'sm'],
      [768, 'md'],
      [1023, 'md'],
      [1024, 'lg'],
      [1279, 'lg'],
      [1280, 'xl'],
      [2560, 'xl'],
    ];
    cases.forEach(([width, breakpoint]) => expect([width, breakpointOf(width)]).toEqual([width, breakpoint]));
  });
});

describe('inferGap / predictZoneLayout', () => {
  it('reads the gap from a row, else from the rows, else 0', () => {
    const oneRow = flowGrid([panel('a', 'third', 100), panel('b', 'half', 50)], 'xl', { width: XL_WIDTH });
    const cellsOf = (flow: FlowResult) => Array.from(flow.rects.entries()).map(([id, rect]) => ({ id, rect }));
    expect(inferGap(cellsOf(oneRow), oneRow.zoneRect.bottom)).toBeCloseTo(16);
    const column = flowGrid([panel('a', 'third', 100), panel('b', 'half', 50)], 'base', { gap: 12 });
    expect(inferGap(cellsOf(column), column.zoneRect.bottom)).toBeCloseTo(12);
    const kpis = flowGrid(
      [
        { id: 'a', height: 72 },
        { id: 'b', height: 72 },
      ],
      { kpiColumns: 5 }
    );
    expect(inferGap(cellsOf(kpis), kpis.zoneRect.bottom)).toBeCloseTo(8);
    expect(inferGap([{ id: 'a', rect: rectOf(0, 0, 10, 10) }], 10)).toBe(0);
  });

  it('predicts exactly what the grid simulator lays out, for every permutation', () => {
    const layouts: [FlowItem[], FlowBreakpoint][] = [
      [[panel('a', 'wide', 120), panel('b', 'half', 300), panel('c', 'third', 60), panel('d', 'full', 90)], 'xl'],
      [[panel('a', 'two_thirds', 120), panel('b', 'narrow', 300), panel('c', 'third', 60)], 'lg'],
      [[panel('a', 'full', 40), panel('b', 'third', 300), panel('c', 'half', 60)], 'base'],
      [['a', 'b', 'c', 'd', 'e', 'f', 'g'].map(id => ({ id, height: 72 })), { kpiColumns: 3 }],
    ];
    for (const [items, breakpoint] of layouts) {
      const start = flowGrid(items, breakpoint, breakpoint === 'xl' ? { width: XL_WIDTH } : {});
      const ids = items.map(item => item.id);
      const cells = ids.map(id => ({ id, rect: start.rects.get(id)! }));
      const gap = inferGap(cells, start.zoneRect.bottom);
      const byId = new Map(items.map(item => [item.id, item] as const));
      for (const order of [ids, [...ids].reverse(), [ids[1], ids[0], ...ids.slice(2)], [...ids.slice(1), ids[0]]]) {
        const real = flowGrid(
          order.map(id => byId.get(id)!),
          breakpoint,
          breakpoint === 'xl' ? { width: XL_WIDTH } : {}
        );
        const predicted = predictZoneLayout(order, start.rects, start.zoneRect, gap);
        for (const id of order) {
          const a = real.rects.get(id)!;
          const b = predicted.rects.get(id)!;
          expect([id, b.left, b.top, b.width, b.height]).toEqual([
            id,
            expect.closeTo(a.left, 6),
            expect.closeTo(a.top, 6),
            expect.closeTo(a.width, 6),
            expect.closeTo(a.height, 6),
          ]);
        }
        expect(predicted.zoneRect.height).toBeCloseTo(real.zoneRect.height, 6);
      }
    }
  });
});

describe('worked fixtures (spec §5.3)', () => {
  it('xl [A third, B half, C full]: A dragged over C’s bottom half lands after C, and stays', () => {
    const items = [panel('a', 'third', 160), panel('b', 'half', 96), panel('c', 'full', 200)];
    const flow = flowGrid(items, 'xl', { width: XL_WIDTH });
    const c = flow.rects.get('c')!;
    const pointer = { x: 500, y: c.bottom - 30 };
    expect(plannedOrder(items, 'xl', 'a', pointer, XL_WIDTH)).toEqual(['b', 'c', 'a']);
    // C moved up; at the same pointer the slot is under it (or in its band): a fixed point.
    const moved = ['b', 'c', 'a'];
    const after = flowGrid(
      moved.map(id => items.find(item => item.id === id)!),
      'xl',
      { width: XL_WIDTH }
    );
    expect(planOrder(moved, 'a', liveReorderTarget(inputFor(after, moved, 'a', pointer)))).toBe(moved);
  });

  it('xl [A half, B half] / [C full]: C dragged to A’s right half (or into row 1’s hole) lands after A, and stays', () => {
    const items = [panel('a', 'half', 120), panel('b', 'half', 120), panel('c', 'full', 100)];
    for (const pointer of [
      { x: 400, y: 60 },
      { x: 700, y: 60 },
    ]) {
      expect(plannedOrder(items, 'xl', 'c', pointer, XL_WIDTH)).toEqual(['a', 'c', 'b']);
      const moved = ['a', 'c', 'b'];
      const after = flowGrid(
        moved.map(id => items.find(item => item.id === id)!),
        'xl',
        { width: XL_WIDTH }
      );
      // C cannot fit on row 1 and wraps; the pointer is over A's right half or in row 1's hole: "after A" again.
      expect(stepTarget(inputFor(after, moved, 'c', pointer))).toEqual({ kind: 'after', id: 'a' });
      expect(planOrder(moved, 'c', liveReorderTarget(inputFor(after, moved, 'c', pointer)))).toBe(moved);
    }
  });

  it('base, one column: short B dragged into tall A’s top half lands first, and stays', () => {
    const items = [panel('a', 'half', 300), panel('b', 'third', 80)];
    const pointer = { x: 100, y: 100 };
    expect(plannedOrder(items, 'base', 'b', pointer)).toEqual(['b', 'a']);
    const moved = ['b', 'a'];
    const after = flowGrid([items[1], items[0]], 'base');
    expect(stepTarget(inputFor(after, moved, 'b', pointer))).toEqual({ kind: 'before', id: 'a' });
    expect(planOrder(moved, 'b', liveReorderTarget(inputFor(after, moved, 'b', pointer)))).toBe(moved);
  });
});

describe('counter-examples the property loops found against the bare geometric rule', () => {
  it('ping-pong: xl [A narrow, B wide] / [C third] — C at B’s lower-left stays put instead of flip-flopping', () => {
    const items = [panel('a', 'narrow', 96), panel('b', 'wide', 240), panel('c', 'third', 48)];
    const order = ['a', 'b', 'c'];
    const flow = flowGrid(items, 'xl', { width: XL_WIDTH });
    const pointer = { x: 500, y: 200 };
    // The geometric rule says "before B" ...
    expect(stepTarget(inputFor(flow, order, 'c', pointer))).toEqual({ kind: 'before', id: 'b' });
    // ... but then B wraps under the pointer, which is past B's midline: "after B", and back again.
    const moved = ['a', 'c', 'b'];
    const after = flowGrid([items[0], items[2], items[1]], 'xl', { width: XL_WIDTH });
    expect(stepTarget(inputFor(after, moved, 'c', pointer))).toEqual({ kind: 'after', id: 'b' });
    // The stable rule refuses the cycle, from either side.
    expect(liveReorderTarget(inputFor(flow, order, 'c', pointer))).toBeNull();
    expect(planOrder(moved, 'c', liveReorderTarget(inputFor(after, moved, 'c', pointer)))).toBe(moved);
    // Higher up B's left half the move is stable, so it happens.
    expect(plannedOrder(items, 'xl', 'c', { x: 500, y: 50 }, XL_WIDTH)).toEqual(['a', 'c', 'b']);
  });

  it('drift: xl [A third] / [B full] / [C narrow] — A at the far end of B’s bottom half lands after C in one move', () => {
    const items = [panel('a', 'third', 320), panel('b', 'full', 96), panel('c', 'narrow', 240)];
    const order = ['a', 'b', 'c'];
    const flow = flowGrid(items, 'xl', { width: XL_WIDTH });
    const pointer = { x: 900, y: 420 };
    // The geometric rule says "after B", which leaves the pointer in the hole right of C: "after C" next.
    expect(stepTarget(inputFor(flow, order, 'a', pointer))).toEqual({ kind: 'after', id: 'b' });
    const result = liveReorderTarget(inputFor(flow, order, 'a', pointer));
    expect(result).toEqual({ kind: 'after', id: 'c' });
    const moved = planOrder(order, 'a', result);
    expect(moved).toEqual(['b', 'c', 'a']);
    const after = flowGrid([items[1], items[2], items[0]], 'xl', { width: XL_WIDTH });
    expect(planOrder(moved, 'a', liveReorderTarget(inputFor(after, moved, 'a', pointer)))).toBe(moved);
  });
});

// ── Property loops ───────────────────────────────────────────────────────────

/** mulberry32: a tiny seeded PRNG, so the "varied" heights are the same on every run. */
function seededRandom(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const HEIGHTS = [48, 96, 160, 240, 320];
const LATTICE_PX = 10;
const IDS = ['a', 'b', 'c', 'd', 'e', 'f', 'g'];
/** Offsets under the hysteresis distance (all < 8 px) and one at it. */
const NEAR: PlanPoint[] = [
  { x: 7, y: 0 },
  { x: -7, y: 0 },
  { x: 0, y: 7 },
  { x: 0, y: -7 },
  { x: 5, y: 5 },
  { x: -5, y: -5 },
];

interface PropertyFailure {
  layout: string;
  activeId: string;
  pointer: PlanPoint;
  orders: string[];
}

/**
 * For every active widget and every lattice pointer over the zone: plan; if the plan moves,
 * re-flow with the independent simulator and re-plan at the SAME pointer. (a) The second
 * plan must change nothing. (b) Any pointer under the hysteresis distance from the reorder
 * must be refused by `movedEnough`.
 */
function checkLayout(items: FlowItem[], breakpoint: FlowBreakpoint, failures: PropertyFailure[]): number {
  const geometry = breakpoint === 'xl' ? { width: XL_WIDTH } : {};
  const byId = new Map(items.map(item => [item.id, item] as const));
  const flows = new Map<string, FlowResult>();
  const flowOf = (order: readonly string[]) => {
    const key = order.join(',');
    let flow = flows.get(key);
    if (!flow) {
      flow = flowGrid(
        order.map(id => byId.get(id)!),
        breakpoint,
        geometry
      );
      flows.set(key, flow);
    }
    return flow;
  };
  const order = items.map(item => item.id);
  const flow = flowOf(order);
  const label = `${typeof breakpoint === 'string' ? breakpoint : `kpi×${breakpoint.kpiColumns}`} ${items
    .map(item => `${item.id}:${item.size ?? 'tile'}:${item.height}`)
    .join(' ')}`;
  let reorders = 0;
  for (const activeId of order) {
    for (let y = flow.zoneRect.top + LATTICE_PX / 2; y < flow.zoneRect.bottom; y += LATTICE_PX) {
      for (let x = flow.zoneRect.left + LATTICE_PX / 2; x < flow.zoneRect.right; x += LATTICE_PX) {
        const pointer = { x, y };
        const next = planOrder(order, activeId, liveReorderTarget(inputFor(flow, order, activeId, pointer)));
        if (next === order) continue;
        reorders += 1;
        const reflowed = flowOf(next);
        const again = planOrder(next, activeId, liveReorderTarget(inputFor(reflowed, next, activeId, pointer)));
        if (again !== next) {
          failures.push({ layout: label, activeId, pointer, orders: [order.join(''), next.join(''), again.join('')] });
        }
        if (NEAR.some(offset => movedEnough(pointer, { x: x + offset.x, y: y + offset.y }))) {
          failures.push({ layout: `${label} (hysteresis)`, activeId, pointer, orders: [] });
        }
      }
    }
  }
  return reorders;
}

describe('property: a stationary pointer is always at a fixed point', () => {
  const random = seededRandom(326);
  const panelLayouts: FlowItem[][] = [];
  for (const first of PANEL_SIZES) {
    for (const second of PANEL_SIZES) {
      const pick = () => HEIGHTS[Math.floor(random() * HEIGHTS.length)];
      panelLayouts.push([panel('a', first, pick()), panel('b', second, pick())]);
      for (const third of PANEL_SIZES) {
        panelLayouts.push([panel('a', first, pick()), panel('b', second, pick()), panel('c', third, pick())]);
      }
    }
  }

  const breakpoints: PanelBreakpoint[] = ['xl', 'lg', 'base'];
  describe.each(breakpoints)('panel grid at %s', breakpoint => {
    it.each<[PanelSize]>(PANEL_SIZES.map(size => [size]))('every pair and triple starting with %s', firstSize => {
      const failures: PropertyFailure[] = [];
      let reorders = 0;
      for (const items of panelLayouts) {
        if (items[0].size === firstSize) reorders += checkLayout(items, breakpoint, failures);
      }
      // The loops exercised real moves, not just "stay put".
      expect(reorders).toBeGreaterThan(0);
      expect(failures.slice(0, 5)).toEqual([]);
    });
  });

  it.each<[KpiColumns]>([[5], [3], [2]])('KPI strip at %i columns, 2 to 7 tiles', columns => {
    const failures: PropertyFailure[] = [];
    let reorders = 0;
    for (let count = 2; count <= IDS.length; count += 1) {
      const tiles = IDS.slice(0, count).map(id => ({ id, height: 72 }));
      reorders += checkLayout(tiles, { kpiColumns: columns }, failures);
    }
    expect(reorders).toBeGreaterThan(0);
    expect(failures.slice(0, 5)).toEqual([]);
  });
});
