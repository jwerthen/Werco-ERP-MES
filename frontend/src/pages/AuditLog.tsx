import React, { useCallback, useEffect, useMemo, useState } from 'react';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import { DataTable, DataTableColumn } from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { formatCentralDateTime } from '../utils/centralTime';
import {
  MagnifyingGlassIcon,
  ShieldCheckIcon,
  UserIcon,
  UsersIcon,
  ExclamationTriangleIcon,
  RectangleStackIcon,
} from '@heroicons/react/24/outline';

interface AuditEntry {
  id: number;
  timestamp: string;
  user_id?: number;
  user_email?: string;
  user_name?: string;
  action: string;
  resource_type: string;
  resource_id?: number;
  resource_identifier?: string;
  description?: string;
  old_values?: Record<string, any>;
  new_values?: Record<string, any>;
  ip_address?: string;
  success: string;
  error_message?: string;
}

interface AuditSummary {
  period_days: number;
  total_events: number;
  failed_events: number;
  by_action: Record<string, number>;
  by_resource: Record<string, number>;
  top_users: Array<{ name: string; count: number }>;
}

const PAGE_SIZE = 50;

const formatTimestamp = (ts: string) =>
  formatCentralDateTime(ts, {
    month: '2-digit',
    day: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });

const actionColors: Record<string, string> = {
  CREATE: 'bg-fd-green/20 text-emerald-300',
  UPDATE: 'bg-fd-blue/20 text-blue-300',
  DELETE: 'bg-fd-red/20 text-red-300',
  LOGIN: 'bg-werco-navy-600/30 text-blue-200',
  LOGOUT: 'bg-slate-800/50 text-slate-100',
  VIEW: 'bg-fd-amber/20 text-amber-300',
  EXPORT: 'bg-werco-navy-500/20 text-blue-300',
};

