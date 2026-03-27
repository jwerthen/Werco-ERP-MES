import React, { useEffect, useState, useCallback } from 'react';
import api from '../services/api';
import {
  PlusIcon,
  XMarkIcon,
  ArrowPathIcon,
  ChartBarIcon,
  CogIcon,
  CalendarDaysIcon,
} from '@heroicons/react/24/outline';
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts';

// ============== Types ==============

interface WorkCenter {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

interface OEEDashboard {
  plant_oee: number;
  plant_availability: number;
  plant_performance: number;
  plant_quality: number;
  work_centers: WorkCenterOEE[];
}

interface WorkCenterOEE {
  work_center_id: number;
  work_center_code: string;
  work_center_name: string;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
}

interface OEETrend {
  date: string;
  oee: number;
  availability: number;
  performance: number;
  quality: number;
}

interface OEERecord {
  id: number;
  work_center_id: number;
  work_center?: { id: number; code: string; name: string };
  record_date: string;
  shift?: string;
  planned_production_time: number;
  actual_run_time: number;
  ideal_cycle_time: number;
  total_pieces: number;
  good_pieces: number;
  rejected_pieces: number;
  availability: number;
  performance: number;
  quality: number;
  oee: number;
  notes?: string;
  created_at: string;
}

// ============== Helpers ==============

function oeeColor(value: number): string {
  if (value >= 85) return 'text-green-600';
  if (value >= 65) return 'text-yellow-600';
  return 'text-red-600';
}

function oeeBgColor(value: number): string {
  if (value >= 85) return 'bg-green-100 border-green-300';
  if (value >= 65) return 'bg-yellow-100 border-yellow-300';
  return 'bg-red-100 border-red-300';
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

function gaugeColor(value: number): string {
  if (value >= 85) return '#22c55e';
  if (value >= 65) return '#eab308';
  return '#ef4444';
}

function defaultDateRange(): { from: string; to: string } {
  const to = new Date();
  const from = new Date();
  from.setDate(from.getDate() - 30);
  return {
    from: from.toISOString().split('T')[0],
    to: to.toISOString().split('T')[0],
  };
}

// ============== Component ==============

export default function OEE() {
  const [workCenters, setWorkCenters] = useState<WorkCenter[]>([]);
  const [dashboard, setDashboard] = useState<OEEDashboard | null>(null);
  const [trends, setTrends] = useState<OEETrend[]>([]);
  const [records, setRecords] = useState<OEERecord[]>([]);
  const [loading, setLoading] = useState(true);

  const [selectedWorkCenter, setSelectedWorkCenter] = useState<string>('');
  const defaultDates = defaultDateRange();
  const [dateFrom, setDateFrom] = useState<string>(defaultDates.from);
  const [dateTo, setDateTo] = useState<string>(defaultDates.to);

  const [showAddModal, setShowAddModal] = useState(false);
  const [addForm, setAddForm] = useState({
    work_center_id: 0,
    record_date: new Date().toISOString().split('T')[0],
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
    try {
      const params: Record<string, any> = {};
      if (selectedWorkCenter) params.work_center_id = parseInt(selectedWorkCenter);
      if (dateFrom) params.date_from = dateFrom;
      if (dateTo) params.date_to = dateTo;

      const [wcRes, dashRes, trendsRes, recordsRes] = await Promise.all([
        api.get('/work-centers?active_only=true'),
        api.get('/oee/dashboard', { params }),
        api.get('/oee/trends', { params: { ...params, days: 30 } }),
        api.get('/oee/records', { params }),
      ]);

      setWorkCenters(Array.isArray(wcRes.data) ? wcRes.data : wcRes.data?.items || []);
      setDashboard(dashRes.data);
      setTrends(Array.isArray(trendsRes.data) ? trendsRes.data : []);
      setRecords(Array.isArray(recordsRes.data) ? recordsRes.data : recordsRes.data?.items || []);
    } catch (err) {
      console.error('Failed to load OEE data:', err);
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
      alert('Please select a work center');
      return;
    }
    try {
      await api.post('/oee/records', {
        work_center_id: addForm.work_center_id,
        record_date: addForm.record_date,
        shift: addForm.shift || null,
        planned_production_time: addForm.planned_production_time,
        actual_run_time: addForm.actual_run_time,
        ideal_cycle_time: addForm.ideal_cycle_time,
        total_pieces: addForm.total_pieces,
        good_pieces: addForm.good_pieces,
        rejected_pieces: addForm.rejected_pieces,
        notes: addForm.notes || null,
      });
      setShowAddModal(false);
      setAddForm({
        work_center_id: 0,
        record_date: new Date().toISOString().split('T')[0],
        shift: '',
        planned_production_time: 480,
        actual_run_time: 0,
        ideal_cycle_time: 0,
        total_pieces: 0,
        good_pieces: 0,
        rejected_pieces: 0,
        notes: '',
      });
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || err.message || 'Failed to add OEE record');
    }
  };

  const selectedWcData = selectedWorkCenter
    ? dashboard?.work_centers?.find((wc) => wc.work_center_id === parseInt(selectedWorkCenter))
    : null;

  const comparisonData = (dashboard?.work_centers || []).map((wc) => ({
    name: wc.work_center_code,
    oee: Math.round(wc.oee * 10) / 10,
    availability: Math.round(wc.availability * 10) / 10,
    performance: Math.round(wc.performance * 10) / 10,
    quality: Math.round(wc.quality * 10) / 10,
  }));

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="loading loading-spinner loading-lg text-primary"></div>
      </div>
    );
  }

