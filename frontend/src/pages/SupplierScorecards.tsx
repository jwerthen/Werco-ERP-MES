import React, { useState, useEffect, useMemo, useCallback } from 'react';
import api from '../services/api';
import {
  ChartBarIcon,
  ShieldCheckIcon,
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
import { Modal } from '../components/ui/Modal';
import { FormField } from '../components/ui/FormField';
import {
  useToast,
  DataTable,
  DataTableColumn,
  MobileDataCard,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';

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
  if (score >= 80) return 'bg-green-500/100';
  if (score >= 60) return 'bg-yellow-500/100';
  return 'bg-red-500/100';
};

const scoreBadge = (score: number) => {
  if (score >= 80) return 'text-emerald-400 bg-green-500/20';
  if (score >= 60) return 'text-yellow-400 bg-yellow-500/20';
  return 'text-red-400 bg-red-500/20';
};

const statusBadge = (status: string) => {
  const s = status?.toLowerCase();
  if (s === 'approved' || s === 'active' || s === 'completed') return 'bg-green-500/20 text-emerald-300';
  if (s === 'pending' || s === 'scheduled') return 'bg-yellow-500/20 text-yellow-300';
  if (s === 'suspended' || s === 'at_risk') return 'bg-red-500/20 text-red-300';
  return 'bg-slate-800/50 text-slate-100';
};

const StatusPill = ({ status }: { status: string }) => (
  <span className={`inline-flex px-2 py-0.5 text-xs font-medium rounded-full ${statusBadge(status)}`}>
    {status}
  </span>
);

const ScorePill = ({ score }: { score: number }) => (
  <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${scoreBadge(score)}`}>
    {score}
  </span>
);

const ScoreBar = ({ score }: { score: number }) => (
  <div className="flex items-center gap-2 min-w-[7rem]">
    <div className="flex-1 h-2 bg-gray-200 rounded-full overflow-hidden">
      <div className={`h-full rounded-full ${scoreColor(score)}`} style={{ width: `${score}%` }} />
    </div>
    <ScorePill score={score} />
  </div>
);

const radarData = (sc: Scorecard) => [
  { dimension: 'Quality', score: sc.quality_score },
  { dimension: 'Delivery', score: sc.delivery_score },
  { dimension: 'Cost', score: sc.cost_score },
  { dimension: 'Responsiveness', score: sc.responsiveness_score },
];

const ScorecardCharts = ({ sc }: { sc: Scorecard }) => (
  <div className="flex flex-col lg:flex-row gap-6">
    <div className="w-full lg:w-1/2 h-64">
      <h4 className="text-sm font-semibold text-slate-300 mb-2">Score Breakdown</h4>
      <ResponsiveContainer width="100%" height="100%">
        <RadarChart data={radarData(sc)}>
          <PolarGrid />
          <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 12, fill: '#94a3b8' }} />
          <PolarRadiusAxis angle={90} domain={[0, 100]} tick={{ fontSize: 10, fill: '#94a3b8' }} />
          <Radar name="Score" dataKey="score" stroke="#3b82f6" fill="#3b82f6" fillOpacity={0.3} />
        </RadarChart>
      </ResponsiveContainer>
    </div>
    <div className="w-full lg:w-1/2 h-64">
      <h4 className="text-sm font-semibold text-slate-300 mb-2">Dimension Comparison</h4>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={radarData(sc)}>
          <CartesianGrid strokeDasharray="3 3" stroke="#334155" />
          <XAxis dataKey="dimension" tick={{ fontSize: 12, fill: '#94a3b8' }} />
          <YAxis domain={[0, 100]} tick={{ fontSize: 10, fill: '#94a3b8' }} />
          <Tooltip contentStyle={{ backgroundColor: '#1a1f2e', border: '1px solid #334155', borderRadius: '12px', color: '#e2e8f0' }} />
          <Legend />
          <Bar dataKey="score" fill="#3b82f6" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  </div>
);

const SupplierScorecards = () => {
  const { showToast } = useToast();
  const [activeTab, setActiveTab] = useState<Tab>('Scorecards');
  const [search, setSearch] = useState('');
  const [stats, setStats] = useState<DashboardStats>({ total_rated: 0, average_score: 0, at_risk_count: 0, audits_due_soon: 0 });
  const [scorecards, setScorecards] = useState<Scorecard[]>([]);
  const [approvedSuppliers, setApprovedSuppliers] = useState<ApprovedSupplier[]>([]);
  const [audits, setAudits] = useState<Audit[]>([]);
  const [expandedRow, setExpandedRow] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
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
    setLoadError(false);
    try {
      const [dashRes, scRes, aslRes, audRes] = await Promise.all([
        api.getSupplierScorecardsDashboard(),
        api.getSupplierScorecards({}),
        api.getApprovedSuppliers({}),
        api.getSupplierAudits({}),
      ]);
      setStats(dashRes.data ?? dashRes);
      setScorecards(scRes.data?.results ?? scRes.data ?? scRes);
      setApprovedSuppliers(aslRes.data?.results ?? aslRes.data ?? aslRes);
      setAudits(audRes.data?.results ?? audRes.data ?? audRes);
    } catch (e) {
      console.error('Failed to fetch supplier scorecard data', e);
      setLoadError(true);
    } finally {
      setLoading(false);
    }

  }, []);

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
      showToast('success', 'Scorecard created');
      fetchData();
    } catch (e) {
      console.error('Failed to create scorecard', e);
      showToast('error', 'Failed to create scorecard');
    }
  }, [scorecardForm, fetchData, showToast]);

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
      showToast('success', 'Audit created');
      fetchData();
    } catch (e) {
      console.error('Failed to create audit', e);
      showToast('error', 'Failed to create audit');
    }
  }, [auditForm, fetchData, showToast]);

  const toggleExpand = useCallback((id: number) => {
    setExpandedRow(prev => prev === id ? null : id);
  }, []);

  const statCards = useMemo(() => [
    { label: 'Suppliers Rated', value: stats.total_rated, icon: ChartBarIcon, iconBg: 'bg-fd-blue/15', iconColor: 'text-fd-blue' },
    { label: 'Average Score', value: stats.average_score?.toFixed(1), icon: ShieldCheckIcon, iconBg: 'bg-fd-green/15', iconColor: 'text-fd-green' },
    { label: 'At-Risk Suppliers', value: stats.at_risk_count, icon: ExclamationTriangleIcon, iconBg: 'bg-fd-red/15', iconColor: 'text-fd-red', valueColor: stats.at_risk_count > 0 ? 'text-fd-red' : undefined },
    { label: 'Audits Due Soon', value: stats.audits_due_soon, icon: CalendarDaysIcon, iconBg: 'bg-fd-amber/15', iconColor: 'text-fd-amber', valueColor: stats.audits_due_soon > 0 ? 'text-fd-amber' : undefined },
  ], [stats]);

  // ---- Scorecards table columns ----
  const scorecardColumns = useMemo<Array<DataTableColumn<Scorecard>>>(() => [
    {
      key: 'expand',
      header: '',
      headerClassName: 'w-10',
      className: 'w-10',
      render: (sc) => (
        expandedRow === sc.id
          ? <ChevronUpIcon className="w-4 h-4 text-slate-400" />
          : <ChevronDownIcon className="w-4 h-4 text-slate-400" />
      ),
    },
    {
      key: 'vendor_name',
      header: 'Vendor',
      sortable: true,
      accessor: (sc) => sc.vendor_name,
      render: (sc) => <span className="font-medium text-white">{sc.vendor_name}</span>,
    },
    {
      key: 'overall_score',
      header: 'Overall Score',
      sortable: true,
      accessor: (sc) => sc.overall_score,
      render: (sc) => <ScoreBar score={sc.overall_score} />,
    },
    {
      key: 'quality_score',
      header: 'Quality',
      sortable: true,
      align: 'center',
      accessor: (sc) => sc.quality_score,
    },
    {
      key: 'delivery_score',
      header: 'Delivery',
      sortable: true,
      align: 'center',
      accessor: (sc) => sc.delivery_score,
    },
    {
      key: 'cost_score',
      header: 'Cost',
      sortable: true,
      align: 'center',
      accessor: (sc) => sc.cost_score,
    },
    {
      key: 'period',
      header: 'Period',
      sortable: true,
      accessor: (sc) => sc.period,
      render: (sc) => <span className="text-slate-400">{sc.period}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (sc) => sc.status,
      render: (sc) => <StatusPill status={sc.status} />,
    },
  ], [expandedRow]);

  const scorecardMobileCard = useCallback((sc: Scorecard) => (
    <MobileDataCard
      title={sc.vendor_name}
      subtitle={sc.period}
      badge={<StatusPill status={sc.status} />}
      onClick={() => toggleExpand(sc.id)}
      fields={[
        { label: 'Overall', value: <ScoreBar score={sc.overall_score} />, fullWidth: true },
        { label: 'Quality', value: sc.quality_score },
        { label: 'Delivery', value: sc.delivery_score },
        { label: 'Cost', value: sc.cost_score },
        { label: 'Responsiveness', value: sc.responsiveness_score },
        ...(expandedRow === sc.id
          ? [{ label: 'Breakdown', value: <ScorecardCharts sc={sc} />, fullWidth: true }]
          : []),
      ]}
    />
  ), [expandedRow, toggleExpand]);

  // ---- Approved Supplier List columns ----
  const aslColumns = useMemo<Array<DataTableColumn<ApprovedSupplier>>>(() => [
    {
      key: 'vendor_name',
      header: 'Vendor',
      sortable: true,
      accessor: (s) => s.vendor_name,
      render: (s) => <span className="font-medium text-white">{s.vendor_name}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (s) => s.status,
      render: (s) => <StatusPill status={s.status} />,
    },
    {
      key: 'approved_date',
      header: 'Approved Date',
      sortable: true,
      accessor: (s) => s.approved_date,
      render: (s) => <span className="text-slate-400">{s.approved_date}</span>,
    },
    {
      key: 'commodity',
      header: 'Commodity',
      sortable: true,
      accessor: (s) => s.commodity,
      render: (s) => <span className="text-slate-400">{s.commodity}</span>,
    },
    {
      key: 'notes',
      header: 'Notes',
      accessor: (s) => s.notes,
      render: (s) => <span className="text-slate-400 max-w-xs truncate block">{s.notes}</span>,
    },
  ], []);

  const aslMobileCard = useCallback((s: ApprovedSupplier) => (
    <MobileDataCard
      title={s.vendor_name}
      badge={<StatusPill status={s.status} />}
      fields={[
        { label: 'Approved', value: s.approved_date || '—' },
        { label: 'Commodity', value: s.commodity || '—' },
        { label: 'Notes', value: s.notes || '—', fullWidth: true },
      ]}
    />
  ), []);

  // ---- Audits columns ----
  const auditColumns = useMemo<Array<DataTableColumn<Audit>>>(() => [
    {
      key: 'vendor_name',
      header: 'Vendor',
      sortable: true,
      accessor: (a) => a.vendor_name,
      render: (a) => <span className="font-medium text-white">{a.vendor_name}</span>,
    },
    {
      key: 'audit_date',
      header: 'Audit Date',
      sortable: true,
      accessor: (a) => a.audit_date,
      render: (a) => <span className="text-slate-400">{a.audit_date}</span>,
    },
    {
      key: 'audit_type',
      header: 'Type',
      sortable: true,
      accessor: (a) => a.audit_type,
      render: (a) => <span className="text-slate-400 capitalize">{a.audit_type}</span>,
    },
    {
      key: 'score',
      header: 'Score',
      sortable: true,
      align: 'center',
      accessor: (a) => a.score,
      render: (a) => <ScorePill score={a.score} />,
    },
    {
      key: 'findings',
      header: 'Findings',
      accessor: (a) => a.findings,
      render: (a) => <span className="text-slate-400 max-w-xs truncate block">{a.findings}</span>,
    },
    {
      key: 'next_audit_due',
      header: 'Next Audit Due',
      sortable: true,
      accessor: (a) => a.next_audit_due,
      render: (a) => <span className="text-slate-400">{a.next_audit_due}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (a) => a.status,
      render: (a) => <StatusPill status={a.status} />,
    },
  ], []);

  const auditMobileCard = useCallback((a: Audit) => (
    <MobileDataCard
      title={a.vendor_name}
      subtitle={a.audit_type}
      badge={<StatusPill status={a.status} />}
      fields={[
        { label: 'Audit Date', value: a.audit_date || '—' },
        { label: 'Score', value: <ScorePill score={a.score} /> },
        { label: 'Next Due', value: a.next_audit_due || '—' },
        { label: 'Findings', value: a.findings || '—', fullWidth: true },
      ]}
    />
  ), []);

  const expandedScorecard = useMemo(
    () => filteredScorecards.find(sc => sc.id === expandedRow) ?? null,
    [filteredScorecards, expandedRow]
  );

  return (
    <div className="p-4 space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold text-white">Supplier Scorecards</h1>
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
      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        {statCards.map(card => (
          <MiniStat
            key={card.label}
            icon={card.icon}
            iconBg={card.iconBg}
            iconColor={card.iconColor}
            label={card.label}
            value={card.value}
            valueColor={card.valueColor}
          />
        ))}
      </MiniStatStrip>

      {/* Search & Tabs */}
      <div className="flex flex-col sm:flex-row sm:items-center gap-4">
        <div className="relative flex-1 max-w-md">
          <MagnifyingGlassIcon className="w-5 h-5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
          <input
            type="text"
            aria-label="Search by vendor name"
            placeholder="Search by vendor name..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="w-full pl-10 pr-4 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
          />
        </div>
        <div className="flex border border-slate-600 rounded-lg overflow-hidden">
          {TABS.map(tab => (
            <button
              key={tab}
              onClick={() => setActiveTab(tab)}
              className={`px-4 py-2 text-sm font-medium ${activeTab === tab ? 'bg-blue-600 text-white' : 'bg-fd-panel text-slate-300 hover:bg-slate-800'}`}
            >
              {tab}
            </button>
          ))}
        </div>
      </div>

      {/* Scorecards Tab */}
      {activeTab === 'Scorecards' && (
        <div className="space-y-4">
          <DataTable<Scorecard>
            columns={scorecardColumns}
            data={filteredScorecards}
            rowKey={(sc) => sc.id}
            onRowClick={(sc) => toggleExpand(sc.id)}
            defaultSort={{ key: 'overall_score', dir: 'desc' }}
            pageSize={25}
            csvExport={{ filename: 'supplier-scorecards' }}
            loading={loading}
            error={loadError}
            onRetry={fetchData}
            mobileCards={scorecardMobileCard}
            empty={{
              icon: ChartBarIcon,
              title: 'No scorecards found',
              description: search ? 'No scorecards match your search.' : 'Rated suppliers will appear here once you create a scorecard.',
              action: search ? undefined : { label: 'New Scorecard', onClick: () => setShowScorecardModal(true) },
            }}
          />
          {/* Expanded score breakdown (desktop) — preserves the click-to-expand charts. */}
          {expandedScorecard && (
            <div className="hidden md:block bg-fd-panel rounded-xl border border-slate-700 px-6 py-4">
              <div className="flex items-center justify-between mb-3">
                <h3 className="text-sm font-semibold text-white">
                  {expandedScorecard.vendor_name} — {expandedScorecard.period}
                </h3>
                <button
                  onClick={() => setExpandedRow(null)}
                  className="text-slate-400 hover:text-slate-200"
                  aria-label="Close breakdown"
                >
                  <XMarkIcon className="w-5 h-5" />
                </button>
              </div>
              <ScorecardCharts sc={expandedScorecard} />
            </div>
          )}
        </div>
      )}

      {/* Approved Supplier List Tab */}
      {activeTab === 'Approved Supplier List' && (
        <DataTable<ApprovedSupplier>
          columns={aslColumns}
          data={filteredASL}
          rowKey={(s) => s.id}
          defaultSort={{ key: 'vendor_name', dir: 'asc' }}
          pageSize={25}
          csvExport={{ filename: 'approved-suppliers' }}
          loading={loading}
          error={loadError}
          onRetry={fetchData}
          mobileCards={aslMobileCard}
          empty={{
            icon: ShieldCheckIcon,
            title: 'No approved suppliers found',
            description: search ? 'No approved suppliers match your search.' : 'Approved suppliers will appear here.',
          }}
        />
      )}

      {/* Audits Tab */}
      {activeTab === 'Audits' && (
        <DataTable<Audit>
          columns={auditColumns}
          data={filteredAudits}
          rowKey={(a) => a.id}
          defaultSort={{ key: 'audit_date', dir: 'desc' }}
          pageSize={25}
          csvExport={{ filename: 'supplier-audits' }}
          loading={loading}
          error={loadError}
          onRetry={fetchData}
          mobileCards={auditMobileCard}
          empty={{
            icon: CalendarDaysIcon,
            title: 'No audits found',
            description: search ? 'No audits match your search.' : 'Supplier audits will appear here once you log one.',
            action: search ? undefined : { label: 'New Audit', onClick: () => setShowAuditModal(true) },
          }}
        />
      )}

      {/* Create Scorecard Modal */}
      <Modal
        open={showScorecardModal}
        onClose={() => setShowScorecardModal(false)}
        size="lg"
        closeOnBackdrop={false}
        padded={false}
      >
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
              <h3 className="text-lg font-semibold text-white">New Supplier Scorecard</h3>
              <button onClick={() => setShowScorecardModal(false)} className="text-slate-400 hover:text-slate-400">
                <XMarkIcon className="w-5 h-5" />
              </button>
            </div>
            <form onSubmit={handleCreateScorecard} className="p-6 space-y-4">
              <FormField label="Vendor ID" required>
                {(field) => (
                  <input
                    {...field}
                    type="number" required
                    value={scorecardForm.vendor_id}
                    onChange={e => setScorecardForm(f => ({ ...f, vendor_id: e.target.value }))}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                )}
              </FormField>
              <FormField label="Period" required>
                {(field) => (
                  <input
                    {...field}
                    type="text" required placeholder="e.g., 2026-Q1"
                    value={scorecardForm.period}
                    onChange={e => setScorecardForm(f => ({ ...f, period: e.target.value }))}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                {(['quality_score', 'delivery_score', 'cost_score', 'responsiveness_score'] as const).map(field => (
                  <FormField
                    key={field}
                    label={field.replace('_score', '').replace('_', ' ')}
                    required
                    labelClassName="capitalize"
                  >
                    {(wiring) => (
                      <input
                        {...wiring}
                        type="number" min="0" max="100" required
                        value={scorecardForm[field]}
                        onChange={e => setScorecardForm(f => ({ ...f, [field]: e.target.value }))}
                        className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                      />
                    )}
                  </FormField>
                ))}
              </div>
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowScorecardModal(false)} className="px-4 py-2 text-sm font-medium text-slate-300 bg-fd-panel border border-slate-600 rounded-lg hover:bg-slate-800">
                  Cancel
                </button>
                <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                  Create Scorecard
                </button>
              </div>
            </form>
      </Modal>

      {/* Create Audit Modal */}
      <Modal
        open={showAuditModal}
        onClose={() => setShowAuditModal(false)}
        size="lg"
        closeOnBackdrop={false}
        padded={false}
      >
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700">
              <h3 className="text-lg font-semibold text-white">New Supplier Audit</h3>
              <button onClick={() => setShowAuditModal(false)} className="text-slate-400 hover:text-slate-400">
                <XMarkIcon className="w-5 h-5" />
              </button>
            </div>
            <form onSubmit={handleCreateAudit} className="p-6 space-y-4">
              <FormField label="Vendor ID" required>
                {(field) => (
                  <input
                    {...field}
                    type="number" required
                    value={auditForm.vendor_id}
                    onChange={e => setAuditForm(f => ({ ...f, vendor_id: e.target.value }))}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Audit Date" required>
                  {(field) => (
                    <input
                      {...field}
                      type="date" required
                      value={auditForm.audit_date}
                      onChange={e => setAuditForm(f => ({ ...f, audit_date: e.target.value }))}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                    />
                  )}
                </FormField>
                <FormField label="Audit Type">
                  {(field) => (
                    <select
                      {...field}
                      value={auditForm.audit_type}
                      onChange={e => setAuditForm(f => ({ ...f, audit_type: e.target.value }))}
                      className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                    >
                      <option value="on-site">On-Site</option>
                      <option value="remote">Remote</option>
                      <option value="document">Document Review</option>
                      <option value="follow-up">Follow-Up</option>
                    </select>
                  )}
                </FormField>
              </div>
              <FormField label="Score (0-100)" required>
                {(field) => (
                  <input
                    {...field}
                    type="number" min="0" max="100" required
                    value={auditForm.score}
                    onChange={e => setAuditForm(f => ({ ...f, score: e.target.value }))}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                )}
              </FormField>
              <FormField label="Findings">
                {(field) => (
                  <textarea
                    {...field}
                    rows={3}
                    value={auditForm.findings}
                    onChange={e => setAuditForm(f => ({ ...f, findings: e.target.value }))}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                )}
              </FormField>
              <FormField label="Next Audit Due">
                {(field) => (
                  <input
                    {...field}
                    type="date"
                    value={auditForm.next_audit_due}
                    onChange={e => setAuditForm(f => ({ ...f, next_audit_due: e.target.value }))}
                    className="w-full px-3 py-2 border border-slate-600 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  />
                )}
              </FormField>
              <div className="flex justify-end gap-3 pt-2">
                <button type="button" onClick={() => setShowAuditModal(false)} className="px-4 py-2 text-sm font-medium text-slate-300 bg-fd-panel border border-slate-600 rounded-lg hover:bg-slate-800">
                  Cancel
                </button>
                <button type="submit" className="px-4 py-2 text-sm font-medium text-white bg-blue-600 rounded-lg hover:bg-blue-700">
                  Create Audit
                </button>
              </div>
            </form>
      </Modal>
    </div>
  );
};

export default SupplierScorecards;
