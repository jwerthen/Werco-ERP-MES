/**
 * The test-only grid simulator must describe the grids the page actually renders: its span
 * table is held to `PANEL_SIZE_CLASSES`, and its placement to CSS sparse auto-placement.
 */
import { PANEL_SIZES, PANEL_SIZE_CLASSES } from '../components/dashboard/widgetTypes';
import { GRID_SPANS, flowGrid, spanOf } from './gridFlow';

describe('GRID_SPANS', () => {
  it.each(PANEL_SIZES.map(size => [size]))('agrees with PANEL_SIZE_CLASSES for %s', size => {
    const classes = PANEL_SIZE_CLASSES[size].split(/\s+/);
    const xl = classes.filter(token => token.startsWith('xl:col-span-'));
    expect(xl).toEqual([`xl:col-span-${GRID_SPANS[size].xl}`]);
    const lg = classes.filter(token => token.startsWith('lg:col-span-'));
    expect(lg).toEqual(GRID_SPANS[size].lg === 2 ? ['lg:col-span-2'] : []);
    // No other breakpoint is spanned (base is always one column).
    expect(classes.filter(token => !token.startsWith('xl:col-span-') && !token.startsWith('lg:col-span-'))).toEqual([]);
  });

  it('covers every panel size', () => {
    expect(Object.keys(GRID_SPANS).sort()).toEqual([...PANEL_SIZES].sort());
  });
});

describe('flowGrid', () => {
  it('places in order without back-filling (sparse), wrapping when a span does not fit', () => {
    // xl: wide (7) + half (6) does not fit → half wraps; third (4) would fit after wide but sparse never goes back.
    const flow = flowGrid(
      [
        { id: 'a', size: 'wide', height: 100 },
        { id: 'b', size: 'half', height: 200 },
        { id: 'c', size: 'third', height: 50 },
      ],
      'xl',
      { width: 1200, gap: 16 }
    );
    expect(flow.rows).toEqual([['a'], ['b', 'c']]);
    const track = (1200 - 11 * 16) / 12;
    expect(flow.rects.get('a')).toEqual({
      left: 0,
      top: 0,
      width: 7 * track + 6 * 16,
      height: 100,
      right: 7 * track + 6 * 16,
      bottom: 100,
    });
    // Row 2 starts one gap under row 1's tallest cell; items-start keeps each height.
    expect(flow.rects.get('b')).toMatchObject({ left: 0, top: 116, height: 200 });
    expect(flow.rects.get('c')).toMatchObject({ left: 6 * (track + 16), top: 116, height: 50 });
    expect(flow.zoneRect).toMatchObject({ left: 0, top: 0, width: 1200, height: 316 });
  });

  it('uses 2 columns at lg (two_thirds and full span both) and 1 at base', () => {
    const items = [
      { id: 'a', size: 'third' as const, height: 10 },
      { id: 'b', size: 'two_thirds' as const, height: 10 },
      { id: 'c', size: 'half' as const, height: 10 },
      { id: 'd', size: 'narrow' as const, height: 10 },
    ];
    expect(flowGrid(items, 'lg').rows).toEqual([['a'], ['b'], ['c', 'd']]);
    expect(flowGrid(items, 'base').rows).toEqual([['a'], ['b'], ['c'], ['d']]);
    expect(spanOf({ size: 'full' }, 'lg')).toBe(2);
    expect(spanOf({ size: 'full' }, 'base')).toBe(1);
    expect(spanOf({}, 'xl')).toBe(1);
  });

  it('lays out a KPI strip as equal tiles stretched to the row height', () => {
    const flow = flowGrid(
      ['a', 'b', 'c', 'd'].map((id, index) => ({ id, height: index === 1 ? 90 : 72 })),
      { kpiColumns: 3 },
      { width: 616 }
    );
    expect(flow.rows).toEqual([['a', 'b', 'c'], ['d']]);
    expect(flow.rects.get('a')).toMatchObject({ left: 0, width: 200, height: 90 });
    expect(flow.rects.get('c')).toMatchObject({ left: 416, width: 200 });
    expect(flow.rects.get('d')).toMatchObject({ top: 98, height: 72 });
  });

  it('is empty for no items', () => {
    const flow = flowGrid([], 'xl');
    expect(flow.rows).toEqual([]);
    expect(flow.zoneRect.height).toBe(0);
  });
});
