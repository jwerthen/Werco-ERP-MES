/**
 * Route-title source of truth.
 *
 * A single path -> human page-title lookup that drives both the top-bar page
 * title and the breadcrumb trails on detail pages, so the two can never drift.
 *
 * List-page titles mirror the sidebar nav labels (see `Layout.tsx`). Detail
 * routes (`/work-orders/:id`, `/parts/:id`, ...) get explicit entries here
 * because they have no nav item of their own — they resolve against a small
 * set of path patterns.
 *
 * `getRouteTitle` matches the current location; `getBreadcrumbParent` returns
 * the parent crumb (label + href) for a detail route so the breadcrumb trail
 * stays terse and consistent.
 */

export interface RouteParent {
  label: string;
  href: string;
}

/**
 * Static path -> title map. Query-param variants (e.g. the Warehouse tabs) are
 * resolved by `getRouteTitle` against `search`, not stored here.
 */
export const routeTitles: Record<string, string> = {
  '/': 'Dashboard',
  '/action-inbox': 'Action Inbox',
  '/notifications': 'Notifications',
  '/settings': 'My Settings',

  // Production
  '/shop-floor': 'Time Clock',
  '/shop-floor/operations': 'Operations',
  '/downtime': 'Downtime',
  '/scheduling': 'Scheduling',
  '/dispatch': 'Dispatch Board',
  '/work-orders': 'Work Orders',
  '/work-orders/new': 'New Work Order',
  '/maintenance': 'Maintenance',
  '/tool-management': 'Tool Management',
  '/oee': 'OEE',

  // Engineering
  '/parts': 'Parts',
  '/bom': 'Bill of Materials',
  '/bom/uom-mismatches': 'BOM Unit Mismatches',
  '/routing': 'Routing',
  '/process-sheets': 'Process Sheets',
  '/engineering-changes': 'Engineering Changes',

  // Inventory & Purchasing
  '/warehouse': 'Warehouse',
  '/materials': 'Materials & Supplies',
  '/inventory': 'Inventory',
  '/inventory/parts': 'Part Inventory',
  '/inventory/materials': 'Material Inventory',
  '/receiving': 'Receiving',
  '/shipping': 'Shipping',
  '/purchasing': 'Purchase Orders',
  '/po-upload': 'Upload PO',
  '/mrp': 'MRP',

  // Sales & Quoting
  '/rfq-packages/new': 'AI RFQ Quote',
  '/quote-calculator': 'Quote Calculator',
  '/estimate-workbench': 'Estimate Workbench',
  '/shop-data': 'Shop Data',
  '/quotes': 'Quotes',
  '/customers': 'Customers',

  // Quality
  '/quality': 'NCR / CAR / FAI',
  '/spc': 'SPC',
  '/calibration': 'Calibration',
  '/traceability': 'Traceability',
  '/customer-complaints': 'Customer Complaints',
  '/qms-standards': 'QMS Standards',

  // Insights
  '/documents': 'Documents',
  '/job-costing': 'Job Costing',
  '/analytics': 'Analytics',
  '/analytics/production': 'Production Analytics',
  '/analytics/quality': 'Quality Analytics',
  '/analytics/inventory': 'Inventory Analytics',
  '/analytics/forecasting': 'Forecasting',
  '/analytics/costs': 'Cost Analytics',
  '/analytics/flow': 'Flow Analytics',
  '/analytics/reports': 'Analytics Reports',
  '/reports': 'Reports',

  // Administration
  '/setup': 'Setup Wizard',
  '/import-center': 'Import Center',
  '/work-centers': 'Work Centers',
  '/users': 'Users',
  '/certifications': 'Operator Certifications',
  '/supplier-scorecards': 'Supplier Scorecards',
  '/custom-fields': 'Custom Fields',
  '/admin/settings': 'Admin Settings',
  '/platform': 'Platform Overview',
  '/audit-log': 'Audit Log',
  '/visitor-log': 'Visitor Log',
  '/visitor-signin': 'Visitor Sign-In',
};

/**
 * Query-param-specific titles for routes that reuse one path across tabs.
 * Key is `"<path>?<param>=<value>"`; checked before the bare path.
 */
const queryTitles: Record<string, string> = {
  // Templates is a TAB on /work-orders rather than a route of its own: the
  // WO-detail pattern below (`/work-orders/(?!new$)[^/]+`) and App.tsx's
  // `/work-orders/:id` route BOTH match `/work-orders/templates`, so a real
  // route there would resolve as a work order whose id is the word "templates".
  '/work-orders?tab=templates': 'Work Order Templates',
  // The soft-delete archive, a tab for the same reason: `/work-orders/deleted` would
  // resolve as a work order whose id is the word "deleted".
  '/work-orders?tab=deleted': 'Deleted Work Orders',
  '/warehouse?tab=inventory': 'Inventory',
  '/warehouse?tab=receiving': 'Receiving',
  '/warehouse?tab=shipping': 'Shipping',
};

