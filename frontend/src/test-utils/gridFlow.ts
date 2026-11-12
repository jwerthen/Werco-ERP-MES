/**
 * TEST-ONLY: a CSS-grid sparse auto-placement simulator for the dashboard's two grids, so
 * the drag planner's pure property tests can produce real-shaped rects for any order,
 * spans and heights. Production code has no span table — it renders the order and reads
 * the rects the browser produced (`components/dashboard/editor/dragPlanning.ts`).
 *
 * What it models (and nothing more):
 * - the panel grid `grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start` with
 *   `PANEL_SIZE_CLASSES` spans (held to them by `gridFlow.test.ts`);
 * - the KPI strip `grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2` (span 1, stretched);
 * - sparse auto-placement of one-row items: an item that does not fit in what is left of
 *   the current row starts the next row at column 1 (no back-filling — `dense` is off);
 * - each row is as tall as its tallest item; `align: 'start'` keeps each item its own
 *   height, `'stretch'` makes it the row's height.
 */
import type { PanelSize } from '../components/dashboard/widgetTypes';

/** Each panel size's column span: of 12 at xl, of 2 at lg (1 at base). */
export const GRID_SPANS: Record<PanelSize, { xl: number; lg: 1 | 2 }> = {
  third: { xl: 4, lg: 1 },
  narrow: { xl: 5, lg: 1 },
  half: { xl: 6, lg: 1 },
  wide: { xl: 7, lg: 1 },
  two_thirds: { xl: 8, lg: 2 },
  full: { xl: 12, lg: 2 },
};

/** The panel grid's three layouts. */
export type PanelBreakpoint = 'xl' | 'lg' | 'base';
/** The KPI strip's column counts (lg / sm / base). */
export type KpiColumns = 5 | 3 | 2;
export type FlowBreakpoint = PanelBreakpoint | { kpiColumns: KpiColumns };

export interface FlowItem {
  id: string;
  /** Panels only; a KPI tile (no size) always spans one column. */
  size?: PanelSize;
  height: number;
}

export interface FlowRect {
  top: number;
  left: number;
  width: number;
  height: number;
  right: number;
  bottom: number;
}

export interface FlowGeometry {
  /** The grid container's width. Defaults: xl 1200, lg 720, base 360. */
  width: number;
  /** Row and column gap. Defaults: 16 (panel grid, `gap-4`), 8 (KPI strip, `gap-2`). */
  gap: number;
  left: number;
  top: number;
  /** Defaults: 'start' (panel grid), 'stretch' (KPI strip). */
  align: 'start' | 'stretch';
}

export interface FlowResult {
  rects: Map<string, FlowRect>;
  /** The grid container's rect (its height is the content's). */
  zoneRect: FlowRect;
  /** Ids per row, top to bottom. */
  rows: string[][];
  columns: number;
}

const DEFAULT_WIDTH: Record<PanelBreakpoint, number> = { xl: 1200, lg: 720, base: 360 };

export function rectOf(left: number, top: number, width: number, height: number): FlowRect {
  return { left, top, width, height, right: left + width, bottom: top + height };
}

function columnsOf(breakpoint: FlowBreakpoint): number {
  if (typeof breakpoint !== 'string') return breakpoint.kpiColumns;
  return breakpoint === 'xl' ? 12 : breakpoint === 'lg' ? 2 : 1;
}

/** An item's column span at a breakpoint. */
export function spanOf(item: Pick<FlowItem, 'size'>, breakpoint: FlowBreakpoint): number {
  if (typeof breakpoint !== 'string' || !item.size || breakpoint === 'base') return 1;
  return GRID_SPANS[item.size][breakpoint];
}

/** Lay `items` out, in order, the way the browser's grid would. */
export function flowGrid(
  items: readonly FlowItem[],
  breakpoint: FlowBreakpoint,
  geometry: Partial<FlowGeometry> = {}
): FlowResult {
  const kpi = typeof breakpoint !== 'string';
  const columns = columnsOf(breakpoint);
  const width = geometry.width ?? (kpi ? DEFAULT_WIDTH.xl : DEFAULT_WIDTH[breakpoint]);
  const gap = geometry.gap ?? (kpi ? 8 : 16);
  const left = geometry.left ?? 0;
  const top = geometry.top ?? 0;
  const align = geometry.align ?? (kpi ? 'stretch' : 'start');
  const track = (width - (columns - 1) * gap) / columns;

  const placed: { item: FlowItem; row: number; column: number; span: number }[] = [];
  const rows: string[][] = [];
  let row = 0;
  let column = 0;
  for (const item of items) {
    const span = Math.min(spanOf(item, breakpoint), columns);
    if (column + span > columns) {
      row += 1;
      column = 0;
    }
    placed.push({ item, row, column, span });
    (rows[row] ??= []).push(item.id);
    column += span;
  }

  const rowHeights = rows.map((_, index) =>
    Math.max(0, ...placed.filter(entry => entry.row === index).map(entry => entry.item.height))
  );
  const rowTops: number[] = [];
  rowHeights.forEach((height, index) => {
    rowTops.push(index === 0 ? top : rowTops[index - 1] + rowHeights[index - 1] + gap);
  });

  const rects = new Map<string, FlowRect>();
  for (const { item, row: at, column: col, span } of placed) {
    rects.set(
      item.id,
      rectOf(
        left + col * (track + gap),
        rowTops[at],
        span * track + (span - 1) * gap,
        align === 'stretch' ? rowHeights[at] : item.height
      )
    );
  }
  const height = rows.length === 0 ? 0 : rowTops[rows.length - 1] + rowHeights[rows.length - 1] - top;
  return { rects, zoneRect: rectOf(left, top, width, height), rows, columns };
}
