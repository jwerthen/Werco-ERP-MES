import React, { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { Modal } from '../components/ui/Modal';
import {
  ErrorState,
  useToast,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
  Button,
  FormField,
  statusColorMap,
  statusVariantClass,
} from '../components/ui';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { formatCentralDate } from '../utils/centralTime';
import {
  PlusIcon,
  ExclamationTriangleIcon,
  ClipboardDocumentCheckIcon,
  DocumentMagnifyingGlassIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';

type TabType = 'ncr' | 'car' | 'fai';

interface NCR {
  id: number;
  ncr_number: string;
  part_id?: number;
  part?: { id: number; part_number: string; name: string };
  lot_number?: string;
  quantity_affected: number;
  source: string;
  status: string;
  disposition: string;
  title: string;
  description: string;
  detected_date?: string;
  created_at: string;
}

interface CAR {
  id: number;
  car_number: string;
  car_type: string;
  status: string;
  priority: number;
  title: string;
  problem_description: string;
  root_cause?: string;
  corrective_action?: string;
  due_date?: string;
  created_at: string;
}

interface FAI {
  id: number;
  fai_number: string;
  part_id: number;
  part?: { id: number; part_number: string; name: string };
  part_revision?: string;
  fai_type: string;
  status: string;
  total_characteristics: number;
  characteristics_passed: number;
  characteristics_failed: number;
  due_date?: string;
  created_at: string;
}

interface QualitySummary {
  open_ncrs: number;
  open_cars: number;
  pending_fais: number;
}

const dispositionColors: Record<string, string> = {
  use_as_is: 'bg-green-500/20 text-green-300',
  rework: 'bg-blue-500/20 text-blue-300',
  repair: 'bg-yellow-500/20 text-yellow-300',
  scrap: 'bg-red-500/20 text-red-300',
  return_to_vendor: 'bg-purple-500/20 text-purple-300',
  pending: 'bg-slate-800 text-slate-100',
};

// Domain override: for NCRs/CARs, `closed` means the nonconformance was
// resolved and corrective action verified — a GOOD terminal, so it reads green
// here rather than the app-wide neutral `closed`->slate (dormant sales/PO sense).
// Every other status defers to the central statusColors map.
const ncrStatusColors: Record<string, string> = {
  ...statusColorMap,
  closed: statusVariantClass.green,
};

export default function QualityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>('ncr');
  const [ncrs, setNcrs] = useState<NCR[]>([]);
  const [cars, setCars] = useState<CAR[]>([]);
  const [fais, setFais] = useState<FAI[]>([]);
  const [summary, setSummary] = useState<QualitySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const { showToast } = useToast();
  const [parts, setParts] = useState<any[]>([]);
  const [ncrStatusFilter, setNcrStatusFilter] = useState<string>(() => {
    const filter = searchParams.get('filter');
    return filter === 'open' ? 'open' : '';
  });
  
  const [showNCRModal, setShowNCRModal] = useState(false);
  const [showCARModal, setShowCARModal] = useState(false);
  const [showFAIModal, setShowFAIModal] = useState(false);

  // Form states
  const [ncrForm, setNcrForm] = useState({
    part_id: 0, title: '', description: '', source: 'in_process',
    quantity_affected: 1, specification: '', actual_value: '', required_value: ''
  });
  const [carForm, setCarForm] = useState({
    title: '', problem_description: '', car_type: 'corrective', priority: 3
  });
  const [faiForm, setFaiForm] = useState({
    part_id: 0, fai_type: 'full', reason: 'new_part', customer_approval_required: false
  });

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const [ncrsRes, carsRes, faisRes, summaryRes, partsRes] = await Promise.all([
        api.getNCRs(),
        api.getCARs(),
        api.getFAIs(),
        api.getQualitySummary(),
        api.getParts({ active_only: true, item_group: 'all' })
      ]);
      setNcrs(ncrsRes);
      setCars(carsRes);
      setFais(faisRes);
      setSummary(summaryRes);
      setParts(partsRes);
    } catch (err) {
      console.error('Failed to load quality data:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  const handleCreateNCR = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const payload = {
        ...ncrForm,
        part_id: ncrForm.part_id || null  // Send null instead of 0
      };
      await api.createNCR(payload);
      setShowNCRModal(false);
      setNcrForm({ part_id: 0, title: '', description: '', source: 'in_process', quantity_affected: 1, specification: '', actual_value: '', required_value: '' });
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create NCR');
    }
  };

  const handleCreateCAR = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createCAR(carForm);
      setShowCARModal(false);
      setCarForm({ title: '', problem_description: '', car_type: 'corrective', priority: 3 });
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create CAR');
    }
  };

  const handleCreateFAI = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await api.createFAI(faiForm);
      setShowFAIModal(false);
      setFaiForm({ part_id: 0, fai_type: 'full', reason: 'new_part', customer_approval_required: false });
      loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to create FAI');
    }
  };

  const priorityLabel = (p: number) => (p === 1 ? 'Critical' : p === 2 ? 'Major' : 'Minor');

  const filteredNcrs = useMemo(
    () => (ncrStatusFilter ? ncrs.filter((ncr) => ncr.status === ncrStatusFilter) : ncrs),
    [ncrs, ncrStatusFilter]
  );

  const ncrColumns = useMemo<Array<DataTableColumn<NCR>>>(() => [
    {
      key: 'ncr_number',
      header: 'NCR #',
      sortable: true,
      className: 'font-medium',
      accessor: (ncr) => ncr.ncr_number,
    },
    {
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (ncr) => ncr.part?.part_number ?? '',
      render: (ncr) => ncr.part?.part_number || '-',
    },
    {
      key: 'title',
      header: 'Title',
      sortable: true,
      accessor: (ncr) => ncr.title,
    },
    {
      key: 'source',
      header: 'Source',
      sortable: true,
      accessor: (ncr) => ncr.source,
      render: (ncr) => <span className="capitalize">{ncr.source.replace(/_/g, ' ')}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (ncr) => ncr.status,
      csv: (ncr) => ncr.status.replace(/_/g, ' '),
      render: (ncr) => <StatusBadge status={ncr.status} colorMap={ncrStatusColors} />,
    },
    {
      key: 'disposition',
      header: 'Disposition',
      sortable: true,
      accessor: (ncr) => ncr.disposition,
      csv: (ncr) => ncr.disposition.replace(/_/g, ' '),
      render: (ncr) => <StatusBadge status={ncr.disposition} colorMap={dispositionColors} />,
    },
    {
      key: 'created_at',
      header: 'Date',
      sortable: true,
      accessor: (ncr) => ncr.created_at,
      csv: (ncr) => formatCentralDate(ncr.created_at),
      render: (ncr) => <span className="text-sm">{formatCentralDate(ncr.created_at)}</span>,
    },
  ], []);

  const carColumns = useMemo<Array<DataTableColumn<CAR>>>(() => [
    {
      key: 'car_number',
      header: 'CAR #',
      sortable: true,
      className: 'font-medium',
      accessor: (car) => car.car_number,
    },
    {
      key: 'car_type',
      header: 'Type',
      sortable: true,
      accessor: (car) => car.car_type,
      render: (car) => <span className="capitalize">{car.car_type}</span>,
    },
    {
      key: 'title',
      header: 'Title',
      sortable: true,
      accessor: (car) => car.title,
    },
    {
      key: 'priority',
      header: 'Priority',
      sortable: true,
      align: 'center',
      headerClassName: 'text-center',
      accessor: (car) => car.priority,
      csv: (car) => priorityLabel(car.priority),
      render: (car) => (
        <span className={`px-2 py-1 rounded text-xs font-medium ${
          car.priority === 1 ? 'bg-red-500/20 text-red-300' :
          car.priority === 2 ? 'bg-yellow-500/20 text-yellow-300' :
          'bg-slate-800 text-slate-100'
        }`}>
          {priorityLabel(car.priority)}
        </span>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (car) => car.status,
      csv: (car) => car.status.replace(/_/g, ' '),
      render: (car) => <StatusBadge status={car.status} colorMap={ncrStatusColors} />,
    },
    {
      key: 'due_date',
      header: 'Due Date',
      sortable: true,
      accessor: (car) => car.due_date ?? '',
      csv: (car) => (car.due_date ? formatCentralDate(car.due_date) : ''),
      render: (car) => <span className="text-sm">{car.due_date ? formatCentralDate(car.due_date) : '-'}</span>,
    },
  ], []);

  const faiColumns = useMemo<Array<DataTableColumn<FAI>>>(() => [
    {
      key: 'fai_number',
      header: 'FAI #',
      sortable: true,
      className: 'font-medium',
      accessor: (fai) => fai.fai_number,
    },
    {
      key: 'part',
      header: 'Part',
      sortable: true,
      accessor: (fai) => fai.part?.part_number ?? '',
      csv: (fai) => `${fai.part?.part_number ?? ''}${fai.part_revision ? ` Rev ${fai.part_revision}` : ''}`.trim(),
      render: (fai) => (
        <span>
          {fai.part?.part_number}
          {fai.part_revision && <span className="text-slate-500 ml-1">Rev {fai.part_revision}</span>}
        </span>
      ),
    },
    {
      key: 'fai_type',
      header: 'Type',
      sortable: true,
      accessor: (fai) => fai.fai_type,
      render: (fai) => <span className="capitalize">{fai.fai_type}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: (fai) => fai.status,
      render: (fai) => <StatusBadge status={fai.status} />,
    },
    {
      key: 'pass_fail',
      header: 'Pass/Fail',
      align: 'center',
      headerClassName: 'text-center',
      accessor: (fai) => fai.characteristics_passed,
      csv: (fai) => `${fai.characteristics_passed}/${fai.characteristics_failed}/${fai.total_characteristics}`,
      render: (fai) => (
        <span className="text-sm">
          <span className="text-green-600">{fai.characteristics_passed}</span>
          {' / '}
          <span className="text-red-600">{fai.characteristics_failed}</span>
          {' / '}
          <span className="text-slate-400">{fai.total_characteristics}</span>
        </span>
      ),
    },
    {
      key: 'due_date',
      header: 'Due Date',
      sortable: true,
      accessor: (fai) => fai.due_date ?? '',
      csv: (fai) => (fai.due_date ? formatCentralDate(fai.due_date) : ''),
      render: (fai) => <span className="text-sm">{fai.due_date ? formatCentralDate(fai.due_date) : '-'}</span>,
    },
  ], []);

  if (loadError) {
    return (
      <div className="space-y-6">
        <div className="flex justify-between items-center">
          <h1 className="text-2xl font-bold text-white">Quality Management</h1>
        </div>
        <ErrorState
          message="Could not load quality data (NCRs, CARs, FAIs)."
          onRetry={loadData}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold text-white">Quality Management</h1>
      </div>

      {/* Summary strip */}
      {summary && (
        <div data-tour="qa-ncr">
        <MiniStatStrip className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          <MiniStat
            icon={ExclamationTriangleIcon}
            iconBg="bg-fd-red/15"
            iconColor="text-fd-red"
            label="Open NCRs"
            value={summary.open_ncrs}
            valueColor={summary.open_ncrs > 0 ? 'text-fd-red' : undefined}
            active={activeTab === 'ncr' && ncrStatusFilter === 'open'}
            onClick={() => {
              setActiveTab('ncr');
              setNcrStatusFilter('open');
              setSearchParams({ filter: 'open' });
            }}
          />
          <MiniStat
            icon={ClipboardDocumentCheckIcon}
            iconBg="bg-fd-amber/15"
            iconColor="text-fd-amber"
            label="Open CARs"
            value={summary.open_cars}
            valueColor={summary.open_cars > 0 ? 'text-fd-amber' : undefined}
            active={activeTab === 'car'}
            onClick={() => setActiveTab('car')}
          />
          <MiniStat
            icon={DocumentMagnifyingGlassIcon}
            iconBg="bg-fd-blue/15"
            iconColor="text-fd-blue"
            label="Pending FAIs"
            value={summary.pending_fais}
            valueColor={summary.pending_fais > 0 ? 'text-fd-blue' : undefined}
            active={activeTab === 'fai'}
            onClick={() => setActiveTab('fai')}
          />
        </MiniStatStrip>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-slate-700">
        <nav className="-mb-px flex space-x-8">
          {[
            { id: 'ncr', label: 'NCR', icon: ExclamationTriangleIcon },
            { id: 'car', label: 'CAR', icon: ClipboardDocumentCheckIcon },
            { id: 'fai', label: 'FAI', icon: DocumentMagnifyingGlassIcon },
          ].map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id as TabType)}
              className={`flex items-center py-4 px-1 border-b-2 font-medium text-sm ${
                activeTab === tab.id
                  ? 'border-werco-primary text-werco-primary'
                  : 'border-transparent text-slate-400 hover:text-slate-300 hover:border-slate-600'
              }`}
            >
              <tab.icon className="h-5 w-5 mr-2" />
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab Content */}
      <div className="card">
        {/* NCR Tab */}
        {activeTab === 'ncr' && (
          <>
            <div className="flex justify-between items-center mb-4">
              <div className="flex items-center gap-4">
                <h2 className="text-lg font-semibold">Non-Conformance Reports</h2>
                <select
                  value={ncrStatusFilter}
                  onChange={(e) => {
                    setNcrStatusFilter(e.target.value);
                    if (e.target.value) {
                      setSearchParams({ filter: e.target.value });
                    } else {
                      setSearchParams({});
                    }
                  }}
                  className="input w-40"
                >
                  <option value="">All Status</option>
                  <option value="open">Open</option>
                  <option value="under_review">Under Review</option>
                  <option value="pending_disposition">Pending Disposition</option>
                  <option value="closed">Closed</option>
                </select>
                {ncrStatusFilter && (
                  <button
                    onClick={() => {
                      setNcrStatusFilter('');
                      setSearchParams({});
                    }}
                    className="flex items-center gap-1 px-3 py-1.5 text-sm bg-werco-100 text-werco-700 rounded-full hover:bg-werco-200"
                  >
                    <XMarkIcon className="h-4 w-4" />
                    Clear
                  </button>
                )}
              </div>
              <Button onClick={() => setShowNCRModal(true)} className="flex items-center">
                <PlusIcon className="h-5 w-5 mr-1" /> New NCR
              </Button>
            </div>
            <DataTable
              columns={ncrColumns}
              data={filteredNcrs}
              rowKey={(ncr) => ncr.id}
              loading={loading}
              defaultSort={{ key: 'created_at', dir: 'desc' }}
              pageSize={25}
              csvExport={{ filename: 'ncrs' }}
              empty={{
                icon: ExclamationTriangleIcon,
                title: 'No NCRs found',
                description: 'Non-conformance reports will appear here once they are created.',
                action: { label: 'New NCR', onClick: () => setShowNCRModal(true) },
              }}
              mobileCards={(ncr) => (
                <MobileDataCard
                  title={ncr.ncr_number}
                  subtitle={ncr.title}
                  badge={<StatusBadge status={ncr.status} colorMap={ncrStatusColors} />}
                  fields={[
                    { label: 'Part', value: ncr.part?.part_number || '-' },
                    { label: 'Source', value: <span className="capitalize">{ncr.source.replace(/_/g, ' ')}</span> },
                    { label: 'Disposition', value: <StatusBadge status={ncr.disposition} colorMap={dispositionColors} /> },
                    { label: 'Date', value: formatCentralDate(ncr.created_at) },
                  ]}
                />
              )}
            />
          </>
        )}

        {/* CAR Tab */}
        {activeTab === 'car' && (
          <>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-lg font-semibold">Corrective Action Requests</h2>
              <Button onClick={() => setShowCARModal(true)} className="flex items-center">
                <PlusIcon className="h-5 w-5 mr-1" /> New CAR
              </Button>
            </div>
            <DataTable
              columns={carColumns}
              data={cars}
              rowKey={(car) => car.id}
              loading={loading}
              defaultSort={{ key: 'car_number', dir: 'desc' }}
              pageSize={25}
              csvExport={{ filename: 'cars' }}
              empty={{
                icon: ClipboardDocumentCheckIcon,
                title: 'No CARs found',
                description: 'Corrective action requests will appear here once they are created.',
                action: { label: 'New CAR', onClick: () => setShowCARModal(true) },
              }}
              mobileCards={(car) => (
                <MobileDataCard
                  title={car.car_number}
                  subtitle={car.title}
                  badge={<StatusBadge status={car.status} colorMap={ncrStatusColors} />}
                  fields={[
                    { label: 'Type', value: <span className="capitalize">{car.car_type}</span> },
                    {
                      label: 'Priority',
                      value: (
                        <span className={`px-2 py-1 rounded text-xs font-medium ${
                          car.priority === 1 ? 'bg-red-500/20 text-red-300' :
                          car.priority === 2 ? 'bg-yellow-500/20 text-yellow-300' :
                          'bg-slate-800 text-slate-100'
                        }`}>
                          {priorityLabel(car.priority)}
                        </span>
                      ),
                    },
                    { label: 'Due Date', value: car.due_date ? formatCentralDate(car.due_date) : '-' },
                  ]}
                />
              )}
            />
          </>
        )}

        {/* FAI Tab */}
        {activeTab === 'fai' && (
          <>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-lg font-semibold">First Article Inspections</h2>
              <Button onClick={() => setShowFAIModal(true)} className="flex items-center">
                <PlusIcon className="h-5 w-5 mr-1" /> New FAI
              </Button>
            </div>
            <DataTable
              columns={faiColumns}
              data={fais}
              rowKey={(fai) => fai.id}
              loading={loading}
              defaultSort={{ key: 'fai_number', dir: 'desc' }}
              pageSize={25}
              csvExport={{ filename: 'fais' }}
              empty={{
                icon: DocumentMagnifyingGlassIcon,
                title: 'No FAIs found',
                description: 'First article inspections will appear here once they are created.',
                action: { label: 'New FAI', onClick: () => setShowFAIModal(true) },
              }}
              mobileCards={(fai) => (
                <MobileDataCard
                  title={fai.fai_number}
                  subtitle={`${fai.part?.part_number ?? ''}${fai.part_revision ? ` Rev ${fai.part_revision}` : ''}`}
                  badge={<StatusBadge status={fai.status} />}
                  fields={[
                    { label: 'Type', value: <span className="capitalize">{fai.fai_type}</span> },
                    {
                      label: 'Pass/Fail',
                      value: (
                        <span>
                          <span className="text-green-600">{fai.characteristics_passed}</span>
                          {' / '}
                          <span className="text-red-600">{fai.characteristics_failed}</span>
                          {' / '}
                          <span className="text-slate-400">{fai.total_characteristics}</span>
                        </span>
                      ),
                    },
                    { label: 'Due Date', value: fai.due_date ? formatCentralDate(fai.due_date) : '-' },
                  ]}
                />
              )}
            />
          </>
        )}
      </div>

      {/* NCR Modal */}
      <Modal open={showNCRModal} onClose={() => setShowNCRModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">New Non-Conformance Report</h3>
              <button onClick={() => setShowNCRModal(false)} aria-label="Close dialog"><XMarkIcon className="h-6 w-6" aria-hidden="true" /></button>
            </div>
            <form onSubmit={handleCreateNCR} className="space-y-4">
              <FormField label="Title" required>
                {(field) => (
                  <input {...field} type="text" value={ncrForm.title} onChange={(e) => setNcrForm({...ncrForm, title: e.target.value})} className="input" required />
                )}
              </FormField>
              <FormField label="Part (optional)">
                {(field) => (
                  <select {...field} value={ncrForm.part_id} onChange={(e) => setNcrForm({...ncrForm, part_id: parseInt(e.target.value)})} className="input">
                    <option value={0}>Select part...</option>
                    {parts.map(p => <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>)}
                  </select>
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Source">
                  {(field) => (
                    <select {...field} value={ncrForm.source} onChange={(e) => setNcrForm({...ncrForm, source: e.target.value})} className="input">
                      <option value="incoming_inspection">Incoming Inspection</option>
                      <option value="in_process">In Process</option>
                      <option value="final_inspection">Final Inspection</option>
                      <option value="customer_return">Customer Return</option>
                    </select>
                  )}
                </FormField>
                <FormField label="Qty Affected">
                  {(field) => (
                    <input {...field} type="number" value={ncrForm.quantity_affected} onChange={(e) => setNcrForm({...ncrForm, quantity_affected: parseFloat(e.target.value)})} className="input" min={1} />
                  )}
                </FormField>
              </div>
              <FormField label="Description" required>
                {(field) => (
                  <textarea {...field} value={ncrForm.description} onChange={(e) => setNcrForm({...ncrForm, description: e.target.value})} className="input" rows={3} required />
                )}
              </FormField>
              <div className="grid grid-cols-3 gap-4">
                <FormField label="Specification">
                  {(field) => (
                    <input {...field} type="text" value={ncrForm.specification} onChange={(e) => setNcrForm({...ncrForm, specification: e.target.value})} className="input" placeholder="e.g., 10.00 ± 0.05" />
                  )}
                </FormField>
                <FormField label="Actual Value">
                  {(field) => (
                    <input {...field} type="text" value={ncrForm.actual_value} onChange={(e) => setNcrForm({...ncrForm, actual_value: e.target.value})} className="input" placeholder="e.g., 10.12" />
                  )}
                </FormField>
                <FormField label="Required">
                  {(field) => (
                    <input {...field} type="text" value={ncrForm.required_value} onChange={(e) => setNcrForm({...ncrForm, required_value: e.target.value})} className="input" placeholder="e.g., 9.95-10.05" />
                  )}
                </FormField>
              </div>
              <div className="flex justify-end gap-3">
                <Button type="button" variant="secondary" onClick={() => setShowNCRModal(false)}>Cancel</Button>
                <Button type="submit">Create NCR</Button>
              </div>
            </form>
      </Modal>

      {/* CAR Modal */}
      <Modal open={showCARModal} onClose={() => setShowCARModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">New Corrective Action Request</h3>
              <button onClick={() => setShowCARModal(false)} aria-label="Close dialog"><XMarkIcon className="h-6 w-6" aria-hidden="true" /></button>
            </div>
            <form onSubmit={handleCreateCAR} className="space-y-4">
              <FormField label="Title" required>
                {(field) => (
                  <input {...field} type="text" value={carForm.title} onChange={(e) => setCarForm({...carForm, title: e.target.value})} className="input" required />
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="Type">
                  {(field) => (
                    <select {...field} value={carForm.car_type} onChange={(e) => setCarForm({...carForm, car_type: e.target.value})} className="input">
                      <option value="corrective">Corrective</option>
                      <option value="preventive">Preventive</option>
                      <option value="improvement">Improvement</option>
                    </select>
                  )}
                </FormField>
                <FormField label="Priority">
                  {(field) => (
                    <select {...field} value={carForm.priority} onChange={(e) => setCarForm({...carForm, priority: parseInt(e.target.value)})} className="input">
                      <option value={1}>Critical</option>
                      <option value={2}>Major</option>
                      <option value={3}>Minor</option>
                    </select>
                  )}
                </FormField>
              </div>
              <FormField label="Problem Description" required>
                {(field) => (
                  <textarea {...field} value={carForm.problem_description} onChange={(e) => setCarForm({...carForm, problem_description: e.target.value})} className="input" rows={4} required />
                )}
              </FormField>
              <div className="flex justify-end gap-3">
                <Button type="button" variant="secondary" onClick={() => setShowCARModal(false)}>Cancel</Button>
                <Button type="submit">Create CAR</Button>
              </div>
            </form>
      </Modal>

      {/* FAI Modal */}
      <Modal open={showFAIModal} onClose={() => setShowFAIModal(false)} size="lg" closeOnBackdrop={false}>
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">New First Article Inspection</h3>
              <button onClick={() => setShowFAIModal(false)} aria-label="Close dialog"><XMarkIcon className="h-6 w-6" aria-hidden="true" /></button>
            </div>
            <form onSubmit={handleCreateFAI} className="space-y-4">
              <FormField label="Part" required>
                {(field) => (
                  <select {...field} value={faiForm.part_id} onChange={(e) => setFaiForm({...faiForm, part_id: parseInt(e.target.value)})} className="input" required>
                    <option value={0}>Select part...</option>
                    {parts.map(p => <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>)}
                  </select>
                )}
              </FormField>
              <div className="grid grid-cols-2 gap-4">
                <FormField label="FAI Type">
                  {(field) => (
                    <select {...field} value={faiForm.fai_type} onChange={(e) => setFaiForm({...faiForm, fai_type: e.target.value})} className="input">
                      <option value="full">Full</option>
                      <option value="partial">Partial</option>
                      <option value="delta">Delta</option>
                    </select>
                  )}
                </FormField>
                <FormField label="Reason">
                  {(field) => (
                    <select {...field} value={faiForm.reason} onChange={(e) => setFaiForm({...faiForm, reason: e.target.value})} className="input">
                      <option value="new_part">New Part</option>
                      <option value="design_change">Design Change</option>
                      <option value="process_change">Process Change</option>
                      <option value="new_supplier">New Supplier</option>
                    </select>
                  )}
                </FormField>
              </div>
              <label className="flex items-center">
                <input type="checkbox" checked={faiForm.customer_approval_required} onChange={(e) => setFaiForm({...faiForm, customer_approval_required: e.target.checked})} className="mr-2" />
                <span className="text-sm">Customer Approval Required</span>
              </label>
              <div className="flex justify-end gap-3">
                <Button type="button" variant="secondary" onClick={() => setShowFAIModal(false)}>Cancel</Button>
                <Button type="submit">Create FAI</Button>
              </div>
            </form>
      </Modal>
    </div>
  );
}
