/**
 * Dashboard edit mode: where a dragged widget should land. Pure — no DOM, no React, no
 * dnd-kit — so every rule here is unit-tested against synthetic rects.
 *
 * The page RENDERS the live order (the real CSS grid places the landing slot); the planner
 * reads the rects the grid produced and decides the next order in two layers:
 *
 * 1. `stepTarget` — the geometric rule: midline of the cell under the pointer, or the
 *    row-band rule for a gutter / row-end hole (see its docstring).
 * 2. `liveReorderTarget` — the STABLE rule. With variable spans a move can re-wrap the
 *    rows so that, at the very same pointer, the geometric rule then asks for a different
 *    order (ping-pong A→B→A, or a drift A→B→C). So the candidate is vetted first: the
 *    zone's reflow is predicted from the measured cell sizes (`predictZoneLayout` — the
 *    grid's sparse row-major flow, in px, with no span table) and the geometric rule is
 *    re-applied at the same pointer until it settles. A chain that settles returns its
 *    final position as ONE target; a chain that cycles returns null (stay put). Result: a
 *    stationary pointer is always at a fixed point — pinned by the property loops in
 *    `dragPlanning.test.ts` against an independent CSS-grid simulator.
 */

/** A rect as the planner reads it (dnd-kit's `ClientRect` and `DOMRect` both satisfy it). */
export interface PlanRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

export interface PlanPoint {
  x: number;
  y: number;
}

/** Look up a cell's rect by widget id (a `Map` satisfies it, including dnd-kit's `droppableRects`). */
export interface RectLookup {
  get(id: string): PlanRect | undefined;
}

export interface ReorderTarget {
  kind: 'before' | 'after';
  id: string;
}

/**
 * The hit id the collision probe reports when the pointer is inside the zone container but
 * over no cell (a gutter, a row-end hole, or below a short item). It contains `:`, which a
 * widget id can never contain (`^[a-z][a-z0-9_.]{0,63}$`), so it cannot collide with one.
 */
export const ZONE_HIT = 'zone:*';

/** A cell at least this fraction of the zone's width is a full-width row: decide by its vertical midline. */
export const FULL_ROW_RATIO = 0.95;
/** After a live reorder, the pointer must move this far before another reorder may happen. */
export const REORDER_HYSTERESIS_PX = 8;
/** Cells whose tops differ by at most this much are on the same grid row. */
export const ROW_TOLERANCE_PX = 2;
/** The drag ghost is the cell's size, capped at this. */
export const GHOST_MAX = { width: 480, height: 280 } as const;
/** Trailing debounce for the "{title}, position {j} of {m}." live-reorder announcement. */
export const POSITION_ANNOUNCE_DEBOUNCE_MS = 500;

export interface PlanCell {
  id: string;
  rect: PlanRect;
}

export interface RowBand {
  /** The row's top (its first cell's top). */
  top: number;
  /** Exclusive: the next row's top, or the zone's bottom for the last row. */
  bottom: number;
  /** The row's cells, left to right. */
  cells: PlanCell[];
}

/**
 * Group cells into grid rows by `top` (±ROW_TOLERANCE_PX), top to bottom, each row's cells
 * left to right. A row's band runs from its top to the next row's top; the last row's band
 * runs to `zoneBottom`.
 */
export function rowBands(cells: readonly PlanCell[], zoneBottom: number): RowBand[] {
  const byTop = [...cells].sort((a, b) => a.rect.top - b.rect.top || a.rect.left - b.rect.left);
  const rows: PlanCell[][] = [];
  for (const cell of byTop) {
    const row = rows[rows.length - 1];
    if (row && Math.abs(cell.rect.top - row[0].rect.top) <= ROW_TOLERANCE_PX) row.push(cell);
    else rows.push([cell]);
  }
  return rows.map((row, index) => ({
    top: row[0].rect.top,
    bottom: index + 1 < rows.length ? rows[index + 1][0].rect.top : zoneBottom,
    cells: [...row].sort((a, b) => a.rect.left - b.rect.left),
  }));
}

export interface LiveReorderInput {
  /** The zone's live order (the session order on screen). */
  order: readonly string[];
  activeId: string;
  /** A cell id from the collision probe (`hitTestZone`), `ZONE_HIT`, or null (outside the zone). */
  hitId: string | null;
  pointer: PlanPoint | null;
  /** Live rects of the zone's cells, keyed by widget id. */
  rects: RectLookup;
  zoneRect: PlanRect;
}

/**
 * The collision probe's hit for a pointer: the zone cell containing it (half-open rects:
 * left/top inclusive, right/bottom exclusive), else `ZONE_HIT` inside the zone container,
 * else null. The planner uses this same test on predicted layouts, so the collision
 * detection should use it too.
 */