export default function AuditLog() {
  const [logs, setLogs] = useState<AuditEntry[]>([]);
  const [summary, setSummary] = useState<AuditSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [selectedLog, setSelectedLog] = useState<AuditEntry | null>(null);

  // Server pagination — the audit endpoint is offset/limit paged and ordered
  // desc(timestamp). `page` is 0-based here; older rows are reached via Next.
  // The list endpoint returns no total count, so we over-fetch one extra row
  // to detect whether a next page exists. This is the compliance-sensitive
  // change: it makes older, previously-unreachable audit rows navigable.
  const [page, setPage] = useState(0);
  const [hasNext, setHasNext] = useState(false);

  // Filters
  const [search, setSearch] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [resourceFilter, setResourceFilter] = useState('');
  const [actions, setActions] = useState<string[]>([]);
  const [resourceTypes, setResourceTypes] = useState<string[]>([]);

  // The filters that are actually applied to the current query. Editing the
  // inputs above does not refetch until "Apply Filters" is pressed.
  const [appliedFilters, setAppliedFilters] = useState<{
    search: string;
    action: string;
    resource_type: string;
  }>({ search: '', action: '', resource_type: '' });

  const loadLogs = useCallback(
    async (targetPage: number, filters: { search: string; action: string; resource_type: string }) => {
      setLoading(true);
      setLoadError(false);
      try {
        const params: {
          limit: number;
          offset: number;
          search?: string;
          action?: string;
          resource_type?: string;
        } = {
          // Over-fetch one row to infer whether a next page exists, without
          // reordering or hiding rows beyond the page boundary.
          limit: PAGE_SIZE + 1,
          offset: targetPage * PAGE_SIZE,
        };
        if (filters.search) params.search = filters.search;
        if (filters.action) params.action = filters.action;
        if (filters.resource_type) params.resource_type = filters.resource_type;

        const logsRes: AuditEntry[] = await api.getAuditLogs(params);
        setHasNext(logsRes.length > PAGE_SIZE);
        setLogs(logsRes.slice(0, PAGE_SIZE));
      } catch (err) {
        console.error('Failed to load audit logs:', err);
        setLoadError(true);
      } finally {
        setLoading(false);
      }
    },
    []
  );

  const loadSummary = useCallback(async () => {
    try {
      const summaryRes = await api.getAuditSummary(30);
      setSummary(summaryRes);
    } catch (err) {
      console.error('Failed to load audit summary:', err);
    }
  }, []);

  const loadFilters = useCallback(async () => {
    try {
      const [actionsRes, typesRes] = await Promise.all([
        api.getAuditActions(),
        api.getAuditResourceTypes(),
      ]);
      setActions(actionsRes);
      setResourceTypes(typesRes);
    } catch (err) {
      console.error('Failed to load filters:', err);
    }
  }, []);

  useEffect(() => {
    // Mount-only initial load with no filters. Subsequent loads are driven
    // explicitly by applyFilters / page changes, so appliedFilters is
    // intentionally not a dependency (it would trigger a redundant refetch).
    loadLogs(0, { search: '', action: '', resource_type: '' });
    loadSummary();
    loadFilters();
  }, [loadLogs, loadSummary, loadFilters]);

  const applyFilters = () => {
    const next = { search, action: actionFilter, resource_type: resourceFilter };
    setAppliedFilters(next);
    // Changing a filter resets to the first (newest) page.
    setPage(0);
    loadLogs(0, next);
  };

  const handlePageChange = (nextPage1Based: number) => {
    const target = nextPage1Based - 1; // DataTable is 1-based; offset math is 0-based.
    setPage(target);
    loadLogs(target, appliedFilters);
  };

  const retry = () => loadLogs(page, appliedFilters);

  const columns = useMemo<Array<DataTableColumn<AuditEntry>>>(
    () => [
      {
        key: 'timestamp',
        header: 'Timestamp',
        className: 'whitespace-nowrap',
        accessor: (log) => log.timestamp,
        render: (log) => formatTimestamp(log.timestamp),
        csv: (log) => formatTimestamp(log.timestamp),
      },
      {
        key: 'user',
        header: 'User',
        accessor: (log) => log.user_name || log.user_email || 'System',
        render: (log) => (
          <div className="flex items-center">
            <UserIcon className="h-4 w-4 text-slate-400 mr-2" />
            {log.user_name || log.user_email || 'System'}
          </div>
        ),
      },
      {
        key: 'action',
        header: 'Action',
        accessor: (log) => log.action,
        render: (log) => (
          <span
            className={`px-2 py-1 rounded-full text-xs font-medium ${
              actionColors[log.action] || 'bg-slate-800/50 text-slate-100'
            }`}
          >
            {log.action}
          </span>
        ),
      },
      {
        key: 'resource',
        header: 'Resource',
        accessor: (log) =>
          log.resource_identifier
            ? `${log.resource_type} ${log.resource_identifier}`
            : log.resource_type,
        render: (log) => (
          <>
            <div className="font-medium">{log.resource_type}</div>
            {log.resource_identifier && (
              <div className="text-xs text-slate-400 font-mono">{log.resource_identifier}</div>
            )}
          </>
        ),
      },
      {
        key: 'description',
        header: 'Description',
        className: 'text-slate-400 max-w-xs truncate',
        accessor: (log) => log.description || '',
        render: (log) => log.description || '-',
      },
      {
        key: 'status',
        header: 'Status',
        align: 'center',
        accessor: (log) => (log.success === 'true' ? 'Success' : 'Failed'),
        render: (log) =>
          log.success === 'true' ? (
            <span className="text-green-600">&#10003;</span>
          ) : (
            <span className="text-red-600">&#10007;</span>
          ),
      },
    ],
    []
  );

  return (
    <div className="space-y-4">
      <div className="flex justify-between items-center">
        <div className="flex items-center">
          <ShieldCheckIcon className="h-8 w-8 text-werco-primary mr-3" />
          <div>
            <h1 className="text-2xl font-bold text-white">Audit Log</h1>
            <p className="text-sm text-slate-400">CMMC Level 2 Compliance Tracking</p>
          </div>
        </div>
      </div>

      {/* Summary Cards */}
      {summary && (
        <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
          <MiniStat
            icon={ShieldCheckIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Total Events (30 days)"
            value={summary.total_events}
          />
          <MiniStat
            icon={ExclamationTriangleIcon}
            iconBg={summary.failed_events > 0 ? 'bg-fd-red/15' : 'bg-fd-green/15'}
            iconColor={summary.failed_events > 0 ? 'text-fd-red' : 'text-fd-green'}
            label="Failed Actions"
            value={summary.failed_events}
            valueColor={summary.failed_events > 0 ? 'text-fd-red' : 'text-fd-green'}
          />
          <MiniStat
            icon={RectangleStackIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Resource Types"
            value={Object.keys(summary.by_resource).length}
          />
          <MiniStat
            icon={UsersIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Active Users"
            value={summary.top_users.length}
          />
        </MiniStatStrip>
      )}

      {/* Filters */}
      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex flex-wrap gap-3 items-end">
          <div className="flex-1 min-w-[200px]">
            <label className="label">Search</label>
            <div className="relative">
              <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && applyFilters()}
                className="input pl-10"
                placeholder="Search description, user..."
              />
            </div>
          </div>
          <div>
            <label className="label">Action</label>
            <select
              value={actionFilter}
              onChange={(e) => setActionFilter(e.target.value)}
              className="input"
            >
              <option value="">All Actions</option>
              {actions.map(a => (
                <option key={a} value={a}>{a}</option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Resource Type</label>
            <select
              value={resourceFilter}
              onChange={(e) => setResourceFilter(e.target.value)}
              className="input"
            >
              <option value="">All Types</option>
              {resourceTypes.map(t => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </div>
          <button onClick={applyFilters} className="btn-primary">
            Apply Filters
          </button>
        </div>
      </div>

      {/* Log Table — server-paged (offset/limit), desc(timestamp). Older rows
          are reachable via Prev/Next; rows are never reordered/hidden client-side. */}
      <DataTable<AuditEntry>
        columns={columns}
        data={logs}
        rowKey={(log) => log.id}
        onRowClick={(log) => setSelectedLog(log)}
        loading={loading}
        error={loadError ? 'Could not load audit logs.' : false}
        onRetry={retry}
        serverPagination={{
          page: page + 1,
          pageSize: PAGE_SIZE,
          hasNext,
          onPageChange: handlePageChange,
        }}
        csvExport={{ filename: 'audit-log' }}
        empty={{
          icon: ShieldCheckIcon,
          title: 'No audit logs found',
          description:
            'Audit events will appear here as users create, update, and delete records. Try adjusting your filters.',
        }}
      />

      {/* Detail Modal */}
      <Modal
        open={!!selectedLog}
        onClose={() => setSelectedLog(null)}
        size="2xl"
        closeOnBackdrop={false}
      >
        {selectedLog && (
          <>
            <div className="flex justify-between items-start mb-4">
              <h3 className="text-lg font-semibold">Audit Log Detail</h3>
              <button onClick={() => setSelectedLog(null)} className="text-slate-400 hover:text-slate-300">
                &#10005;
              </button>
            </div>
            
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-sm text-slate-400">Timestamp</label>
                  <p className="font-medium">{formatTimestamp(selectedLog.timestamp)}</p>
                </div>
                <div>
                  <label className="text-sm text-slate-400">User</label>
                  <p className="font-medium">{selectedLog.user_name || selectedLog.user_email || 'System'}</p>
                </div>
                <div>
                  <label className="text-sm text-slate-400">Action</label>
                  <p>
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${actionColors[selectedLog.action] || 'bg-slate-800/50'}`}>
                      {selectedLog.action}
                    </span>
                  </p>
                </div>
                <div>
                  <label className="text-sm text-slate-400">Resource</label>
                  <p className="font-medium">{selectedLog.resource_type}</p>
                  {selectedLog.resource_identifier && (
                    <p className="text-sm font-mono text-slate-400">{selectedLog.resource_identifier}</p>
                  )}
                </div>
              </div>
              
              {selectedLog.description && (
                <div>
                  <label className="text-sm text-slate-400">Description</label>
                  <p>{selectedLog.description}</p>
                </div>
              )}
              
              {selectedLog.ip_address && (
                <div>
                  <label className="text-sm text-slate-400">IP Address</label>
                  <p className="font-mono">{selectedLog.ip_address}</p>
                </div>
              )}
              
              {selectedLog.old_values && Object.keys(selectedLog.old_values).length > 0 && (
                <div>
                  <label className="text-sm text-slate-400">Previous Values</label>
                  <pre className="bg-slate-800/50 p-2 rounded text-sm overflow-x-auto">
                    {JSON.stringify(selectedLog.old_values, null, 2)}
                  </pre>
                </div>
              )}
              
              {selectedLog.new_values && Object.keys(selectedLog.new_values).length > 0 && (
                <div>
                  <label className="text-sm text-slate-400">New Values</label>
                  <pre className="bg-slate-800/50 p-2 rounded text-sm overflow-x-auto">
                    {JSON.stringify(selectedLog.new_values, null, 2)}
                  </pre>
                </div>
              )}
              
              {selectedLog.success === 'false' && selectedLog.error_message && (
                <div className="bg-red-500/10 border border-red-500/30 p-3 rounded">
                  <label className="text-sm text-red-600 font-medium">Error</label>
                  <p className="text-red-300">{selectedLog.error_message}</p>
                </div>
              )}
            </div>
            
            <div className="mt-6 text-right">
              <button onClick={() => setSelectedLog(null)} className="btn-secondary">
                Close
              </button>
            </div>
          </>
        )}
      </Modal>
    </div>
  );
}
