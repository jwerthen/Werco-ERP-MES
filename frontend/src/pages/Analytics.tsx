import React, { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import {
  ChartBarIcon,
  ArrowTrendingUpIcon,
  ArrowTrendingDownIcon,
  MinusIcon,
  ArrowPathIcon,
  CalendarDaysIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  CubeIcon,
  ClockIcon,
  CurrencyDollarIcon,
  BeakerIcon,
} from '@heroicons/react/24/outline';
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, 
  Tooltip, ResponsiveContainer
} from 'recharts';

interface KPIValue {
  value: number;
  target: number | null;
  prior_value: number | null;
  change_pct: number | null;
  trend: 'up' | 'down' | 'flat';
  sparkline: number[];
}

interface KPIDashboard {
  oee: KPIValue;
  on_time_delivery: KPIValue;
  first_pass_yield: KPIValue;
  scrap_rate: KPIValue;
  open_ncrs: KPIValue;
  quote_win_rate: KPIValue;
  backlog_hours: KPIValue;
  inventory_turnover: KPIValue;
  period_start: string;
  period_end: string;
}

interface CapacityForecast {
  week_start: string;
  week_end: string;
  work_centers: {
    work_center_id: number;
    work_center_name: string;
    committed_hours: number;
    available_hours: number;
    utilization_pct: number;
    is_overloaded: boolean;
  }[];
  overall_utilization: number;
}

interface ProductionDataPoint {
  date: string;
  units_produced: number;
  units_scrapped: number;
  total_hours: number;
}

const PERIODS = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: '7 Days' },
  { value: '30d', label: '30 Days' },
  { value: '90d', label: '90 Days' },
  { value: 'ytd', label: 'Year to Date' },
];