export function hitTestZone(
  order: readonly string[],
  rects: RectLookup,
  zoneRect: PlanRect,
  pointer: PlanPoint | null
): string | null {
  if (!pointer) return null;
  const inside = (rect: PlanRect) =>
    pointer.x >= rect.left &&
    pointer.x < rect.left + rect.width &&
    pointer.y >= rect.top &&
    pointer.y < rect.top + rect.height;
  for (const id of order) {
    const rect = rects.get(id);
    if (rect && inside(rect)) return id;
  }
  return inside(zoneRect) ? ZONE_HIT : null;
}

/**
 * The geometric rule, for one layout (no stability vetting — see `liveReorderTarget`).
 *
 * - Over the active cell itself: null.
 * - Over another cell O: before/after O by which side of O's midline the pointer is on —
 *   the vertical midline when O spans the zone (width ≥ FULL_ROW_RATIO × zone width), else
 *   the horizontal one.
 * - `ZONE_HIT`: the row band the pointer is in; after the last cell of that row whose left
 *   edge is at or left of the pointer (before the row's first cell when there is none);
 *   null when that cell is the active one or the pointer is above the first row.
 * - Anything else (null, a cell of another zone): null.
 */
export function stepTarget({
  order,
  activeId,
  hitId,
  pointer,
  rects,
  zoneRect,
}: LiveReorderInput): ReorderTarget | null {
  if (!pointer || hitId === null || hitId === activeId || !order.includes(activeId)) return null;

  if (hitId !== ZONE_HIT) {
    if (!order.includes(hitId)) return null;
    const rect = rects.get(hitId);
    if (!rect) return null;
    const vertical = rect.width >= FULL_ROW_RATIO * zoneRect.width;
    const before = vertical ? pointer.y < rect.top + rect.height / 2 : pointer.x < rect.left + rect.width / 2;
    return { kind: before ? 'before' : 'after', id: hitId };
  }

  const cells: PlanCell[] = [];
  for (const id of order) {
    const rect = rects.get(id);
    if (rect) cells.push({ id, rect });
  }
  const bands = rowBands(cells, zoneRect.top + zoneRect.height);
  if (bands.length === 0 || pointer.y < bands[0].top) return null;
  const band = bands.find(row => pointer.y >= row.top && pointer.y < row.bottom);
  if (!band) return null;
  let last: PlanCell | null = null;
  for (const cell of band.cells) {
    if (cell.rect.left <= pointer.x) last = cell;
  }
  if (!last) {
    const first = band.cells[0];
    return first.id === activeId ? null : { kind: 'before', id: first.id };
  }
  return last.id === activeId ? null : { kind: 'after', id: last.id };
}

/** Sub-pixel slack for "does the next cell still fit on this row" (browsers snap layout to device pixels). */
const FIT_EPSILON_PX = 0.5;

/**
 * The grid's gap, read off the measured layout: the space between two cells of one row, or
 * between a row's tallest cell and the next row. Both dashboard grids use one gap for rows
 * and columns. 0 when there is a single cell (no reorder is possible then anyway).
 */
export function inferGap(cells: readonly PlanCell[], zoneBottom: number): number {
  const bands = rowBands(cells, zoneBottom);
  for (const band of bands) {
    if (band.cells.length > 1) {
      const [first, second] = band.cells;
      return Math.max(0, second.rect.left - (first.rect.left + first.rect.width));
    }
  }
  if (bands.length > 1) {
    const rowHeight = Math.max(...bands[0].cells.map(cell => cell.rect.height));
    return Math.max(0, bands[1].top - (bands[0].top + rowHeight));
  }
  return 0;
}

export interface PredictedLayout {
  rects: Map<string, PlanRect>;
  zoneRect: PlanRect;
}

/**
 * Where the browser's grid will put the zone's cells for `order`: sparse row-major
 * auto-placement of one-row items, in px. A cell keeps its measured size (its span — hence
 * its width, hence its height — does not depend on its position), goes on the current row
 * when it fits in what is left of it and starts the next row otherwise; a row is as tall as
 * its tallest cell. For column-track grids this is exactly CSS sparse placement (a span-s
 * cell fits iff the spans so far plus s ≤ the column count), with no span table.
 */
export function predictZoneLayout(
  order: readonly string[],
  sizes: RectLookup,
  zoneRect: PlanRect,
  gap: number
): PredictedLayout {
  const rects = new Map<string, PlanRect>();
  const right = zoneRect.left + zoneRect.width;
  let x = zoneRect.left;
  let rowTop = zoneRect.top;
  let rowHeight = 0;
  let placedOnRow = 0;
  for (const id of order) {
    const size = sizes.get(id);
    if (!size) continue;
    if (placedOnRow > 0 && x + size.width > right + FIT_EPSILON_PX) {
      rowTop += rowHeight + gap;
      x = zoneRect.left;
      rowHeight = 0;
      placedOnRow = 0;
    }
    rects.set(id, { left: x, top: rowTop, width: size.width, height: size.height });
    x += size.width + gap;
    rowHeight = Math.max(rowHeight, size.height);
    placedOnRow += 1;
  }
  const height = rects.size === 0 ? 0 : rowTop + rowHeight - zoneRect.top;
  return { rects, zoneRect: { left: zoneRect.left, top: zoneRect.top, width: zoneRect.width, height } };
}

