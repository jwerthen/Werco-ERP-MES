import React, { Suspense } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './context/AuthContext';
import { CompanyProvider } from './context/CompanyContext';
import { TourProvider } from './context/TourContext';
import { KeyboardShortcutsProvider } from './context/KeyboardShortcutsContext';
import { usePermissions } from './hooks/usePermissions';
import type { Permission } from './utils/permissions';
import { ToastProvider } from './components/ui/Toast';
import { TourHighlight } from './components/Tour';
import { ErrorBoundary } from './components/ErrorBoundary';
import Layout from './components/Layout';
import { SkeletonDashboard, LoadingOverlay } from './components/ui/Skeleton';
import { getKioskStationId, isKioskMode, syncKioskMode } from './utils/kiosk';
import { lazyWithRetry } from './utils/lazyWithRetry';

// Eagerly loaded - critical path
import Login from './pages/Login';
import Register from './pages/Register';
import CompanyRegister from './pages/CompanyRegister';
import Dashboard from './pages/Dashboard';
import Unauthorized from './pages/Unauthorized';

// Lazy loaded pages - code splitting for better performance
const WorkOrders = lazyWithRetry(() => import('./pages/WorkOrders'));
const WorkOrderNew = lazyWithRetry(() => import('./pages/WorkOrderNew'));
const WorkOrderDetail = lazyWithRetry(() => import('./pages/WorkOrderDetail'));
const ShopFloor = lazyWithRetry(() => import('./pages/ShopFloor'));
const ShopFloorSimple = lazyWithRetry(() => import('./pages/ShopFloorSimple'));
const OperatorKiosk = lazyWithRetry(() => import('./pages/OperatorKiosk'));
const CrewStationKiosk = lazyWithRetry(() => import('./pages/CrewStationKiosk'));
const Wallboard = lazyWithRetry(() => import('./pages/Wallboard'));
const TvPair = lazyWithRetry(() => import('./pages/TvPair'));
const WorkCenters = lazyWithRetry(() => import('./pages/WorkCenters'));
const Parts = lazyWithRetry(() => import('./pages/PartsNew'));
const PartDetail = lazyWithRetry(() => import('./pages/PartDetail'));
const PartEdit = lazyWithRetry(() => import('./pages/PartEdit'));
const BOM = lazyWithRetry(() => import('./pages/BOM'));
const Routing = lazyWithRetry(() => import('./pages/Routing'));
const ProcessSheets = lazyWithRetry(() => import('./pages/ProcessSheets'));
const SetupWizard = lazyWithRetry(() => import('./pages/SetupWizard'));
const ImportCenter = lazyWithRetry(() => import('./pages/ImportCenter'));
const ActionInbox = lazyWithRetry(() => import('./pages/ActionInbox'));
const Notifications = lazyWithRetry(() => import('./pages/Notifications'));
const Warehouse = lazyWithRetry(() => import('./pages/Warehouse'));
const Materials = lazyWithRetry(() => import('./pages/Materials'));
const MRP = lazyWithRetry(() => import('./pages/MRP'));
const Quality = lazyWithRetry(() => import('./pages/Quality'));
const CustomFields = lazyWithRetry(() => import('./pages/CustomFields'));
const Purchasing = lazyWithRetry(() => import('./pages/Purchasing'));
const Scheduling = lazyWithRetry(() => import('./pages/Scheduling'));
const DispatchBoard = lazyWithRetry(() => import('./pages/DispatchBoard'));
const Documents = lazyWithRetry(() => import('./pages/Documents'));
const Reports = lazyWithRetry(() => import('./pages/Reports'));
const Quotes = lazyWithRetry(() => import('./pages/Quotes'));
const RFQQuoting = lazyWithRetry(() => import('./pages/RFQQuoting'));
const Users = lazyWithRetry(() => import('./pages/Users'));
const Customers = lazyWithRetry(() => import('./pages/Customers'));
const Calibration = lazyWithRetry(() => import('./pages/Calibration'));
const PrintTraveler = lazyWithRetry(() => import('./pages/PrintTraveler'));
const PrintBadges = lazyWithRetry(() => import('./pages/PrintBadges'));
const PrintPurchaseOrder = lazyWithRetry(() => import('./pages/PrintPurchaseOrder'));
const Traceability = lazyWithRetry(() => import('./pages/Traceability'));
const PrintPackingSlip = lazyWithRetry(() => import('./pages/PrintPackingSlip'));
const PrintShippingLabel = lazyWithRetry(() => import('./pages/PrintShippingLabel'));
const AuditLog = lazyWithRetry(() => import('./pages/AuditLog'));
const QuoteCalculator = lazyWithRetry(() => import('./pages/QuoteCalculator'));
const EstimateWorkbench = lazyWithRetry(() => import('./pages/EstimateWorkbench'));
const ShopData = lazyWithRetry(() => import('./pages/ShopData'));
const AdminSettings = lazyWithRetry(() => import('./pages/AdminSettings'));
const POUpload = lazyWithRetry(() => import('./pages/POUpload'));
const Analytics = lazyWithRetry(() => import('./pages/Analytics'));
const JobCosting = lazyWithRetry(() => import('./pages/JobCosting'));
const DowntimeTracking = lazyWithRetry(() => import('./pages/DowntimeTracking'));
const Maintenance = lazyWithRetry(() => import('./pages/Maintenance'));
const OEEDashboard = lazyWithRetry(() => import('./pages/OEE'));
const OperatorCertifications = lazyWithRetry(() => import('./pages/OperatorCertifications'));
const EngineeringChanges = lazyWithRetry(() => import('./pages/EngineeringChanges'));
const SPCPage = lazyWithRetry(() => import('./pages/SPC'));
const CustomerComplaints = lazyWithRetry(() => import('./pages/CustomerComplaints'));
const ToolManagement = lazyWithRetry(() => import('./pages/ToolManagement'));
const SupplierScorecards = lazyWithRetry(() => import('./pages/SupplierScorecards'));
const QMSStandards = lazyWithRetry(() => import('./pages/QMSStandards'));
const PlatformOverview = lazyWithRetry(() => import('./pages/PlatformOverview'));
const VisitorSignIn = lazyWithRetry(() => import('./pages/VisitorSignIn'));
const VisitorLog = lazyWithRetry(() => import('./pages/VisitorLog'));

