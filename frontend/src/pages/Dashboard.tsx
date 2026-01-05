import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../services/api';
import { DashboardData, WorkCenterStatus } from '../types';
import {
  ClipboardDocumentListIcon,
  ExclamationTriangleIcon,
  CalendarIcon,
  CheckCircleIcon,
  CubeIcon,
  TruckIcon,
  WrenchScrewdriverIcon,
  ShieldExclamationIcon
} from '@heroicons/react/24/outline';

const workCenterTypeColors: Record<string, string> = {
  fabrication: 'bg-blue-100 text-blue-800',
  cnc_machining: 'bg-purple-100 text-purple-800',
  paint: 'bg-yellow-100 text-yellow-800',
  powder_coating: 'bg-orange-100 text-orange-800',
  assembly: 'bg-green-100 text-green-800',
  welding: 'bg-red-100 text-red-800',
  inspection: 'bg-cyan-100 text-cyan-800',
  shipping: 'bg-gray-100 text-gray-800',
};

const statusColors: Record<string, string> = {
  available: 'bg-green-500',
  in_use: 'bg-blue-500',
  maintenance: 'bg-yellow-500',
  offline: 'bg-red-500',
};

interface Alert {
  type: 'error' | 'warning' | 'info';
  message: string;
  link?: string;
}

