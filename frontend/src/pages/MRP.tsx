import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import {
  PlayIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ClockIcon,
  TruckIcon,
  WrenchScrewdriverIcon
} from '@heroicons/react/24/outline';

interface MRPRun {
  id: number;
  run_number: string;
  planning_horizon_days: number;
  status: 'pending' | 'running' | 'complete' | 'error';
  started_at?: string;
  completed_at?: string;
  total_parts_analyzed: number;
  total_requirements: number;
  total_actions: number;
  created_at: string;
}

interface MRPAction {
  id: number;
  part_id: number;
  part?: {
    id: number;
    part_number: string;
    name: string;
    part_type: string;
  };
  action_type: 'order' | 'manufacture' | 'reschedule_in' | 'reschedule_out' | 'cancel' | 'expedite';
  priority: number;
  quantity: number;
  required_date: string;
  suggested_order_date: string;
  is_processed: boolean;
  notes?: string;
}

interface ShortagesSummary {
  mrp_run_id: number;
  mrp_run_number: string;
  run_date?: string;
  total_shortages: number;
  expedite_count: number;
  shortages: Array<{
    action_id: number;
    part_id: number;
    part_number: string;
    part_name: string;
    action_type: string;
    quantity: number;
    required_date: string;
    order_by_date: string;
    priority: number;
    is_expedite: boolean;
  }>;
}

const actionTypeConfig: Record<string, { label: string; color: string; icon: any }> = {
  order: { label: 'Purchase', color: 'bg-green-100 text-green-800', icon: TruckIcon },
  manufacture: { label: 'Manufacture', color: 'bg-blue-100 text-blue-800', icon: WrenchScrewdriverIcon },
  expedite: { label: 'EXPEDITE', color: 'bg-red-100 text-red-800', icon: ExclamationTriangleIcon },
  reschedule_in: { label: 'Reschedule In', color: 'bg-yellow-100 text-yellow-800', icon: ClockIcon },
  reschedule_out: { label: 'Reschedule Out', color: 'bg-gray-100 text-gray-800', icon: ClockIcon },
  cancel: { label: 'Cancel', color: 'bg-gray-100 text-gray-600', icon: ClockIcon },
};

