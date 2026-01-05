import React, { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import api from '../services/api';
import { format } from 'date-fns';
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

const statusColors: Record<string, string> = {
  open: 'bg-red-100 text-red-800',
  under_review: 'bg-yellow-100 text-yellow-800',
  pending_disposition: 'bg-orange-100 text-orange-800',
  closed: 'bg-green-100 text-green-800',
  void: 'bg-gray-100 text-gray-800',
  root_cause_analysis: 'bg-blue-100 text-blue-800',
  corrective_action: 'bg-purple-100 text-purple-800',
  verification: 'bg-indigo-100 text-indigo-800',
  pending: 'bg-yellow-100 text-yellow-800',
  in_progress: 'bg-blue-100 text-blue-800',
  passed: 'bg-green-100 text-green-800',
  failed: 'bg-red-100 text-red-800',
  conditional: 'bg-orange-100 text-orange-800',
};

const dispositionColors: Record<string, string> = {
  use_as_is: 'bg-green-100 text-green-800',
  rework: 'bg-blue-100 text-blue-800',
  repair: 'bg-yellow-100 text-yellow-800',
  scrap: 'bg-red-100 text-red-800',
  return_to_vendor: 'bg-purple-100 text-purple-800',
  pending: 'bg-gray-100 text-gray-800',
};

export default function QualityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [activeTab, setActiveTab] = useState<TabType>('ncr');
  const [ncrs, setNcrs] = useState<NCR[]>([]);
  const [cars, setCars] = useState<CAR[]>([]);
  const [fais, setFais] = useState<FAI[]>([]);
  const [summary, setSummary] = useState<QualitySummary | null>(null);
  const [loading, setLoading] = useState(true);
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
    try {
      const [ncrsRes, carsRes, faisRes, summaryRes, partsRes] = await Promise.all([
        api.getNCRs(),
        api.getCARs(),
        api.getFAIs(),
        api.getQualitySummary(),
        api.getParts({ active_only: true })
      ]);
      setNcrs(ncrsRes);
      setCars(carsRes);
      setFais(faisRes);
      setSummary(summaryRes);
      setParts(partsRes);
    } catch (err) {
      console.error('Failed to load quality data:', err);
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
      alert(err.response?.data?.detail || 'Failed to create NCR');
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
      alert(err.response?.data?.detail || 'Failed to create CAR');
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
      alert(err.response?.data?.detail || 'Failed to create FAI');
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
        <h1 className="text-2xl font-bold text-gray-900">Quality Management</h1>
      </div>

      {/* Summary Cards */}
      {summary && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className={`card flex items-center ${summary.open_ncrs > 0 ? 'border-l-4 border-red-500' : ''}`}>
            <ExclamationTriangleIcon className="h-10 w-10 text-red-500 mr-4" />
            <div>
              <div className="text-2xl font-bold">{summary.open_ncrs}</div>
              <div className="text-sm text-gray-500">Open NCRs</div>
            </div>
          </div>
          <div className={`card flex items-center ${summary.open_cars > 0 ? 'border-l-4 border-yellow-500' : ''}`}>
            <ClipboardDocumentCheckIcon className="h-10 w-10 text-yellow-500 mr-4" />
            <div>
              <div className="text-2xl font-bold">{summary.open_cars}</div>
              <div className="text-sm text-gray-500">Open CARs</div>
            </div>
          </div>
          <div className={`card flex items-center ${summary.pending_fais > 0 ? 'border-l-4 border-blue-500' : ''}`}>
            <DocumentMagnifyingGlassIcon className="h-10 w-10 text-blue-500 mr-4" />
            <div>
              <div className="text-2xl font-bold">{summary.pending_fais}</div>
              <div className="text-sm text-gray-500">Pending FAIs</div>
            </div>
          </div>
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-gray-200">
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
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
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
              <button onClick={() => setShowNCRModal(true)} className="btn-primary flex items-center">
                <PlusIcon className="h-5 w-5 mr-1" /> New NCR
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">NCR #</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Title</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Source</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Disposition</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Date</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {(ncrStatusFilter ? ncrs.filter(ncr => ncr.status === ncrStatusFilter) : ncrs).map((ncr) => (
                    <tr key={ncr.id} className="hover:bg-gray-50 cursor-pointer">
                      <td className="px-4 py-3 font-medium">{ncr.ncr_number}</td>
                      <td className="px-4 py-3">{ncr.part?.part_number || '-'}</td>
                      <td className="px-4 py-3">{ncr.title}</td>
                      <td className="px-4 py-3 text-sm">{ncr.source.replace(/_/g, ' ')}</td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${statusColors[ncr.status] || 'bg-gray-100'}`}>
                          {ncr.status.replace(/_/g, ' ')}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${dispositionColors[ncr.disposition] || 'bg-gray-100'}`}>
                          {ncr.disposition.replace(/_/g, ' ')}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm">{format(new Date(ncr.created_at), 'MMM d, yyyy')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {ncrs.length === 0 && <p className="text-center text-gray-500 py-8">No NCRs found</p>}
            </div>
          </>
        )}

        {/* CAR Tab */}
        {activeTab === 'car' && (
          <>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-lg font-semibold">Corrective Action Requests</h2>
              <button onClick={() => setShowCARModal(true)} className="btn-primary flex items-center">
                <PlusIcon className="h-5 w-5 mr-1" /> New CAR
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">CAR #</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Title</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Priority</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due Date</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {cars.map((car) => (
                    <tr key={car.id} className="hover:bg-gray-50 cursor-pointer">
                      <td className="px-4 py-3 font-medium">{car.car_number}</td>
                      <td className="px-4 py-3 text-sm capitalize">{car.car_type}</td>
                      <td className="px-4 py-3">{car.title}</td>
                      <td className="px-4 py-3 text-center">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${
                          car.priority === 1 ? 'bg-red-100 text-red-800' :
                          car.priority === 2 ? 'bg-yellow-100 text-yellow-800' :
                          'bg-gray-100 text-gray-800'
                        }`}>
                          {car.priority === 1 ? 'Critical' : car.priority === 2 ? 'Major' : 'Minor'}
                        </span>
                      </td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${statusColors[car.status] || 'bg-gray-100'}`}>
                          {car.status.replace(/_/g, ' ')}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-sm">{car.due_date ? format(new Date(car.due_date), 'MMM d, yyyy') : '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {cars.length === 0 && <p className="text-center text-gray-500 py-8">No CARs found</p>}
            </div>
          </>
        )}

        {/* FAI Tab */}
        {activeTab === 'fai' && (
          <>
            <div className="flex justify-between items-center mb-4">
              <h2 className="text-lg font-semibold">First Article Inspections</h2>
              <button onClick={() => setShowFAIModal(true)} className="btn-primary flex items-center">
                <PlusIcon className="h-5 w-5 mr-1" /> New FAI
              </button>
            </div>
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200">
                <thead className="bg-gray-50">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">FAI #</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Part</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Type</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Status</th>
                    <th className="px-4 py-3 text-center text-xs font-medium text-gray-500 uppercase">Pass/Fail</th>
                    <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Due Date</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {fais.map((fai) => (
                    <tr key={fai.id} className="hover:bg-gray-50 cursor-pointer">
                      <td className="px-4 py-3 font-medium">{fai.fai_number}</td>
                      <td className="px-4 py-3">
                        {fai.part?.part_number}
                        {fai.part_revision && <span className="text-gray-400 ml-1">Rev {fai.part_revision}</span>}
                      </td>
                      <td className="px-4 py-3 text-sm capitalize">{fai.fai_type}</td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-1 rounded text-xs font-medium ${statusColors[fai.status] || 'bg-gray-100'}`}>
                          {fai.status}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-center text-sm">
                        <span className="text-green-600">{fai.characteristics_passed}</span>
                        {' / '}
                        <span className="text-red-600">{fai.characteristics_failed}</span>
                        {' / '}
                        <span className="text-gray-500">{fai.total_characteristics}</span>
                      </td>
                      <td className="px-4 py-3 text-sm">{fai.due_date ? format(new Date(fai.due_date), 'MMM d, yyyy') : '-'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {fais.length === 0 && <p className="text-center text-gray-500 py-8">No FAIs found</p>}
            </div>
          </>
        )}
      </div>

      {/* NCR Modal */}
      {showNCRModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4 max-h-[90vh] overflow-y-auto">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">New Non-Conformance Report</h3>
              <button onClick={() => setShowNCRModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <form onSubmit={handleCreateNCR} className="space-y-4">
              <div>
                <label className="label">Title</label>
                <input type="text" value={ncrForm.title} onChange={(e) => setNcrForm({...ncrForm, title: e.target.value})} className="input" required />
              </div>
              <div>
                <label className="label">Part (optional)</label>
                <select value={ncrForm.part_id} onChange={(e) => setNcrForm({...ncrForm, part_id: parseInt(e.target.value)})} className="input">
                  <option value={0}>Select part...</option>
                  {parts.map(p => <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>)}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Source</label>
                  <select value={ncrForm.source} onChange={(e) => setNcrForm({...ncrForm, source: e.target.value})} className="input">
                    <option value="incoming_inspection">Incoming Inspection</option>
                    <option value="in_process">In Process</option>
                    <option value="final_inspection">Final Inspection</option>
                    <option value="customer_return">Customer Return</option>
                  </select>
                </div>
                <div>
                  <label className="label">Qty Affected</label>
                  <input type="number" value={ncrForm.quantity_affected} onChange={(e) => setNcrForm({...ncrForm, quantity_affected: parseFloat(e.target.value)})} className="input" min={1} />
                </div>
              </div>
              <div>
                <label className="label">Description</label>
                <textarea value={ncrForm.description} onChange={(e) => setNcrForm({...ncrForm, description: e.target.value})} className="input" rows={3} required />
              </div>
              <div className="grid grid-cols-3 gap-4">
                <div>
                  <label className="label">Specification</label>
                  <input type="text" value={ncrForm.specification} onChange={(e) => setNcrForm({...ncrForm, specification: e.target.value})} className="input" placeholder="e.g., 10.00 ± 0.05" />
                </div>
                <div>
                  <label className="label">Actual Value</label>
                  <input type="text" value={ncrForm.actual_value} onChange={(e) => setNcrForm({...ncrForm, actual_value: e.target.value})} className="input" placeholder="e.g., 10.12" />
                </div>
                <div>
                  <label className="label">Required</label>
                  <input type="text" value={ncrForm.required_value} onChange={(e) => setNcrForm({...ncrForm, required_value: e.target.value})} className="input" placeholder="e.g., 9.95-10.05" />
                </div>
              </div>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowNCRModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create NCR</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* CAR Modal */}
      {showCARModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">New Corrective Action Request</h3>
              <button onClick={() => setShowCARModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <form onSubmit={handleCreateCAR} className="space-y-4">
              <div>
                <label className="label">Title</label>
                <input type="text" value={carForm.title} onChange={(e) => setCarForm({...carForm, title: e.target.value})} className="input" required />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">Type</label>
                  <select value={carForm.car_type} onChange={(e) => setCarForm({...carForm, car_type: e.target.value})} className="input">
                    <option value="corrective">Corrective</option>
                    <option value="preventive">Preventive</option>
                    <option value="improvement">Improvement</option>
                  </select>
                </div>
                <div>
                  <label className="label">Priority</label>
                  <select value={carForm.priority} onChange={(e) => setCarForm({...carForm, priority: parseInt(e.target.value)})} className="input">
                    <option value={1}>Critical</option>
                    <option value={2}>Major</option>
                    <option value={3}>Minor</option>
                  </select>
                </div>
              </div>
              <div>
                <label className="label">Problem Description</label>
                <textarea value={carForm.problem_description} onChange={(e) => setCarForm({...carForm, problem_description: e.target.value})} className="input" rows={4} required />
              </div>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowCARModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create CAR</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* FAI Modal */}
      {showFAIModal && (
        <div className="fixed inset-0 bg-black bg-opacity-50 flex items-center justify-center z-50">
          <div className="bg-white rounded-lg p-6 max-w-lg w-full mx-4">
            <div className="flex justify-between items-center mb-4">
              <h3 className="text-lg font-semibold">New First Article Inspection</h3>
              <button onClick={() => setShowFAIModal(false)}><XMarkIcon className="h-6 w-6" /></button>
            </div>
            <form onSubmit={handleCreateFAI} className="space-y-4">
              <div>
                <label className="label">Part</label>
                <select value={faiForm.part_id} onChange={(e) => setFaiForm({...faiForm, part_id: parseInt(e.target.value)})} className="input" required>
                  <option value={0}>Select part...</option>
                  {parts.map(p => <option key={p.id} value={p.id}>{p.part_number} - {p.name}</option>)}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="label">FAI Type</label>
                  <select value={faiForm.fai_type} onChange={(e) => setFaiForm({...faiForm, fai_type: e.target.value})} className="input">
                    <option value="full">Full</option>
                    <option value="partial">Partial</option>
                    <option value="delta">Delta</option>
                  </select>
                </div>
                <div>
                  <label className="label">Reason</label>
                  <select value={faiForm.reason} onChange={(e) => setFaiForm({...faiForm, reason: e.target.value})} className="input">
                    <option value="new_part">New Part</option>
                    <option value="design_change">Design Change</option>
                    <option value="process_change">Process Change</option>
                    <option value="new_supplier">New Supplier</option>
                  </select>
                </div>
              </div>
              <label className="flex items-center">
                <input type="checkbox" checked={faiForm.customer_approval_required} onChange={(e) => setFaiForm({...faiForm, customer_approval_required: e.target.checked})} className="mr-2" />
                <span className="text-sm">Customer Approval Required</span>
              </label>
              <div className="flex justify-end gap-3">
                <button type="button" onClick={() => setShowFAIModal(false)} className="btn-secondary">Cancel</button>
                <button type="submit" className="btn-primary">Create FAI</button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
