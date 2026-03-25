import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ChartBarIcon,
  ExclamationTriangleIcon,
  BeakerIcon,
  ClipboardDocumentCheckIcon,
  PlusIcon,
  XMarkIcon,
  ArrowPathIcon,
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

interface DashboardStats {
  characteristics_monitored: number;
  out_of_control_count: number;
  avg_cpk: number;
  measurements_today: number;
}

interface Characteristic {
  id: number;
  name: string;
  part_id: number;
  nominal: number;
  usl: number;
  lsl: number;
  chart_type: string;
}

interface ControlLimits {
  ucl: number;
  cl: number;
  lcl: number;
}

interface Capability {
  cp: number;
  cpk: number;
  pp: number;
  ppk: number;
}

interface Measurement {
  id: number;
  value: number;
  measured_by: string;
  measured_at: string;
  notes: string;
}

interface ChartPoint {
  index: number;
  value: number;
  timestamp: string;
}

interface Violation {
  id: number;
  rule: string;
  description: string;
  detected_at: string;
  measurement_value: number;
}

const capabilityColor = (val: number): string => {
  if (val >= 1.33) return 'text-green-600';
  if (val >= 1.0) return 'text-yellow-600';
  return 'text-red-600';
};

const capabilityBg = (val: number): string => {
  if (val >= 1.33) return 'bg-green-50 border-green-200';
  if (val >= 1.0) return 'bg-yellow-50 border-yellow-200';
  return 'bg-red-50 border-red-200';
};

