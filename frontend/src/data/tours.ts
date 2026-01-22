import { Tour } from '../context/TourContext';

export const tours: Record<string, Tour> = {
  'getting-started': {
    id: 'getting-started',
    name: 'Getting Started',
    description: 'Learn the basics of navigating the Werco MES system',
    startPath: '/',  // Dashboard
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
    startPath: '/work-orders',
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
    startPath: '/shop-floor/operations',
    steps: [
      {
        target: '[data-tour="sf-clock"]',
        title: 'Time Clock',
        description: 'Operators clock in/out of operations here. Select a work order and operation, then start the timer to track labor.',
        position: 'bottom',
        path: '/shop-floor/operations',
      },
      {
        target: '[data-tour="sf-operations"]',
        title: 'Active Operations',
        description: 'See all operations assigned to your work center. Color-coded by priority and status.',
        position: 'bottom',
        path: '/shop-floor/operations',
      },
      {
        target: '[data-tour="sf-complete"]',
        title: 'Complete Operation',
        description: 'When finished, enter the quantity completed and any scrap. The system auto-advances to the next operation if configured.',
        position: 'top',
        path: '/shop-floor/operations',
      },
    ],
  },
  'engineering': {
    id: 'engineering',
    name: 'Engineering',
    description: 'Manage parts, BOMs, and routings',
    startPath: '/parts',
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
      },
      {
        target: '[data-tour="eng-routing"]',
        title: 'Routing',
        description: 'Define the manufacturing operations for each part. Set work centers, setup/run times, and operation sequence.',
        position: 'right',
        path: '/routing',
      },
    ],
  },
  'quality': {
    id: 'quality',
    name: 'Quality Management',
    description: 'NCRs, CARs, FAIs, and calibration',
    startPath: '/quality',
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
    startPath: '/quote-calculator',
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
};

export const getTour = (tourId: string): Tour | undefined => {
  return tours[tourId];
};

export const getAllTours = (): Tour[] => {
  return Object.values(tours);
};
