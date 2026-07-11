import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import {
  PlusIcon,
  XMarkIcon,
  ArrowPathIcon,
  ChartBarIcon,
  CogIcon,
  CalendarDaysIcon,
  ClockIcon,
  BoltIcon,
  CheckBadgeIcon,
} from '@heroicons/react/24/outline';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import { EmptyState, ErrorState, FormField, useToast } from '../components/ui';
import { formatCentralDate, getCentralDateStamp, getCentralTodayISODate } from '../utils/centralTime';

// ============== Types ==============

interface WorkCenter {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

// NOTE: these mirror the REAL response shapes built in backend/app/api/endpoints/oee.py
// (dashboard ~L557, records _record_to_response ~L162, trends ~L616). The metric fields
// are `*_pct`, and the per-work-center dashboard metrics are legitimately `null` until an
// OEE record exists for that work center in the window — render them null-safe.
interface WorkCenterOEE {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  current_oee_pct: number | null;
  availability_pct: number | null;
  performance_pct: number | null;
  quality_pct: number | null;
  record_date: string | null;
  target_oee_pct: number;
  target_availability_pct: number;
  target_performance_pct: number;
  target_quality_pct: number;
}

interface OEEDashboard {
  // Only plant_oee_pct is returned at the top level (no plant A/P/Q); the plant A/P/Q
  // shown in the strip are derived from work_centers (see meanOrNull below).
  plant_oee_pct: number;
  work_centers: WorkCenterOEE[];
  comparison: Array<{
    work_center_id: number;
    work_center_name: string;
    avg_oee_pct: number;
    target_oee_pct: number;
  }>;
  period: string;
}

// One point of the trends time series. GET /oee/trends returns an OBJECT
// ({ time_series: [...], target_*_pct, period }), not a bare array — unwrap time_series.
interface OEETrend {
  date: string;
  oee_pct: number;
  availability_pct: number;
  performance_pct: number;
  quality_pct: number;
}

interface OEERecord {
  id: number;
  work_center_id: number;
  work_center_name?: string | null;
  record_date: string;
  shift?: string | null;
  availability_pct: number;
  performance_pct: number;
  quality_pct: number;
  oee_pct: number;
  total_parts: number;
  good_parts: number;
  defect_parts: number;
  notes?: string | null;
  created_at?: string | null;
}

// ============== Helpers ==============

/** Format a percentage metric, showing `--` when there's no data (null/undefined/NaN)
 *  so an empty metric reads as "no data yet", not a measured 0% OEE. */
function fmtPct(value: number | null | undefined, digits = 1): string {
  return value == null || !Number.isFinite(value) ? '--' : `${value.toFixed(digits)}%`;
}

/** Mean of the finite values, or null when none are present. Mirrors the backend's
 *  plant_oee_pct derivation (average across the work centers that actually have data),
 *  so a plant metric with no underlying records shows `--` rather than a fabricated 0%. */
function meanOrNull(values: Array<number | null | undefined>): number | null {
  const nums = values.filter((v): v is number => v != null && Number.isFinite(v));
  return nums.length ? nums.reduce((a, b) => a + b, 0) / nums.length : null;
}

function oeeColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return 'text-fd-mute';
  if (value >= 85) return 'text-green-600';
  if (value >= 65) return 'text-yellow-600';
  return 'text-red-600';
}

function oeeBgColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return 'bg-fd-raised border-fd-line';
  if (value >= 85) return 'bg-green-500/20 border-green-500/40';
  if (value >= 65) return 'bg-yellow-500/20 border-yellow-500/40';
  return 'bg-red-500/20 border-red-500/40';
}


function gaugeArc(pct: number): string {
  const clamp = Math.min(100, Math.max(0, pct));
  const angle = (clamp / 100) * 180;
  const rad = (angle * Math.PI) / 180;
  const r = 60;
  const cx = 70;
  const cy = 70;
  const x = cx + r * Math.cos(Math.PI - rad);
  const y = cy - r * Math.sin(Math.PI - rad);
  const large = angle > 180 ? 1 : 0;
  return `M ${cx - r} ${cy} A ${r} ${r} 0 ${large} 1 ${x} ${y}`;
}