export default function MRPPage() {
  const [runs, setRuns] = useState<MRPRun[]>([]);
  const [shortages, setShortages] = useState<ShortagesSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [runningMRP, setRunningMRP] = useState(false);
  const [selectedRun, setSelectedRun] = useState<MRPRun | null>(null);
  const [runActions, setRunActions] = useState<MRPAction[]>([]);

  // Run parameters
  const [horizonDays, setHorizonDays] = useState(90);
  const [includeSafetyStock, setIncludeSafetyStock] = useState(true);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      const [runsRes, shortagesRes] = await Promise.all([
        api.getMRPRuns(),
        api.getMRPShortages()
      ]);
      setRuns(runsRes);
      setShortages(shortagesRes);
    } catch (err) {
      console.error('Failed to load MRP data:', err);
    } finally {
      setLoading(false);
    }
  };

  const runMRP = async () => {
    setRunningMRP(true);
    try {
      const result = await api.runMRP({
        planning_horizon_days: horizonDays,
        include_safety_stock: includeSafetyStock,
        include_allocated: true
      });
      setRuns([result, ...runs]);
      loadData(); // Refresh shortages
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to run MRP');
    } finally {
      setRunningMRP(false);
    }
  };

  const loadRunActions = async (run: MRPRun) => {
    setSelectedRun(run);
    try {
      const actions = await api.getMRPActions(run.id);
      setRunActions(actions);
    } catch (err) {
      console.error('Failed to load actions:', err);
    }
  };

  const processAction = async (actionId: number) => {
    try {
      await api.processMRPAction(actionId);
      // Reload actions
      if (selectedRun) {
        loadRunActions(selectedRun);
      }
      loadData();
    } catch (err: any) {
      alert(err.response?.data?.detail || 'Failed to process action');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-gray-900">Material Requirements Planning</h1>
      </div>

      {/* Run MRP Panel */}
      <div className="card">
        <h2 className="text-lg font-semibold mb-4">Run MRP</h2>
        <div className="flex flex-wrap items-end gap-4">
          <div>
            <label className="label">Planning Horizon (days)</label>
            <input
              type="number"
              value={horizonDays}
              onChange={(e) => setHorizonDays(parseInt(e.target.value))}
              className="input w-32"
              min={7}
              max={365}
            />
          </div>
          <div>
            <label className="flex items-center">
              <input
                type="checkbox"
                checked={includeSafetyStock}
                onChange={(e) => setIncludeSafetyStock(e.target.checked)}
                className="mr-2"
              />
              <span className="text-sm">Include Safety Stock</span>
            </label>
          </div>
          <button
            onClick={runMRP}
            disabled={runningMRP}
            className="btn-primary flex items-center"
          >
            {runningMRP ? (
              <>
                <div className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full mr-2" />
                Running...
              </>
            ) : (
              <>
                <PlayIcon className="h-5 w-5 mr-2" />
                Run MRP
              </>
            )}
          </button>
        </div>
      </div>

      {/* Shortages Summary */}
      {shortages && shortages.total_shortages > 0 && (
        <div className={`card ${shortages.expedite_count > 0 ? 'border-l-4 border-red-500' : 'border-l-4 border-yellow-500'}`}>
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center">
              <ExclamationTriangleIcon className={`h-6 w-6 mr-2 ${shortages.expedite_count > 0 ? 'text-red-500' : 'text-yellow-500'}`} />
              <h2 className="text-lg font-semibold">Material Shortages</h2>
            </div>
            <div className="text-sm text-gray-500">
              From run {shortages.mrp_run_number}
            </div>
          </div>
          
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
            <div className="bg-gray-50 rounded-lg p-3">
              <div className="text-2xl font-bold">{shortages.total_shortages}</div>
              <div className="text-sm text-gray-500">Total Shortages</div>
            </div>
            <div className={`rounded-lg p-3 ${shortages.expedite_count > 0 ? 'bg-red-50' : 'bg-gray-50'}`}>
              <div className={`text-2xl font-bold ${shortages.expedite_count > 0 ? 'text-red-600' : ''}`}>
                {shortages.expedite_count}
              </div>
              <div className="text-sm text-gray-500">Need Expedite</div>
            </div>
          </div>

          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200">
              <thead className="bg-gray-50">
                <tr>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Action</th>
                  <th className="px-4 py-2 text-right text-xs font-medium text-gray-500 uppercase">Qty</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Needed By</th>
                  <th className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase">Order By</th>
                  <th className="px-4 py-2 text-center text-xs font-medium text-gray-500 uppercase">Actions</th>
                </tr>
              </thead>
              <tbody className="bg-white divide-y divide-gray-200">
                {shortages.shortages.slice(0, 10).map((shortage) => {
                  const config = actionTypeConfig[shortage.action_type] || actionTypeConfig.order;
                  return (
                    <tr key={shortage.action_id} className={shortage.is_expedite ? 'bg-red-50' : ''}>
                      <td className="px-4 py-2">
                        <div className="font-medium">{shortage.part_number}</div>
                        <div className="text-sm text-gray-500">{shortage.part_name}</div>
                      </td>
                      <td className="px-4 py-2">
                        <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${config.color}`}>
                          {config.label}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-right font-medium">{shortage.quantity}</td>
                      <td className="px-4 py-2 text-sm">{format(new Date(shortage.required_date), 'MMM d, yyyy')}</td>
                      <td className="px-4 py-2 text-sm">{format(new Date(shortage.order_by_date), 'MMM d, yyyy')}</td>
                      <td className="px-4 py-2 text-center">
                        <button
                          onClick={() => processAction(shortage.action_id)}
                          className="text-werco-primary hover:text-blue-700 text-sm"
                        >
                          Process
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {shortages.shortages.length > 10 && (
            <p className="text-sm text-gray-500 mt-2">
              Showing 10 of {shortages.shortages.length} shortages
            </p>
          )}
        </div>
      )}

      {/* Recent Runs */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">Recent MRP Runs</h2>
          <div className="space-y-2">
            {runs.map((run) => (
              <div
                key={run.id}
                onClick={() => loadRunActions(run)}
                className={`p-3 rounded-lg border cursor-pointer transition-colors ${
                  selectedRun?.id === run.id
                    ? 'border-werco-primary bg-blue-50'
                    : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <div className="flex justify-between items-start">
                  <div>
                    <div className="font-medium">{run.run_number}</div>
                    <div className="text-sm text-gray-500">
                      {run.completed_at
                        ? format(new Date(run.completed_at), 'MMM d, yyyy h:mm a')
                        : 'In progress...'}
                    </div>
                  </div>
                  <span className={`text-xs px-2 py-1 rounded ${
                    run.status === 'complete' ? 'bg-green-100 text-green-800' :
                    run.status === 'running' ? 'bg-blue-100 text-blue-800' :
                    run.status === 'error' ? 'bg-red-100 text-red-800' :
                    'bg-gray-100 text-gray-800'
                  }`}>
                    {run.status}
                  </span>
                </div>
                <div className="flex gap-4 mt-2 text-xs text-gray-500">
                  <span>{run.total_parts_analyzed} parts</span>
                  <span>{run.total_requirements} requirements</span>
                  <span>{run.total_actions} actions</span>
                </div>
              </div>
            ))}
            {runs.length === 0 && (
              <p className="text-gray-500 text-center py-4">No MRP runs yet</p>
            )}
          </div>
        </div>

        {/* Run Details */}
        <div className="card">
          <h2 className="text-lg font-semibold mb-4">
            {selectedRun ? `Actions - ${selectedRun.run_number}` : 'Run Details'}
          </h2>
          {selectedRun ? (
            <div className="space-y-2 max-h-96 overflow-y-auto">
              {runActions.map((action) => {
                const config = actionTypeConfig[action.action_type] || actionTypeConfig.order;
                const IconComponent = config.icon;
                return (
                  <div
                    key={action.id}
                    className={`p-3 rounded-lg border ${action.is_processed ? 'bg-gray-50 opacity-60' : 'bg-white'}`}
                  >
                    <div className="flex items-start justify-between">
                      <div className="flex items-start">
                        <IconComponent className="h-5 w-5 mr-2 mt-0.5 text-gray-400" />
                        <div>
                          <div className="font-medium">{action.part?.part_number}</div>
                          <div className="text-sm text-gray-500">{action.part?.name}</div>
                        </div>
                      </div>
                      <span className={`inline-flex px-2 py-1 rounded text-xs font-medium ${config.color}`}>
                        {config.label}
                      </span>
                    </div>
                    <div className="mt-2 flex justify-between items-center text-sm">
                      <div>
                        <span className="text-gray-500">Qty:</span>
                        <span className="font-medium ml-1">{action.quantity}</span>
                        <span className="text-gray-500 ml-3">Order by:</span>
                        <span className="ml-1">{format(new Date(action.suggested_order_date), 'MMM d')}</span>
                      </div>
                      {!action.is_processed ? (
                        <button
                          onClick={() => processAction(action.id)}
                          className="text-werco-primary hover:text-blue-700"
                        >
                          Process
                        </button>
                      ) : (
                        <span className="flex items-center text-green-600">
                          <CheckCircleIcon className="h-4 w-4 mr-1" />
                          Done
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
              {runActions.length === 0 && (
                <p className="text-gray-500 text-center py-4">No actions in this run</p>
              )}
            </div>
          ) : (
            <p className="text-gray-500 text-center py-8">Select a run to view details</p>
          )}
        </div>
      </div>
    </div>
  );
}