export default function Analytics() {
  const navigate = useNavigate();
  const [period, setPeriod] = useState('30d');
  const [loading, setLoading] = useState(true);
  const [kpis, setKpis] = useState<KPIDashboard | null>(null);
  const [capacityForecast, setCapacityForecast] = useState<CapacityForecast[]>([]);
  const [productionTrends, setProductionTrends] = useState<ProductionDataPoint[]>([]);
  const [error, setError] = useState('');

  const loadData = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const [kpiRes, capacityRes, productionRes] = await Promise.all([
        api.getKPIDashboard({ period }),
        api.getCapacityForecast(4),
        api.getProductionTrends({ period, granularity: 'day' }),
      ]);
      setKpis(kpiRes);
      setCapacityForecast(capacityRes.weeks || []);
      setProductionTrends(productionRes.time_series || []);
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load analytics data');
    } finally {
      setLoading(false);
    }
  }, [period]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const getTrendIcon = (trend: 'up' | 'down' | 'flat', isGoodUp: boolean = true) => {
    if (trend === 'up') {
      return isGoodUp 
        ? <ArrowTrendingUpIcon className="h-4 w-4 text-green-500" />
        : <ArrowTrendingUpIcon className="h-4 w-4 text-red-500" />;
    } else if (trend === 'down') {
      return isGoodUp 
        ? <ArrowTrendingDownIcon className="h-4 w-4 text-red-500" />
        : <ArrowTrendingDownIcon className="h-4 w-4 text-green-500" />;
    }
    return <MinusIcon className="h-4 w-4 text-gray-400" />;
  };

  const formatKPIValue = (value: number, type: string) => {
    if (type === 'percent') return `${value.toFixed(1)}%`;
    if (type === 'hours') return `${value.toFixed(0)}h`;
    if (type === 'count') return value.toFixed(0);
    if (type === 'ratio') return value.toFixed(2);
    return value.toString();
  };

  const renderSparkline = (data: number[]) => {
    if (!data || data.length === 0) return null;
    const max = Math.max(...data);
    const min = Math.min(...data);
    const range = max - min || 1;
    
    return (
      <div className="flex items-end h-8 gap-px">
        {data.map((val, i) => (
          <div
            key={i}
            className="bg-werco-primary/60 rounded-t w-2"
            style={{ height: `${((val - min) / range) * 100}%`, minHeight: '4px' }}
          />
        ))}
      </div>
    );
  };

  const KPICard = ({ 
    title, 
    kpi, 
    type, 
    icon: Icon, 
    isGoodUp = true,
    onClick 
  }: { 
    title: string; 
    kpi: KPIValue; 
    type: string;
    icon: React.ElementType;
    isGoodUp?: boolean;
    onClick?: () => void;
  }) => {
    const isOnTarget = kpi.target !== null && (
      isGoodUp ? kpi.value >= kpi.target : kpi.value <= kpi.target
    );
    
    return (
      <div 
        className={`card hover:shadow-lg transition-shadow cursor-pointer ${onClick ? 'hover:border-werco-primary' : ''}`}
        onClick={onClick}
      >
        <div className="flex justify-between items-start mb-2">
          <div className="flex items-center gap-2">
            <Icon className="h-5 w-5 text-gray-500" />
            <span className="text-sm font-medium text-gray-600">{title}</span>
          </div>
          {kpi.target !== null && (
            <span className={`text-xs px-2 py-0.5 rounded-full ${isOnTarget ? 'bg-green-100 text-green-700' : 'bg-amber-100 text-amber-700'}`}>
              Target: {formatKPIValue(kpi.target, type)}
            </span>
          )}
        </div>
        
        <div className="flex items-end justify-between">
          <div>
            <p className="text-3xl font-bold text-gray-900">{formatKPIValue(kpi.value, type)}</p>
            {kpi.change_pct !== null && (
              <div className="flex items-center gap-1 mt-1">
                {getTrendIcon(kpi.trend, isGoodUp)}
                <span className={`text-sm ${
                  (isGoodUp && kpi.trend === 'up') || (!isGoodUp && kpi.trend === 'down')
                    ? 'text-green-600' 
                    : kpi.trend === 'flat' 
                      ? 'text-gray-500' 
                      : 'text-red-600'
                }`}>
                  {Math.abs(kpi.change_pct).toFixed(1)}% vs prior
                </span>
              </div>
            )}
          </div>
          <div className="w-20">
            {renderSparkline(kpi.sparkline)}
          </div>
        </div>
      </div>
    );
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <ArrowPathIcon className="h-8 w-8 animate-spin text-werco-primary" />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex justify-between items-center">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Analytics Dashboard</h1>
          <p className="text-gray-500 mt-1">Real-time KPIs and business intelligence</p>
        </div>
        <div className="flex items-center gap-4">
          <select
            value={period}
            onChange={(e) => setPeriod(e.target.value)}
            className="input"
          >
            {PERIODS.map(p => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
          </select>
          <button onClick={loadData} className="btn-secondary flex items-center gap-2">
            <ArrowPathIcon className="h-4 w-4" />
            Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-xl p-4 flex items-center gap-3">
          <ExclamationTriangleIcon className="h-5 w-5 text-red-600" />
          <span className="text-red-800">{error}</span>
        </div>
      )}

      {kpis && (
        <>
          {/* KPI Cards */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <KPICard 
              title="OEE" 
              kpi={kpis.oee} 
              type="percent" 
              icon={ChartBarIcon}
              onClick={() => navigate('/analytics/production')}
            />
            <KPICard 
              title="On-Time Delivery" 
              kpi={kpis.on_time_delivery} 
              type="percent" 
              icon={CalendarDaysIcon}
            />
            <KPICard 
              title="First Pass Yield" 
              kpi={kpis.first_pass_yield} 
              type="percent" 
              icon={CheckCircleIcon}
              onClick={() => navigate('/analytics/quality')}
            />
            <KPICard 
              title="Scrap Rate" 
              kpi={kpis.scrap_rate} 
              type="percent" 
              icon={BeakerIcon}
              isGoodUp={false}
              onClick={() => navigate('/analytics/quality')}
            />
            <KPICard 
              title="Open NCRs" 
              kpi={kpis.open_ncrs} 
              type="count" 
              icon={ExclamationTriangleIcon}
              isGoodUp={false}
              onClick={() => navigate('/quality?filter=open')}
            />
            <KPICard 
              title="Quote Win Rate" 
              kpi={kpis.quote_win_rate} 
              type="percent" 
              icon={CurrencyDollarIcon}
            />
            <KPICard 
              title="Backlog Hours" 
              kpi={kpis.backlog_hours} 
              type="hours" 
              icon={ClockIcon}
            />
            <KPICard 
              title="Inventory Turnover" 
              kpi={kpis.inventory_turnover} 
              type="ratio" 
              icon={CubeIcon}
              onClick={() => navigate('/analytics/inventory')}
            />
          </div>

          {/* Charts Row */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            {/* Production Trends */}
            <div className="card">
              <div className="flex justify-between items-center mb-4">
                <h3 className="text-lg font-semibold">Production Trends</h3>
                <button 
                  onClick={() => navigate('/analytics/production')}
                  className="text-sm text-werco-primary hover:underline"
                >
                  View Details →
                </button>
              </div>
              <div className="h-64">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={productionTrends}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#E5E7EB" />
                    <XAxis 
                      dataKey="date" 
                      tick={{ fontSize: 12 }}
                      tickFormatter={(val) => new Date(val).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                    />
                    <YAxis tick={{ fontSize: 12 }} />
                    <Tooltip 
                      formatter={(value: number) => [value.toLocaleString(), '']}
                      labelFormatter={(label) => new Date(label).toLocaleDateString()}
                    />
                    <Line 
                      type="monotone" 
                      dataKey="units_produced" 
                      name="Units Produced"
                      stroke="#1B4D9C" 
                      strokeWidth={2}
                      dot={false}
                    />
                    <Line 
                      type="monotone" 
                      dataKey="units_scrapped" 
                      name="Units Scrapped"
                      stroke="#C8352B" 
                      strokeWidth={2}
                      dot={false}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </div>

            {/* Capacity Forecast */}
            <div className="card">
              <div className="flex justify-between items-center mb-4">
                <h3 className="text-lg font-semibold">Capacity Forecast (4 Weeks)</h3>
                <button 
                  onClick={() => navigate('/analytics/forecasting')}
                  className="text-sm text-werco-primary hover:underline"
                >
                  View Details →
                </button>
              </div>
              {capacityForecast.length > 0 && capacityForecast[0].work_centers && (
                <div className="space-y-3">
                  {capacityForecast[0].work_centers.map((wc) => (
                    <div key={wc.work_center_id}>
                      <div className="flex justify-between text-sm mb-1">
                        <span className="font-medium">{wc.work_center_name}</span>
                        <span className={wc.is_overloaded ? 'text-red-600 font-semibold' : 'text-gray-600'}>
                          {wc.utilization_pct.toFixed(0)}%
                        </span>
                      </div>
                      <div className="h-3 bg-gray-200 rounded-full overflow-hidden">
                        <div 
                          className={`h-full rounded-full transition-all ${
                            wc.utilization_pct > 90 ? 'bg-red-500' :
                            wc.utilization_pct > 75 ? 'bg-amber-500' : 'bg-green-500'
                          }`}
                          style={{ width: `${Math.min(wc.utilization_pct, 100)}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Quick Links */}
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <button
              onClick={() => navigate('/analytics/production')}
              className="card hover:border-werco-primary transition-colors text-left"
            >
              <ChartBarIcon className="h-8 w-8 text-werco-primary mb-2" />
              <h4 className="font-semibold">Production Analytics</h4>
              <p className="text-sm text-gray-500">OEE, trends, utilization</p>
            </button>
            <button
              onClick={() => navigate('/analytics/costs')}
              className="card hover:border-werco-primary transition-colors text-left"
            >
              <CurrencyDollarIcon className="h-8 w-8 text-werco-primary mb-2" />
              <h4 className="font-semibold">Cost Analysis</h4>
              <p className="text-sm text-gray-500">Job costs, margins</p>
            </button>
            <button
              onClick={() => navigate('/analytics/quality')}
              className="card hover:border-werco-primary transition-colors text-left"
            >
              <BeakerIcon className="h-8 w-8 text-werco-primary mb-2" />
              <h4 className="font-semibold">Quality Metrics</h4>
              <p className="text-sm text-gray-500">Defects, NCRs, yield</p>
            </button>
            <button
              onClick={() => navigate('/analytics/reports')}
              className="card hover:border-werco-primary transition-colors text-left"
            >
              <ClockIcon className="h-8 w-8 text-werco-primary mb-2" />
              <h4 className="font-semibold">Custom Reports</h4>
              <p className="text-sm text-gray-500">Build & export reports</p>
            </button>
          </div>
        </>
      )}
    </div>
  );
}
