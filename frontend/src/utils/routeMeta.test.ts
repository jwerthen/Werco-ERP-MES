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

import { getRouteTitle, getBreadcrumbParent, formatTabTitle } from './routeMeta';

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

  it('resolves the BOM unit-mismatch sub-route ahead of the bare /bom title', () => {
    expect(getRouteTitle(loc('/bom'))).toBe('Bill of Materials');
    expect(getRouteTitle(loc('/bom/uom-mismatches'))).toBe('BOM Unit Mismatches');
    // Filters live in the query string; the title must not drift with them.
    expect(getRouteTitle(loc('/bom/uom-mismatches', '?part_id=900&page=2'))).toBe(
      'BOM Unit Mismatches'
    );
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

  it('titles the work-order Templates tab, which is a TAB precisely because it cannot be a route', () => {
    // `/work-orders/templates` is matched by App.tsx's `/work-orders/:id` route
    // AND by the WO-detail pattern below, so a real route there would resolve as
    // a work order whose id is the word "templates". Hence `?tab=templates` —
    // and hence a query title, since the bare path is still the list.
    expect(getRouteTitle(loc('/work-orders', '?tab=templates'))).toBe('Work Order Templates');
    // The tab rides alongside the page's other URL filters, which must not
    // stop it resolving.
    expect(getRouteTitle(loc('/work-orders', '?status=in_progress&tab=templates'))).toBe(
      'Work Order Templates'
    );
    expect(getRouteTitle(loc('/work-orders'))).toBe('Work Orders');
    expect(getRouteTitle(loc('/work-orders', '?status=in_progress'))).toBe('Work Orders');
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

  it('nests the BOM unit-mismatch worklist under Bill of Materials', () => {
    // A sub-route of /bom, not a list route of its own: the correction happens
    // on the BOM, and the breadcrumb has to say where "back" goes.
    expect(getBreadcrumbParent('/bom/uom-mismatches')).toEqual({
      label: 'Bill of Materials',
      href: '/bom',
    });
  });

  it('crumbs the work-order create form back to the Work Orders list', () => {
    // Previously null; the :id detail pattern still excludes `new` via its
    // lookahead, so this entry cannot shadow the detail crumb.
    expect(getBreadcrumbParent('/work-orders/new')).toEqual({
      label: 'Work Orders',
      href: '/work-orders',
    });
  });

  it('crumbs every analytics sub-view back to the Analytics hub', () => {
    const subViews = ['production', 'quality', 'inventory', 'forecasting', 'costs', 'flow', 'reports'];
    for (const sub of subViews) {
      expect(getBreadcrumbParent(`/analytics/${sub}`)).toEqual({
        label: 'Analytics',
        href: '/analytics',
      });
    }
    // The bare hub is a top-level page — no parent crumb.
    expect(getBreadcrumbParent('/analytics')).toBeNull();
    // Unknown sub-path stays unmatched rather than inventing a crumb.
    expect(getBreadcrumbParent('/analytics/nope')).toBeNull();
  });

  it('crumbs the inventory sub-views back to Inventory', () => {
    expect(getBreadcrumbParent('/inventory/parts')).toEqual({
      label: 'Inventory',
      href: '/inventory',
    });
    expect(getBreadcrumbParent('/inventory/materials')).toEqual({
      label: 'Inventory',
      href: '/inventory',
    });
    expect(getBreadcrumbParent('/inventory')).toBeNull();
  });

  it('crumbs the PO upload flow back to Purchase Orders', () => {
    expect(getBreadcrumbParent('/po-upload')).toEqual({
      label: 'Purchase Orders',
      href: '/purchasing',
    });
  });

  it('crumbs the AI RFQ package form back to Quotes (its publishing hub)', () => {
    // There is no /rfq-packages list page; Quotes is where a package lands.
    expect(getBreadcrumbParent('/rfq-packages/new')).toEqual({
      label: 'Quotes',
      href: '/quotes',
    });
  });

  it('returns null for an unknown route', () => {
    expect(getBreadcrumbParent('/totally-unknown')).toBeNull();
  });
});

describe('formatTabTitle', () => {
  it('suffixes a real page title with the app name', () => {
    expect(formatTabTitle('Work Orders')).toBe('Work Orders · Werco ERP');
    expect(formatTabTitle('Dashboard')).toBe('Dashboard · Werco ERP');
  });

  it('keeps the generic fallback as the bare app name — never doubled', () => {
    expect(formatTabTitle('Werco ERP')).toBe('Werco ERP');
    expect(formatTabTitle('Werco ERP')).not.toBe('Werco ERP · Werco ERP');
  });
});
