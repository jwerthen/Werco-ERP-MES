import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ChartBarIcon,
  ShieldCheckIcon,
  ClipboardDocumentCheckIcon,
  ExclamationTriangleIcon,
  MagnifyingGlassIcon,
  PlusIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  XMarkIcon,
  CalendarDaysIcon,
} from '@heroicons/react/24/outline';
import {
  RadarChart, Radar, PolarGrid, PolarAngleAxis, PolarRadiusAxis,
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer,
} from 'recharts';

interface DashboardStats {
  total_rated: number;
  average_score: number;
  at_risk_count: number;
  audits_due_soon: number;
}

interface Scorecard {
  id: number;
  vendor_id: number;
  vendor_name: string;
  overall_score: number;
  quality_score: number;
  delivery_score: number;
  cost_score: number;
  responsiveness_score: number;
  period: string;
  status: string;
}

interface ApprovedSupplier {
  id: number;
  vendor_id: number;
  vendor_name: string;
  status: string;
  approved_date: string;
  commodity: string;
  notes: string;
}

interface Audit {
  id: number;
  vendor_id: number;
  vendor_name: string;
  audit_date: string;
  audit_type: string;
  score: number;
  findings: string;
  next_audit_due: string;
  status: string;
}

const TABS = ['Scorecards', 'Approved Supplier List', 'Audits'] as const;
type Tab = typeof TABS[number];

const scoreColor = (score: number) => {
  if (score >= 80) return 'bg-green-500';
  if (score >= 60) return 'bg-yellow-500';
  return 'bg-red-500';
};

const scoreBadge = (score: number) => {
  if (score >= 80) return 'text-green-700 bg-green-100';
  if (score >= 60) return 'text-yellow-700 bg-yellow-100';
  return 'text-red-700 bg-red-100';
};

const statusBadge = (status: string) => {
  const s = status?.toLowerCase();
  if (s === 'approved' || s === 'active' || s === 'completed') return 'bg-green-100 text-green-800';
  if (s === 'pending' || s === 'scheduled') return 'bg-yellow-100 text-yellow-800';
  if (s === 'suspended' || s === 'at_risk') return 'bg-red-100 text-red-800';
  return 'bg-gray-100 text-gray-800';
};

