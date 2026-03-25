import { Tour } from '../context/TourContext';
import { UserRole } from '../types';
import { Permission, hasAnyPermission } from '../utils/permissions';

// ─── Tour Definitions ────────────────────────────────────────────────
// Each tour includes role visibility, permission requirements, and
// role-specific description overrides so the help menu adapts to
// the logged-in user's capabilities.

export const tours: Record<string, Tour> = {
  'getting-started': {
    id: 'getting-started',
    name: 'Getting Started',
    description: 'Learn the basics of navigating the Werco MES system',
    category: 'getting-started',
    icon: 'RocketLaunchIcon',
    startPath: '/',
    // Visible to every role — role descriptions tailor the messaging
    roleDescriptions: {
      operator: 'Learn where to find your assigned operations and clock in/out',
      shipping: 'Learn how to navigate to shipping and inventory areas',
      quality: 'Learn where to access quality tools, NCRs, and calibration',
      viewer: 'Learn how to navigate and view data across the system',
      supervisor: 'Learn the basics and where to manage your team\'s work',
      manager: 'Get oriented with dashboards, approvals, and department tools',
      admin: 'Quick overview of navigation, settings, and system administration',
    },
    steps: [
      {
        target: '[data-tour="sidebar"]',
        title: 'Navigation Sidebar',
        description: 'Access all modules from here. Click on any section to expand and see sub-menus. The sidebar organizes everything from Shop Floor operations to Quality management.',
        position: 'right',
        path: '/',
      },
      {
        target: '[data-tour="dashboard-stats"]',
        title: 'Key Metrics Dashboard',
        description: 'Monitor your most important KPIs at a glance: active work orders, overdue items, inventory alerts, and quality metrics. These update in real-time.',
        position: 'bottom',
        path: '/',
      },
      {
        target: '[data-tour="search"]',
        title: 'Quick Search',
        description: 'Press Cmd/Ctrl + K or click the search icon to instantly find parts, work orders, customers, or any data in the system.',
        position: 'bottom',
        path: '/',
      },
      {
        target: '[data-tour="user-menu"]',
        title: 'User Menu',
        description: 'Access your profile, settings, and logout from here. You can also restart this tour anytime from the help menu.',
        position: 'left',
        path: '/',
      },
    ],
  },

  'work-orders': {
    id: 'work-orders',
    name: 'Work Orders',
    description: 'Learn how to create and manage work orders',
    category: 'production',
    icon: 'ClipboardDocumentListIcon',
    startPath: '/work-orders',
    requiredPermissions: ['work_orders:view'],
    roleDescriptions: {
      operator: 'See your assigned work orders and track completion',
      supervisor: 'Create, assign, and track work orders for your team',
      manager: 'Create, manage, release, and approve work orders across departments',
      admin: 'Full work order lifecycle management with delete and release controls',
      quality: 'View work orders linked to quality inspections and NCRs',
      viewer: 'Browse work orders and their status (read-only)',
    },
    roleStepOverrides: {
      1: { // "Create Work Order" step
        operator: {
          title: 'Work Order Details',
          description: 'Click any work order row to see its full details, assigned operations, and current status. Your assigned operations appear on the Shop Floor.',
        },
        viewer: {
          title: 'Work Order Details',
          description: 'Click any row to view full work order details including status, priority, and operation history.',
        },
        quality: {
          title: 'Work Order Details',
          description: 'Click any work order to view details. Quality holds and NCRs linked to work orders appear here.',
        },
      },
    },
    steps: [
      {
        target: '[data-tour="wo-list"]',
        title: 'Work Order List',
        description: 'View all work orders with their status, priority, and due dates. Click any row to see full details.',
        position: 'bottom',
        path: '/work-orders',
      },
      {
        target: '[data-tour="wo-create"]',
        title: 'Create Work Order',
        description: 'Click here to create a new work order. You\'ll select a part, set quantity, and the system will auto-populate operations from the routing.',
        position: 'left',
        path: '/work-orders',
        requiredPermissions: ['work_orders:create'],
      },
      {
        target: '[data-tour="wo-filters"]',
        title: 'Filter & Search',
        description: 'Filter work orders by status, priority, customer, or date range. Use the search to find specific work order numbers.',
        position: 'bottom',
        path: '/work-orders',
      },
    ],
  },

  'shop-floor': {
    id: 'shop-floor',
    name: 'Shop Floor',
    description: 'Learn how operators use the shop floor module',
    category: 'production',
    icon: 'WrenchScrewdriverIcon',
    startPath: '/shop-floor',
    roles: ['admin', 'manager', 'supervisor', 'operator'],
    roleDescriptions: {
      operator: 'Your primary workspace — clock in, run operations, and record completions',
      supervisor: 'Monitor your team\'s active operations and throughput in real-time',
      manager: 'Oversee shop floor activity, labor utilization, and operation status',
      admin: 'Full shop floor visibility with configuration access',
    },
    roleStepOverrides: {
      0: {
        supervisor: {
          description: 'Select a work center to see who is clocked in and which operations are running. Track labor hours and identify bottlenecks.',
        },
        manager: {
          description: 'Select a work center to view real-time labor tracking. Use this data for capacity planning and labor cost analysis.',
        },
      },
      2: {
        supervisor: {
          title: 'Priority Focus',
          description: 'Review the top priority jobs for your work center. Check quantities, scrap rates, and flag any issues for quality review.',
        },
        manager: {
          title: 'Priority & Completion',
          description: 'See the highest-priority jobs at a glance. Track completion rates, scrap percentages, and on-time delivery.',
        },
      },
    },
    steps: [
      {
        target: '[data-tour="sf-clock"]',
        title: 'Work Center Selector',
        description: 'Select a work center to view its job queue. Operators clock in/out of operations from here by clicking Clock In on a queue item.',
        position: 'bottom',
        path: '/shop-floor',
      },
      {
        target: '[data-tour="sf-operations"]',
        title: 'Job Queue',
        description: 'See all operations queued for this work center. Sorted by priority and due date. Click Clock In to start tracking time on a job.',
        position: 'top',
        path: '/shop-floor',
      },
      {
        target: '[data-tour="sf-complete"]',
        title: 'Priority Focus',
        description: 'The top priority jobs to run next, at a glance. When you clock out, enter quantity completed and any scrap to record production.',
        position: 'bottom',
        path: '/shop-floor',
      },
    ],
  },

  'engineering': {
    id: 'engineering',
    name: 'Engineering',
    description: 'Manage parts, BOMs, and routings',
    category: 'engineering',
    icon: 'CogIcon',
    startPath: '/parts',
    requiredPermissions: ['parts:view'],
    roles: ['admin', 'manager', 'supervisor', 'operator', 'quality', 'viewer'],
    roleDescriptions: {
      operator: 'View part specs, BOMs, and routing steps for your operations',
      supervisor: 'Create and edit parts, BOMs, and routings for your team\'s work',
      manager: 'Full engineering data management with release and approval controls',
      admin: 'Complete engineering module access including delete and configuration',
      quality: 'Review part specifications, BOMs, and routing details for inspections',
      viewer: 'Browse engineering data including parts, BOMs, and routings (read-only)',
    },
    roleStepOverrides: {
      0: {
        operator: {
          description: 'Look up part numbers, materials, and specifications needed for your current operations.',
        },
        quality: {
          description: 'Review part specifications, material requirements, and revision history relevant to quality inspections.',
        },
      },
    },
    steps: [
      {
        target: '[data-tour="eng-parts"]',
        title: 'Parts Master',
        description: 'Create and manage all part numbers here. Parts can be manufactured, purchased, assemblies, or raw materials.',
        position: 'right',
        path: '/parts',
      },
      {
        target: '[data-tour="eng-bom"]',
        title: 'Bill of Materials',
        description: 'Define what components make up an assembly. BOMs support multiple levels and can include both manufactured and purchased parts.',
        position: 'right',
        path: '/bom',
        requiredPermissions: ['boms:view'],
      },
      {
        target: '[data-tour="eng-routing"]',
        title: 'Routing',
        description: 'Define the manufacturing operations for each part. Set work centers, setup/run times, and operation sequence.',
        position: 'right',
        path: '/routing',
        requiredPermissions: ['routings:view'],
      },
    ],
  },

  'quality': {
    id: 'quality',
    name: 'Quality Management',
    description: 'NCRs, CARs, FAIs, and calibration',
    category: 'quality',
    icon: 'ShieldCheckIcon',
    startPath: '/quality',
    requiredPermissions: ['quality:view'],
    roleDescriptions: {
      quality: 'Your primary workspace — manage NCRs, calibration, inspections, and traceability',
      supervisor: 'Review quality issues, NCRs, and calibration status for your work center',
      manager: 'Approve dispositions, review quality trends, and manage calibration programs',
      admin: 'Full quality system access with configuration and approval controls',
      operator: 'View quality alerts and NCRs related to your operations',
      viewer: 'Browse quality records, NCRs, and calibration data (read-only)',
    },
    roleStepOverrides: {
      0: {
        quality: {
          description: 'Create and manage NCRs here. Document non-conformances, link to work orders and lots, assign dispositions, and track corrective actions through closure.',
        },
        operator: {
          description: 'View NCRs related to your operations. If you spot a quality issue, notify your supervisor to create an NCR.',
        },
      },
      1: {
        quality: {
          description: 'Your calibration dashboard — manage all calibrated equipment, schedule calibrations, upload certificates, and set up automated alerts before items come due.',
        },
      },
    },
    steps: [
      {
        target: '[data-tour="qa-ncr"]',
        title: 'Non-Conformance Reports',
        description: 'Document quality issues here. NCRs can be linked to work orders, lots, and suppliers. Track disposition and corrective actions.',
        position: 'bottom',
        path: '/quality',
      },
      {
        target: '[data-tour="qa-calibration"]',
        title: 'Calibration Tracking',
        description: 'Manage all calibrated equipment. Set calibration intervals, track certificates, and get alerts before items are due.',
        position: 'bottom',
        path: '/calibration',
        requiredPermissions: ['quality:calibration'],
      },
      {
        target: '[data-tour="qa-traceability"]',
        title: 'Lot Traceability',
        description: 'Full traceability from raw material to finished goods. Track lot numbers, certifications, and material test reports.',
        position: 'bottom',
        path: '/traceability',
      },
    ],
  },

  'quote-calculator': {
    id: 'quote-calculator',
    name: 'Quote Calculator',
    description: 'Generate instant quotes for CNC and sheet metal work',
    category: 'production',
    icon: 'CalculatorIcon',
    startPath: '/quote-calculator',
    roles: ['admin', 'manager', 'supervisor'],
    roleDescriptions: {
      supervisor: 'Generate quick cost estimates for parts to support planning',
      manager: 'Create detailed quotes with full cost breakdowns for customer proposals',
      admin: 'Full quoting access with rate configuration',
    },
    steps: [
      {
        target: '[data-tour="quote-type"]',
        title: 'Select Quote Type',
        description: 'Choose between CNC Machining for milled/turned parts, or Sheet Metal for laser cut and formed parts.',
        position: 'bottom',
        path: '/quote-calculator',
      },
      {
        target: '[data-tour="quote-inputs"]',
        title: 'Enter Part Details',
        description: 'Input dimensions, material, complexity factors, and quantity. For sheet metal, you can upload a DXF file to auto-extract cut length and features.',
        position: 'right',
        path: '/quote-calculator',
      },
      {
        target: '[data-tour="quote-result"]',
        title: 'Quote Result',
        description: 'See the calculated price with full cost breakdown: material, machining time, setup, and any finishing operations.',
        position: 'left',
        path: '/quote-calculator',
      },
    ],
  },

  'shipping-receiving': {
    id: 'shipping-receiving',
    name: 'Shipping & Receiving',
    description: 'Manage shipments, receiving, and inventory movements',
    category: 'production',
    icon: 'TruckIcon',
    startPath: '/shipping',
    roles: ['admin', 'manager', 'supervisor', 'shipping'],
    requiredPermissions: ['shipping:view'],
    roleDescriptions: {
      shipping: 'Your primary workspace — create shipments, print labels, and complete deliveries',
      supervisor: 'Track shipments and receiving for your team\'s work orders',
      manager: 'Oversee shipping schedules, on-time delivery metrics, and receiving inspections',
      admin: 'Full shipping and receiving access with configuration',
    },
    steps: [
      {
        target: '[data-tour="sidebar"]',
        title: 'Shipping Module',
        description: 'Access shipping and receiving from the sidebar. Create shipments, track packages, and manage receiving inspections.',
        position: 'right',
        path: '/shipping',
      },
    ],
  },

  'admin-settings': {
    id: 'admin-settings',
    name: 'System Administration',
    description: 'Manage users, roles, permissions, and system settings',
    category: 'admin',
    icon: 'Cog6ToothIcon',
    startPath: '/admin/settings',
    roles: ['admin'],
    requiredPermissions: ['admin:settings'],
    steps: [
      {
        target: '[data-tour="sidebar"]',
        title: 'Admin Tools',
        description: 'Access user management, role permissions, system settings, and audit logs from the Admin section in the sidebar.',
        position: 'right',
        path: '/admin/settings',
      },
      {
        target: '[data-tour="user-menu"]',
        title: 'User Management',
        description: 'Create and manage user accounts, assign roles, reset passwords, and control access. Navigate to Users from the sidebar.',
        position: 'left',
        path: '/users',
      },
    ],
  },
};