function gaugeColor(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '#64748b'; // slate-500 — no data
  if (value >= 85) return '#22c55e';
  if (value >= 65) return '#eab308';
  return '#ef4444';
}

function defaultDateRange(): { from: string; to: string } {
  const from = new Date();
  from.setDate(from.getDate() - 30);
  // Central-local date stamps so the default report window doesn't shift a day
  // on a Central evening (UTC-midnight off-by-one).
  return {
    from: getCentralDateStamp(from),
    to: getCentralTodayISODate(),
  };
}

// ============== Component ==============

export default function OEE() {
  const { showToast } = useToast();
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [dashboard, setDashboard] = useState<OEEDashboard | null>(null);
  const [trends, setTrends] = useState<OEETrend[]>([]);
  const [records, setRecords] = useState<OEERecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  const [selectedWorkCenter, setSelectedWorkCenter] = useState<string>('');
  const defaultDates = defaultDateRange();
  const [dateFrom, setDateFrom] = useState<string>(defaultDates.from);
  const [dateTo, setDateTo] = useState<string>(defaultDates.to);

  const [showAddModal, setShowAddModal] = useState(false);
  const [addForm, setAddForm] = useState({
    work_center_id: 0,
    record_date: getCentralTodayISODate(),
    shift: '',
    planned_production_time: 480,
    actual_run_time: 0,
    ideal_cycle_time: 0,
    total_pieces: 0,
    good_pieces: 0,
    rejected_pieces: 0,
    notes: '',
  });

  const loadData = useCallback(async () => {
    setLoadError(false);
    try {
      const params: Record<string, any> = {};
      if (selectedWorkCenter) params.work_center_id = parseInt(selectedWorkCenter);
      if (dateFrom) params.date_from = dateFrom;
      if (dateTo) params.date_to = dateTo;

      // allSettled (not all): the work-centers call only feeds the filter dropdown, so
      // its failure must not blank the whole dashboard. Only a failed core /oee/dashboard
      // call sets loadError (which the render gate pairs with `!dashboard`).
      const [wcRes, dashRes, trendsRes, recordsRes] = await Promise.allSettled([
        api.get('/work-centers/', { params: { active_only: true } }),
        api.get('/oee/dashboard', { params }),
        api.get('/oee/trends', { params: { ...params, days: 30 } }),
        api.get('/oee/records', { params }),
      ]);

      if (wcRes.status === 'fulfilled') {
        const wcData = wcRes.value.data;
        setWorkCenters(Array.isArray(wcData) ? wcData : wcData?.items || []);
      } else {
        console.error('Failed to load work centers:', wcRes.reason);
      }

      if (dashRes.status === 'fulfilled') {
        setDashboard(dashRes.value.data);
      } else {
        console.error('Failed to load OEE dashboard:', dashRes.reason);
        setLoadError(true);
      }

      if (trendsRes.status === 'fulfilled') {
        // /oee/trends returns { time_series: [...] }, not a bare array — unwrap it
        // (tolerate a bare array too in case the contract is ever simplified).
        const td = trendsRes.value.data;
        const series = Array.isArray(td) ? td : td?.time_series;
        setTrends(Array.isArray(series) ? series : []);
      } else {
        console.error('Failed to load OEE trends:', trendsRes.reason);
      }

      if (recordsRes.status === 'fulfilled') {
        const recData = recordsRes.value.data;
        setRecords(Array.isArray(recData) ? recData : recData?.items || []);
      } else {
        console.error('Failed to load OEE records:', recordsRes.reason);
      }
    } catch (err) {
      console.error('Failed to load OEE data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [selectedWorkCenter, dateFrom, dateTo]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const handleAddRecord = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!addForm.work_center_id) {
      showToast('error', 'Please select a work center');
      return;
    }
    try {
      await api.post('/oee/records', {
        work_center_id: addForm.work_center_id,
        record_date: addForm.record_date,
        shift: addForm.shift || null,
        // Backend OEERecordCreate field names (endpoints/oee.py ~L48). The old body sent
        // planned_production_time/total_pieces/... which Pydantic silently ignored, so every
        // "Add Record" created an all-zero record. actual_run_time doubles as the performance
        // operating-time denominator so the saved metrics match the live preview above.
        planned_production_time_minutes: addForm.planned_production_time,
        actual_run_time_minutes: addForm.actual_run_time,
        actual_operating_time_minutes: addForm.actual_run_time,
        ideal_cycle_time_seconds: addForm.ideal_cycle_time,
        total_parts_produced: addForm.total_pieces,
        total_parts: addForm.total_pieces,
        good_parts: addForm.good_pieces,
        defect_parts: addForm.rejected_pieces,
        notes: addForm.notes || null,
      });
      setShowAddModal(false);
      setAddForm({
        work_center_id: 0,
        record_date: getCentralTodayISODate(),
        shift: '',
        planned_production_time: 480,
        actual_run_time: 0,
        ideal_cycle_time: 0,
        total_pieces: 0,
        good_pieces: 0,
        rejected_pieces: 0,
        notes: '',
      });
      showToast('success', 'OEE record added');
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || err.message || 'Failed to add OEE record');
    }
  };

  const selectedWcData = selectedWorkCenter
    ? dashboard?.work_centers?.find((wc) => wc.work_center_id === parseInt(selectedWorkCenter))
    : null;

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="loading loading-spinner loading-lg text-primary"></div>
      </div>
    );
  }

  if (loadError && !dashboard) {
    return (
      <div className="p-3">
        <ErrorState
          title="Failed to load OEE data"
          message="Could not load the OEE dashboard. Check your connection and try again."
          onRetry={loadData}
        />
      </div>
    );
  }

  const wcList = dashboard?.work_centers ?? [];
  // The API returns only plant_oee_pct (no plant A/P/Q). Derive all four plant metrics
  // the same way the backend derives OEE — average across the work centers that actually
  // have data — so a plant with no records yet shows `--` instead of a fabricated 0%.
  const plantOEE = meanOrNull(wcList.map((wc) => wc.current_oee_pct));
  const plantA = meanOrNull(wcList.map((wc) => wc.availability_pct));
  const plantP = meanOrNull(wcList.map((wc) => wc.performance_pct));
  const plantQ = meanOrNull(wcList.map((wc) => wc.quality_pct));

  return (
    <div className="p-3 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-bold text-fd-ink">OEE Dashboard</h1>
          <p className="text-xs text-fd-mute mt-0.5">Overall Equipment Effectiveness monitoring</p>
        </div>
        <div className="flex gap-2 flex-shrink-0">
          <button onClick={() => loadData()} className="btn btn-ghost btn-sm" title="Refresh">
            <ArrowPathIcon className="h-5 w-5" />
          </button>
          <button
            onClick={() => {
              setAddForm({
                work_center_id: 0,
                record_date: getCentralTodayISODate(),
                shift: '',
                planned_production_time: 480,
                actual_run_time: 0,
                ideal_cycle_time: 0,
                total_pieces: 0,
                good_pieces: 0,
                rejected_pieces: 0,
                notes: '',
              });
              setShowAddModal(true);
            }}
            className="btn btn-primary btn-sm"
          >
            <PlusIcon className="h-5 w-5 mr-1" />
            Add Record
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="card card-compact !p-2.5">
        <div className="flex flex-wrap gap-3 items-end">
          <div>
            <label htmlFor="oee-filter-work-center" className="label !py-0"><span className="label-text text-[10px] uppercase tracking-wide text-fd-mute">Work Center</span></label>
            <select
              id="oee-filter-work-center"
              className="select select-bordered select-sm rounded-sm"
              value={selectedWorkCenter}
              onChange={(e) => setSelectedWorkCenter(e.target.value)}
            >
              <option value="">All Work Centers</option>
              {workCenters.map((wc) => (
                <option key={wc.id} value={wc.id}>
                  {wc.code} - {wc.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="oee-filter-from" className="label !py-0"><span className="label-text text-[10px] uppercase tracking-wide text-fd-mute">From</span></label>
            <input
              id="oee-filter-from"
              type="date"
              aria-label="Filter from date"
              className="input input-bordered input-sm rounded-sm"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="oee-filter-to" className="label !py-0"><span className="label-text text-[10px] uppercase tracking-wide text-fd-mute">To</span></label>
            <input
              id="oee-filter-to"
              type="date"
              aria-label="Filter to date"
              className="input input-bordered input-sm rounded-sm"
              value={dateTo}
              onChange={(e) => setDateTo(e.target.value)}
            />
          </div>
          {(selectedWorkCenter || dateFrom !== defaultDates.from || dateTo !== defaultDates.to) && (
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => {
                setSelectedWorkCenter('');
                setDateFrom(defaultDates.from);
                setDateTo(defaultDates.to);
              }}
            >
              Clear Filters
            </button>
          )}
        </div>
      </div>

      {/* Plant-wide OEE — single MiniStat strip (de-duped A/P/Q) */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={ChartBarIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Plant-wide OEE"
          value={fmtPct(plantOEE)}
          valueColor={oeeColor(plantOEE)}
          subtitle="Target: 85%"
        />
        <MiniStat
          icon={ClockIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Availability"
          value={fmtPct(plantA)}
          valueColor={oeeColor(plantA)}
        />
        <MiniStat
          icon={BoltIcon}
          iconBg="bg-fd-amber/15"
          iconColor="text-fd-amber"
          label="Performance"
          value={fmtPct(plantP)}
          valueColor={oeeColor(plantP)}
        />
        <MiniStat
          icon={CheckBadgeIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Quality"
          value={fmtPct(plantQ)}
          valueColor={oeeColor(plantQ)}
        />
      </MiniStatStrip>

      {/* Work Center OEE tiles + selected-WC detail side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
        <CockpitPanel
          title="Work Center OEE"
          subtitle="Tap a work center to filter and inspect its detail"
          className={selectedWcData ? 'xl:col-span-7' : 'xl:col-span-12'}
          footer={`${(dashboard?.work_centers || []).length} work centers`}
        >
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-2">
            {(dashboard?.work_centers || []).map((wc) => (
              <button
                type="button"
                key={wc.work_center_id}
                onClick={() => setSelectedWorkCenter(String(wc.work_center_id))}
                className={`text-left rounded-sm p-2.5 border cursor-pointer transition-colors min-w-0 ${oeeBgColor(wc.current_oee_pct)} ${
                  selectedWorkCenter === String(wc.work_center_id) ? 'ring-1 ring-fd-blue' : ''
                }`}
              >
                <div className="font-bold text-sm text-fd-ink truncate">{wc.work_center_code}</div>
                <div className="text-[10px] text-fd-mute truncate mb-1.5">{wc.work_center_name}</div>
                <div className={`text-xl font-bold tabular-nums ${oeeColor(wc.current_oee_pct)}`}>
                  {fmtPct(wc.current_oee_pct)}
                </div>
                <div className="mt-1 space-y-0.5">
                  <div className="flex justify-between text-[10px] text-fd-mute tabular-nums">
                    <span>A</span>
                    <span>{fmtPct(wc.availability_pct, 0)}</span>
                  </div>
                  <div className="flex justify-between text-[10px] text-fd-mute tabular-nums">
                    <span>P</span>
                    <span>{fmtPct(wc.performance_pct, 0)}</span>
                  </div>
                  <div className="flex justify-between text-[10px] text-fd-mute tabular-nums">
                    <span>Q</span>
                    <span>{fmtPct(wc.quality_pct, 0)}</span>
                  </div>
                </div>
              </button>
            ))}
            {(dashboard?.work_centers || []).length === 0 && (
              <div className="col-span-full">
                <EmptyState
                  icon={ChartBarIcon}
                  title="No OEE data"
                  description="No OEE data available for the selected period. Add a record or adjust the filters."
                />
              </div>
            )}
          </div>
        </CockpitPanel>

        {/* Selected Work Center Detail — Gauge Cards (canonical per-WC A/P/Q view) */}
        {selectedWcData && (
          <CockpitPanel
            title={`${selectedWcData.work_center_code} — ${selectedWcData.work_center_name}`}
            subtitle="Selected work center detail"
            headerExtra={<CogIcon className="h-5 w-5 text-fd-mute" />}
            className="xl:col-span-5"
          >
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              {[
                { label: 'Availability', value: selectedWcData.availability_pct, target: 90 },
                { label: 'Performance', value: selectedWcData.performance_pct, target: 95 },
                { label: 'Quality', value: selectedWcData.quality_pct, target: 99 },
              ].map((metric) => (
                <div key={metric.label} className="flex flex-col items-center p-2.5 rounded-sm border border-fd-line min-w-0">
                  <svg width="140" height="80" viewBox="0 0 140 80">
                    {/* Background arc */}
                    <path
                      d={gaugeArc(100)}
                      fill="none"
                      stroke="#334155"
                      strokeWidth="12"
                      strokeLinecap="round"
                    />
                    {/* Value arc */}
                    <path
                      d={gaugeArc(metric.value ?? 0)}
                      fill="none"
                      stroke={gaugeColor(metric.value)}
                      strokeWidth="12"
                      strokeLinecap="round"
                    />
                    <text x="70" y="70" textAnchor="middle" className="text-xl font-bold" fill={gaugeColor(metric.value)}>
                      {fmtPct(metric.value)}
                    </text>
                  </svg>
                  <div className="text-sm font-medium text-fd-body mt-1">{metric.label}</div>
                  <div className="text-[10px] text-fd-mute">Target: {metric.target}%</div>
                  <div
                    className={`text-[10px] mt-1 font-medium ${
                      metric.value == null
                        ? 'text-fd-mute'
                        : metric.value >= metric.target
                          ? 'text-green-600'
                          : 'text-red-600'
                    }`}
                  >
                    {metric.value == null
                      ? 'No data'
                      : metric.value >= metric.target
                        ? 'On Target'
                        : `${(metric.target - metric.value).toFixed(1)}% below target`}
                  </div>
                </div>
              ))}
            </div>
          </CockpitPanel>
        )}
      </div>

      {/* Trend Chart */}
      {trends.length > 0 && (
        <CockpitPanel
          title="OEE Trends (30 Days)"
          headerExtra={<CalendarDaysIcon className="h-5 w-5 text-fd-mute" />}
          bodyClassName="lg:max-h-none"
        >
          <ResponsiveContainer width="100%" height={350}>
            <LineChart data={trends} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 11, fill: '#94a3b8' }}
                tickFormatter={(val) =>
                  formatCentralDate(val as string, { month: 'numeric', day: 'numeric', year: undefined })
                }
              />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11, fill: '#94a3b8' }} />
              <Tooltip
                contentStyle={{ backgroundColor: '#1a1f2e', border: '1px solid #334155', borderRadius: '4px', color: '#e2e8f0' }}
                formatter={(value: number | undefined, name: string | undefined) => [`${(value ?? 0).toFixed(1)}%`, name ?? '']}
                labelFormatter={(label) => formatCentralDate(label as string)}
              />
              <Legend />
              <ReferenceLine y={85} stroke="#9ca3af" strokeDasharray="5 5" label={{ value: '85% Target', position: 'right', fontSize: 10 }} />
              <Line type="monotone" dataKey="oee_pct" stroke="#2563eb" strokeWidth={2} name="OEE" dot={false} />
              <Line type="monotone" dataKey="availability_pct" stroke="#3b82f6" strokeWidth={1} name="Availability" dot={false} strokeDasharray="4 2" />
              <Line type="monotone" dataKey="performance_pct" stroke="#8b5cf6" strokeWidth={1} name="Performance" dot={false} strokeDasharray="4 2" />
              <Line type="monotone" dataKey="quality_pct" stroke="#14b8a6" strokeWidth={1} name="Quality" dot={false} strokeDasharray="4 2" />
            </LineChart>
          </ResponsiveContainer>
        </CockpitPanel>
      )}

      {/* OEE Records Table */}
      <CockpitPanel title="OEE Records" footer={`${records.length} records`}>
        <div className="overflow-x-auto">
          <table className="table table-sm w-full">
            <thead>
              <tr className="bg-fd-raised">
                <th>Date</th>
                <th>Work Center</th>
                <th>Shift</th>
                <th>OEE</th>
                <th>Availability</th>
                <th>Performance</th>
                <th>Quality</th>
                <th>Total Pcs</th>
                <th>Good Pcs</th>
                <th>Rejected</th>
                <th>Notes</th>
              </tr>
            </thead>
            <tbody>
              {records.length === 0 ? (
                <tr>
                  <td colSpan={11} className="p-0">
                    <EmptyState
                      icon={ClockIcon}
                      title="No OEE records"
                      description="No OEE records found for the selected period."
                      action={{ label: 'Add Record', onClick: () => setShowAddModal(true) }}
                    />
                  </td>
                </tr>
              ) : (
                records.map((rec) => (
                  <tr key={rec.id} className="hover">
                    <td className="text-sm tabular-nums">{rec.record_date}</td>
                    <td className="font-medium text-sm">
                      {rec.work_center_name || `WC-${rec.work_center_id}`}
                    </td>
                    <td className="text-sm">{rec.shift || '-'}</td>
                    <td>
                      <span className={`font-bold text-sm tabular-nums ${oeeColor(rec.oee_pct)}`}>
                        {fmtPct(rec.oee_pct)}
                      </span>
                    </td>
                    <td className="text-sm tabular-nums">{fmtPct(rec.availability_pct)}</td>
                    <td className="text-sm tabular-nums">{fmtPct(rec.performance_pct)}</td>
                    <td className="text-sm tabular-nums">{fmtPct(rec.quality_pct)}</td>
                    <td className="text-sm tabular-nums">{rec.total_parts}</td>
                    <td className="text-sm tabular-nums">{rec.good_parts}</td>
                    <td className="text-sm tabular-nums">{rec.defect_parts}</td>
                    <td className="text-sm text-fd-mute max-w-[200px] truncate">{rec.notes || '-'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </CockpitPanel>

      {/* Add Record Modal */}
      {showAddModal && (
        <div className="modal modal-open">
          <div className="modal-box max-w-2xl">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-bold">Add OEE Record</h3>
              <button onClick={() => setShowAddModal(false)} className="btn btn-ghost btn-sm btn-circle">
                <XMarkIcon className="h-5 w-5" />
              </button>
            </div>
            <form onSubmit={handleAddRecord} className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Work Center" required labelClassName="font-medium">
                  {(field) => (
                    <select
                      {...field}
                      className="select select-bordered w-full"
                      value={addForm.work_center_id}
                      onChange={(e) => setAddForm({ ...addForm, work_center_id: parseInt(e.target.value) })}
                      required
                    >
                      <option value={0} disabled>Select work center...</option>
                      {workCenters.map((wc) => (
                        <option key={wc.id} value={wc.id}>
                          {wc.code} - {wc.name}
                        </option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label="Date" required labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="date"
                      className="input input-bordered w-full"
                      value={addForm.record_date}
                      onChange={(e) => setAddForm({ ...addForm, record_date: e.target.value })}
                      required
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <FormField label="Shift" labelClassName="font-medium">
                  {(field) => (
                    <select
                      {...field}
                      className="select select-bordered w-full"
                      value={addForm.shift}
                      onChange={(e) => setAddForm({ ...addForm, shift: e.target.value })}
                    >
                      <option value="">N/A</option>
                      <option value="1st">1st Shift</option>
                      <option value="2nd">2nd Shift</option>
                      <option value="3rd">3rd Shift</option>
                    </select>
                  )}
                </FormField>
                <FormField label="Planned Time (min)" required labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      className="input input-bordered w-full"
                      value={addForm.planned_production_time}
                      onChange={(e) => setAddForm({ ...addForm, planned_production_time: parseFloat(e.target.value) || 0 })}
                      min={0}
                      required
                    />
                  )}
                </FormField>
                <FormField label="Actual Run Time (min)" required labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      className="input input-bordered w-full"
                      value={addForm.actual_run_time}
                      onChange={(e) => setAddForm({ ...addForm, actual_run_time: parseFloat(e.target.value) || 0 })}
                      min={0}
                      required
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <FormField label="Ideal Cycle Time (sec)" labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      className="input input-bordered w-full"
                      value={addForm.ideal_cycle_time}
                      onChange={(e) => setAddForm({ ...addForm, ideal_cycle_time: parseFloat(e.target.value) || 0 })}
                      min={0}
                      step="0.01"
                    />
                  )}
                </FormField>
                <FormField label="Total Pieces" required labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      className="input input-bordered w-full"
                      value={addForm.total_pieces}
                      onChange={(e) => setAddForm({ ...addForm, total_pieces: parseInt(e.target.value) || 0 })}
                      min={0}
                      required
                    />
                  )}
                </FormField>
                <FormField label="Good Pieces" required labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      className="input input-bordered w-full"
                      value={addForm.good_pieces}
                      onChange={(e) => setAddForm({ ...addForm, good_pieces: parseInt(e.target.value) || 0 })}
                      min={0}
                      required
                    />
                  )}
                </FormField>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <FormField label="Rejected Pieces" labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      className="input input-bordered w-full"
                      value={addForm.rejected_pieces}
                      onChange={(e) => setAddForm({ ...addForm, rejected_pieces: parseInt(e.target.value) || 0 })}
                      min={0}
                    />
                  )}
                </FormField>
                <FormField label="Notes" labelClassName="font-medium">
                  {(field) => (
                    <input
                      {...field}
                      type="text"
                      className="input input-bordered w-full"
                      value={addForm.notes}
                      onChange={(e) => setAddForm({ ...addForm, notes: e.target.value })}
                      placeholder="Optional notes..."
                    />
                  )}
                </FormField>
              </div>

              {/* Live OEE Preview */}
              {addForm.planned_production_time > 0 && addForm.total_pieces > 0 && (
                <div className="p-3 bg-slate-800 rounded-lg">
                  <div className="text-xs font-medium text-slate-400 mb-2">Calculated OEE Preview</div>
                  <div className="grid grid-cols-4 gap-3 text-center">
                    {(() => {
                      // Cap each factor at 100% to mirror the backend (calculate_oee applies
                      // min(x, 100) before multiplying), so the preview never shows a value the
                      // saved record can't hold.
                      const a = Math.min(100, addForm.planned_production_time > 0
                        ? (addForm.actual_run_time / addForm.planned_production_time) * 100
                        : 0);
                      const p = Math.min(100, addForm.actual_run_time > 0 && addForm.ideal_cycle_time > 0
                        ? ((addForm.ideal_cycle_time * addForm.total_pieces) / (addForm.actual_run_time * 60)) * 100
                        : 0);
                      const q = Math.min(100, addForm.total_pieces > 0
                        ? (addForm.good_pieces / addForm.total_pieces) * 100
                        : 0);
                      const oee = (a / 100) * (p / 100) * (q / 100) * 100;
                      return (
                        <>
                          <div>
                            <div className="text-xs text-slate-400">Availability</div>
                            <div className={`text-lg font-bold ${oeeColor(a)}`}>{a.toFixed(1)}%</div>
                          </div>
                          <div>
                            <div className="text-xs text-slate-400">Performance</div>
                            <div className={`text-lg font-bold ${oeeColor(p)}`}>{p.toFixed(1)}%</div>
                          </div>
                          <div>
                            <div className="text-xs text-slate-400">Quality</div>
                            <div className={`text-lg font-bold ${oeeColor(q)}`}>{q.toFixed(1)}%</div>
                          </div>
                          <div>
                            <div className="text-xs text-slate-400">OEE</div>
                            <div className={`text-lg font-bold ${oeeColor(oee)}`}>{oee.toFixed(1)}%</div>
                          </div>
                        </>
                      );
                    })()}
                  </div>
                </div>
              )}

              <div className="modal-action">
                <button type="button" onClick={() => setShowAddModal(false)} className="btn btn-ghost">
                  Cancel
                </button>
                <button type="submit" className="btn btn-primary">
                  <PlusIcon className="h-4 w-4 mr-1" />
                  Add Record
                </button>
              </div>
            </form>
          </div>
          <div
            className="modal-backdrop"
            role="presentation"
            onClick={(e) => {
              if (e.target === e.currentTarget) setShowAddModal(false);
            }}
          ></div>
        </div>
      )}
    </div>
  );
}
