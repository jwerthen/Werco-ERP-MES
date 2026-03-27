import React, { Suspense } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './context/AuthContext';
import { TourProvider } from './context/TourContext';
import { KeyboardShortcutsProvider } from './context/KeyboardShortcutsContext';
import { ToastProvider } from './components/ui/Toast';
import { TourHighlight } from './components/Tour';
import { ErrorBoundary } from './components/ErrorBoundary';
import Layout from './components/Layout';
import { SkeletonDashboard, LoadingOverlay } from './components/ui/Skeleton';
import { isKioskMode, syncKioskMode } from './utils/kiosk';
import { lazyWithRetry } from './utils/lazyWithRetry';

// Eagerly loaded - critical path
import Login from './pages/Login';
import Register from './pages/Register';
import Dashboard from './pages/Dashboard';
import Unauthorized from './pages/Unauthorized';

// Lazy loaded pages - code splitting for better performance
const WorkOrders = lazyWithRetry(() => import('./pages/WorkOrders'));
const WorkOrderNew = lazyWithRetry(() => import('./pages/WorkOrderNew'));
const WorkOrderDetail = lazyWithRetry(() => import('./pages/WorkOrderDetail'));
const ShopFloor = lazyWithRetry(() => import('./pages/ShopFloor'));
const ShopFloorSimple = lazyWithRetry(() => import('./pages/ShopFloorSimple'));
const WorkCenters = lazyWithRetry(() => import('./pages/WorkCenters'));
const Parts = lazyWithRetry(() => import('./pages/PartsNew'));
const PartDetail = lazyWithRetry(() => import('./pages/PartDetail'));
const PartEdit = lazyWithRetry(() => import('./pages/PartEdit'));
const BOM = lazyWithRetry(() => import('./pages/BOM'));
const Routing = lazyWithRetry(() => import('./pages/Routing'));
const Warehouse = lazyWithRetry(() => import('./pages/Warehouse'));
const MRP = lazyWithRetry(() => import('./pages/MRP'));
const Quality = lazyWithRetry(() => import('./pages/Quality'));
const CustomFields = lazyWithRetry(() => import('./pages/CustomFields'));
const Purchasing = lazyWithRetry(() => import('./pages/Purchasing'));
const Scheduling = lazyWithRetry(() => import('./pages/Scheduling'));
const Documents = lazyWithRetry(() => import('./pages/Documents'));
const Reports = lazyWithRetry(() => import('./pages/Reports'));
const Quotes = lazyWithRetry(() => import('./pages/Quotes'));
const RFQQuoting = lazyWithRetry(() => import('./pages/RFQQuoting'));
const Users = lazyWithRetry(() => import('./pages/Users'));
const Customers = lazyWithRetry(() => import('./pages/Customers'));
const Calibration = lazyWithRetry(() => import('./pages/Calibration'));
const PrintTraveler = lazyWithRetry(() => import('./pages/PrintTraveler'));
const PrintPurchaseOrder = lazyWithRetry(() => import('./pages/PrintPurchaseOrder'));
const Traceability = lazyWithRetry(() => import('./pages/Traceability'));
const PrintPackingSlip = lazyWithRetry(() => import('./pages/PrintPackingSlip'));
const AuditLog = lazyWithRetry(() => import('./pages/AuditLog'));
const QuoteCalculator = lazyWithRetry(() => import('./pages/QuoteCalculator'));
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

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  
  if (isLoading) {
    return <LoadingOverlay message="Authenticating..." />;
  }
  
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" />;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isAuthenticated, isLoading } = useAuth();
  
  if (isLoading) {
    return <LoadingOverlay message="Authenticating..." />;
  }
  
  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }
  
  if (user?.role !== 'admin' && !user?.is_superuser) {
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
      <Route path="/unauthorized" element={<Unauthorized />} />
      
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
      
      {/* Warehouse (unified Inventory + Receiving + Shipping) */}
      <Route path="/warehouse" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Warehouse /></LazyRoute>
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
    <AuthProvider>
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
    </AuthProvider>
  );
}

export default App;