/**
 * Dynamic detail routes: a regex on the pathname plus the parent crumb the
 * breadcrumb should point back up to. The `:param` segment is rendered by the
 * page itself (it knows the real WO/part number), so the title here is a
 * generic fallback used only by the top bar before data loads.
 */
interface DetailRoute {
  pattern: RegExp;
  title: string;
  parent: RouteParent;
}

const detailRoutes: DetailRoute[] = [
  // Static-mapped above (so the title comes from `routeTitles`); listed here
  // only so the breadcrumb resolves its parent from the same source.
  {
    pattern: /^\/bom\/uom-mismatches$/,
    title: 'BOM Unit Mismatches',
    parent: { label: 'Bill of Materials', href: '/bom' },
  },
  {
    // The WO-detail pattern below deliberately excludes `new` via its
    // lookahead; the create form crumbs back to the list it was opened from.
    pattern: /^\/work-orders\/new$/,
    title: 'New Work Order',
    parent: { label: 'Work Orders', href: '/work-orders' },
  },
  {
    // All seven analytics sub-views crumb back up to the Analytics hub.
    pattern: /^\/analytics\/(production|quality|inventory|forecasting|costs|flow|reports)$/,
    title: 'Analytics',
    parent: { label: 'Analytics', href: '/analytics' },
  },
  {
    pattern: /^\/inventory\/(parts|materials)$/,
    title: 'Inventory',
    parent: { label: 'Inventory', href: '/inventory' },
  },
  {
    // The upload flow creates purchase orders; Purchasing is its hub.
    pattern: /^\/po-upload$/,
    title: 'Upload PO',
    parent: { label: 'Purchase Orders', href: '/purchasing' },
  },
  {
    // There is no /rfq-packages list page; Quotes is the hub an AI RFQ
    // package publishes into, so that's where "back" goes.
    pattern: /^\/rfq-packages\/new$/,
    title: 'AI RFQ Quote',
    parent: { label: 'Quotes', href: '/quotes' },
  },
  {
    pattern: /^\/estimate-workbench\/(?!new$)[^/]+$/,
    title: 'Estimate Workbench',
    parent: { label: 'Estimate Workbench', href: '/estimate-workbench' },
  },
  {
    pattern: /^\/work-orders\/(?!new$)[^/]+$/,
    title: 'Work Order',
    parent: { label: 'Work Orders', href: '/work-orders' },
  },
  {
    pattern: /^\/parts\/[^/]+\/edit$/,
    title: 'Edit Part',
    parent: { label: 'Parts', href: '/parts' },
  },
  {
    pattern: /^\/parts\/[^/]+$/,
    title: 'Part',
    parent: { label: 'Parts', href: '/parts' },
  },
];

/** Resolve the human page title for the current location. */
export function getRouteTitle(location: { pathname: string; search: string }): string {
  // Exact path + query match first (tabbed routes).
  if (location.search) {
    const params = new URLSearchParams(location.search);
    for (const [key, title] of Object.entries(queryTitles)) {
      const [path, query] = key.split('?');
      if (location.pathname !== path) continue;
      const want = new URLSearchParams(query);
      let match = true;
      want.forEach((value, name) => {
        if (params.get(name) !== value) match = false;
      });
      if (match) return title;
    }
  }

  // Static path map.
  const exact = routeTitles[location.pathname];
  if (exact) return exact;

  // Dynamic detail routes.
  for (const route of detailRoutes) {
    if (route.pattern.test(location.pathname)) return route.title;
  }

  return 'Werco ERP';
}

/**
 * Format a resolved route title as the browser-tab title ("{title} · Werco
 * ERP"). The generic unknown-route fallback stays the bare app name — never
 * "Werco ERP · Werco ERP". Layout feeds this to `usePageTitle`; station
 * surfaces outside Layout set their literal titles directly.
 */
export function formatTabTitle(title: string): string {
  return title === 'Werco ERP' ? 'Werco ERP' : `${title} · Werco ERP`;
}

/**
 * Return the parent crumb for a detail/sub route, or null for top-level routes.
 * Drives the back-up link in `<Breadcrumbs>` from the same source as the title.
 */
export function getBreadcrumbParent(pathname: string): RouteParent | null {
  for (const route of detailRoutes) {
    if (route.pattern.test(pathname)) return route.parent;
  }
  return null;
}
