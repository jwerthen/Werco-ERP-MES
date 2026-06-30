import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { formatCentralDate, formatCentralDateTime } from '../utils/centralTime';
import { MiniStat, MiniStatStrip, CockpitPanel } from '../components/cockpit';
import { EmptyState, ErrorState, useToast } from '../components/ui';
import {
  PlayIcon,
  ExclamationTriangleIcon,
  CheckCircleIcon,
  ClockIcon,
  TruckIcon,
  WrenchScrewdriverIcon,
  CubeIcon,
  ClipboardDocumentListIcon,
  BoltIcon,
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
  order: { label: 'Purchase', color: 'bg-green-500/20 text-emerald-300', icon: TruckIcon },
  manufacture: { label: 'Manufacture', color: 'bg-blue-500/20 text-blue-300', icon: WrenchScrewdriverIcon },
  expedite: { label: 'EXPEDITE', color: 'bg-red-500/20 text-red-300', icon: ExclamationTriangleIcon },
  reschedule_in: { label: 'Reschedule In', color: 'bg-yellow-500/20 text-yellow-300', icon: ClockIcon },
  reschedule_out: { label: 'Reschedule Out', color: 'bg-slate-800/50 text-slate-100', icon: ClockIcon },
  cancel: { label: 'Cancel', color: 'bg-slate-800/50 text-slate-400', icon: ClockIcon },
};

/** Normalized shape consumed by the shared ActionRow — both the shortages table
 *  and the run-detail list feed this, so the action data is rendered through one
 *  dense component and cross-linked by the stable action_id. */
interface ActionRowData {
  actionId: number;
  partId: number;
  partNumber: string;
  partName: string;
  actionType: string;
  quantity: number;
  /** "Needed by" date (required_date). */
  requiredDate?: string;
  /** "Order by" date (suggested_order_date / order_by_date). */
  orderByDate?: string;
  isProcessed: boolean;
  isExpedite?: boolean;
}

/**
 * Dense, single-row renderer for one MRP action. Reused by both the Shortages
 * panel and the Run Details panel so the action data lives in exactly one
 * component; rows are keyed/cross-linked by the stable actionId.
 */
function ActionRow({
  data,
  onProcess,
  highlight,
}: {
  data: ActionRowData;
  onProcess?: (actionId: number) => void;
  /** Marks this action as also surfaced in the other panel (same actionId). */
  highlight?: boolean;
}) {
  const config = actionTypeConfig[data.actionType] || actionTypeConfig.order;
  const Icon = config.icon;
  return (
    <div
      data-action-id={data.actionId}
      className={`flex items-center gap-2 px-2.5 py-2 min-w-0 ${
        data.isProcessed ? 'opacity-60' : data.isExpedite ? 'bg-fd-red/10' : highlight ? 'bg-fd-blue/5' : ''
      }`}
    >
      <Icon className="h-4 w-4 flex-shrink-0 text-slate-400" />
      <div className="min-w-0 flex-1">
        <div className="font-medium truncate text-sm">{data.partNumber}</div>
        <div className="text-[11px] text-slate-400 truncate">{data.partName}</div>
      </div>
      <span className={`inline-flex flex-shrink-0 px-1.5 py-0.5 rounded-sm text-[10px] font-medium ${config.color}`}>
        {config.label}
      </span>
      <div className="flex-shrink-0 text-right tabular-nums">
        <div className="text-sm font-medium">{data.quantity}</div>
        <div className="text-[10px] text-slate-400 leading-tight">
          {data.requiredDate && <span>need {formatCentralDate(data.requiredDate, { year: undefined })}</span>}
          {data.requiredDate && data.orderByDate && <span className="mx-1 text-slate-600">·</span>}
          {data.orderByDate && <span>order {formatCentralDate(data.orderByDate, { year: undefined })}</span>}
        </div>
      </div>
      <div className="flex-shrink-0 w-16 text-right">
        {!data.isProcessed && onProcess ? (
          <button
            onClick={() => onProcess(data.actionId)}
            className="text-werco-primary hover:text-blue-400 text-xs font-medium"
          >
            Process
          </button>
        ) : data.isProcessed ? (
          <span className="inline-flex items-center justify-end text-fd-green text-xs">
            <CheckCircleIcon className="h-3.5 w-3.5 mr-1" />
            Done
          </span>
        ) : null}
      </div>
    </div>
  );
}