const SupplierScorecards = () => {
  const [activeTab, setActiveTab] = useState<Tab>('Scorecards');
  const [search, setSearch] = useState('');
  const [stats, setStats] = useState<DashboardStats>({ total_rated: 0, average_score: 0, at_risk_count: 0, audits_due_soon: 0 });
  const [scorecards, setScorecards] = useState<Scorecard[]>([]);
  const [approvedSuppliers, setApprovedSuppliers] = useState<ApprovedSupplier[]>([]);
  const [audits, setAudits] = useState<Audit[]>([]);
  const [expandedRow, setExpandedRow] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [showScorecardModal, setShowScorecardModal] = useState(false);
  const [showAuditModal, setShowAuditModal] = useState(false);

  const [scorecardForm, setScorecardForm] = useState({
    vendor_id: '', period: '', quality_score: '', delivery_score: '', cost_score: '', responsiveness_score: '',
  });
  const [auditForm, setAuditForm] = useState({
    vendor_id: '', audit_date: '', audit_type: 'on-site', score: '', findings: '', next_audit_due: '',
  });

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const [dashRes, scRes, aslRes, audRes] = await Promise.all([
        api.getSupplierScorecardsDashboard(),
        api.getSupplierScorecards({ search }),
        api.getApprovedSuppliers({ search }),
        api.getSupplierAudits({ search }),
      ]);
      setStats(dashRes.data ?? dashRes);
      setScorecards(scRes.data?.results ?? scRes.data ?? scRes);
      setApprovedSuppliers(aslRes.data?.results ?? aslRes.data ?? aslRes);
      setAudits(audRes.data?.results ?? audRes.data ?? audRes);
    } catch (e) {
      console.error('Failed to fetch supplier scorecard data', e);
    } finally {
      setLoading(false);
    }
  }, [search]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const filteredScorecards = useMemo(() =>
    scorecards.filter(s => s.vendor_name?.toLowerCase().includes(search.toLowerCase())),
    [scorecards, search]
  );

  const filteredASL = useMemo(() =>
    approvedSuppliers.filter(s => s.vendor_name?.toLowerCase().includes(search.toLowerCase())),
    [approvedSuppliers, search]
  );

  const filteredAudits = useMemo(() =>
    audits.filter(a => a.vendor_name?.toLowerCase().includes(search.toLowerCase())),
    [audits, search]
  );

  const handleCreateScorecard = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createSupplierScorecard({
        vendor_id: Number(scorecardForm.vendor_id),
        period: scorecardForm.period,
        quality_score: Number(scorecardForm.quality_score),
        delivery_score: Number(scorecardForm.delivery_score),
        cost_score: Number(scorecardForm.cost_score),
        responsiveness_score: Number(scorecardForm.responsiveness_score),
      });
      setShowScorecardModal(false);
      setScorecardForm({ vendor_id: '', period: '', quality_score: '', delivery_score: '', cost_score: '', responsiveness_score: '' });
      fetchData();
    } catch (e) {
      console.error('Failed to create scorecard', e);
    }
  }, [scorecardForm, fetchData]);

  const handleCreateAudit = useCallback(async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createSupplierAudit({
        vendor_id: Number(auditForm.vendor_id),
        audit_date: auditForm.audit_date,
        audit_type: auditForm.audit_type,
        score: Number(auditForm.score),
        findings: auditForm.findings,
        next_audit_due: auditForm.next_audit_due,
      });
      setShowAuditModal(false);
      setAuditForm({ vendor_id: '', audit_date: '', audit_type: 'on-site', score: '', findings: '', next_audit_due: '' });
      fetchData();
    } catch (e) {
      console.error('Failed to create audit', e);
    }
  }, [auditForm, fetchData]);

  const radarData = useCallback((sc: Scorecard) => [
    { dimension: 'Quality', score: sc.quality_score },
    { dimension: 'Delivery', score: sc.delivery_score },
    { dimension: 'Cost', score: sc.cost_score },
    { dimension: 'Responsiveness', score: sc.responsiveness_score },
  ], []);

  const toggleExpand = useCallback((id: number) => {
    setExpandedRow(prev => prev === id ? null : id);
  }, []);

  const statCards = useMemo(() => [
    { label: 'Suppliers Rated', value: stats.total_rated, icon: ChartBarIcon, color: 'text-blue-600 bg-blue-100' },
    { label: 'Average Score', value: stats.average_score?.toFixed(1), icon: ShieldCheckIcon, color: 'text-green-600 bg-green-100' },
    { label: 'At-Risk Suppliers', value: stats.at_risk_count, icon: ExclamationTriangleIcon, color: 'text-red-600 bg-red-100' },
    { label: 'Audits Due Soon', value: stats.audits_due_soon, icon: CalendarDaysIcon, color: 'text-amber-600 bg-amber-100' },
  ], [stats]);

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Supplier Scorecards</h1>
        <div className="flex gap-2">
          {activeTab === 'Scorecards' && (
            <button onClick={() => setShowScorecardModal(true)} className="inline-flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
              <PlusIcon className="w-4 h-4" /> New Scorecard
            </button>
          )}
          {activeTab === 'Audits' && (
            <button onClick={() => setShowAuditModal(true)} className="inline-flex items-center gap-1.5 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm font-medium">
              <PlusIcon className="w-4 h-4" /> New Audit
            </button>
          )}
        </div>
      </div>

      {/* Stat Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {statCards.map(card => (
          <div key={card.label} className="bg-white rounded-xl shadow-sm border border-gray-200 p-5 flex items-center gap-4">
            <div className={`p-3 rounded-lg ${card.color}`}>
              <card.icon className="w-6 h-6" />
            </div>
            <div>
              <p className="text-sm text-gray-500">{card.label}</p>
              <p className="text-2xl font-bold text-gray-900">{card.value}</p>
            </div>
          </div>
        ))}
      </div>

      {/* Search & Tabs */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="relative flex-1 max-w-md">
          <MagnifyingGlassIcon className="w-5 h-5 text-gray-400 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            placeholder="Search by vendor name..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
          />
        </div>
        <div className="flex border border-gray-300 rounded-lg overflow-hidden">
          {TABS.map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-sm font-medium ${activeTab === tab ? 'bg-blue-600 text-white' : 'bg-white text-gray-700 hover:bg-gray-50'}`}
            >
              {tab}
            </button>
          ))}
        </div>
      </div>

      {loading ? (
        <div className="flex justify-center py-12">
          <div className="animate-spin rounded-full h-10 w-10 border-b-2 border-blue-600" />
        </div>
      ) : (
        <>
          {/* Scorecards Tab */}
          {activeTab === 'Scorecards' && (
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="w-10 px-4 py-3" />
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Overall Score</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Quality</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Delivery</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Cost</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Period</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200">
                  {filteredScorecards.length === 0 ? (
                    <tr><td colSpan={8} className="px-4 py-8 text-center text-gray-500">No scorecards found.</td></tr>
                  ) : filteredScorecards.map(sc => (
                    <React.Fragment key={sc.id}>
                      <tr className="hover:bg-gray-50 cursor-pointer" onClick={() => toggleExpand(sc.id)}>
                        <td className="px-4 py-3">
                          {expandedRow === sc.id
                            ? <ChevronUpIcon className="w-4 h-4 text-gray-500" />
                            : <ChevronDownIcon className="w-4 h-4 text-gray-500" />}
                        </td>
                        <td className="px-4 py-3 text-sm font-medium text-gray-900">{sc.vendor_name}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
                              <div className={`h-full rounded-full ${scoreColor(sc.overall_score)}`} style={{ width: `${sc.overall_score}%` }} />
                            </div>
                            <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${scoreBadge(sc.overall_score)}`}>
                              {sc.overall_score}
                            </span>
                          </div>
                        </td>
                        <td className="px-4 py-3 text-center text-sm">{sc.quality_score}</td>
                        <td className="px-4 py-3 text-center text-sm">{sc.delivery_score}</td>
                        <td className="px-4 py-3 text-center text-sm">{sc.cost_score}</td>
                        <td className="px-4 py-3 text-sm text-gray-600">{sc.period}</td>
                        <td className="px-4 py-3">
                          <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${statusBadge(sc.status)}`}>
                            {sc.status}
                          </span>
                        </td>
                      </tr>
                      {expandedRow === sc.id && (
                        <tr>
                          <td colSpan={8} className="px-6 py-4 bg-gray-50">
                            <div className="flex flex-col lg:flex-row gap-6">
                              <div className="w-full lg:w-1/2 h-64">
                                <h4 className="text-sm font-semibold text-gray-700 mb-2">Score Breakdown</h4>
                                <ResponsiveContainer width="100%" height="100%">
                                  <RadarChart data={radarData(sc)}>
                                    <PolarGrid />
                                    <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 12 }} />
                                    <PolarRadiusAxis angle={90} domain={[0, 100]} tick={{ fontSize: 10 }} />
                                    <Radar name="Score" dataKey="score" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.3} />
                                  </RadarChart>
                                </ResponsiveContainer>
                              </div>
                              <div className="w-full lg:w-1/2 h-64">
                                <h4 className="text-sm font-semibold text-gray-700 mb-2">Dimension Comparison</h4>
                                <ResponsiveContainer width="100%" height="100%">
                                  <BarChart data={radarData(sc)}>
                                    <CartesianGrid strokeDasharray="3 3" />
                                    <XAxis dataKey="dimension" tick={{ fontSize: 12 }} />
                                    <YAxis domain={[0, 100]} tick={{ fontSize: 10 }} />
                                    <Tooltip />
                                    <Legend />
                                    <Bar dataKey="score" fill="#3b82f6" radius={[4, 4, 0, 0]} />
                                  </BarChart>
                                </ResponsiveContainer>
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Approved Supplier List Tab */}
          {activeTab === 'Approved Supplier List' && (
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Approved Date</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Commodity</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Notes</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200">
                  {filteredASL.length === 0 ? (
                    <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-500">No approved suppliers found.</td></tr>
                  ) : filteredASL.map(s => (
                    <tr key={s.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 text-sm font-medium text-gray-900">{s.vendor_name}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${statusBadge(s.status)}`}>
                          {s.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-600">{s.approved_date}</td>
                      <td className="px-4 py-3 text-sm text-gray-600">{s.commodity}</td>
                      <td className="px-4 py-3 text-sm text-gray-500 max-w-xs truncate">{s.notes}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Audits Tab */}
          {activeTab === 'Audits' && (
            <div className="bg-white rounded-xl shadow-sm border border-gray-200 overflow-hidden">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Vendor</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Audit Date</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Score</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Findings</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Next Audit Due</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-200">
                  {filteredAudits.length === 0 ? (
                    <tr><td colSpan={7} className="px-4 py-8 text-center text-gray-500">No audits found.</td></tr>
                  ) : filteredAudits.map(a => (
                    <tr key={a.id} className="hover:bg-gray-50">
                      <td className="px-4 py-3 text-sm font-medium text-gray-900">{a.vendor_name}</td>
                      <td className="px-4 py-3 text-sm text-gray-600">{a.audit_date}</td>
                      <td className="px-4 py-3 text-sm text-gray-600 capitalize">{a.audit_type}</td>
                      <td className="px-4 py-3 text-center">
                        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${scoreBadge(a.score)}`}>
                          {a.score}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-500 max-w-xs truncate">{a.findings}</td>
                      <td className="px-4 py-3 text-sm text-gray-600">{a.next_audit_due}</td>
                      <td className="px-4 py-3">
                        <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${statusBadge(a.status)}`}>
                          {a.status}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}

      {/* Create Scorecard Modal */}
      {showScorecardModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
              <h3 className="text-lg font-semibold text-gray-900">New Supplier Scorecard</h3>
              <button onClick={() => setShowScorecardModal(false)} className="text-gray-400 hover:text-gray-600">
                <XMarkIcon className="w-5 h-5" />
              </button>
            </div>
            <form onSubmit={handleCreateScorecard} className="p-6 space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Vendor ID</label>
                <input
                  type="number" required
                  value={scorecardForm.vendor_id}
                  onChange={e => setScorecardForm(f => ({ ...f, vendor_id: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Period</label>
                <input
                  type="text" required placeholder="e.g., 2026-Q1"
                  value={scorecardForm.period}
                  onChange={e => setScorecardForm(f => ({ ...f, period: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                {(['quality_score', 'delivery_score', 'cost_score', 'responsiveness_score'] as const).map(field => (
                  <div key={field}>
                    <label className="block text-sm font-medium text-gray-700 mb-1 capitalize">
                      {field.replace('_score', '').replace('_', ' ')}
                    </label>
                    <input
                      type="number" min="0" max="100" required
                      value={scorecardForm[field]}
                      onChange={e => setScorecardForm(f => ({ ...f, [field]: e.target.value }))}
                      className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                    />
                  </div>
                ))}
              </div>
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowScorecardModal(false)} className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50">
                  Cancel
                </button>
                <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                  Create Scorecard
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Create Audit Modal */}
      {showAuditModal && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black bg-opacity-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4">
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
              <h3 className="text-lg font-semibold text-gray-900">New Supplier Audit</h3>
              <button onClick={() => setShowAuditModal(false)} className="text-gray-400 hover:text-gray-600">
                <XMarkIcon className="w-5 h-5" />
              </button>
            </div>
            <form onSubmit={handleCreateAudit} className="p-6 space-y-4">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Vendor ID</label>
                <input
                  type="number" required
                  value={auditForm.vendor_id}
                  onChange={e => setAuditForm(f => ({ ...f, vendor_id: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Audit Date</label>
                  <input
                    type="date" required
                    value={auditForm.audit_date}
                    onChange={e => setAuditForm(f => ({ ...f, audit_date: e.target.value }))}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Audit Type</label>
                  <select
                    value={auditForm.audit_type}
                    onChange={e => setAuditForm(f => ({ ...f, audit_type: e.target.value }))}
                    className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  >
                    <option value="on-site">On-Site</option>
                    <option value="remote">Remote</option>
                    <option value="document">Document Review</option>
                    <option value="follow-up">Follow-Up</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Score (0-100)</label>
                <input
                  type="number" min="0" max="100" required
                  value={auditForm.score}
                  onChange={e => setAuditForm(f => ({ ...f, score: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Findings</label>
                <textarea
                  rows={3}
                  value={auditForm.findings}
                  onChange={e => setAuditForm(f => ({ ...f, findings: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                />
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Next Audit Due</label>
                <input
                  type="date"
                  value={auditForm.next_audit_due}
                  onChange={e => setAuditForm(f => ({ ...f, next_audit_due: e.target.value }))}
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                />
              </div>
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowAuditModal(false)} className="px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300 rounded-lg hover:bg-gray-50">
                  Cancel
                </button>
                <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                  Create Audit
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
};

export default SupplierScorecards;