  const plantOEE = dashboard?.plant_oee ?? 0;
  const plantA = dashboard?.plant_availability ?? 0;
  const plantP = dashboard?.plant_performance ?? 0;
  const plantQ = dashboard?.plant_quality ?? 0;

  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">OEE Dashboard</h1>
          <p className="text-sm text-gray-500 mt-1">Overall Equipment Effectiveness monitoring</p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => loadData()} className="btn btn-ghost btn-sm" title="Refresh">
            <ArrowPathIcon className="h-5 w-5" />
          </button>
          <button
            onClick={() => {
              setAddForm({
                work_center_id: 0,
                record_date: new Date().toISOString().split('T')[0],
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
      <div className="bg-white rounded-lg shadow p-4">
        <div className="flex flex-wrap gap-3 items-end">
          <div>
            <label className="label"><span className="label-text text-xs font-medium">Work Center</span></label>
            <select
              className="select select-bordered select-sm"
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
            <label className="label"><span className="label-text text-xs font-medium">From</span></label>
            <input
              type="date"
              className="input input-bordered input-sm"
              value={dateFrom}
              onChange={(e) => setDateFrom(e.target.value)}
            />
          </div>
          <div>
            <label className="label"><span className="label-text text-xs font-medium">To</span></label>
            <input
              type="date"
              className="input input-bordered input-sm"
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

      {/* Plant-wide OEE */}
      <div className="bg-white rounded-lg shadow p-6">
        <div className="flex flex-col md:flex-row items-center gap-8">
          <div className="flex flex-col items-center">
            <div className="text-sm text-gray-500 font-medium mb-1">Plant-wide OEE</div>
            <div className={`text-6xl font-bold ${oeeColor(plantOEE)}`}>
              {plantOEE.toFixed(1)}%
            </div>
            <div className="flex items-center gap-1 mt-2">
              <ChartBarIcon className="h-4 w-4 text-gray-400" />
              <span className="text-xs text-gray-400">Target: 85%</span>
            </div>
          </div>
          <div className="flex-1 grid grid-cols-3 gap-4 w-full">
            <div className="text-center p-4 rounded-lg bg-blue-50 border border-blue-200">
              <div className="text-xs text-blue-600 font-medium mb-1">Availability</div>
              <div className={`text-2xl font-bold ${oeeColor(plantA)}`}>{plantA.toFixed(1)}%</div>
            </div>
            <div className="text-center p-4 rounded-lg bg-purple-50 border border-purple-200">
              <div className="text-xs text-purple-600 font-medium mb-1">Performance</div>
              <div className={`text-2xl font-bold ${oeeColor(plantP)}`}>{plantP.toFixed(1)}%</div>
            </div>
            <div className="text-center p-4 rounded-lg bg-teal-50 border border-teal-200">
              <div className="text-xs text-teal-600 font-medium mb-1">Quality</div>
              <div className={`text-2xl font-bold ${oeeColor(plantQ)}`}>{plantQ.toFixed(1)}%</div>
            </div>
          </div>
        </div>
      </div>

      {/* Work Center OEE Cards */}
      <div className="bg-white rounded-lg shadow p-4">
        <h2 className="text-lg font-semibold text-gray-900 mb-3">Work Center OEE</h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-3">
          {(dashboard?.work_centers || []).map((wc) => (
            <div
              key={wc.work_center_id}
              onClick={() => setSelectedWorkCenter(String(wc.work_center_id))}
              className={`rounded-lg p-3 border-2 cursor-pointer transition-all hover:shadow-md ${oeeBgColor(wc.oee)} ${
                selectedWorkCenter === String(wc.work_center_id) ? 'ring-2 ring-blue-500' : ''
              }`}
            >
              <div className="font-bold text-sm text-gray-900">{wc.work_center_code}</div>
              <div className="text-xs text-gray-500 truncate mb-2">{wc.work_center_name}</div>
              <div className={`text-2xl font-bold ${oeeColor(wc.oee)}`}>
                {wc.oee.toFixed(1)}%
              </div>
              <div className="mt-1 space-y-0.5">
                <div className="flex justify-between text-xs text-gray-600">
                  <span>A</span>
                  <span>{wc.availability.toFixed(0)}%</span>
                </div>
                <div className="flex justify-between text-xs text-gray-600">
                  <span>P</span>
                  <span>{wc.performance.toFixed(0)}%</span>
                </div>
                <div className="flex justify-between text-xs text-gray-600">
                  <span>Q</span>
                  <span>{wc.quality.toFixed(0)}%</span>
                </div>
              </div>
            </div>
          ))}
          {(dashboard?.work_centers || []).length === 0 && (
            <div className="col-span-full text-center text-gray-400 py-8">
              No OEE data available for the selected period
            </div>
          )}
        </div>
      </div>

      {/* Selected Work Center Detail - Gauge Cards */}
      {selectedWcData && (
        <div className="bg-white rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold text-gray-900 mb-3">
            <CogIcon className="h-5 w-5 inline mr-2 text-gray-400" />
            {selectedWcData.work_center_code} - {selectedWcData.work_center_name}
          </h2>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
            {[
              { label: 'Availability', value: selectedWcData.availability, target: 90 },
              { label: 'Performance', value: selectedWcData.performance, target: 95 },
              { label: 'Quality', value: selectedWcData.quality, target: 99 },
            ].map((metric) => (
              <div key={metric.label} className="flex flex-col items-center p-4 rounded-lg border">
                <svg width="140" height="80" viewBox="0 0 140 80">
                  {/* Background arc */}
                  <path
                    d={gaugeArc(100)}
                    fill="none"
                    stroke="#e5e7eb"
                    strokeWidth="12"
                    strokeLinecap="round"
                  />
                  {/* Value arc */}
                  <path
                    d={gaugeArc(metric.value)}
                    fill="none"
                    stroke={gaugeColor(metric.value)}
                    strokeWidth="12"
                    strokeLinecap="round"
                  />
                  <text x="70" y="70" textAnchor="middle" className="text-xl font-bold" fill={gaugeColor(metric.value)}>
                    {metric.value.toFixed(1)}%
                  </text>
                </svg>
                <div className="text-sm font-medium text-gray-700 mt-1">{metric.label}</div>
                <div className="text-xs text-gray-400">Target: {metric.target}%</div>
                <div className={`text-xs mt-1 font-medium ${metric.value >= metric.target ? 'text-green-600' : 'text-red-600'}`}>
                  {metric.value >= metric.target ? 'On Target' : `${(metric.target - metric.value).toFixed(1)}% below target`}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Trend Chart */}
      {trends.length > 0 && (
        <div className="bg-white rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold text-gray-900 mb-3">
            <CalendarDaysIcon className="h-5 w-5 inline mr-2 text-gray-400" />
            OEE Trends (30 Days)
          </h2>
          <ResponsiveContainer width="100%" height={350}>
            <LineChart data={trends} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 11 }}
                tickFormatter={(val) => {
                  const d = new Date(val);
                  return `${d.getMonth() + 1}/${d.getDate()}`;
                }}
              />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
              <Tooltip
                formatter={(value: number, name: string) => [`${value.toFixed(1)}%`, name]}
                labelFormatter={(label) => {
                  const d = new Date(label);
                  return d.toLocaleDateString();
                }}
              />
              <Legend />
              <ReferenceLine y={85} stroke="#9ca3af" strokeDasharray="5 5" label={{ value: '85% Target', position: 'right', fontSize: 10 }} />
              <Line type="monotone" dataKey="oee" stroke="#2563eb" strokeWidth={2} name="OEE" dot={false} />
              <Line type="monotone" dataKey="availability" stroke="#3b82f6" strokeWidth={1} name="Availability" dot={false} strokeDasharray="4 2" />
              <Line type="monotone" dataKey="performance" stroke="#8b5cf6" strokeWidth={1} name="Performance" dot={false} strokeDasharray="4 2" />
              <Line type="monotone" dataKey="quality" stroke="#14b8a6" strokeWidth={1} name="Quality" dot={false} strokeDasharray="4 2" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* Work Center Comparison Bar Chart */}
      {comparisonData.length > 0 && (
        <div className="bg-white rounded-lg shadow p-4">
          <h2 className="text-lg font-semibold text-gray-900 mb-3">Work Center Comparison</h2>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={comparisonData} margin={{ top: 5, right: 20, left: 10, bottom: 40 }}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis dataKey="name" angle={-35} textAnchor="end" interval={0} height={60} tick={{ fontSize: 11 }} />
              <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
              <Tooltip formatter={(value: number) => [`${value}%`]} />
              <Legend />
              <ReferenceLine y={85} stroke="#ef4444" strokeDasharray="5 5" label={{ value: '85% Target', position: 'right', fontSize: 10 }} />
              <Bar dataKey="oee" fill="#2563eb" name="OEE" radius={[4, 4, 0, 0]} />
              <Bar dataKey="availability" fill="#60a5fa" name="Availability" radius={[4, 4, 0, 0]} />
              <Bar dataKey="performance" fill="#a78bfa" name="Performance" radius={[4, 4, 0, 0]} />
              <Bar dataKey="quality" fill="#2dd4bf" name="Quality" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* OEE Records Table */}
      <div className="bg-white rounded-lg shadow p-4">
        <h2 className="text-lg font-semibold text-gray-900 mb-3">OEE Records</h2>
        <div className="overflow-x-auto">
          <table className="table table-sm w-full">
            <thead>
              <tr className="bg-gray-50">
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
                  <td colSpan={11} className="text-center text-gray-400 py-8">
                    No OEE records found for the selected period
                  </td>
                </tr>
              ) : (
                records.map((rec) => (
                  <tr key={rec.id} className="hover">
                    <td className="text-sm">{rec.record_date}</td>
                    <td className="font-medium text-sm">
                      {rec.work_center?.code || `WC-${rec.work_center_id}`}
                    </td>
                    <td className="text-sm">{rec.shift || '-'}</td>
                    <td>
                      <span className={`font-bold text-sm ${oeeColor(rec.oee)}`}>
                        {rec.oee.toFixed(1)}%
                      </span>
                    </td>
                    <td className="text-sm">{rec.availability.toFixed(1)}%</td>
                    <td className="text-sm">{rec.performance.toFixed(1)}%</td>
                    <td className="text-sm">{rec.quality.toFixed(1)}%</td>
                    <td className="text-sm">{rec.total_pieces}</td>
                    <td className="text-sm">{rec.good_pieces}</td>
                    <td className="text-sm">{rec.rejected_pieces}</td>
                    <td className="text-sm text-gray-500 max-w-[200px] truncate">{rec.notes || '-'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

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
                <div>
                  <label className="label"><span className="label-text font-medium">Work Center *</span></label>
                  <select
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
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Date *</span></label>
                  <input
                    type="date"
                    className="input input-bordered w-full"
                    value={addForm.record_date}
                    onChange={(e) => setAddForm({ ...addForm, record_date: e.target.value })}
                    required
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="label"><span className="label-text font-medium">Shift</span></label>
                  <select
                    className="select select-bordered w-full"
                    value={addForm.shift}
                    onChange={(e) => setAddForm({ ...addForm, shift: e.target.value })}
                  >
                    <option value="">N/A</option>
                    <option value="1st">1st Shift</option>
                    <option value="2nd">2nd Shift</option>
                    <option value="3rd">3rd Shift</option>
                  </select>
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Planned Time (min) *</span></label>
                  <input
                    type="number"
                    className="input input-bordered w-full"
                    value={addForm.planned_production_time}
                    onChange={(e) => setAddForm({ ...addForm, planned_production_time: parseFloat(e.target.value) || 0 })}
                    min={0}
                    required
                  />
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Actual Run Time (min) *</span></label>
                  <input
                    type="number"
                    className="input input-bordered w-full"
                    value={addForm.actual_run_time}
                    onChange={(e) => setAddForm({ ...addForm, actual_run_time: parseFloat(e.target.value) || 0 })}
                    min={0}
                    required
                  />
                </div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="label"><span className="label-text font-medium">Ideal Cycle Time (sec)</span></label>
                  <input
                    type="number"
                    className="input input-bordered w-full"
                    value={addForm.ideal_cycle_time}
                    onChange={(e) => setAddForm({ ...addForm, ideal_cycle_time: parseFloat(e.target.value) || 0 })}
                    min={0}
                    step="0.01"
                  />
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Total Pieces *</span></label>
                  <input
                    type="number"
                    className="input input-bordered w-full"
                    value={addForm.total_pieces}
                    onChange={(e) => setAddForm({ ...addForm, total_pieces: parseInt(e.target.value) || 0 })}
                    min={0}
                    required
                  />
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Good Pieces *</span></label>
                  <input
                    type="number"
                    className="input input-bordered w-full"
                    value={addForm.good_pieces}
                    onChange={(e) => setAddForm({ ...addForm, good_pieces: parseInt(e.target.value) || 0 })}
                    min={0}
                    required
                  />
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label"><span className="label-text font-medium">Rejected Pieces</span></label>
                  <input
                    type="number"
                    className="input input-bordered w-full"
                    value={addForm.rejected_pieces}
                    onChange={(e) => setAddForm({ ...addForm, rejected_pieces: parseInt(e.target.value) || 0 })}
                    min={0}
                  />
                </div>
                <div>
                  <label className="label"><span className="label-text font-medium">Notes</span></label>
                  <input
                    type="text"
                    className="input input-bordered w-full"
                    value={addForm.notes}
                    onChange={(e) => setAddForm({ ...addForm, notes: e.target.value })}
                    placeholder="Optional notes..."
                  />
                </div>
              </div>

              {/* Live OEE Preview */}
              {addForm.planned_production_time > 0 && addForm.total_pieces > 0 && (
                <div className="p-3 bg-gray-50 rounded-lg">
                  <div className="text-xs font-medium text-gray-500 mb-2">Calculated OEE Preview</div>
                  <div className="grid grid-cols-4 gap-3 text-center">
                    {(() => {
                      const a = addForm.planned_production_time > 0
                        ? (addForm.actual_run_time / addForm.planned_production_time) * 100
                        : 0;
                      const p = addForm.actual_run_time > 0 && addForm.ideal_cycle_time > 0
                        ? ((addForm.ideal_cycle_time * addForm.total_pieces) / (addForm.actual_run_time * 60)) * 100
                        : 0;
                      const q = addForm.total_pieces > 0
                        ? (addForm.good_pieces / addForm.total_pieces) * 100
                        : 0;
                      const oee = (a / 100) * (p / 100) * (q / 100) * 100;
                      return (
                        <>
                          <div>
                            <div className="text-xs text-gray-400">Availability</div>
                            <div className={`text-lg font-bold ${oeeColor(a)}`}>{a.toFixed(1)}%</div>
                          </div>
                          <div>
                            <div className="text-xs text-gray-400">Performance</div>
                            <div className={`text-lg font-bold ${oeeColor(p)}`}>{p.toFixed(1)}%</div>
                          </div>
                          <div>
                            <div className="text-xs text-gray-400">Quality</div>
                            <div className={`text-lg font-bold ${oeeColor(q)}`}>{q.toFixed(1)}%</div>
                          </div>
                          <div>
                            <div className="text-xs text-gray-400">OEE</div>
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
          <div className="modal-backdrop" onClick={() => setShowAddModal(false)}></div>
        </div>
      )}
    </div>
  );
}
