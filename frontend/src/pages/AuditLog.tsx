import React, { useEffect, useState } from 'react';
import api from '../services/api';
import { format } from 'date-fns';
import {
  MagnifyingGlassIcon,
  ShieldCheckIcon,
  UserIcon,
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

const actionColors: Record<string, string> = {
  CREATE: 'bg-green-100 text-green-800',
  UPDATE: 'bg-blue-100 text-blue-800',
  DELETE: 'bg-red-100 text-red-800',
  LOGIN: 'bg-purple-100 text-purple-800',
  LOGOUT: 'bg-gray-100 text-gray-800',
  VIEW: 'bg-yellow-100 text-yellow-800',
  EXPORT: 'bg-indigo-100 text-indigo-800',
};

export default function AuditLog() {
  const [logs, setLogs] = useState<AuditEntry[]>([]);
  const [summary, setSummary] = useState<AuditSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedLog, setSelectedLog] = useState<AuditEntry | null>(null);
  
  // Filters
  const [search, setSearch] = useState('');
  const [actionFilter, setActionFilter] = useState('');
  const [resourceFilter, setResourceFilter] = useState('');
  const [actions, setActions] = useState<string[]>([]);
  const [resourceTypes, setResourceTypes] = useState<string[]>([]);

  useEffect(() => {
    loadData();
    loadFilters();
  }, []);

  const loadData = async () => {
    try {
      const [logsRes, summaryRes] = await Promise.all([
        api.getAuditLogs({ limit: 100 }),
        api.getAuditSummary(30)
      ]);
      setLogs(logsRes);
      setSummary(summaryRes);
    } catch (err) {
      console.error('Failed to load audit logs:', err);
    } finally {
      setLoading(false);
    }
  };

  const loadFilters = async () => {
    try {
      const [actionsRes, typesRes] = await Promise.all([
        api.getAuditActions(),
        api.getAuditResourceTypes()
      ]);
      setActions(actionsRes);
      setResourceTypes(typesRes);
    } catch (err) {
      console.error('Failed to load filters:', err);
    }
  };

  const applyFilters = async () => {
    setLoading(true);
    try {
      const params: any = { limit: 200 };
      if (search) params.search = search;
      if (actionFilter) params.action = actionFilter;
      if (resourceFilter) params.resource_type = resourceFilter;
      
      const logsRes = await api.getAuditLogs(params);
      setLogs(logsRes);
    } catch (err) {
      console.error('Failed to filter:', err);
    } finally {
      setLoading(false);
    }
  };

  if (loading && logs.length === 0) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary"></div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <div className="flex items-center">
          <ShieldCheckIcon className="h-8 w-8 text-werco-primary mr-3" />
          <div>
            <h1 className="text-2xl font-bold text-gray-900">Audit Log</h1>
            <p className="text-sm text-gray-500">CMMC Level 2 Compliance Tracking</p>
          </div>
        </div>
      </div>

      {/* Summary Cards */}
      {summary && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <div className="card bg-blue-50 border-blue-200 text-center">
            <p className="text-sm text-blue-600">Total Events (30 days)</p>
            <p className="text-3xl font-bold text-blue-800">{summary.total_events}</p>
          </div>
          <div className={`card text-center ${summary.failed_events > 0 ? 'bg-red-50 border-red-200' : 'bg-green-50 border-green-200'}`}>
            <p className="text-sm text-gray-600">Failed Actions</p>
            <p className={`text-3xl font-bold ${summary.failed_events > 0 ? 'text-red-800' : 'text-green-800'}`}>
              {summary.failed_events}
            </p>
          </div>
          <div className="card bg-purple-50 border-purple-200 text-center">
            <p className="text-sm text-purple-600">Resource Types</p>
            <p className="text-3xl font-bold text-purple-800">{Object.keys(summary.by_resource).length}</p>
          </div>
          <div className="card bg-indigo-50 border-indigo-200 text-center">
            <p className="text-sm text-indigo-600">Active Users</p>
            <p className="text-3xl font-bold text-indigo-800">{summary.top_users.length}</p>
          </div>
        </div>
      )}

      {/* Filters */}
      <div className="card">
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex-1 min-w-[200px]">
            <label className="label">Search</label>
            <div className="relative">
              <MagnifyingGlassIcon className="h-5 w-5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
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

      {/* Log Table */}
      <div className="card overflow-hidden">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-gray-200">
            <thead className="bg-gray-50">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Timestamp</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">User</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Action</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Resource</th>
                <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Description</th>
                <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200">
              {logs.map((log) => (
                <tr 
                  key={log.id} 
                  className="hover:bg-gray-50 cursor-pointer"
                  onClick={() => setSelectedLog(log)}
                >
                  <td className="px-4 py-3 text-sm whitespace-nowrap">
                    {format(new Date(log.timestamp), 'MM/dd/yyyy HH:mm:ss')}
                  </td>
                  <td className="px-4 py-3 text-sm">
                    <div className="flex items-center">
                      <UserIcon className="h-4 w-4 text-gray-400 mr-2" />
                      {log.user_name || log.user_email || 'System'}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${actionColors[log.action] || 'bg-gray-100 text-gray-800'}`}>
                      {log.action}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-sm">
                    <div className="font-medium">{log.resource_type}</div>
                    {log.resource_identifier && (
                      <div className="text-xs text-gray-500 font-mono">{log.resource_identifier}</div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-600 max-w-xs truncate">
                    {log.description || '-'}
                  </td>
                  <td className="px-4 py-3 text-center">
                    {log.success === 'true' ? (
                      <span className="text-green-600">&#10003;</span>
                    ) : (
                      <span className="text-red-600">&#10007;</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {logs.length === 0 && (
          <p className="text-center text-gray-500 py-8">No audit logs found</p>
        )}
      </div>

      {/* Detail Modal */}
      {selectedLog && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto">
            <div className="flex justify-between items-start mb-4">
              <h3 className="text-lg font-semibold">Audit Log Detail</h3>
              <button onClick={() => setSelectedLog(null)} className="text-gray-500 hover:text-gray-700">
                &#10005;
              </button>
            </div>
            
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-sm text-gray-500">Timestamp</label>
                  <p className="font-medium">{format(new Date(selectedLog.timestamp), 'MM/dd/yyyy HH:mm:ss')}</p>
                </div>
                <div>
                  <label className="text-sm text-gray-500">User</label>
                  <p className="font-medium">{selectedLog.user_name || selectedLog.user_email || 'System'}</p>
                </div>
                <div>
                  <label className="text-sm text-gray-500">Action</label>
                  <p>
                    <span className={`px-2 py-1 rounded-full text-xs font-medium ${actionColors[selectedLog.action] || 'bg-gray-100'}`}>
                      {selectedLog.action}
                    </span>
                  </p>
                </div>
                <div>
                  <label className="text-sm text-gray-500">Resource</label>
                  <p className="font-medium">{selectedLog.resource_type}</p>
                  {selectedLog.resource_identifier && (
                    <p className="text-sm font-mono text-gray-600">{selectedLog.resource_identifier}</p>
                  )}
                </div>
              </div>
              
              {selectedLog.description && (
                <div>
                  <label className="text-sm text-gray-500">Description</label>
                  <p>{selectedLog.description}</p>
                </div>
              )}
              
              {selectedLog.ip_address && (
                <div>
                  <label className="text-sm text-gray-500">IP Address</label>
                  <p className="font-mono">{selectedLog.ip_address}</p>
                </div>
              )}
              
              {selectedLog.old_values && Object.keys(selectedLog.old_values).length > 0 && (
                <div>
                  <label className="text-sm text-gray-500">Previous Values</label>
                  <pre className="bg-gray-100 p-2 rounded text-sm overflow-x-auto">
                    {JSON.stringify(selectedLog.old_values, null, 2)}
                  </pre>
                </div>
              )}
              
              {selectedLog.new_values && Object.keys(selectedLog.new_values).length > 0 && (
                <div>
                  <label className="text-sm text-gray-500">New Values</label>
                  <pre className="bg-gray-100 p-2 rounded text-sm overflow-x-auto">
                    {JSON.stringify(selectedLog.new_values, null, 2)}
                  </pre>
                </div>
              )}
              
              {selectedLog.success === 'false' && selectedLog.error_message && (
                <div className="bg-red-50 border border-red-200 p-3 rounded">
                  <label className="text-sm text-red-600 font-medium">Error</label>
                  <p className="text-red-800">{selectedLog.error_message}</p>
                </div>
              )}
            </div>
            
            <div className="mt-6 text-right">
              <button onClick={() => setSelectedLog(null)} className="btn-secondary">
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