// Loading fallback for lazy-loaded pages
const PageLoader = () => (
  <div className="p-6">
    <SkeletonDashboard />
  </div>
);

// 404 Not Found page
const NotFoundPage = () => (
  <div className="flex flex-col items-center justify-center min-h-screen bg-gray-50">
    <h1 className="text-4xl font-bold text-gray-800 mb-4">404 - Page Not Found</h1>
    <p className="text-gray-600 mb-6">The page you are looking for does not exist.</p>
    <a
      href="/"
      className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
    >
      Back to Dashboard
    </a>
  </div>
);

interface RouteAccessRequirement {
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

function getRouteAccessRequirement(pathname: string): RouteAccessRequirement | undefined {
  return routeAccessRequirements
    .filter(requirement => pathname === requirement.prefix || pathname.startsWith(`${requirement.prefix}/`))
    .sort((a, b) => b.prefix.length - a.prefix.length)[0];
}

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();
  const { can, canAny, canAll } = usePermissions();
  
  if (isLoading) {
    return <LoadingOverlay message="Authenticating..." />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }

  const requirement = getRouteAccessRequirement(location.pathname);
  if (requirement?.permission && !can(requirement.permission)) {
    return <Navigate to="/unauthorized" state={{ from: location }} replace />;
  }
  if (requirement?.anyOf && !canAny(requirement.anyOf)) {
    return <Navigate to="/unauthorized" state={{ from: location }} replace />;
  }
  if (requirement?.allOf && !canAll(requirement.allOf)) {
    return <Navigate to="/unauthorized" state={{ from: location }} replace />;
  }

  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isAuthenticated, isLoading } = useAuth();
  
  if (isLoading) {
    return <LoadingOverlay message="Authenticating..." />;
  }
  
  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }
  
  if (user?.role !== 'admin' && user?.role !== 'platform_admin' && !user?.is_superuser) {
    return <Navigate to="/unauthorized" />;
  }
  
  return <>{children}</>;
}

function PlatformAdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return <LoadingOverlay message="Authenticating..." />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }

  if (user?.role !== 'platform_admin' && !user?.is_superuser) {
    return <Navigate to="/unauthorized" />;
  }

  return <>{children}</>;
}

function KioskGuard({ children }: { children: React.ReactNode }) {
  const { user, isAuthenticated, isLoading } = useAuth();
  const location = useLocation();
  const kioskMode = isKioskMode(location.pathname, location.search);
  const enforceKiosk = kioskMode && user?.role === 'operator';

  if (!enforceKiosk) {
    return <>{children}</>;
  }

  if (isLoading) {
    return <LoadingOverlay message="Authenticating..." />;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login?kiosk=1" replace />;
  }

  return <>{children}</>;
}


/**
 * /kiosk mode dispatcher: ?station=<id> selects the crew-station kiosk
 * (shared-PIN station auth, multi-operator roster); ?work_center_id=N keeps
 * the existing single-operator badge-login kiosk unchanged.
 */
function KioskRouteDispatcher() {
  const location = useLocation();
  return getKioskStationId(location.search) != null ? <CrewStationKiosk /> : <OperatorKiosk />;
}

// Wrapper for lazy-loaded routes with Suspense
function LazyRoute({ children }: { children: React.ReactNode }) {
  return (
    <Suspense fallback={<PageLoader />}>
      {children}
    </Suspense>
  );
}