// ─── Help Tips ───────────────────────────────────────────────────────
// Contextual quick-help tips shown in the help menu, filtered by role.

export interface HelpTip {
  id: string;
  title: string;
  description: string;
  /** Keyboard shortcut hint, if applicable */
  shortcut?: string;
  /** Roles that should see this tip. If empty/undefined, visible to all. */
  roles?: UserRole[];
  requiredPermissions?: Permission[];
}

export const helpTips: HelpTip[] = [
  {
    id: 'search',
    title: 'Quick Search',
    description: 'Find anything instantly — parts, work orders, customers.',
    shortcut: 'Ctrl+K',
  },
  {
    id: 'keyboard',
    title: 'Keyboard Shortcuts',
    description: 'Press Ctrl+/ to see all available keyboard shortcuts.',
    shortcut: 'Ctrl+/',
  },
  {
    id: 'clock-in',
    title: 'Clock In/Out',
    description: 'Go to Shop Floor > Operations to start or stop tracking time on an operation.',
    roles: ['operator', 'supervisor'],
  },
  {
    id: 'create-wo',
    title: 'Create Work Order',
    description: 'Navigate to Work Orders and click "+ New" to create one. The routing auto-populates operations.',
    requiredPermissions: ['work_orders:create'],
  },
  {
    id: 'release-wo',
    title: 'Release Work Orders',
    description: 'Only released work orders appear on the shop floor. Use the Release action from the work order detail page.',
    requiredPermissions: ['work_orders:release'],
  },
  {
    id: 'create-ncr',
    title: 'Report Quality Issues',
    description: 'Go to Quality > NCRs to create a non-conformance report. Link it to the relevant work order or lot.',
    requiredPermissions: ['quality:inspect'],
  },
  {
    id: 'calibration-alerts',
    title: 'Calibration Alerts',
    description: 'Calibration items due within 30 days appear as alerts. Manage schedules in Quality > Calibration.',
    requiredPermissions: ['quality:calibration'],
  },
  {
    id: 'ship-complete',
    title: 'Complete Shipments',
    description: 'Mark shipments as complete from the Shipping page to update inventory and notify customers.',
    requiredPermissions: ['shipping:complete'],
  },
  {
    id: 'audit-logs',
    title: 'Audit Trail',
    description: 'All actions are logged for CMMC compliance. View the audit log from Admin > Audit Logs.',
    requiredPermissions: ['admin:audit_logs'],
  },
  {
    id: 'manage-roles',
    title: 'Customize Permissions',
    description: 'Override default role permissions from Admin > Settings > Role Permissions.',
    requiredPermissions: ['users:roles'],
  },
];

