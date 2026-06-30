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
import { Modal } from '../components/ui/Modal';
import { EmptyState, ErrorState, FormField, useToast } from '../components/ui';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';

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
  if (val >= 1.33) return 'text-fd-green';
  if (val >= 1.0) return 'text-fd-amber';
  return 'text-fd-red';
};

const capabilityBg = (val: number): string => {
  if (val >= 1.33) return 'bg-fd-green/10 border-fd-green/30';
  if (val >= 1.0) return 'bg-fd-amber/10 border-fd-amber/30';
  return 'bg-fd-red/10 border-fd-red/30';
};

const SPC = () => {
  const { showToast } = useToast();
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
  const [dashboardError, setDashboardError] = useState(false);
  const [detailsError, setDetailsError] = useState(false);
  const [showAddMeasurement, setShowAddMeasurement] = useState(false);
  const [showCreateChar, setShowCreateChar] = useState(false);
  const [measurementForm, setMeasurementForm] = useState({ value: '', measured_by: '', notes: '' });
  const [charForm, setCharForm] = useState({
    name: '', part_id: '', nominal: '', usl: '', lsl: '', chart_type: 'xbar_r',
  });

  const fetchDashboard = useCallback(async () => {
    setDashboardError(false);
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
      setDashboardError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchDashboard();
  }, [fetchDashboard]);

  const fetchCharacteristicDetails = useCallback(async (id: number) => {
    setDetailsError(false);
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
      setDetailsError(true);
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
      showToast('success', 'Measurement added.');
    } catch (err) {
      console.error('Failed to add measurement', err);
      showToast('error', 'Failed to add measurement.');
    }
  }, [selectedId, measurementForm, fetchCharacteristicDetails, fetchDashboard, showToast]);

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
      showToast('success', 'Characteristic created.');
    } catch (err) {
      console.error('Failed to create characteristic', err);
      showToast('error', 'Failed to create characteristic.');
    }
  }, [charForm, fetchDashboard, showToast]);

  const handleRecalculate = useCallback(async () => {
    if (!selectedId) return;
    try {
      await api.calculateSPCControlLimits(selectedId);
      await api.runSPCCapabilityStudy(selectedId);
      fetchCharacteristicDetails(selectedId);
      showToast('success', 'Control limits and capability recalculated.');
    } catch (err) {
      console.error('Failed to recalculate', err);
      showToast('error', 'Failed to recalculate control limits.');
    }
  }, [selectedId, fetchCharacteristicDetails, showToast]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-fd-blue" />
      </div>
    );
  }

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Statistical Process Control</h1>
        <button
          onClick={() => setShowCreateChar(true)}
          className="inline-flex items-center px-3 py-1.5 bg-fd-blue text-white rounded-sm hover:bg-fd-blue/90 text-sm font-medium"
        >
          <PlusIcon className="h-4 w-4 mr-2" />
          New Characteristic
        </button>
      </div>

      {dashboardError && (
        <ErrorState
          message="Could not load SPC dashboard data."
          onRetry={fetchDashboard}
        />
      )}

      {/* Summary Cards */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={ChartBarIcon}
          iconBg="bg-fd-blue/15"
          iconColor="text-fd-blue"
          label="Characteristics Monitored"
          value={stats?.characteristics_monitored ?? 0}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg="bg-fd-red/15"
          iconColor="text-fd-red"
          label="Out-of-Control Alerts"
          value={stats?.out_of_control_count ?? 0}
          valueColor={(stats?.out_of_control_count ?? 0) > 0 ? 'text-fd-red' : undefined}
        />
        <MiniStat
          icon={BeakerIcon}
          iconBg="bg-fd-green/15"
          iconColor="text-fd-green"
          label="Average Cpk"
          value={stats?.avg_cpk?.toFixed(2) ?? '--'}
        />
        <MiniStat
          icon={ClipboardDocumentCheckIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Measurements Today"
          value={stats?.measurements_today ?? 0}
        />
      </MiniStatStrip>

      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4 items-start">
        {/* Characteristic Selector */}
        <CockpitPanel
          title="Characteristics"
          className="lg:col-span-1"
          footer={`${characteristics.length} characteristic${characteristics.length === 1 ? '' : 's'}`}
        >
          <div className="space-y-1">
            {characteristics.map((c) => (
              <button
                key={c.id}
                onClick={() => setSelectedId(c.id)}
                className={`w-full text-left px-3 py-2 rounded-sm text-sm transition-colors min-w-0 ${
                  selectedId === c.id
                    ? 'bg-fd-blue/20 text-fd-blue font-medium'
                    : 'text-slate-300 hover:bg-fd-raised'
                }`}
              >
                <span className="block truncate">{c.name}</span>
                <span className="block text-xs text-slate-500 truncate">{c.chart_type}</span>
              </button>
            ))}
            {characteristics.length === 0 && (
              <EmptyState
                icon={BeakerIcon}
                title="No characteristics"
                description="Define a characteristic to start monitoring it."
                action={{ label: 'New Characteristic', onClick: () => setShowCreateChar(true) }}
                className="px-3 py-8"
              />
            )}
          </div>
        </CockpitPanel>

        {/* Main Content */}
        <div className="lg:col-span-3 space-y-4">
          {selectedChar && detailsError ? (
            <ErrorState
              message={`Could not load details for ${selectedChar.name}.`}
              onRetry={() => fetchCharacteristicDetails(selectedChar.id)}
            />
          ) : selectedChar ? (
            <>
              {/* Control Chart + Process Capability side-by-side */}
              <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
                <CockpitPanel
                  title={`Control Chart: ${selectedChar.name}`}
                  className="xl:col-span-8"
                  bodyClassName="lg:max-h-none"
                  headerExtra={
                    <button
                      onClick={handleRecalculate}
                      className="inline-flex items-center px-3 py-1.5 text-sm text-fd-blue border border-fd-blue/40 rounded-sm hover:bg-fd-blue/20"
                    >
                      <ArrowPathIcon className="h-4 w-4 mr-1" />
                      Recalculate
                    </button>
                  }
                >
                  <div className="h-80">
                    <ResponsiveContainer width="100%" height="100%">
                      <LineChart data={chartData} margin={{ top: 5, right: 20, left: 10, bottom: 5 }}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
                        <XAxis dataKey="index" tick={{ fontSize: 12, fill: '#94a3b8' }} stroke="#334155" />
                        <YAxis tick={{ fontSize: 12, fill: '#94a3b8' }} stroke="#334155" domain={['auto', 'auto']} />
                        <Tooltip contentStyle={{ backgroundColor: '#1a1f2e', border: '1px solid #334155', borderRadius: '3px', color: '#e2e8f0' }} />
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
                  <div className="mt-3 flex gap-4 text-xs text-slate-400 tabular-nums">
                    <span>Nominal: {selectedChar.nominal}</span>
                    <span>USL: {selectedChar.usl}</span>
                    <span>LSL: {selectedChar.lsl}</span>
                  </div>
                </CockpitPanel>

                {/* Process Capability */}
                {capability && (
                  <CockpitPanel
                    title="Process Capability"
                    className="xl:col-span-4"
                    bodyClassName="lg:max-h-none"
                  >
                    <div className="grid grid-cols-2 gap-2">
                      {[
                        { label: 'Cp', value: capability.cp },
                        { label: 'Cpk', value: capability.cpk },
                        { label: 'Pp', value: capability.pp },
                        { label: 'Ppk', value: capability.ppk },
                      ].map((item) => (
                        <div
                          key={item.label}
                          className={`border rounded-sm p-3 text-center min-w-0 ${capabilityBg(item.value)}`}
                        >
                          <p className="text-xs font-medium text-slate-400">{item.label}</p>
                          <p className={`text-xl font-bold mt-1 tabular-nums ${capabilityColor(item.value)}`}>
                            {item.value?.toFixed(3) ?? '--'}
                          </p>
                          <p className="text-[10px] text-slate-500 mt-1 truncate">
                            {item.value >= 1.33 ? 'Capable' : item.value >= 1.0 ? 'Marginal' : 'Not Capable'}
                          </p>
                        </div>
                      ))}
                    </div>
                  </CockpitPanel>
                )}
              </div>

              {/* Recent Measurements + Violations side-by-side */}
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
                {/* Recent Measurements */}
                <CockpitPanel
                  title="Recent Measurements"
                  footer={`${measurements.length} measurement${measurements.length === 1 ? '' : 's'}`}
                  headerExtra={
                    <button
                      onClick={() => setShowAddMeasurement(true)}
                      className="inline-flex items-center px-3 py-1.5 text-sm bg-fd-green text-white rounded-sm hover:bg-fd-green/90"
                    >
                      <PlusIcon className="h-4 w-4 mr-1" />
                      Add Measurement
                    </button>
                  }
                >
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-fd-line text-sm">
                      <thead className="bg-fd-sunken">
                        <tr>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Value</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Measured By</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Time</th>
                          <th className="px-3 py-2 text-left font-medium text-slate-400">Notes</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-fd-line/40">
                        {measurements.map((m) => {
                          const ooc =
                            controlLimits &&
                            (m.value > controlLimits.ucl || m.value < controlLimits.lcl);
                          return (
                            <tr key={m.id} className={ooc ? 'bg-fd-red/10' : ''}>
                              <td className={`px-3 py-2 font-mono tabular-nums ${ooc ? 'text-fd-red font-bold' : ''}`}>
                                {m.value}
                              </td>
                              <td className="px-3 py-2 text-slate-300 truncate">{m.measured_by}</td>
                              <td className="px-3 py-2 text-slate-400 whitespace-nowrap">
                                {new Date(m.measured_at).toLocaleString()}
                              </td>
                              <td className="px-3 py-2 text-slate-400 truncate">{m.notes || '--'}</td>
                            </tr>
                          );
                        })}
                        {measurements.length === 0 && (
                          <tr>
                            <td colSpan={4} className="px-3 py-6 text-center text-slate-500">
                              No measurements recorded.
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                </CockpitPanel>

                {/* Violations */}
                {violations.length > 0 && (
                  <CockpitPanel
                    title="Control Violations"
                    className="border-fd-red/30"
                    footer={`${violations.length} violation${violations.length === 1 ? '' : 's'}`}
                    headerExtra={<ExclamationTriangleIcon className="h-5 w-5 text-fd-red" />}
                  >
                    <div className="space-y-2">
                      {violations.map((v) => (
                        <div key={v.id} className="flex items-start gap-3 p-3 bg-fd-red/10 rounded-sm min-w-0">
                          <ExclamationTriangleIcon className="h-5 w-5 text-fd-red mt-0.5 flex-shrink-0" />
                          <div className="min-w-0">
                            <p className="text-sm font-medium text-red-300 truncate">{v.rule}</p>
                            <p className="text-sm text-fd-red">{v.description}</p>
                            <p className="text-xs text-red-400 mt-1 tabular-nums">
                              Value: {v.measurement_value} | {new Date(v.detected_at).toLocaleString()}
                            </p>
                          </div>
                        </div>
                      ))}
                    </div>
                  </CockpitPanel>
                )}
              </div>
            </>
          ) : (
            <EmptyState
              icon={ChartBarIcon}
              title="Select a characteristic to view control charts"
              description="Choose from the list on the left or create a new one."
              action={{ label: 'New Characteristic', onClick: () => setShowCreateChar(true) }}
            />
          )}
        </div>
      </div>

      {/* Out-of-Control Alerts */}
      {outOfControl.length > 0 && (
        <CockpitPanel
          title={`Out-of-Control Alerts (${outOfControl.length})`}
          className="border-fd-red/30"
          footer={`${outOfControl.length} alert${outOfControl.length === 1 ? '' : 's'}`}
          headerExtra={<ExclamationTriangleIcon className="h-5 w-5 text-fd-red" />}
        >
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {outOfControl.map((alert: any, idx: number) => (
              <button
                type="button"
                key={idx}
                onClick={() => alert.characteristic_id && setSelectedId(alert.characteristic_id)}
                className="text-left w-full p-3 bg-fd-red/10 rounded-sm border border-fd-red/30 cursor-pointer hover:bg-fd-red/20 transition-colors min-w-0"
              >
                <p className="text-sm font-medium text-red-300 truncate">{alert.characteristic_name || `Characteristic #${alert.characteristic_id}`}</p>
                <p className="text-xs text-fd-red mt-1 truncate">{alert.reason || alert.description || 'Out of control'}</p>
              </button>
            ))}
          </div>
        </CockpitPanel>
      )}

      {/* Add Measurement Modal */}
      <Modal
        open={showAddMeasurement}
        onClose={() => setShowAddMeasurement(false)}
        size="md"
        closeOnBackdrop={false}
      >
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-white">Add Measurement</h3>
              <button onClick={() => setShowAddMeasurement(false)}>
                <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" />
              </button>
            </div>
            <div className="space-y-4">
              <FormField label="Value" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    step="any"
                    value={measurementForm.value}
                    onChange={(e) => setMeasurementForm({ ...measurementForm, value: e.target.value })}
                    className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    placeholder="Enter measured value"
                  />
                )}
              </FormField>
              <FormField label="Measured By" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={measurementForm.measured_by}
                    onChange={(e) => setMeasurementForm({ ...measurementForm, measured_by: e.target.value })}
                    className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    placeholder="Operator name"
                  />
                )}
              </FormField>
              <FormField label="Notes" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <textarea
                    {...field}
                    value={measurementForm.notes}
                    onChange={(e) => setMeasurementForm({ ...measurementForm, notes: e.target.value })}
                    className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    rows={2}
                    placeholder="Optional notes"
                  />
                )}
              </FormField>
              <div className="flex justify-end gap-3 pt-2">
                <button
                  onClick={() => setShowAddMeasurement(false)}
                  className="px-4 py-2 text-sm text-slate-300 border border-slate-600 rounded-md hover:bg-slate-800/50"
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
      </Modal>

      {/* Create Characteristic Modal */}
      <Modal
        open={showCreateChar}
        onClose={() => setShowCreateChar(false)}
        size="md"
        closeOnBackdrop={false}
      >
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold text-white">New Characteristic</h3>
              <button onClick={() => setShowCreateChar(false)}>
                <XMarkIcon className="h-5 w-5 text-slate-500 hover:text-slate-400" />
              </button>
            </div>
            <div className="space-y-4">
              <FormField label="Name" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input
                    {...field}
                    type="text"
                    value={charForm.name}
                    onChange={(e) => setCharForm({ ...charForm, name: e.target.value })}
                    className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    placeholder="e.g., Bore Diameter"
                  />
                )}
              </FormField>
              <FormField label="Part ID" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <input
                    {...field}
                    type="number"
                    value={charForm.part_id}
                    onChange={(e) => setCharForm({ ...charForm, part_id: e.target.value })}
                    className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  />
                )}
              </FormField>
              <div className="grid grid-cols-3 gap-3">
                <FormField label="Nominal" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      step="any"
                      value={charForm.nominal}
                      onChange={(e) => setCharForm({ ...charForm, nominal: e.target.value })}
                      className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    />
                  )}
                </FormField>
                <FormField label="USL" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      step="any"
                      value={charForm.usl}
                      onChange={(e) => setCharForm({ ...charForm, usl: e.target.value })}
                      className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    />
                  )}
                </FormField>
                <FormField label="LSL" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                  {(field) => (
                    <input
                      {...field}
                      type="number"
                      step="any"
                      value={charForm.lsl}
                      onChange={(e) => setCharForm({ ...charForm, lsl: e.target.value })}
                      className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                    />
                  )}
                </FormField>
              </div>
              <FormField label="Chart Type" labelClassName="block text-sm font-medium text-slate-300 mb-1">
                {(field) => (
                  <select
                    {...field}
                    value={charForm.chart_type}
                    onChange={(e) => setCharForm({ ...charForm, chart_type: e.target.value })}
                    className="w-full border border-slate-600 rounded-md px-3 py-2 text-sm focus:ring-blue-500 focus:border-blue-500"
                  >
                    <option value="xbar_r">X-bar & R</option>
                    <option value="xbar_s">X-bar & S</option>
                    <option value="individual_mr">Individual & MR</option>
                    <option value="p_chart">P Chart</option>
                    <option value="np_chart">NP Chart</option>
                    <option value="c_chart">C Chart</option>
                    <option value="u_chart">U Chart</option>
                  </select>
                )}
              </FormField>
              <div className="flex justify-end gap-3 pt-2">
                <button
                  onClick={() => setShowCreateChar(false)}
                  className="px-4 py-2 text-sm text-slate-300 border border-slate-600 rounded-md hover:bg-slate-800/50"
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
      </Modal>
    </div>
  );
};

export default SPC;