const SPC = () => {
  const [stats, setStats] = useState<DashboardStats | null>(null);
  const [characteristics, setCharacteristics] = useState<Characteristic[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [controlLimits, setControlLimits] = useState<ControlLimits | null>(null);
  const [capability, setCapability] = useState<Capability | null>(null);
  const [measurements, setMeasurements] = useState<Measurement[]>([]);
  const [chartData, setChartData] = useState<ChartPoint[]>([]);
  const [violations, setViolations] = useState<Violation[]>([]);
  const [outOfControl, setOutOfControl] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAddMeasurement, setShowAddMeasurement] = useState(false);
  const [showCreateChar, setShowCreateChar] = useState(false);
  const [measurementForm, setMeasurementForm] = useState({ value: '', measured_by: '', notes: '' });
  const [charForm, setCharForm] = useState({
    name: '', part_id: '', nominal: '', usl: '', lsl: '', chart_type: 'xbar_r',
  });

  const fetchDashboard = useCallback(async () => {
    try {
      const [dashRes, charRes, oocRes] = await Promise.all([
        api.getSPCDashboard(),
        api.getSPCCharacteristics({}),
        api.getSPCOutOfControl(),
      ]);
      setStats(dashRes.data);
      setCharacteristics(charRes.data?.results || charRes.data || []);
      setOutOfControl(oocRes.data?.results || oocRes.data || []);
    } catch (err) {
      console.error('Failed to load SPC dashboard', err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  const fetchCharacteristicDetails = useCallback(async (id: number) => {
    try {
      const [limitsRes, capRes, measRes, chartRes, violRes] = await Promise.all([
        api.getSPCControlLimits(id),
        api.getSPCCapability(id),
        api.getSPCMeasurements(id, { limit: 20 }),
        api.getSPCChartData(id, {}),
        api.getSPCViolations(id),
      ]);
      setControlLimits(limitsRes.data);
      setCapability(capRes.data);
      setMeasurements(measRes.data?.results || measRes.data || []);
      setChartData(chartRes.data?.results || chartRes.data || []);
      setViolations(violRes.data?.results || violRes.data || []);
    } catch (err) {
      console.error('Failed to load characteristic details', err);
    }
  }, []);

  useEffect(() => {
    if (selectedId) {
      fetchCharacteristicDetails(selectedId);
    }
  }, [selectedId, fetchCharacteristicDetails]);

  const selectedChar = useMemo(
    () => characteristics.find((c) => c.id === selectedId) || null,
    [characteristics, selectedId]
  );

  const handleAddMeasurement = useCallback(async () => {
    if (!selectedId || !measurementForm.value) return;
    try {
      await api.addSPCMeasurements({
        characteristic_id: selectedId,
        value: parseFloat(measurementForm.value),
        measured_by: measurementForm.measured_by,
        notes: measurementForm.notes,
      });
      setMeasurementForm({ value: '', measured_by: '', notes: '' });
      setShowAddMeasurement(false);
      fetchCharacteristicDetails(selectedId);
      fetchDashboard();
    } catch (err) {
      console.error('Failed to add measurement', err);
    }
  }, [selectedId, measurementForm, fetchCharacteristicDetails, fetchDashboard]);

  const handleCreateCharacteristic = useCallback(async () => {
    if (!charForm.name) return;
    try {
      await api.createSPCCharacteristic({
        name: charForm.name,
        part_id: parseInt(charForm.part_id),
        nominal: parseFloat(charForm.nominal),
        usl: parseFloat(charForm.usl),
        lsl: parseFloat(charForm.lsl),
        chart_type: charForm.chart_type,
      });
      setCharForm({ name: '', part_id: '', nominal: '', usl: '', lsl: '', chart_type: 'xbar_r' });
      setShowCreateChar(false);
      fetchDashboard();
    } catch (err) {
      console.error('Failed to create characteristic', err);
    }
  }, [charForm, fetchDashboard]);

  const handleRecalculate = useCallback(async () => {
    if (!selectedId) return;
    try {
      await api.calculateSPCControlLimits(selectedId);
      await api.runSPCCapabilityStudy(selectedId);
      fetchCharacteristicDetails(selectedId);
    } catch (err) {
      console.error('Failed to recalculate', err);
    }
  }, [selectedId, fetchCharacteristicDetails]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Statistical Process Control</h1>
        <button
          onClick={() => setShowCreateChar(true)}
          className="inline-flex items-center px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium"
        >
          <PlusIcon className="h-4 w-4 mr-2" />
          New Characteristic
        </button>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {[
          { label: 'Characteristics Monitored', value: stats?.characteristics_monitored ?? 0, icon: ChartBarIcon, color: 'blue' },
          { label: 'Out-of-Control Alerts', value: stats?.out_of_control_count ?? 0, icon: ExclamationTriangleIcon, color: 'red' },
          { label: 'Average Cpk', value: stats?.avg_cpk?.toFixed(2) ?? '--', icon: BeakerIcon, color: 'green' },
          { label: 'Measurements Today', value: stats?.measurements_today ?? 0, icon: ClipboardDocumentCheckIcon, color: 'purple' },
        ].map((card) => (
          <div key={card.label} className="bg-white rounded-lg shadow p-5 border border-gray-200">
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm text-gray-500">{card.label}</p>
                <p className={`text-2xl font-bold text-${card.color}-600 mt-1`}>{card.value}</p>
              </div>
              <card.icon className={`h-10 w-10 text-${card.color}-400`} />
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-6">
        {/* Characteristic Selector */}
        <div className="lg:col-span-1 bg-white rounded-lg shadow border border-gray-200 p-4">
          <h2 className="text-lg font-semibold text-gray-900 mb-3">Characteristics</h2>
          <div className="space-y-1 max-h-96 overflow-y-auto">
            {characteristics.map((c) => (
              <button
                key={c.id}
                onClick={() => setSelectedId(c.id)}
                className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors ${
                  selectedId === c.id
                    ? 'bg-blue-100 text-blue-800 font-medium'
                    : 'text-gray-700 hover:bg-gray-100'
                }`}
              >
                {c.name}
                <span className="block text-xs text-gray-400">{c.chart_type}</span>
              </button>
            ))}
            {characteristics.length === 0 && (
              <p className="text-sm text-gray-400 py-2">No characteristics defined.</p>
            )}
          </div>
        </div>

        {/* Main Content */}
        <div className="lg:col-span-3 space-y-6">
          {selectedChar ? (
            <>
              {/* Control Chart */}
              <div className="bg-white rounded-lg shadow border border-gray-200 p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-lg font-semibold text-gray-900">
                    Control Chart: {selectedChar.name}
                  </h2>
                  <button
                    onClick={handleRecalculate}
                    className="inline-flex items-center px-3 py-1.5 text-sm text-blue-600 border border-blue-300 rounded-md hover:bg-blue-50"
                  >
                    <ArrowPathIcon className="h-4 w-4 mr-1" />
                    Recalculate
                  </button>
                </div>
                <div className="h-80">
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                      <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                      <XAxis dataKey="index" tick={{ fontSize: 12 }} />
                      <YAxis tick={{ fontSize: 12 }} domain={['auto', 'auto']} />
                      <Tooltip />
                      <Legend />
                      {controlLimits && (
                        <>
                          <ReferenceLine y={controlLimits.ucl} stroke="#ef4444" strokeDasharray="5 5" label="UCL" />
                          <ReferenceLine y={controlLimits.cl} stroke="#22c55e" strokeDasharray="3 3" label="CL" />
                          <ReferenceLine y={controlLimits.lcl} stroke="#ef4444" strokeDasharray="5 5" label="LCL" />
                        </>
                      )}
                      <Line
                        type="monotone"
                        dataKey="value"
                        stroke="#3b82f6"
                        strokeWidth={2}
                        dot={{ r: 3, fill: '#3b82f6' }}
                        activeDot={{ r: 5 }}
                        name="Measurement"
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
                <div className="mt-3 flex gap-4 text-xs text-gray-500">
                  <span>Nominal: {selectedChar.nominal}</span>
                  <span>USL: {selectedChar.usl}</span>
                  <span>LSL: {selectedChar.lsl}</span>
                </div>
              </div>

              {/* Process Capability */}
              {capability && (
                <div className="bg-white rounded-lg shadow border border-gray-200 p-5">
                  <h2 className="text-lg font-semibold text-gray-900 mb-4">Process Capability</h2>
                  <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                    {[
                      { label: 'Cp', value: capability.cp },
                      { label: 'Cpk', value: capability.cpk },
                      { label: 'Pp', value: capability.pp },
                      { label: 'Ppk', value: capability.ppk },
                    ].map((item) => (
                      <div
                        key={item.label}
                        className={`border rounded-lg p-4 text-center ${capabilityBg(item.value)}`}
                      >
                        <p className="text-sm font-medium text-gray-600">{item.label}</p>
                        <p className={`text-2xl font-bold mt-1 ${capabilityColor(item.value)}`}>
                          {item.value?.toFixed(3) ?? '--'}
                        </p>
                        <p className="text-xs text-gray-400 mt-1">
                          {item.value >= 1.33 ? 'Capable' : item.value >= 1.0 ? 'Marginal' : 'Not Capable'}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Recent Measurements */}
              <div className="bg-white rounded-lg shadow border border-gray-200 p-5">
                <div className="flex items-center justify-between mb-4">
                  <h2 className="text-lg font-semibold text-gray-900">Recent Measurements</h2>
                  <button
                    onClick={() => setShowAddMeasurement(true)}
                    className="inline-flex items-center px-3 py-1.5 text-sm bg-green-600 text-white rounded-md hover:bg-green-700"
                  >
                    <PlusIcon className="h-4 w-4 mr-1" />
                    Add Measurement
                  </button>
                </div>
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-gray-200 text-sm">
                    <thead className="bg-gray-50">
                      <tr>
                        <th className="px-4 py-2 text-left font-medium text-gray-500">Value</th>
                        <th className="px-4 py-2 text-left font-medium text-gray-500">Measured By</th>
                        <th className="px-4 py-2 text-left font-medium text-gray-500">Time</th>
                        <th className="px-4 py-2 text-left font-medium text-gray-500">Notes</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100">
                      {measurements.map((m) => {
                        const ooc =
                          controlLimits &&
                          (m.value > controlLimits.ucl || m.value < controlLimits.lcl);
                        return (
                          <tr key={m.id} className={ooc ? 'bg-red-50' : ''}>
                            <td className={`px-4 py-2 font-mono ${ooc ? 'text-red-600 font-bold' : ''}`}>
                              {m.value}
                            </td>
                            <td className="px-4 py-2 text-gray-700">{m.measured_by}</td>
                            <td className="px-4 py-2 text-gray-500">
                              {new Date(m.measured_at).toLocaleString()}
                            </td>
                            <td className="px-4 py-2 text-gray-500">{m.notes || '--'}</td>
                          </tr>
                        );
                      })}
                      {measurements.length === 0 && (
                        <tr>
                          <td colSpan={4} className="px-4 py-6 text-center text-gray-400">
                            No measurements recorded.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Violations */}
              {violations.length > 0 && (
                <div className="bg-white rounded-lg shadow border border-red-200 p-5">
                  <h2 className="text-lg font-semibold text-red-700 mb-3 flex items-center">
                    <ExclamationTriangleIcon className="h-5 w-5 mr-2" />
                    Control Violations
                  </h2>
                  <div className="space-y-2">
                    {violations.map((v) => (
                      <div key={v.id} className="flex items-start gap-3 p-3 bg-red-50 rounded-md">
                        <ExclamationTriangleIcon className="h-5 w-5 text-red-500 mt-0.5 flex-shrink-0" />
                        <div>
                          <p className="text-sm font-medium text-red-800">{v.rule}</p>
                          <p className="text-sm text-red-600">{v.description}</p>
                          <p className="text-xs text-red-400 mt-1">
                            Value: {v.measurement_value} | {new Date(v.detected_at).toLocaleString()}
                          </p>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          ) : (
            <div className="bg-white rounded-lg shadow border border-gray-200 p-12 text-center">
              <ChartBarIcon className="h-16 w-16 text-gray-300 mx-auto mb-4" />
              <p className="text-gray-500 text-lg">Select a characteristic to view control charts</p>
              <p className="text-gray-400 text-sm mt-1">
                Choose from the list on the left or create a new one.
              </p>
            </div>
          )}
        </div>
      </div>

      {/* Out-of-Control Alerts */}
      {outOfControl.length > 0 && (
        <div className="bg-white rounded-lg shadow border border-red-200 p-5">
          <h2 className="text-lg font-semibold text-red-700 mb-3 flex items-center">
            <ExclamationTriangleIcon className="h-5 w-5 mr-2" />
            Out-of-Control Alerts ({outOfControl.length})
          </h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {outOfControl.map((alert: any, idx: number) => (
              <div
                key={idx}
                onClick={() => alert.characteristic_id && setSelectedId(alert.characteristic_id)}
                className="p-3 bg-red-50 rounded-md border border-red-200 cursor-pointer hover:bg-red-100 transition-colors"
              >
                <p className="text-sm font-medium text-red-800">{alert.characteristic_name || `Characteristic #${alert.characteristic_id}`}</p>
                <p className="text-xs text-red-600 mt-1">{alert.reason || alert.description || 'Out of control'}</p>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Add Measurement Modal */}
      {showAddMeasurement && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-gray-900">Add Measurement</h3>
              <button onClick={() => setShowAddMeasurement(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-400 hover:text-gray-600" />
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Value *</label>
                <input
                  type="number"
                  step="any"
                  value={measurementForm.value}
                  onChange={(e) => setMeasurementForm({ ...measurementForm, value: e.target.value })}
                  className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  placeholder="Enter measured value"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Measured By</label>
                <input
                  type="text"
                  value={measurementForm.measured_by}
                  onChange={(e) => setMeasurementForm({ ...measurementForm, measured_by: e.target.value })}
                  className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  placeholder="Operator name"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Notes</label>
                <textarea
                  value={measurementForm.notes}
                  onChange={(e) => setMeasurementForm({ ...measurementForm, notes: e.target.value })}
                  className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  rows={2}
                  placeholder="Optional notes"
                />
              </div>
              <div className="flex justify-end gap-3 pt-2">
                <button
                  onClick={() => setShowAddMeasurement(false)}
                  className="px-4 py-2 text-sm text-gray-700 border border-gray-300 rounded-md hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleAddMeasurement}
                  className="px-4 py-2 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700"
                >
                  Save Measurement
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Create Characteristic Modal */}
      {showCreateChar && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-40">
          <div className="bg-white rounded-lg shadow-xl w-full max-w-md p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-gray-900">New Characteristic</h3>
              <button onClick={() => setShowCreateChar(false)}>
                <XMarkIcon className="h-5 w-5 text-gray-400 hover:text-gray-600" />
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Name *</label>
                <input
                  type="text"
                  value={charForm.name}
                  onChange={(e) => setCharForm({ ...charForm, name: e.target.value })}
                  className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  placeholder="e.g., Bore Diameter"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Part ID</label>
                <input
                  type="number"
                  value={charForm.part_id}
                  onChange={(e) => setCharForm({ ...charForm, part_id: e.target.value })}
                  className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                />
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Nominal</label>
                  <input
                    type="number"
                    step="any"
                    value={charForm.nominal}
                    onChange={(e) => setCharForm({ ...charForm, nominal: e.target.value })}
                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">USL</label>
                  <input
                    type="number"
                    step="any"
                    value={charForm.usl}
                    onChange={(e) => setCharForm({ ...charForm, usl: e.target.value })}
                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">LSL</label>
                  <input
                    type="number"
                    step="any"
                    value={charForm.lsl}
                    onChange={(e) => setCharForm({ ...charForm, lsl: e.target.value })}
                    className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Chart Type</label>
                <select
                  value={charForm.chart_type}
                  onChange={(e) => setCharForm({ ...charForm, chart_type: e.target.value })}
                  className="w-full border border-gray-300 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                >
                  <option value="xbar_r">X-bar & R</option>
                  <option value="xbar_s">X-bar & S</option>
                  <option value="individual_mr">Individual & MR</option>
                  <option value="p_chart">P Chart</option>
                  <option value="np_chart">NP Chart</option>
                  <option value="c_chart">C Chart</option>
                  <option value="u_chart">U Chart</option>
                </select>
              </div>
              <div className="flex justify-end gap-3 pt-2">
                <button
                  onClick={() => setShowCreateChar(false)}
                  className="px-4 py-2 text-sm text-gray-700 border border-gray-300 rounded-md hover:bg-gray-50"
                >
                  Cancel
                </button>
                <button
                  onClick={handleCreateCharacteristic}
                  className="px-4 py-2 text-sm bg-blue-600 text-white rounded-md hover:bg-blue-700"
                >
                  Create
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default SPC;