function AppRoutes() {
  const location = useLocation();
  const { user } = useAuth();

  // Sync kiosk mode based on URL query params
  syncKioskMode(location.pathname, location.search);

  return (
    <ErrorBoundary level="page" name="AppRoutes">
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/register" element={<Register />} />
      <Route path="/register-company" element={<CompanyRegister />} />
      <Route path="/unauthorized" element={<Unauthorized />} />

      {/* TV wallboard (A0.5) — full-screen, NO Layout chrome, NO PrivateRoute.
          Auth is a scoped display token via ?token= (or a signed-in session);
          the backend endpoint rejects anything else. */}
      <Route path="/wallboard" element={<LazyRoute><Wallboard /></LazyRoute>} />

      {/* TV pairing — full-screen, NO Layout chrome, NO PrivateRoute. Safe as a
          TV's browser homepage: already-paired displays bounce straight to
          /wallboard; otherwise an 8-char setup code (issued in Admin Settings →
          Wallboard Displays) is claimed via the PUBLIC single-use claim endpoint. */}
      <Route path="/tv" element={<LazyRoute><TvPair /></LazyRoute>} />
      <Route path="/tv/:code" element={<LazyRoute><TvPair /></LazyRoute>} />

      {/* Visitor sign-in tablet — full-screen, NO Layout chrome, NO PrivateRoute.
          Auth is a shared-PIN station token minted via POST /visitor-logs/station-login;
          the backend write endpoints reject anything else. */}
      <Route path="/visitor-signin" element={<LazyRoute><VisitorSignIn /></LazyRoute>} />

      {/* Platform Administration (platform admin only) */}
      <Route path="/platform" element={
        <PlatformAdminRoute>
          <Layout>
            <LazyRoute><PlatformOverview /></LazyRoute>
          </Layout>
        </PlatformAdminRoute>
      } />
      
      {/* Dashboard - eagerly loaded */}
      <Route path="/" element={
        <PrivateRoute>
          {user?.role === 'operator' ? (
            <Navigate to="/shop-floor/operations?kiosk=1" replace />
          ) : (
            <Layout>
              <Dashboard />
            </Layout>
          )}
        </PrivateRoute>
      } />
      
      {/* Work Orders */}
      <Route path="/work-orders" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><WorkOrders /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/work-orders/new" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><WorkOrderNew /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/work-orders/:id" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><WorkOrderDetail /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Operator kiosk (A0.3) — full-screen, no Layout. Handles its own auth:
          unauthenticated visitors get the badge-login screen, not a redirect.
          ?station=<id> routes to the crew-station kiosk (shared-PIN station
          auth + per-badge operator tokens); ?work_center_id=N stays the
          single-operator badge-login kiosk. */}
      <Route path="/kiosk" element={
        <LazyRoute><KioskRouteDispatcher /></LazyRoute>
      } />

      {/* Shop Floor */}
      <Route path="/shop-floor" element={
        <PrivateRoute>
          <KioskGuard>
            <Layout>
              <LazyRoute><ShopFloor /></LazyRoute>
            </Layout>
          </KioskGuard>
        </PrivateRoute>
      } />
      <Route path="/shop-floor/operations" element={
        <PrivateRoute>
          <KioskGuard>
            <Layout>
              <LazyRoute><ShopFloorSimple /></LazyRoute>
            </Layout>
          </KioskGuard>
        </PrivateRoute>
      } />
      
      {/* Work Centers */}
      <Route path="/work-centers" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><WorkCenters /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Parts & BOM */}
      <Route path="/parts" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Parts /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/parts/:id" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><PartDetail /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/parts/:id/edit" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><PartEdit /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/bom" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><BOM /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/routing" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Routing /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      {/* Reads are open to any authenticated user (mirrors the backend);
          author/release actions are role-gated inside the page. */}
      <Route path="/process-sheets" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><ProcessSheets /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/setup" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><SetupWizard /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/import-center" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><ImportCenter /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/action-inbox" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><ActionInbox /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      {/* In-app notification inbox — all authenticated roles (auth-only gate). */}
      <Route path="/notifications" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Notifications /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Warehouse (unified Inventory + Receiving + Shipping) */}
      <Route path="/warehouse" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Warehouse /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/materials" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Materials /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      {/* Legacy redirects */}
      <Route path="/inventory" element={
        <PrivateRoute>
          <Navigate to="/warehouse?tab=inventory" replace />
        </PrivateRoute>
      } />
      <Route path="/inventory/parts" element={
        <PrivateRoute>
          <Navigate to="/warehouse?tab=inventory&group=parts" replace />
        </PrivateRoute>
      } />
      <Route path="/inventory/materials" element={
        <PrivateRoute>
          <Navigate to="/warehouse?tab=inventory&group=materials" replace />
        </PrivateRoute>
      } />
      <Route path="/mrp" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><MRP /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Quality */}
      <Route path="/quality" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Quality /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Custom Fields */}
      <Route path="/custom-fields" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><CustomFields /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Purchasing & Receiving */}
      <Route path="/purchasing" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Purchasing /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/receiving" element={
        <PrivateRoute>
          <Navigate to="/warehouse?tab=receiving" replace />
        </PrivateRoute>
      } />
      <Route path="/po-upload" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><POUpload /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Scheduling */}
      <Route path="/scheduling" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Scheduling /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Dispatch Board — manager-controlled run order (write tool) */}
      <Route path="/dispatch" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><DispatchBoard /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Documents & Reports */}
      <Route path="/documents" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Documents /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/reports" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Reports /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Shipping (redirect to warehouse) */}
      <Route path="/shipping" element={
        <PrivateRoute>
          <Navigate to="/warehouse?tab=shipping" replace />
        </PrivateRoute>
      } />
      
      {/* Quotes */}
      <Route path="/rfq-packages/new" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><RFQQuoting /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/quotes" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Quotes /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/quote-calculator" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><QuoteCalculator /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/estimate-workbench" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><EstimateWorkbench /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/estimate-workbench/:estimateId" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><EstimateWorkbench /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/shop-data" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><ShopData /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Users & Customers */}
      <Route path="/users" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Users /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/customers" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Customers /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      
      {/* Calibration */}
      <Route path="/calibration" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Calibration /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Print Pages (no Layout) */}
      <Route path="/print/traveler/:id" element={
        <PrivateRoute>
          <LazyRoute><PrintTraveler /></LazyRoute>
        </PrivateRoute>
      } />
      <Route path="/print/purchase-order/:id" element={
        <PrivateRoute>
          <LazyRoute><PrintPurchaseOrder /></LazyRoute>
        </PrivateRoute>
      } />
      <Route path="/print/packing-slip/:id" element={
        <PrivateRoute>
          <LazyRoute><PrintPackingSlip /></LazyRoute>
        </PrivateRoute>
      } />
      <Route path="/print/shipping-label/:id" element={
        <PrivateRoute>
          <LazyRoute><PrintShippingLabel /></LazyRoute>
        </PrivateRoute>
      } />
      {/* A0.4 badge print sheet — admin/manager only (its GET /users fetch is
          server-enforced to ADMIN/MANAGER; see routeAccessRequirements). */}
      <Route path="/print/badges" element={
        <PrivateRoute>
          <LazyRoute><PrintBadges /></LazyRoute>
        </PrivateRoute>
      } />
      
      {/* Traceability */}
      <Route path="/traceability" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Traceability /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Audit Log */}
      <Route path="/audit-log" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><AuditLog /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Visitor Log (admin) */}
      <Route path="/visitor-log" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><VisitorLog /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      
      {/* Admin Settings */}
      <Route path="/admin/settings" element={
        <AdminRoute>
          <Layout>
            <LazyRoute><AdminSettings /></LazyRoute>
          </Layout>
        </AdminRoute>
      } />
      
      {/* Analytics */}
      <Route path="/analytics" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/production" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/quality" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/inventory" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/forecasting" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/costs" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/flow" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/analytics/reports" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Analytics /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Job Costing */}
      <Route path="/job-costing" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><JobCosting /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Downtime Tracking */}
      <Route path="/downtime" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><DowntimeTracking /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Maintenance */}
      <Route path="/maintenance" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Maintenance /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* OEE */}
      <Route path="/oee" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><OEEDashboard /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Operator Certifications */}
      <Route path="/certifications" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><OperatorCertifications /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Engineering Changes */}
      <Route path="/engineering-changes" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><EngineeringChanges /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* SPC */}
      <Route path="/spc" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><SPCPage /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Customer Complaints */}
      <Route path="/customer-complaints" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><CustomerComplaints /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Tool Management */}
      <Route path="/tool-management" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><ToolManagement /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* QMS Standards & Audit Readiness */}
      <Route path="/qms-standards" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><QMSStandards /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Supplier Scorecards */}
      <Route path="/supplier-scorecards" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><SupplierScorecards /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />

      {/* Catch-all 404 */}
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
    </ErrorBoundary>
  );
}

function App() {
  return (
    // Global boundary OUTSIDE every provider — including ToastProvider, whose toast
    // list renders above the router's page-level boundary. Without this, a render
    // error there (e.g. a raw 422 detail array in a toast) unmounts the whole SPA to
    // a blank #root; now it falls back to a friendly full-page reload screen instead.
    <ErrorBoundary level="global" name="App">
      <AuthProvider>
        <CompanyProvider>
          <ToastProvider>
            <TourProvider>
              <Router>
                <KeyboardShortcutsProvider>
                  <AppRoutes />
                  <TourHighlight />
                </KeyboardShortcutsProvider>
              </Router>
            </TourProvider>
          </ToastProvider>
        </CompanyProvider>
      </AuthProvider>
    </ErrorBoundary>
  );
}

export default App;