/** The target that puts `activeId` where it is in `final` (after its predecessor, else before its successor). */
function targetFor(final: readonly string[], activeId: string): ReorderTarget | null {
  const index = final.indexOf(activeId);
  if (index > 0) return { kind: 'after', id: final[index - 1] };
  if (index === 0 && final.length > 1) return { kind: 'before', id: final[1] };
  return null;
}

/**
 * Where the active widget should go for the pointer, or null to stay put — the STABLE rule.
 *
 * Takes `stepTarget`'s answer for the live layout, then predicts the reflow and re-applies
 * `stepTarget` at the same pointer until the order stops changing:
 * - it settles → the settled position, as one target (a two-step drift lands in one move);
 * - it revisits an order (a ping-pong between two wraps) → null: the slot stays where it is
 *   until the pointer reaches a spot that is stable after the move;
 * - the geometric answer changes nothing → that answer (a no-op for `planOrder`).
 */
export function liveReorderTarget(input: LiveReorderInput): ReorderTarget | null {
  const { order, activeId, pointer, rects, zoneRect } = input;
  const first = stepTarget(input);
  if (!first || !pointer) return first;
  const firstOrder = planOrder(order, activeId, first);
  if (firstOrder === order) return first;
  let current = firstOrder;

  const cells: PlanCell[] = [];
  for (const id of order) {
    const rect = rects.get(id);
    if (!rect) return null;
    cells.push({ id, rect });
  }
  const sizes = new Map(cells.map(cell => [cell.id, cell.rect] as const));
  const gap = inferGap(cells, zoneRect.top + zoneRect.height);
  const visited = new Set<string>([order.join('\n'), firstOrder.join('\n')]);
  for (let step = 0; step <= order.length; step += 1) {
    const predicted = predictZoneLayout(current, sizes, zoneRect, gap);
    const target = stepTarget({
      order: current,
      activeId,
      hitId: hitTestZone(current, predicted.rects, predicted.zoneRect, pointer),
      pointer,
      rects: predicted.rects,
      zoneRect: predicted.zoneRect,
    });
    const next = planOrder(current, activeId, target);
    if (next === current) return current === firstOrder ? first : targetFor(current, activeId);
    const key = next.join('\n');
    if (visited.has(key)) return null;
    visited.add(key);
    current = next;
  }
  return null;
}

/**
 * The order with the active widget moved before/after the target. Returns the SAME array
 * when the result equals `order` (or there is nothing to do), so a caller can skip the
 * dispatch by reference.
 */
export function planOrder(order: readonly string[], activeId: string, target: ReorderTarget | null): readonly string[] {
  if (!target || target.id === activeId || !order.includes(activeId) || !order.includes(target.id)) return order;
  const rest = order.filter(id => id !== activeId);
  const at = rest.indexOf(target.id) + (target.kind === 'after' ? 1 : 0);
  const next = [...rest.slice(0, at), activeId, ...rest.slice(at)];
  return next.every((id, index) => id === order[index]) ? order : next;
}

/** True when the pointer has moved at least `threshold` px from where the last reorder happened (always, before the first). */
export function movedEnough(
  last: PlanPoint | null,
  pointer: PlanPoint,
  threshold: number = REORDER_HYSTERESIS_PX
): boolean {
  if (!last) return true;
  const dx = pointer.x - last.x;
  const dy = pointer.y - last.y;
  return Math.sqrt(dx * dx + dy * dy) >= threshold;
}

export interface GhostBox {
  width: number;
  height: number;
  /** Offset inside the cell-sized overlay wrapper. */
  left: number;
  top: number;
}

/**
 * The drag ghost inside the cell-sized overlay: the cell's size capped at GHOST_MAX, placed
 * so the grab point (relative to the cell) keeps the same fractional position inside the
 * ghost as it had inside the cell.
 */
export function ghostBox(cell: { width: number; height: number }, grab: PlanPoint): GhostBox {
  const width = Math.max(0, Math.min(cell.width, GHOST_MAX.width));
  const height = Math.max(0, Math.min(cell.height, GHOST_MAX.height));
  const left = cell.width > 0 ? grab.x * (1 - width / cell.width) : 0;
  const top = cell.height > 0 ? grab.y * (1 - height / cell.height) : 0;
  return { width, height, left, top };
}

/** Tailwind's breakpoints as the dashboard grids use them (sm 640 / md 768 / lg 1024 / xl 1280). */
export type Breakpoint = 'base' | 'sm' | 'md' | 'lg' | 'xl';

export function breakpointOf(viewportWidth: number): Breakpoint {
  if (viewportWidth >= 1280) return 'xl';
  if (viewportWidth >= 1024) return 'lg';
  if (viewportWidth >= 768) return 'md';
  if (viewportWidth >= 640) return 'sm';
  return 'base';
}