export default function MRPPage() {
  const { showToast } = useToast();
  const [runs, setRuns] = useState<MRPRun[]>([]);
  const [shortages, setShortages] = useState<ShortagesSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [actionsError, setActionsError] = useState(false);
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
    setLoadError(false);
    try {
      const [runsRes, shortagesRes] = await Promise.all([
        api.getMRPRuns(),
        api.getMRPShortages()
      ]);
      setRuns(runsRes);
      setShortages(shortagesRes);
    } catch (err) {
      console.error('Failed to load MRP data:', err);
      setLoadError(true);
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
      showToast('error', err.response?.data?.detail || 'Failed to run MRP');
    } finally {
      setRunningMRP(false);
    }
  };

  const loadRunActions = async (run: MRPRun) => {
    setSelectedRun(run);
    setActionsError(false);
    try {
      const actions = await api.getMRPActions(run.id);
      setRunActions(actions);
    } catch (err) {
      console.error('Failed to load actions:', err);
      setActionsError(true);
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
      showToast('error', err.response?.data?.detail || 'Failed to process action');
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  const latestRun = runs[0];
  const totalShortages = shortages?.total_shortages ?? 0;
  const expediteCount = shortages?.expedite_count ?? 0;
  // Stable cross-link: action_ids surfaced in the Shortages panel, so a selected
  // run's actions can be flagged where they overlap (by id, never by name).
  const shortageActionIds = new Set((shortages?.shortages ?? []).map((s) => s.action_id));

  return (
    <div className="space-y-4">
      {/* Header + Run MRP toolbar */}
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-2xl font-bold text-white">Material Requirements Planning</h1>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label htmlFor="mrp-horizon-days" className="label !text-[10px] uppercase tracking-wide">
              Horizon (days)
            </label>
            <input
              id="mrp-horizon-days"
              type="number"
              aria-label="Horizon (days)"
              value={horizonDays}
              onChange={(e) => setHorizonDays(parseInt(e.target.value))}
              className="input w-24 tabular-nums"
              min={7}
              max={365}
            />
          </div>
          <label className="flex items-center h-9">
            <input
              type="checkbox"
              aria-label="Safety Stock"
              checked={includeSafetyStock}
              onChange={(e) => setIncludeSafetyStock(e.target.checked)}
              className="mr-2"
            />
            <span className="text-sm">Safety Stock</span>
          </label>
          <button onClick={runMRP} disabled={runningMRP} className="btn-primary btn-sm flex items-center">
            {runningMRP ? (
              <>
                <div className="animate-spin h-4 w-4 border-2 border-white border-t-transparent rounded-full mr-2" />
                Running...
              </>
            ) : (
              <>
                <PlayIcon className="h-4 w-4 mr-1.5" />
                Run MRP
              </>
            )}
          </button>
        </div>
      </div>

      {/* KPI strip */}
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-5 gap-2">
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={totalShortages > 0 ? 'bg-amber-500/20' : 'bg-fd-green/15'}
          iconColor={totalShortages > 0 ? 'text-amber-600' : 'text-fd-green'}
          label="Total Shortages"
          value={totalShortages}
          valueColor={totalShortages > 0 ? 'text-fd-amber' : undefined}
          subtitle={shortages ? `Run ${shortages.mrp_run_number}` : undefined}
        />
        <MiniStat
          icon={BoltIcon}
          iconBg={expediteCount > 0 ? 'bg-red-500/20' : 'bg-fd-green/15'}
          iconColor={expediteCount > 0 ? 'text-red-600' : 'text-fd-green'}
          label="Need Expedite"
          value={expediteCount}
          valueColor={expediteCount > 0 ? 'text-fd-red' : undefined}
        />
        <MiniStat
          icon={CubeIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-blue-600"
          label="Parts Analyzed"
          value={latestRun?.total_parts_analyzed ?? 0}
          subtitle={latestRun ? `Run ${latestRun.run_number}` : 'No runs yet'}
        />
        <MiniStat
          icon={ClipboardDocumentListIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-werco-navy-600"
          label="Requirements"
          value={latestRun?.total_requirements ?? 0}
        />
        <MiniStat
          icon={WrenchScrewdriverIcon}
          iconBg="bg-blue-500/20"
          iconColor="text-blue-600"
          label="Actions"
          value={latestRun?.total_actions ?? 0}
        />
      </MiniStatStrip>

      {/* Page-level load failure: surface an error + retry instead of a blank cockpit. */}
      {loadError && (
        <ErrorState message="Could not load MRP runs and shortages." onRetry={loadData} />
      )}

      {/* Cockpit grid: Shortages (wide) + Recent Runs (narrow), Run Details (wide) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 xl:grid-cols-12 gap-4 items-start">
        {/* Shortages */}
        {shortages && shortages.total_shortages > 0 && (
          <CockpitPanel
            title="Material Shortages"
            subtitle={`From run ${shortages.mrp_run_number}`}
            className="xl:col-span-7"
            footer={`${shortages.shortages.length} shortages`}
            headerExtra={
              <span
                className={`inline-flex items-center gap-1 text-xs font-medium ${
                  shortages.expedite_count > 0 ? 'text-fd-red' : 'text-fd-amber'
                }`}
              >
                <ExclamationTriangleIcon className="h-4 w-4" />
                {shortages.expedite_count > 0 ? `${shortages.expedite_count} expedite` : 'review'}
              </span>
            }
          >
            <div className="divide-y divide-fd-line">
              {shortages.shortages.map((shortage) => (
                <ActionRow
                  key={shortage.action_id}
                  data={{
                    actionId: shortage.action_id,
                    partId: shortage.part_id,
                    partNumber: shortage.part_number,
                    partName: shortage.part_name,
                    actionType: shortage.action_type,
                    quantity: shortage.quantity,
                    requiredDate: shortage.required_date,
                    orderByDate: shortage.order_by_date,
                    isProcessed: false,
                    isExpedite: shortage.is_expedite,
                  }}
                  onProcess={processAction}
                />
              ))}
            </div>
          </CockpitPanel>
        )}

        {/* Recent Runs */}
        <CockpitPanel
          title="Recent MRP Runs"
          className="xl:col-span-5"
          footer={runs.length ? `${runs.length} runs` : undefined}
        >
          <div className="space-y-1.5">
            {runs.map((run) => (
              <button
                key={run.id}
                aria-label={`View MRP run ${run.run_number}`}
                onClick={() => loadRunActions(run)}
                className={`w-full text-left p-2.5 rounded-sm border cursor-pointer transition-colors min-w-0 ${
                  selectedRun?.id === run.id
                    ? 'border-werco-primary bg-blue-500/10'
                    : 'border-fd-line hover:border-fd-line-bright'
                }`}
              >
                <div className="flex justify-between items-start gap-2">
                  <div className="min-w-0">
                    <div className="font-medium truncate">{run.run_number}</div>
                    <div className="text-xs text-slate-400 truncate">
                      {run.completed_at ? formatCentralDateTime(run.completed_at) : 'In progress...'}
                    </div>
                  </div>
                  <span
                    className={`flex-shrink-0 text-[10px] px-1.5 py-0.5 rounded-sm ${
                      run.status === 'complete'
                        ? 'bg-green-500/20 text-emerald-300'
                        : run.status === 'running'
                        ? 'bg-blue-500/20 text-blue-300'
                        : run.status === 'error'
                        ? 'bg-red-500/20 text-red-300'
                        : 'bg-slate-800/50 text-slate-100'
                    }`}
                  >
                    {run.status}
                  </span>
                </div>
                <div className="flex gap-3 mt-1.5 text-[11px] text-slate-400 tabular-nums">
                  <span>{run.total_parts_analyzed} parts</span>
                  <span>{run.total_requirements} reqs</span>
                  <span>{run.total_actions} actions</span>
                </div>
              </button>
            ))}
            {runs.length === 0 && (
              <EmptyState
                icon={BoltIcon}
                title="No MRP runs yet"
                description="Run MRP to analyze requirements and surface shortages."
                action={{ label: 'Run MRP', onClick: runMRP }}
              />
            )}
          </div>
        </CockpitPanel>

        {/* Run Details */}
        <CockpitPanel
          title={selectedRun ? `Actions — ${selectedRun.run_number}` : 'Run Details'}
          subtitle={selectedRun ? undefined : 'Select a run to view its actions'}
          className="xl:col-span-12"
          footer={selectedRun ? `${runActions.length} actions` : undefined}
        >
          {!selectedRun ? (
            <EmptyState
              icon={ClipboardDocumentListIcon}
              title="No run selected"
              description="Select a run from Recent MRP Runs to view its actions."
            />
          ) : actionsError ? (
            <ErrorState
              message="Could not load actions for this run."
              onRetry={() => loadRunActions(selectedRun)}
            />
          ) : (
            <div className="divide-y divide-fd-line">
              {runActions.map((action) => (
                <ActionRow
                  key={action.id}
                  data={{
                    actionId: action.id,
                    partId: action.part_id,
                    partNumber: action.part?.part_number ?? '',
                    partName: action.part?.name ?? '',
                    actionType: action.action_type,
                    quantity: action.quantity,
                    requiredDate: action.required_date,
                    orderByDate: action.suggested_order_date,
                    isProcessed: action.is_processed,
                  }}
                  onProcess={processAction}
                  highlight={shortageActionIds.has(action.id)}
                />
              ))}
              {runActions.length === 0 && (
                <EmptyState
                  icon={CheckCircleIcon}
                  title="No actions in this run"
                  description="This MRP run produced no planning actions."
                />
              )}
            </div>
          )}
        </CockpitPanel>
      </div>
    </div>
  );
}
