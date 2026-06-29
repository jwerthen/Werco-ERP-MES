/**
 * routeMeta — the single source of truth for page titles + breadcrumb parents.
 *
 * Batch 7 (navigation & wayfinding) introduced `getRouteTitle` and
 * `getBreadcrumbParent`, which drive the top-bar/mobile page title and the
 * detail-page breadcrumb trails from one place so the two can never drift.
 *
 * These are pure functions over a `{ pathname, search }` shape, so they're
 * tested directly — no DOM, no router. We cover each resolution tier:
 * static list routes, query-tab variants, dynamic detail patterns, the
 * dashboard root, and the unknown-route fallback; plus the breadcrumb-parent
 * mapping for detail vs. list routes.
 */

import { getRouteTitle, getBreadcrumbParent } from './routeMeta';

// Small helper: getRouteTitle takes a location-like `{ pathname, search }`.
function loc(pathname: string, search = '') {
  return { pathname, search };
}

describe('getRouteTitle', () => {
  it('resolves a static list route to its sidebar title', () => {
    expect(getRouteTitle(loc('/work-orders'))).toBe('Work Orders');
    expect(getRouteTitle(loc('/parts'))).toBe('Parts');
  });

  it('resolves the dashboard root', () => {
    expect(getRouteTitle(loc('/'))).toBe('Dashboard');
  });

  it('resolves a query-tab variant by its tab param', () => {
    // Same /warehouse path, different titles per ?tab= value.
    expect(getRouteTitle(loc('/warehouse', '?tab=receiving'))).toBe('Receiving');
    expect(getRouteTitle(loc('/warehouse', '?tab=inventory'))).toBe('Inventory');
    expect(getRouteTitle(loc('/warehouse', '?tab=shipping'))).toBe('Shipping');
  });

  it('matches the query-tab variant even with extra params present', () => {
    // The matcher checks the wanted param is present, not that it's the only one.
    expect(getRouteTitle(loc('/warehouse', '?tab=receiving&page=2'))).toBe('Receiving');
  });

  it('falls back to the bare-path title when the query has no matching tab', () => {
    // Unknown tab value -> no query-title match -> static /warehouse title.
    expect(getRouteTitle(loc('/warehouse', '?tab=nope'))).toBe('Warehouse');
    expect(getRouteTitle(loc('/warehouse'))).toBe('Warehouse');
  });

  it('resolves a dynamic detail route to its generic title', () => {
    expect(getRouteTitle(loc('/work-orders/4'))).toBe('Work Order');
    expect(getRouteTitle(loc('/parts/1'))).toBe('Part');
  });

  it('does not treat /work-orders/new as a detail route', () => {
    // The "new" segment is an explicit static entry, not the :id detail pattern.
    expect(getRouteTitle(loc('/work-orders/new'))).toBe('New Work Order');
  });

  it('resolves the parts edit sub-route ahead of the bare detail route', () => {
    expect(getRouteTitle(loc('/parts/9/edit'))).toBe('Edit Part');
  });

  it('falls back to the app name for an unknown route', () => {
    expect(getRouteTitle(loc('/totally-unknown'))).toBe('Werco ERP');
    expect(getRouteTitle(loc('/work-orders/4/extra/segments'))).toBe('Werco ERP');
  });
});

describe('getBreadcrumbParent', () => {
  it('returns the Work Orders list as the parent of a work-order detail route', () => {
    expect(getBreadcrumbParent('/work-orders/4')).toEqual({
      label: 'Work Orders',
      href: '/work-orders',
    });
  });

  it('returns the Parts list as the parent of a part detail route', () => {
    expect(getBreadcrumbParent('/parts/1')).toEqual({
      label: 'Parts',
      href: '/parts',
    });
  });

  it('returns the Parts list as the parent of the part edit route', () => {
    expect(getBreadcrumbParent('/parts/1/edit')).toEqual({
      label: 'Parts',
      href: '/parts',
    });
  });

  it('returns null for a list route (no parent crumb)', () => {
    expect(getBreadcrumbParent('/work-orders')).toBeNull();
    expect(getBreadcrumbParent('/parts')).toBeNull();
  });

  it('returns null for an unknown route', () => {
    expect(getBreadcrumbParent('/totally-unknown')).toBeNull();
  });
});
