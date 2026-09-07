import type { Permission } from './permissions';

export interface RouteAccessRequirement {
  prefix: string;
  permission?: Permission;
  anyOf?: Permission[];
  allOf?: Permission[];
}

const routeAccessRequirements: RouteAccessRequirement[] = [
  { prefix: '/admin/settings', permission: 'admin:settings' },
  { prefix: '/audit-log', permission: 'admin:audit_logs' },
  { prefix: '/visitor-log', permission: 'visitor_logs:view' },
  { prefix: '/users', permission: 'users:view' },
  { prefix: '/work-orders/new', permission: 'work_orders:create' },
  { prefix: '/work-orders', permission: 'work_orders:view' },
  { prefix: '/print/traveler', permission: 'work_orders:view' },
  // Badge printing loads GET /users, which is server-enforced to ADMIN/MANAGER —
  // gate to the matching admin/manager permissions (canManageUsers), not users:view,
  // so a Supervisor is not routed into a guaranteed 403.
  { prefix: '/print/badges', anyOf: ['users:create', 'users:edit'] },
  { prefix: '/print/purchase-order', permission: 'purchasing:view' },
  { prefix: '/print/packing-slip', permission: 'shipping:view' },
  { prefix: '/print/shipping-label', permission: 'shipping:view' },
  { prefix: '/shop-floor', permission: 'work_orders:view' },
  { prefix: '/parts', permission: 'parts:view' },
  // The unit-mismatch worklist is gated ADMIN / MANAGER / SUPERVISOR server-side
  // (bom.py -> list_bom_uom_mismatches) — the roles that can actually edit a BOM
  // line or arm `Part.backflush_components`. `boms:edit` is exactly that set, so
  // a Viewer/Operator with boms:view is not routed into a guaranteed 403.
  // `getRouteAccessRequirement` picks the LONGEST matching prefix, so this wins
  // over the `/bom` entry below.
  { prefix: '/bom/uom-mismatches', permission: 'boms:edit' },
  { prefix: '/bom', permission: 'boms:view' },
  { prefix: '/routing', permission: 'routings:view' },
  { prefix: '/engineering-changes', anyOf: ['parts:view', 'boms:view', 'routings:view'] },
  { prefix: '/warehouse', anyOf: ['inventory:view', 'receiving:view', 'shipping:view'] },
  { prefix: '/materials', permission: 'inventory:view' },
  { prefix: '/inventory', permission: 'inventory:view' },
  { prefix: '/receiving', permission: 'receiving:view' },
  { prefix: '/shipping', permission: 'shipping:view' },
  { prefix: '/purchasing', permission: 'purchasing:view' },
  { prefix: '/po-upload', permission: 'purchasing:create' },
  { prefix: '/mrp', permission: 'purchasing:create' },
  { prefix: '/quality', permission: 'quality:view' },
  { prefix: '/calibration', permission: 'quality:calibration' },
  { prefix: '/traceability', permission: 'quality:view' },
  { prefix: '/spc', permission: 'quality:view' },
  { prefix: '/customer-complaints', permission: 'quality:view' },
  { prefix: '/qms-standards', permission: 'quality:view' },
  { prefix: '/supplier-scorecards', permission: 'purchasing:view' },
  { prefix: '/quotes', permission: 'purchasing:view' },
  { prefix: '/nest', permission: 'purchasing:view' },
  { prefix: '/quote-calculator', permission: 'purchasing:view' },
  { prefix: '/estimate-workbench', permission: 'purchasing:view' },
  { prefix: '/shop-data', permission: 'purchasing:view' },
  { prefix: '/rfq-packages', permission: 'purchasing:create' },
  { prefix: '/customers', permission: 'purchasing:view' },
  { prefix: '/scheduling', permission: 'work_orders:view' },
  // Dispatch Board is a dispatching WRITE tool (it sets the run order operators
  // see), so it is gated on work_orders:edit — admin / manager / supervisor —
  // rather than the read-only work_orders:view that Scheduling uses.
  { prefix: '/dispatch', permission: 'work_orders:edit' },
  { prefix: '/documents', permission: 'work_orders:view' },
  { prefix: '/downtime', permission: 'work_orders:view' },
  { prefix: '/maintenance', permission: 'work_orders:view' },
  { prefix: '/oee', permission: 'analytics:view' },
  { prefix: '/tool-management', permission: 'inventory:view' },
  // Operator Certifications View is open to ALL authenticated roles (RBAC doc); the
  // backend read endpoints (operator_certifications.py) use get_current_user and only
  // skill-matrix WRITES require SUPERVISOR. No routeAccessRequirements entry → falls
  // through to auth-only, so it must NOT depend on users:view (which is admin+manager).
  { prefix: '/analytics', permission: 'analytics:view' },
  { prefix: '/reports', permission: 'analytics:view' },
  { prefix: '/job-costing', permission: 'analytics:view' },
  { prefix: '/setup', permission: 'admin:settings' },
  { prefix: '/import-center', permission: 'admin:settings' },
  { prefix: '/work-centers', permission: 'admin:settings' },
  { prefix: '/custom-fields', permission: 'admin:settings' },
];

export function getRouteAccessRequirement(pathname: string): RouteAccessRequirement | undefined {
  return routeAccessRequirements
    .filter(requirement => pathname === requirement.prefix || pathname.startsWith(`${requirement.prefix}/`))
    .sort((a, b) => b.prefix.length - a.prefix.length)[0];
}

export function canAccessPath(path: string, can: (permission: Permission) => boolean): boolean {
  const requirement = getRouteAccessRequirement(path.split(/[?#]/)[0]);
  return (
    !requirement ||
    ((!requirement.permission || can(requirement.permission)) &&
      (!requirement.anyOf || requirement.anyOf.some(can)) &&
      (!requirement.allOf || requirement.allOf.every(can)))
  );
}