export default function Dashboard() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [openNCRs, setOpenNCRs] = useState(0);
  const [lowInventory, setLowInventory] = useState(0);
  const [equipmentDue, setEquipmentDue] = useState(0);
  const [pendingPOs, setPendingPOs] = useState(0);

  useEffect(() => {
    loadDashboard();
    const interval = setInterval(loadDashboard, 30000); // Refresh every 30 seconds
    return () => clearInterval(interval);
  }, []);

  const loadDashboard = async () => {
    try {
      const [dashboardData, qualitySummary, equipmentDueData, lowStockData] = await Promise.all([
        api.getDashboard(),
        api.getQualitySummary().catch(() => ({ open_ncrs: 0 })),
        api.getEquipmentDueSoon(30).catch(() => []),
        api.getLowStockAlerts().catch(() => [])
      ]);
      
      setData(dashboardData);
      setOpenNCRs(qualitySummary.open_ncrs || 0);
      setEquipmentDue(equipmentDueData.length || 0);
      setLowInventory(lowStockData.length || 0);
      
      // Build alerts
      const newAlerts: Alert[] = [];
      if (dashboardData.summary.overdue > 0) {
        newAlerts.push({
          type: 'error',
          message: `${dashboardData.summary.overdue} work order(s) are overdue`,
          link: '/work-orders'
        });
      }
      if (qualitySummary.open_ncrs > 0) {
        newAlerts.push({
          type: 'warning',
          message: `${qualitySummary.open_ncrs} open NCR(s) require attention`,
          link: '/quality'
        });
      }
      if (equipmentDueData.length > 0) {
        const overdue = equipmentDueData.filter((e: any) => e.days_until_due < 0).length;
        if (overdue > 0) {
          newAlerts.push({
            type: 'error',
            message: `${overdue} equipment item(s) overdue for calibration`,
            link: '/calibration'
          });
        } else {
          newAlerts.push({
            type: 'warning',
            message: `${equipmentDueData.length} equipment item(s) due for calibration within 30 days`,
            link: '/calibration'
          });
        }
      }
      if (lowStockData.length > 0) {
        const critical = lowStockData.filter((i: any) => i.is_critical).length;
        if (critical > 0) {
          newAlerts.push({
            type: 'error',
            message: `${critical} part(s) at critical inventory levels (below safety stock)`,
            link: '/inventory'
          });
        } else {
          newAlerts.push({
            type: 'warning',
            message: `${lowStockData.length} part(s) below reorder point`,
            link: '/inventory'
          });
        }
      }
      setAlerts(newAlerts);
      setError('');
    } catch (err) {
      setError('Failed to load dashboard data');
    } finally {
      setLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg">
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Dashboard</h1>
        <div className="flex gap-2">
          <Link to="/scanner" className="btn-secondary">
            Scanner
          </Link>
          <Link to="/shop-floor" className="btn-primary">
            Shop Floor
          </Link>
        </div>
      </div>

      {/* Alerts Banner */}
      {alerts.length > 0 && (
        <div className="space-y-2">
          {alerts.map((alert, idx) => (
            <Link
              key={idx}
              to={alert.link || '#'}
              className={`block p-3 rounded-lg border ${
                alert.type === 'error' ? 'bg-red-50 border-red-200 text-red-800' :
                alert.type === 'warning' ? 'bg-yellow-50 border-yellow-200 text-yellow-800' :
                'bg-blue-50 border-blue-200 text-blue-800'
              }`}
            >
              <div className="flex items-center">
                <ExclamationTriangleIcon className="h-5 w-5 mr-2" />
                <span className="font-medium">{alert.message}</span>
                <span className="ml-auto text-sm">View &rarr;</span>
              </div>
            </Link>
          ))}
        </div>
      )}

      {/* Summary Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div className="card flex items-center">
          <div className="p-3 rounded-full bg-blue-100 mr-4">
            <ClipboardDocumentListIcon className="h-8 w-8 text-blue-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Active Work Orders</p>
            <p className="text-2xl font-bold text-gray-900">{data?.summary.active_work_orders || 0}</p>
          </div>
        </div>

        <div className="card flex items-center">
          <div className="p-3 rounded-full bg-yellow-100 mr-4">
            <CalendarIcon className="h-8 w-8 text-yellow-600" />
          </div>
          <div>
            <p className="text-sm text-gray-500">Due Today</p>
            <p className="text-2xl font-bold text-gray-900">{data?.summary.due_today || 0}</p>
          </div>
        </div>

        <div className="card flex items-center">
          <div className={`p-3 rounded-full mr-4 ${data?.summary.overdue ? 'bg-red-100' : 'bg-green-100'}`}>
            <ExclamationTriangleIcon className={`h-8 w-8 ${data?.summary.overdue ? 'text-red-600' : 'text-green-600'}`} />
          </div>
          <div>
            <p className="text-sm text-gray-500">Overdue</p>
            <p className={`text-2xl font-bold ${data?.summary.overdue ? 'text-red-600' : 'text-gray-900'}`}>
              {data?.summary.overdue || 0}
            </p>
          </div>
        </div>

        <Link to="/quality" className="card flex items-center hover:bg-gray-50">
          <div className={`p-3 rounded-full mr-4 ${openNCRs > 0 ? 'bg-orange-100' : 'bg-green-100'}`}>
            <ShieldExclamationIcon className={`h-8 w-8 ${openNCRs > 0 ? 'text-orange-600' : 'text-green-600'}`} />
          </div>
          <div>
            <p className="text-sm text-gray-500">Open NCRs</p>
            <p className={`text-2xl font-bold ${openNCRs > 0 ? 'text-orange-600' : 'text-gray-900'}`}>
              {openNCRs}
            </p>
          </div>
        </Link>

        <Link to="/calibration" className="card flex items-center hover:bg-gray-50">
          <div className={`p-3 rounded-full mr-4 ${equipmentDue > 0 ? 'bg-yellow-100' : 'bg-green-100'}`}>
            <WrenchScrewdriverIcon className={`h-8 w-8 ${equipmentDue > 0 ? 'text-yellow-600' : 'text-green-600'}`} />
          </div>
          <div>
            <p className="text-sm text-gray-500">Cal Due Soon</p>
            <p className={`text-2xl font-bold ${equipmentDue > 0 ? 'text-yellow-600' : 'text-gray-900'}`}>
              {equipmentDue}
            </p>
          </div>
        </Link>

        <Link to="/inventory" className="card flex items-center hover:bg-gray-50">
          <div className={`p-3 rounded-full mr-4 ${lowInventory > 0 ? 'bg-red-100' : 'bg-green-100'}`}>
            <CubeIcon className={`h-8 w-8 ${lowInventory > 0 ? 'text-red-600' : 'text-green-600'}`} />
          </div>
          <div>
            <p className="text-sm text-gray-500">Low Stock</p>
            <p className={`text-2xl font-bold ${lowInventory > 0 ? 'text-red-600' : 'text-gray-900'}`}>
              {lowInventory}
            </p>
          </div>
        </Link>
      </div>

      {/* Work Center Status */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Work Center Status</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
          {data?.work_centers.map((wc: WorkCenterStatus) => (
            <div key={wc.id} className="border rounded-lg p-4">
              <div className="flex items-center justify-between mb-2">
                <span className="font-medium text-gray-900">{wc.name}</span>
                <span className={`h-3 w-3 rounded-full ${statusColors[wc.status] || 'bg-gray-500'}`} />
              </div>
              <span className={`inline-block px-2 py-1 rounded text-xs font-medium ${workCenterTypeColors[wc.type] || 'bg-gray-100'}`}>
                {wc.type.replace('_', ' ')}
              </span>
              <div className="mt-3 flex justify-between text-sm">
                <div>
                  <span className="text-gray-500">Active: </span>
                  <span className="font-medium">{wc.active_operations}</span>
                </div>
                <div>
                  <span className="text-gray-500">Queued: </span>
                  <span className="font-medium">{wc.queued_operations}</span>
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Recent Completions */}
      <div className="card">
        <h2 className="text-lg font-semibold text-gray-900 mb-4">Recent Completions</h2>
        {data?.recent_completions.length ? (
          <div className="space-y-3">
            {data.recent_completions.map((completion, index) => (
              <div key={index} className="flex items-center justify-between py-2 border-b last:border-0">
                <div className="flex items-center">
                  <CheckCircleIcon className="h-5 w-5 text-green-500 mr-3" />
                  <span className="font-medium">{completion.work_order_number}</span>
                </div>
                <div className="text-sm text-gray-500">
                  Qty: {completion.quantity_complete}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-gray-500">No recent completions</p>
        )}
      </div>
    </div>
  );
}