// ─── Filtering Utilities ─────────────────────────────────────────────

/**
 * Get tours visible to a specific role, with role-customized descriptions
 * and filtered steps based on permissions.
 */
export function getToursForRole(
  role: UserRole | undefined,
  isSuperuser?: boolean
): Tour[] {
  if (!role && !isSuperuser) return [];

  return Object.values(tours)
    .filter((tour) => {
      // Superusers see everything
      if (isSuperuser) return true;
      // Check role restriction
      if (tour.roles && tour.roles.length > 0 && !tour.roles.includes(role!)) {
        return false;
      }
      // Check permission restriction
      if (tour.requiredPermissions && tour.requiredPermissions.length > 0) {
        return hasAnyPermission(role, tour.requiredPermissions);
      }
      return true;
    })
    .map((tour) => {
      // Apply role-specific description
      const description: string =
        (role && tour.roleDescriptions?.[role]) || tour.description;

      // Filter steps by permission and apply role overrides
      const steps = tour.steps
        .filter((step) => {
          if (isSuperuser) return true;
          if (step.requiredPermissions && step.requiredPermissions.length > 0) {
            return hasAnyPermission(role, step.requiredPermissions);
          }
          return true;
        })
        .map((step, _filteredIdx) => {
          // Find the original index for this step in the unfiltered array
          const originalIdx = tour.steps.indexOf(step);
          const override =
            role && tour.roleStepOverrides?.[originalIdx]?.[role];
          if (override) {
            return { ...step, ...override };
          }
          return step;
        });

      return { ...tour, description, steps };
    })
    // Don't show tours with 0 steps after filtering
    .filter((tour) => tour.steps.length > 0);
}

/**
 * Get help tips visible to a specific role.
 */
export function getHelpTipsForRole(
  role: UserRole | undefined,
  isSuperuser?: boolean
): HelpTip[] {
  if (!role && !isSuperuser) return [];

  return helpTips.filter((tip) => {
    if (isSuperuser) return true;
    if (tip.roles && tip.roles.length > 0 && !tip.roles.includes(role!)) {
      return false;
    }
    if (tip.requiredPermissions && tip.requiredPermissions.length > 0) {
      return hasAnyPermission(role, tip.requiredPermissions);
    }
    return true;
  });
}

/**
 * Get a single tour by ID (original, not role-filtered).
 */
export const getTour = (tourId: string): Tour | undefined => {
  return tours[tourId];
};

/**
 * Get all tours (original, not role-filtered).
 */
export const getAllTours = (): Tour[] => {
  return Object.values(tours);
};
