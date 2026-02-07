import React, { Suspense, lazy } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, useLocation } from 'react-router-dom';
import { AuthProvider, useAuth } from './context/AuthContext';
import { TourProvider } from './context/TourContext';
import { KeyboardShortcutsProvider } from './context/KeyboardShortcutsContext';
import { TourHighlight } from './components/Tour';
import Layout from './components/Layout';
import { SkeletonDashboard, LoadingOverlay } from './components/ui/Skeleton';
import { isKioskMode, syncKioskMode } from './utils/kiosk';

// Eagerly loaded - critical path
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Unauthorized from './pages/Unauthorized';

// Lazy loaded pages - code splitting for better performance
const WorkOrders = lazy(() => import('./pages/WorkOrders'));
const WorkOrderNew = lazy(() => import('./pages/WorkOrderNew'));
const WorkOrderDetail = lazy(() => import('./pages/WorkOrderDetail'));
const ShopFloor = lazy(() => import('./pages/ShopFloor'));
const ShopFloorSimple = lazy(() => import('./pages/ShopFloorSimple'));
const WorkCenters = lazy(() => import('./pages/WorkCenters'));
const Parts = lazy(() => import('./pages/Parts'));
const BOM = lazy(() => import('./pages/BOM'));
const Routing = lazy(() => import('./pages/Routing'));
const Inventory = lazy(() => import('./pages/Inventory'));
const MRP = lazy(() => import('./pages/MRP'));
const Quality = lazy(() => import('./pages/Quality'));
const CustomFields = lazy(() => import('./pages/CustomFields'));
const Purchasing = lazy(() => import('./pages/Purchasing'));
const Scheduling = lazy(() => import('./pages/Scheduling'));
const Documents = lazy(() => import('./pages/Documents'));
const Reports = lazy(() => import('./pages/Reports'));
const Shipping = lazy(() => import('./pages/Shipping'));
const Quotes = lazy(() => import('./pages/Quotes'));
const RFQQuoting = lazy(() => import('./pages/RFQQuoting'));
const Users = lazy(() => import('./pages/Users'));
const Customers = lazy(() => import('./pages/Customers'));
const Calibration = lazy(() => import('./pages/Calibration'));
const PrintTraveler = lazy(() => import('./pages/PrintTraveler'));
const PrintPurchaseOrder = lazy(() => import('./pages/PrintPurchaseOrder'));
const Traceability = lazy(() => import('./pages/Traceability'));
const PrintPackingSlip = lazy(() => import('./pages/PrintPackingSlip'));
const AuditLog = lazy(() => import('./pages/AuditLog'));
const QuoteCalculator = lazy(() => import('./pages/QuoteCalculator'));
const AdminSettings = lazy(() => import('./pages/AdminSettings'));
const Receiving = lazy(() => import('./pages/Receiving'));
const POUpload = lazy(() => import('./pages/POUpload'));
const Analytics = lazy(() => import('./pages/Analytics'));

// Loading fallback for lazy-loaded pages
const PageLoader = () => (
  <div className="p-6">
    <SkeletonDashboard />
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

function KioskOnly({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const kioskMode = isKioskMode(location.pathname, location.search);

  if (!kioskMode) {
    return <Navigate to="/shop-floor" replace />;
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
    <Routes>
      <Route path="/login" element={<Login />} />
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
            <KioskOnly>
              <Layout>
                <LazyRoute><ShopFloorSimple /></LazyRoute>
              </Layout>
            </KioskOnly>
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
      
      {/* Inventory & MRP */}
      <Route path="/inventory" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Inventory /></LazyRoute>
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/inventory/parts" element={
        <PrivateRoute>
          <Navigate to="/inventory?group=parts" replace />
        </PrivateRoute>
      } />
      <Route path="/inventory/materials" element={
        <PrivateRoute>
          <Navigate to="/inventory?group=materials" replace />
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
          <Layout>
            <LazyRoute><Receiving /></LazyRoute>
          </Layout>
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
      
      {/* Shipping */}
      <Route path="/shipping" element={
        <PrivateRoute>
          <Layout>
            <LazyRoute><Shipping /></LazyRoute>
          </Layout>
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
    </Routes>
  );
}

function App() {
  return (
    <AuthProvider>
      <TourProvider>
        <Router>
          <KeyboardShortcutsProvider>
            <AppRoutes />
            <TourHighlight />
          </KeyboardShortcutsProvider>
        </Router>
      </TourProvider>
    </AuthProvider>
  );
}

export default App;
