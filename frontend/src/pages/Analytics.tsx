import React, { useEffect, useState, useCallback, useMemo, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import api from '../services/api';
import { useWebSocket } from '../hooks/useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
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

interface OEEComponents {
  availability: number;
  performance: number;
  quality: number;
  oee: number;
}

interface OEEDataPoint {
  date: string;
  work_center_name?: string | null;
  availability: number;
  performance: number;
  quality: number;
  oee: number;
  planned_time?: number;
  operating_time?: number;
  downtime?: number;
  ideal_cycle_time?: number;
  actual_cycle_time?: number;
  total_units?: number;
  good_units?: number;
  defect_units?: number;
}

interface OEEResponse {
  summary: OEEComponents;
  time_series: OEEDataPoint[];
  by_work_center: OEEDataPoint[];
}

interface ProductionTrendsResponse {
  time_series: ProductionDataPoint[];
  totals: Record<string, number>;
  by_group?: Record<string, ProductionDataPoint[]>;
}

interface QualityParetoItem {
  defect_type: string;
  count: number;
  percentage: number;
  cumulative_pct: number;
}

interface QualitySeriesPoint {
  date: string;
  defect_rate: number;
  first_pass_yield: number;
  ncr_count: number;
  units_inspected: number;
  units_passed: number;
  units_failed: number;
}

interface VendorQuality {
  vendor_name: string;
  acceptance_rate: number;
  ncr_count: number;
}

interface QualityMetricsResponse {
  summary: Record<string, number>;
  defect_pareto: QualityParetoItem[];
  time_series: QualitySeriesPoint[];
  by_vendor: VendorQuality[];
  control_limits: Record<string, number>;
}

interface InventoryTurnoverItem {
  category?: string | null;
  part_number?: string | null;
  avg_inventory_value: number;
  cogs: number;
  turnover_ratio: number;
  days_on_hand: number;
}

interface StockTrendItem {
  date: string;
  part_number: string;
  quantity_on_hand: number;
  reorder_point: number;
  is_below_reorder: boolean;
}

interface InventoryAnalyticsResponse {
  turnover_by_category: InventoryTurnoverItem[];
  low_turnover_items: InventoryTurnoverItem[];
  stock_trends: StockTrendItem[];
  summary: Record<string, number>;
}

interface CostJobItem {
  work_order_id: number;
  work_order_number: string;
  part_number?: string | null;
  customer_name?: string | null;
  estimated_cost: number;
  actual_cost: number;
  variance: number;
  variance_pct: number;
  margin?: number | null;
  margin_pct?: number | null;
}

interface CostAnalysisResponse {
  jobs: CostJobItem[];
  summary: Record<string, number>;
  avg_margin: number;
  avg_variance_pct: number;
  time_series: Record<string, number>[];
}

interface CapacityForecastResponse {
  weeks: CapacityForecast[];
  alerts: Record<string, any>[];
}

interface InventoryDemandItem {
  part_number: string;
  part_name: string;
  current_stock: number;
  predicted_stockout_date?: string | null;
  days_until_stockout?: number | null;
  urgency: string;
}

interface InventoryDemandResponse {
  predictions: InventoryDemandItem[];
  critical_count: number;
  warning_count: number;
}

interface ReportTemplate {
  id: number;
  name: string;
  description?: string | null;
  data_source: string;
  is_shared: boolean;
  created_at: string;
}

interface DataSourceField {
  name: string;
  type: string;
  label: string;
}

interface DataSourceInfo {
  label: string;
  fields: DataSourceField[];
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
  const location = useLocation();
  const [period, setPeriod] = useState('30d');
  const [loading, setLoading] = useState(true);
  const [kpis, setKpis] = useState<KPIDashboard | null>(null);
  const [capacityForecast, setCapacityForecast] = useState<CapacityForecast[]>([]);
  const [productionTrends, setProductionTrends] = useState<ProductionDataPoint[]>([]);
  const [oeeDetails, setOeeDetails] = useState<OEEResponse | null>(null);
  const [productionDetail, setProductionDetail] = useState<ProductionTrendsResponse | null>(null);
  const [qualityMetrics, setQualityMetrics] = useState<QualityMetricsResponse | null>(null);
  const [inventoryAnalytics, setInventoryAnalytics] = useState<InventoryAnalyticsResponse | null>(null);
  const [costAnalysis, setCostAnalysis] = useState<CostAnalysisResponse | null>(null);
  const [capacityForecastDetail, setCapacityForecastDetail] = useState<CapacityForecastResponse | null>(null);
  const [inventoryDemand, setInventoryDemand] = useState<InventoryDemandResponse | null>(null);
  const [reportTemplates, setReportTemplates] = useState<ReportTemplate[]>([]);
  const [dataSources, setDataSources] = useState<Record<string, DataSourceInfo>>({});
  const [error, setError] = useState('');
  const realtimeRefreshRef = useRef<NodeJS.Timeout | null>(null);
  const view = useMemo(() => {
    const path = location.pathname.replace(/\/+$/, '');
    if (path === '/analytics') return 'overview';
    const [, , section] = path.split('/');
    switch (section) {
      case 'production':
      case 'quality':
      case 'inventory':
      case 'forecasting':
      case 'costs':
      case 'reports':
        return section;
      default:
        return 'overview';
    }
  }, [location.pathname]);
  const realtimeUrl = useMemo(() => {
    const token = getAccessToken();
    return buildWsUrl('/ws/updates', token ? { token } : undefined);
  }, []);

  const loadData = useCallback(async (showLoading = true) => {
    if (showLoading) {
      setLoading(true);
    }
    setError('');
    try {
      if (view === 'overview') {
        const [kpiRes, capacityRes, productionRes] = await Promise.all([
          api.getKPIDashboard({ period }),
          api.getCapacityForecast(4),
          api.getProductionTrends({ period, granularity: 'day' }),
        ]);
        setKpis(kpiRes);
        setCapacityForecast(capacityRes.weeks || []);
        setProductionTrends(productionRes.time_series || []);
        return;
      }

      if (view === 'production') {
        const [oeeRes, productionRes] = await Promise.all([
          api.getOEEDetails({ period, granularity: 'day' }),
          api.getProductionTrends({ period, granularity: 'day' }),
        ]);
        setOeeDetails(oeeRes);
        setProductionDetail(productionRes);
        return;
      }

      if (view === 'quality') {
        const qualityRes = await api.getAnalyticsQualityMetrics({ period, metric_type: 'all' });
        setQualityMetrics(qualityRes);
        return;
      }

      if (view === 'inventory') {
        const inventoryRes = await api.getInventoryTurnover({ period });
        setInventoryAnalytics(inventoryRes);
        return;
      }

      if (view === 'forecasting') {
        const [capacityRes, inventoryRes] = await Promise.all([
          api.getCapacityForecast(6),
          api.getInventoryDemandPrediction(),
        ]);
        setCapacityForecastDetail(capacityRes);
        setInventoryDemand(inventoryRes);
        return;
      }

      if (view === 'costs') {
        const costRes = await api.getCostAnalysis({ period });
        setCostAnalysis(costRes);
        return;
      }

      if (view === 'reports') {
        const [templatesRes, sourcesRes] = await Promise.all([
          api.getReportTemplates(),
          api.getDataSources(),
        ]);
        setReportTemplates(templatesRes || []);
        setDataSources(sourcesRes || {});
        return;
      }
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Failed to load analytics data');
    } finally {
      if (showLoading) {
        setLoading(false);
      }
    }
  }, [period, view]);

  const scheduleRealtimeRefresh = useCallback(() => {
    if (realtimeRefreshRef.current) return;
    realtimeRefreshRef.current = setTimeout(() => {
      realtimeRefreshRef.current = null;
      loadData(false);
    }, 1200);
  }, [loadData]);

  useWebSocket({
    url: realtimeUrl,
    enabled: true,
    onMessage: (message) => {
      if (message.type === 'connected' || message.type === 'ping') return;
      if (['dashboard_update', 'work_order_update', 'shop_floor_update', 'quality_alert'].includes(message.type)) {
        scheduleRealtimeRefresh();
      }
    }
  });

  useEffect(() => {
    loadData(true);
  }, [loadData]);

  useEffect(() => {
    return () => {
      if (realtimeRefreshRef.current) {
        clearTimeout(realtimeRefreshRef.current);
        realtimeRefreshRef.current = null;
      }
    };
  }, []);

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

  const formatMetric = (value: number | null | undefined, suffix: string = '') => {
    if (value === null || value === undefined || Number.isNaN(value)) return '—';
    return `${value.toFixed(1)}${suffix}`;
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

  const viewMeta = useMemo(() => {
    switch (view) {
      case 'production':
        return { title: 'Production Analytics', subtitle: 'OEE, throughput, and production trends' };
      case 'quality':
        return { title: 'Quality Analytics', subtitle: 'Defects, NCRs, and yield performance' };
      case 'inventory':
        return { title: 'Inventory Analytics', subtitle: 'Turnover, stock trends, and slow movers' };
      case 'forecasting':
        return { title: 'Forecasting', subtitle: 'Capacity outlook and inventory risk' };
      case 'costs':
        return { title: 'Cost Analytics', subtitle: 'Job cost variance and margins' };
      case 'reports':
        return { title: 'Custom Reports', subtitle: 'Report templates and data sources' };
      default:
        return { title: 'Analytics Dashboard', subtitle: 'Real-time KPIs and business intelligence' };
    }
  }, [view]);

  const showPeriodSelector = view !== 'reports';

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
          <h1 className="text-3xl font-bold text-gray-900">{viewMeta.title}</h1>
          <p className="text-gray-500 mt-1">{viewMeta.subtitle}</p>
        </div>
        <div className="flex items-center gap-4">
          {showPeriodSelector && (
            <select
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
              className="input"
            >
              {PERIODS.map(p => (
                <option key={p.value} value={p.value}>{p.label}</option>
              ))}
            </select>
          )}
          <button onClick={() => loadData(true)} className="btn-secondary flex items-center gap-2">
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

      {view === 'overview' && kpis && (
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

      {view === 'production' && (
        <div className="space-y-6">
          {oeeDetails ? (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="card">
                  <p className="text-sm text-gray-500">Availability</p>
                  <p className="text-2xl font-semibold">{formatMetric(oeeDetails.summary.availability, '%')}</p>
                </div>
                <div className="card">
                  <p className="text-sm text-gray-500">Performance</p>
                  <p className="text-2xl font-semibold">{formatMetric(oeeDetails.summary.performance, '%')}</p>
                </div>
                <div className="card">
                  <p className="text-sm text-gray-500">Quality</p>
                  <p className="text-2xl font-semibold">{formatMetric(oeeDetails.summary.quality, '%')}</p>
                </div>
                <div className="card">
                  <p className="text-sm text-gray-500">OEE</p>
                  <p className="text-2xl font-semibold text-werco-primary">{formatMetric(oeeDetails.summary.oee, '%')}</p>
                </div>
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div className="card">
                  <h3 className="text-lg font-semibold mb-4">OEE Trend</h3>
                  <div className="h-64">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={oeeDetails.time_series || []}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#E5E7EB" />
                        <XAxis
                          dataKey="date"
                          tick={{ fontSize: 12 }}
                          tickFormatter={(val) => new Date(val).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                        />
                        <YAxis tick={{ fontSize: 12 }} />
                        <Tooltip
                          formatter={(value: number) => [`${value.toFixed(1)}%`, '']}
                          labelFormatter={(label) => new Date(label).toLocaleDateString()}
                        />
                        <Line type="monotone" dataKey="oee" name="OEE" stroke="#1B4D9C" strokeWidth={2} dot={false} />
                        <Line type="monotone" dataKey="availability" name="Availability" stroke="#10B981" strokeWidth={2} dot={false} />
                        <Line type="monotone" dataKey="performance" name="Performance" stroke="#F59E0B" strokeWidth={2} dot={false} />
                        <Line type="monotone" dataKey="quality" name="Quality" stroke="#EF4444" strokeWidth={2} dot={false} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>

                <div className="card">
                  <h3 className="text-lg font-semibold mb-4">Production Trends</h3>
                  <div className="h-64">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={productionDetail?.time_series || []}>
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
                        <Line type="monotone" dataKey="units_produced" name="Units Produced" stroke="#1B4D9C" strokeWidth={2} dot={false} />
                        <Line type="monotone" dataKey="units_scrapped" name="Units Scrapped" stroke="#C8352B" strokeWidth={2} dot={false} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>
              </div>

              <div className="card">
                <h3 className="text-lg font-semibold mb-4">OEE by Work Center</h3>
                {oeeDetails.by_work_center?.length ? (
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-gray-200 text-sm">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-4 py-2 text-left font-medium text-gray-500 uppercase">Work Center</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Availability</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Performance</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Quality</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">OEE</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {oeeDetails.by_work_center.map((wc, idx) => (
                          <tr key={`${wc.work_center_name || 'wc'}-${idx}`}>
                            <td className="px-4 py-2">{wc.work_center_name || 'Unassigned'}</td>
                            <td className="px-4 py-2 text-right">{formatMetric(wc.availability, '%')}</td>
                            <td className="px-4 py-2 text-right">{formatMetric(wc.performance, '%')}</td>
                            <td className="px-4 py-2 text-right">{formatMetric(wc.quality, '%')}</td>
                            <td className="px-4 py-2 text-right font-semibold">{formatMetric(wc.oee, '%')}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-sm text-gray-500">No work center data for this period.</p>
                )}
              </div>
            </>
          ) : (
            <div className="card">
              <p className="text-sm text-gray-500">No production analytics available.</p>
            </div>
          )}
        </div>
      )}

      {view === 'quality' && (
        <div className="space-y-6">
          {qualityMetrics ? (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {Object.entries(qualityMetrics.summary || {}).map(([key, value]) => (
                  <div key={key} className="card">
                    <p className="text-sm text-gray-500 capitalize">{key.replace(/_/g, ' ')}</p>
                    <p className="text-2xl font-semibold">{formatMetric(value)}</p>
                  </div>
                ))}
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div className="card">
                  <h3 className="text-lg font-semibold mb-4">Quality Trend</h3>
                  <div className="h-64">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={qualityMetrics.time_series || []}>
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
                        <Line type="monotone" dataKey="defect_rate" name="Defect Rate" stroke="#C8352B" strokeWidth={2} dot={false} />
                        <Line type="monotone" dataKey="first_pass_yield" name="First Pass Yield" stroke="#10B981" strokeWidth={2} dot={false} />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </div>

                <div className="card">
                  <h3 className="text-lg font-semibold mb-4">Defect Pareto</h3>
                  {qualityMetrics.defect_pareto?.length ? (
                    <div className="space-y-2">
                      {qualityMetrics.defect_pareto.slice(0, 8).map((item) => (
                        <div key={item.defect_type} className="flex items-center justify-between text-sm">
                          <span className="font-medium">{item.defect_type}</span>
                          <span className="text-gray-600">{item.count} ({item.percentage.toFixed(1)}%)</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-500">No defect pareto data.</p>
                  )}
                </div>
              </div>

              <div className="card">
                <h3 className="text-lg font-semibold mb-4">Vendor Quality</h3>
                {qualityMetrics.by_vendor?.length ? (
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-gray-200 text-sm">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-4 py-2 text-left font-medium text-gray-500 uppercase">Vendor</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Acceptance Rate</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">NCR Count</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {qualityMetrics.by_vendor.map((vendor) => (
                          <tr key={vendor.vendor_name}>
                            <td className="px-4 py-2">{vendor.vendor_name}</td>
                            <td className="px-4 py-2 text-right">{formatMetric(vendor.acceptance_rate, '%')}</td>
                            <td className="px-4 py-2 text-right">{vendor.ncr_count}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-sm text-gray-500">No vendor quality data.</p>
                )}
              </div>
            </>
          ) : (
            <div className="card">
              <p className="text-sm text-gray-500">No quality analytics available.</p>
            </div>
          )}
        </div>
      )}

      {view === 'inventory' && (
        <div className="space-y-6">
          {inventoryAnalytics ? (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                {Object.entries(inventoryAnalytics.summary || {}).map(([key, value]) => (
                  <div key={key} className="card">
                    <p className="text-sm text-gray-500 capitalize">{key.replace(/_/g, ' ')}</p>
                    <p className="text-2xl font-semibold">{formatMetric(value)}</p>
                  </div>
                ))}
              </div>

              <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div className="card">
                  <h3 className="text-lg font-semibold mb-4">Turnover by Category</h3>
                  {inventoryAnalytics.turnover_by_category?.length ? (
                    <div className="overflow-x-auto">
                      <table className="min-w-full divide-y divide-gray-200 text-sm">
                        <thead className="bg-gray-50">
                          <tr>
                            <th className="px-4 py-2 text-left font-medium text-gray-500 uppercase">Category</th>
                            <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Turnover</th>
                            <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Days on Hand</th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-100">
                          {inventoryAnalytics.turnover_by_category.map((item, idx) => (
                            <tr key={`${item.category || 'cat'}-${idx}`}>
                              <td className="px-4 py-2">{item.category || 'Uncategorized'}</td>
                              <td className="px-4 py-2 text-right">{formatMetric(item.turnover_ratio)}</td>
                              <td className="px-4 py-2 text-right">{formatMetric(item.days_on_hand)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="text-sm text-gray-500">No turnover data.</p>
                  )}
                </div>

                <div className="card">
                  <h3 className="text-lg font-semibold mb-4">Low Turnover Items</h3>
                  {inventoryAnalytics.low_turnover_items?.length ? (
                    <div className="space-y-2">
                      {inventoryAnalytics.low_turnover_items.slice(0, 8).map((item, idx) => (
                        <div key={`${item.part_number || 'part'}-${idx}`} className="flex items-center justify-between text-sm">
                          <span className="font-medium">{item.part_number || 'Unknown Part'}</span>
                          <span className="text-gray-600">{formatMetric(item.turnover_ratio)} turns</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <p className="text-sm text-gray-500">No low-turnover items.</p>
                  )}
                </div>
              </div>
            </>
          ) : (
            <div className="card">
              <p className="text-sm text-gray-500">No inventory analytics available.</p>
            </div>
          )}
        </div>
      )}

      {view === 'forecasting' && (
        <div className="space-y-6">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <div className="card">
              <h3 className="text-lg font-semibold mb-4">Capacity Forecast</h3>
              {capacityForecastDetail?.weeks?.length ? (
                <div className="space-y-4">
                  {capacityForecastDetail.weeks.slice(0, 2).map((week) => (
                    <div key={week.week_start}>
                      <div className="flex justify-between text-sm mb-2">
                        <span className="font-medium">
                          {new Date(week.week_start).toLocaleDateString()} - {new Date(week.week_end).toLocaleDateString()}
                        </span>
                        <span className="text-gray-600">{week.overall_utilization.toFixed(0)}%</span>
                      </div>
                      <div className="space-y-2">
                        {week.work_centers.slice(0, 5).map((wc) => (
                          <div key={wc.work_center_id}>
                            <div className="flex justify-between text-xs text-gray-600 mb-1">
                              <span>{wc.work_center_name}</span>
                              <span>{wc.utilization_pct.toFixed(0)}%</span>
                            </div>
                            <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                              <div
                                className={`h-full rounded-full ${wc.utilization_pct > 90 ? 'bg-red-500' : wc.utilization_pct > 75 ? 'bg-amber-500' : 'bg-green-500'}`}
                                style={{ width: `${Math.min(wc.utilization_pct, 100)}%` }}
                              />
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-500">No capacity forecast data.</p>
              )}
            </div>

            <div className="card">
              <h3 className="text-lg font-semibold mb-4">Inventory Demand Risk</h3>
              {inventoryDemand?.predictions?.length ? (
                <div className="space-y-2">
                  {inventoryDemand.predictions.slice(0, 8).map((item) => (
                    <div key={item.part_number} className="flex items-center justify-between text-sm">
                      <div>
                        <div className="font-medium">{item.part_number}</div>
                        <div className="text-xs text-gray-500">{item.part_name}</div>
                      </div>
                      <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                        item.urgency === 'critical' ? 'bg-red-100 text-red-700' :
                        item.urgency === 'warning' ? 'bg-amber-100 text-amber-700' :
                        'bg-emerald-100 text-emerald-700'
                      }`}>
                        {item.urgency}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-gray-500">No inventory risk data.</p>
              )}
            </div>
          </div>
        </div>
      )}

      {view === 'costs' && (
        <div className="space-y-6">
          {costAnalysis ? (
            <>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <div className="card">
                  <p className="text-sm text-gray-500">Avg Margin</p>
                  <p className="text-2xl font-semibold">{formatMetric(costAnalysis.avg_margin, '%')}</p>
                </div>
                <div className="card">
                  <p className="text-sm text-gray-500">Avg Variance</p>
                  <p className="text-2xl font-semibold">{formatMetric(costAnalysis.avg_variance_pct, '%')}</p>
                </div>
              </div>

              <div className="card">
                <h3 className="text-lg font-semibold mb-4">Job Cost Summary</h3>
                {costAnalysis.jobs?.length ? (
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-gray-200 text-sm">
                      <thead className="bg-gray-50">
                        <tr>
                          <th className="px-4 py-2 text-left font-medium text-gray-500 uppercase">WO #</th>
                          <th className="px-4 py-2 text-left font-medium text-gray-500 uppercase">Part</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Estimated</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Actual</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Variance</th>
                          <th className="px-4 py-2 text-right font-medium text-gray-500 uppercase">Margin</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-gray-100">
                        {costAnalysis.jobs.slice(0, 12).map((job) => (
                          <tr key={job.work_order_id}>
                            <td className="px-4 py-2 font-medium">{job.work_order_number}</td>
                            <td className="px-4 py-2">{job.part_number || '—'}</td>
                            <td className="px-4 py-2 text-right">${job.estimated_cost.toFixed(2)}</td>
                            <td className="px-4 py-2 text-right">${job.actual_cost.toFixed(2)}</td>
                            <td className={`px-4 py-2 text-right ${job.variance > 0 ? 'text-red-600' : 'text-emerald-600'}`}>
                              {job.variance.toFixed(2)}
                            </td>
                            <td className="px-4 py-2 text-right">{job.margin_pct !== null && job.margin_pct !== undefined ? `${job.margin_pct.toFixed(1)}%` : '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <p className="text-sm text-gray-500">No cost analysis data.</p>
                )}
              </div>
            </>
          ) : (
            <div className="card">
              <p className="text-sm text-gray-500">No cost analytics available.</p>
            </div>
          )}
        </div>
      )}

      {view === 'reports' && (
        <div className="space-y-6">
          <div className="card">
            <h3 className="text-lg font-semibold mb-4">Report Templates</h3>
            {reportTemplates.length ? (
              <div className="space-y-3">
                {reportTemplates.map((template) => (
                  <div key={template.id} className="border border-gray-200 rounded-lg p-3">
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="font-medium">{template.name}</p>
                        <p className="text-sm text-gray-500">{template.description || template.data_source}</p>
                      </div>
                      {template.is_shared && (
                        <span className="text-xs px-2 py-0.5 rounded-full bg-werco-50 text-werco-primary">Shared</span>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-gray-500">No report templates available.</p>
            )}
          </div>

          <div className="card">
            <h3 className="text-lg font-semibold mb-4">Available Data Sources</h3>
            {Object.keys(dataSources).length ? (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {Object.entries(dataSources).map(([key, info]) => (
                  <div key={key} className="border border-gray-200 rounded-lg p-3">
                    <p className="font-medium">{info.label}</p>
                    <p className="text-sm text-gray-500">{info.fields.length} fields</p>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-gray-500">No data sources found.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
