import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { AuthProvider, useAuth } from './context/AuthContext';
import { TourProvider } from './context/TourContext';
import { TourHighlight } from './components/Tour';
import Layout from './components/Layout';
import Login from './pages/Login';
import Dashboard from './pages/Dashboard';
import Unauthorized from './pages/Unauthorized';
import WorkOrders from './pages/WorkOrders';
import WorkOrderNew from './pages/WorkOrderNew';
import WorkOrderDetail from './pages/WorkOrderDetail';
import ShopFloor from './pages/ShopFloor';
import WorkCenters from './pages/WorkCenters';
import Parts from './pages/Parts';
import BOM from './pages/BOM';
import Routing from './pages/Routing';
import Inventory from './pages/Inventory';
import MRP from './pages/MRP';
import Quality from './pages/Quality';
import CustomFields from './pages/CustomFields';
import Purchasing from './pages/Purchasing';
import Scheduling from './pages/Scheduling';
import Documents from './pages/Documents';
import Reports from './pages/Reports';
import Shipping from './pages/Shipping';
import Quotes from './pages/Quotes';
import Users from './pages/Users';
import Customers from './pages/Customers';
import Scanner from './pages/Scanner';
import Calibration from './pages/Calibration';
import PrintTraveler from './pages/PrintTraveler';
import ScannerMappings from './pages/ScannerMappings';
import Traceability from './pages/Traceability';
import PrintPackingSlip from './pages/PrintPackingSlip';
import AuditLog from './pages/AuditLog';
import QuoteCalculator from './pages/QuoteCalculator';
import AdminSettings from './pages/AdminSettings';
import Receiving from './pages/Receiving';
import POUpload from './pages/POUpload';
import Analytics from './pages/Analytics';
import ShopFloorSimple from './pages/ShopFloorSimple';

function PrivateRoute({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isLoading } = useAuth();
  
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }
  
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" />;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isAuthenticated, isLoading } = useAuth();
  
  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }
  
  if (!isAuthenticated) {
    return <Navigate to="/login" />;
  }
  
  // Allow admin role or superuser
  if (user?.role !== 'admin' && !user?.is_superuser) {
    return <Navigate to="/unauthorized" />;
  }
  
  return <>{children}</>;
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/unauthorized" element={<Unauthorized />} />
      <Route path="/" element={
        <PrivateRoute>
          <Layout>
            <Dashboard />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/work-orders" element={
        <PrivateRoute>
          <Layout>
            <WorkOrders />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/work-orders/new" element={
        <PrivateRoute>
          <Layout>
            <WorkOrderNew />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/work-orders/:id" element={
        <PrivateRoute>
          <Layout>
            <WorkOrderDetail />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/shop-floor" element={
        <PrivateRoute>
          <Layout>
            <ShopFloor />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/shop-floor/operations" element={
        <PrivateRoute>
          <Layout>
            <ShopFloorSimple />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/work-centers" element={
        <PrivateRoute>
          <Layout>
            <WorkCenters />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/parts" element={
        <PrivateRoute>
          <Layout>
            <Parts />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/bom" element={
        <PrivateRoute>
          <Layout>
            <BOM />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/routing" element={
        <PrivateRoute>
          <Layout>
            <Routing />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/inventory" element={
        <PrivateRoute>
          <Layout>
            <Inventory />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/mrp" element={
        <PrivateRoute>
          <Layout>
            <MRP />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/quality" element={
        <PrivateRoute>
          <Layout>
            <Quality />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/custom-fields" element={
        <PrivateRoute>
          <Layout>
            <CustomFields />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/purchasing" element={
        <PrivateRoute>
          <Layout>
            <Purchasing />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/receiving" element={
        <PrivateRoute>
          <Layout>
            <Receiving />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/po-upload" element={
        <PrivateRoute>
          <Layout>
            <POUpload />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/scheduling" element={
        <PrivateRoute>
          <Layout>
            <Scheduling />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/documents" element={
        <PrivateRoute>
          <Layout>
            <Documents />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/reports" element={
        <PrivateRoute>
          <Layout>
            <Reports />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/shipping" element={
        <PrivateRoute>
          <Layout>
            <Shipping />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/quotes" element={
        <PrivateRoute>
          <Layout>
            <Quotes />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/users" element={
        <PrivateRoute>
          <Layout>
            <Users />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/customers" element={
        <PrivateRoute>
          <Layout>
            <Customers />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/scanner" element={
        <PrivateRoute>
          <Layout>
            <Scanner />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/scanner/mappings" element={
        <PrivateRoute>
          <Layout>
            <ScannerMappings />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/calibration" element={
        <PrivateRoute>
          <Layout>
            <Calibration />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/print/traveler/:id" element={
        <PrivateRoute>
          <PrintTraveler />
        </PrivateRoute>
      } />
      <Route path="/traceability" element={
        <PrivateRoute>
          <Layout>
            <Traceability />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/print/packing-slip/:id" element={
        <PrivateRoute>
          <PrintPackingSlip />
        </PrivateRoute>
      } />
      <Route path="/audit-log" element={
        <PrivateRoute>
          <Layout>
            <AuditLog />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/quote-calculator" element={
        <PrivateRoute>
          <Layout>
            <QuoteCalculator />
          </Layout>
        </PrivateRoute>
      } />
      <Route path="/admin/settings" element={
        <AdminRoute>
          <Layout>
            <AdminSettings />
          </Layout>
        </AdminRoute>
      } />
      <Route path="/analytics" element={
        <PrivateRoute>
          <Layout>
            <Analytics />
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
          <AppRoutes />
          <TourHighlight />
        </Router>
      </TourProvider>
    </AuthProvider>
  );
}

export default App;
