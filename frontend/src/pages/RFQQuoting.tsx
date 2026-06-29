import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import api from '../services/api';
import { ArrowDownTrayIcon, CheckCircleIcon, DocumentArrowUpIcon, SparklesIcon } from '@heroicons/react/24/outline';

interface CustomerOption {
  id: number;
  name: string;
}

interface RfqPackageResponse {
  id: number;
  rfq_number: string;
  customer_id?: number;
  customer_name?: string;
  rfq_reference?: string;
  status: string;
  warnings: string[];
  file_count: number;
  files: Array<{
    id?: number;
    name: string;
    extension?: string;
    parse_status?: string;
    parse_error?: string | null;
    summary?: Record<string, any>;
  }>;
  quote_id?: number;
}

interface QuoteLineSummary {
  part_number?: string;
  part_name: string;
  quantity: number;
  material?: string;
  thickness?: string;
  flat_area?: number;
  cut_length?: number;
  hole_count?: number;
  bend_count?: number;
  finish?: string;
  part_total: number;
  confidence: Record<string, number>;
  sources: Record<string, string[]>;
  notes?: string;
  parent_part_number?: string;
  line_type?: string;
  item_type?: string;
  bom_level?: number;
  item_number?: string;
  quantity_per_assembly?: number;
  unit_of_measure?: string;
}

interface EstimateResponse {
  rfq_package_id: number;
  estimate_id: number;
  quote_id: number;
  quote_number: string;
  totals: Record<string, number>;
  lead_time: { label?: string; min_days?: number; max_days?: number; confidence?: number };
  confidence: { overall: number; details?: Record<string, unknown> };
  assumptions: Array<Record<string, any>>;
  missing_specs: Array<Record<string, any>>;
  source_attribution: Record<string, string[]>;
  line_summaries: QuoteLineSummary[];
}

function downloadBlob(blob: Blob, filename: string) {
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
}

function formatSummary(summary?: Record<string, any>) {
  if (!summary) return '-';
  return Object.entries(summary)
    .filter(([, value]) => value !== null && value !== undefined && value !== '' && (!Array.isArray(value) || value.length > 0))
    .map(([key, value]) => {
      const label = key.replace(/_/g, ' ');
      const display = Array.isArray(value) ? value.join(', ') : String(value);
      return `${label}: ${display}`;
    })
    .join(' | ') || '-';
}

function lineSourceTrail(line: QuoteLineSummary) {
  const priority = [
    'drawing_pdf',
    'flat_pattern_dxf',
    'bom',
    'material',
    'thickness',
    'geometry',
    'finish',
    'features',
  ];
  return priority
    .flatMap((key) => (line.sources?.[key] || []).map((ref) => `${key.replace(/_/g, ' ')}: ${ref}`))
    .slice(0, 4);
}

function lineTypeLabel(type?: string) {
  const normalized = (type || 'manufactured').replace(/_/g, ' ');
  return normalized.charAt(0).toUpperCase() + normalized.slice(1);
}

function lineTypeClass(type?: string) {
  switch (type) {
    case 'assembly':
      return 'bg-blue-500/20 text-blue-300';
    case 'manufactured':
      return 'bg-emerald-500/20 text-emerald-300';
    case 'hardware':
    case 'consumable':
    case 'purchased':
      return 'bg-amber-500/20 text-amber-300';
    case 'process':
      return 'bg-cyan-500/20 text-cyan-300';
    case 'reference':
      return 'bg-slate-700 text-slate-300';
    default:
      return 'bg-slate-800 text-slate-300';
  }
}

export default function RFQQuoting() {
  const navigate = useNavigate();
  const [customers, setCustomers] = useState<CustomerOption[]>([]);
  const [selectedCustomerId, setSelectedCustomerId] = useState<number | ''>('');
  const [customerName, setCustomerName] = useState('');
  const [rfqReference, setRfqReference] = useState('');
  const [notes, setNotes] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [targetMargin, setTargetMargin] = useState(22);
  const [validDays, setValidDays] = useState(30);
  const [packageData, setPackageData] = useState<RfqPackageResponse | null>(null);
  const [estimate, setEstimate] = useState<EstimateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const loadCustomers = async () => {
      try {
        const data = await api.getCustomers(true);
        setCustomers(data || []);
      } catch (err) {
        console.error('Failed to load customers', err);
      }
    };
    loadCustomers();
  }, []);

  const uploadPackage = async () => {
    if (files.length === 0) {
      setError('Select at least one RFQ file.');
      return;
    }
    setLoading(true);
    setError('');
    try {
      const formData = new FormData();
      if (selectedCustomerId !== '') formData.append('customer_id', String(selectedCustomerId));
      if (customerName.trim()) formData.append('customer_name', customerName.trim());
      if (rfqReference.trim()) formData.append('rfq_reference', rfqReference.trim());
      if (notes.trim()) formData.append('notes', notes.trim());
      files.forEach((file) => formData.append('files', file));
      const response = await api.createRfqPackage(formData);
      setPackageData(response);
      setEstimate(null);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to upload RFQ package.');
    } finally {
      setLoading(false);
    }
  };

  const generateEstimate = async () => {
    if (!packageData) return;
    setLoading(true);
    setError('');
    try {
      const response = await api.generateRfqEstimate(packageData.id, {
        target_margin_pct: targetMargin,
        valid_days: validDays,
      });
      setEstimate(response);
      const refreshed = await api.getRfqPackage(packageData.id);
      setPackageData(refreshed);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Estimate generation failed.');
    } finally {
      setLoading(false);
    }
  };

  const approveEstimate = async () => {
    if (!packageData) return;
    setLoading(true);
    setError('');
    try {
      const result = await api.approveRfqEstimate(packageData.id);
      if (result?.quote_id) {
        navigate('/quotes');
      }
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Failed to approve estimate.');
    } finally {
      setLoading(false);
    }
  };

  const exportInternalEstimate = async () => {
    if (!packageData) return;
    try {
      const blob = await api.exportInternalEstimate(packageData.id);
      const fileName = `${packageData.rfq_number || 'rfq'}_internal_estimate.json`;
      downloadBlob(blob, fileName);
    } catch {
      setError('Failed to export internal estimate.');
    }
  };

  const generateCustomerPdf = async () => {
    if (!estimate?.quote_id) return;
    try {
      const blob = await api.generateCustomerQuotePdf(estimate.quote_id);
      downloadBlob(blob, `${estimate.quote_number}.pdf`);
    } catch {
      setError('Failed to generate customer quote PDF.');
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">AI Quoting Agent (Sheet Metal)</h1>
          <p className="text-sm text-slate-400 mt-1">
            Upload BOM spreadsheets, assembly PDFs, STEP models, and matching flat-pattern DXFs, then review traced quote inputs before publishing.
          </p>
        </div>
      </div>

      {error && <div className="rounded-sm border border-fd-red/30 bg-fd-red/10 text-fd-red px-4 py-3 text-sm">{error}</div>}

      <div className="bg-fd-panel border border-fd-line rounded-sm p-3 space-y-4">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <DocumentArrowUpIcon className="h-5 w-5 text-werco-navy-600" />
          New RFQ Package
        </h2>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="label">Customer</label>
            <select
              value={selectedCustomerId}
              onChange={(e) => setSelectedCustomerId(e.target.value ? parseInt(e.target.value, 10) : '')}
              className="input"
            >
              <option value="">Select customer...</option>
              {customers.map((customer) => (
                <option key={customer.id} value={customer.id}>
                  {customer.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">Customer Name Override</label>
            <input
              className="input"
              value={customerName}
              onChange={(e) => setCustomerName(e.target.value)}
              placeholder="Optional"
            />
          </div>
          <div>
            <label className="label">RFQ Reference</label>
            <input
              className="input"
              value={rfqReference}
              onChange={(e) => setRfqReference(e.target.value)}
              placeholder="RFQ-12345"
            />
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label className="label">Target Margin (%)</label>
            <input
              className="input"
              type="number"
              value={targetMargin}
              min={0}
              step={0.5}
              onChange={(e) => setTargetMargin(parseFloat(e.target.value) || 0)}
            />
          </div>
          <div>
            <label className="label">Quote Valid Days</label>
            <input
              className="input"
              type="number"
              value={validDays}
              min={1}
              onChange={(e) => setValidDays(parseInt(e.target.value, 10) || 30)}
            />
          </div>
          <div>
            <label className="label">Files</label>
            <input
              className="input"
              type="file"
              multiple
              accept=".pdf,.xlsx,.xls,.dxf,.step,.stp"
              onChange={(e) => setFiles(Array.from(e.target.files || []))}
            />
          </div>
        </div>

        <div>
          <label className="label">Notes</label>
          <textarea className="input" rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
        </div>

        <div className="flex flex-wrap gap-3">
          <button onClick={uploadPackage} className="btn-primary" disabled={loading}>
            Upload RFQ Package
          </button>
          <button onClick={generateEstimate} className="btn-secondary" disabled={loading || !packageData}>
            <SparklesIcon className="h-4 w-4 mr-2 inline" />
            Generate AI Estimate
          </button>
        </div>

        {packageData && (
          <div className="text-sm text-slate-400 border-t border-fd-line pt-3 space-y-2">
            <p>
              Package <span className="font-semibold text-white">{packageData.rfq_number}</span> with {packageData.file_count} files is ready.
            </p>
            {packageData.warnings?.length > 0 && (
              <div className="rounded-sm bg-fd-amber/10 border border-fd-amber/30 px-3 py-2 text-fd-amber">
                {packageData.warnings.map((warning, idx) => (
                  <p key={`warning-${idx}`}>- {warning}</p>
                ))}
              </div>
            )}
            {packageData.files?.length > 0 && (
              <div className="rounded-sm border border-fd-line overflow-x-auto">
                <table className="min-w-full text-xs">
                  <thead className="bg-fd-sunken text-slate-400">
                    <tr>
                      <th className="text-left px-3 py-2">File</th>
                      <th className="text-left px-3 py-2">Type</th>
                      <th className="text-left px-3 py-2">Parse Status</th>
                      <th className="text-left px-3 py-2">Notes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {packageData.files.map((file, idx) => (
                      <tr key={`${file.name}-${idx}`} className="border-t border-fd-line">
                        <td className="px-3 py-2">{file.name}</td>
                        <td className="px-3 py-2">{file.extension || '-'}</td>
                        <td className="px-3 py-2">
                          <span
                            className={`px-2 py-0.5 rounded-sm ${
                              file.parse_status === 'error'
                                ? 'bg-fd-red/20 text-fd-red'
                                : file.parse_status?.includes('parsed')
                                  ? 'bg-fd-green/20 text-fd-green'
                                  : 'bg-fd-sunken text-slate-300'
                            }`}
                          >
                            {file.parse_status || 'pending'}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-slate-400">
                          {file.parse_error || formatSummary(file.summary)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </div>

      {estimate && (
        <div className="bg-fd-panel border border-fd-line rounded-sm p-3 space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-lg font-semibold text-white">
              Estimate Review ({estimate.quote_number})
            </h2>
            <div className="text-sm text-slate-400">
              Lead Time: <span className="font-medium text-white">{estimate.lead_time.label || 'TBD'}</span>
            </div>
          </div>

          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
            <div className="rounded-sm bg-fd-sunken border border-fd-line p-3">
              <p className="text-xs text-slate-400">Material</p>
              <p className="font-semibold tabular-nums">${(estimate.totals.material || 0).toFixed(2)}</p>
            </div>
            <div className="rounded-sm bg-fd-sunken border border-fd-line p-3">
              <p className="text-xs text-slate-400">Hardware+Consumables</p>
              <p className="font-semibold tabular-nums">${(estimate.totals.hardware_consumables || 0).toFixed(2)}</p>
            </div>
            <div className="rounded-sm bg-fd-sunken border border-fd-line p-3">
              <p className="text-xs text-slate-400">Outside Services</p>
              <p className="font-semibold tabular-nums">${(estimate.totals.outside_services || 0).toFixed(2)}</p>
            </div>
            <div className="rounded-sm bg-fd-sunken border border-fd-line p-3">
              <p className="text-xs text-slate-400">Shop Labor+OH</p>
              <p className="font-semibold tabular-nums">${(estimate.totals.shop_labor_oh || 0).toFixed(2)}</p>
            </div>
            <div className="rounded-sm bg-fd-sunken border border-fd-line p-3">
              <p className="text-xs text-slate-400">Margin</p>
              <p className="font-semibold tabular-nums">${(estimate.totals.margin || 0).toFixed(2)}</p>
            </div>
            <div className="rounded-sm bg-fd-blue/10 p-3 border border-fd-blue/30">
              <p className="text-xs text-fd-blue">Grand Total</p>
              <p className="font-bold text-fd-blue tabular-nums">${(estimate.totals.grand_total || 0).toFixed(2)}</p>
            </div>
          </div>

          <div className="overflow-x-auto border border-fd-line rounded-sm">
            <table className="min-w-full text-sm">
              <thead className="bg-fd-sunken text-slate-400">
                <tr>
                  <th className="text-left px-3 py-2">Part</th>
                  <th className="text-left px-3 py-2">Type</th>
                  <th className="text-right px-3 py-2">Qty</th>
                  <th className="text-right px-3 py-2">Qty/Asm</th>
                  <th className="text-left px-3 py-2">Material</th>
                  <th className="text-left px-3 py-2">Thickness</th>
                  <th className="text-right px-3 py-2">Area in^2</th>
                  <th className="text-right px-3 py-2">Cut Len in</th>
                  <th className="text-right px-3 py-2">Bends</th>
                  <th className="text-left px-3 py-2">Finish</th>
                  <th className="text-right px-3 py-2">Total</th>
                </tr>
              </thead>
              <tbody>
                {estimate.line_summaries.map((line, idx) => {
                  const sourceTrail = lineSourceTrail(line);
                  return (
                    <tr key={`${line.part_number || line.part_name}-${idx}`} className="border-t border-fd-line">
                      <td className="px-3 py-2 min-w-[300px]" style={{ paddingLeft: `${12 + (line.bom_level || 0) * 18}px` }}>
                        <div className="font-medium text-white">{line.part_number || line.part_name}</div>
                        <div className="text-xs text-slate-400">{line.part_name}</div>
                        {line.parent_part_number && <div className="text-[11px] text-slate-500">Parent: {line.parent_part_number}</div>}
                        {line.notes && <div className="text-xs text-slate-500 mt-1">{line.notes}</div>}
                        {sourceTrail.length > 0 && (
                          <div className="text-[11px] leading-4 text-slate-500 mt-1 max-w-md">
                            {sourceTrail.join(' | ')}
                          </div>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <span className={`px-2 py-0.5 rounded-sm text-xs whitespace-nowrap ${lineTypeClass(line.line_type)}`}>
                          {line.item_number ? `${line.item_number} · ` : ''}{lineTypeLabel(line.line_type)}
                        </span>
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">{line.quantity}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{line.quantity_per_assembly ?? '-'}</td>
                      <td className="px-3 py-2">{line.material || 'TBD'}</td>
                      <td className="px-3 py-2">{line.thickness || 'TBD'}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{line.flat_area ? line.flat_area.toFixed(2) : '-'}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{line.cut_length ? line.cut_length.toFixed(2) : '-'}</td>
                      <td className="px-3 py-2 text-right tabular-nums">{line.bend_count ?? '-'}</td>
                      <td className="px-3 py-2">{line.finish || '-'}</td>
                      <td className="px-3 py-2 text-right font-semibold tabular-nums">${line.part_total.toFixed(2)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="rounded-sm border border-fd-line p-3">
              <h3 className="font-semibold text-white mb-2">Assumptions</h3>
              {estimate.assumptions.length === 0 && <p className="text-sm text-slate-400">No assumptions required.</p>}
              {estimate.assumptions.map((item, idx) => (
                <p key={idx} className="text-sm text-slate-300 mb-1">
                  - {item.field || 'item'}: {item.assumption || 'N/A'}
                </p>
              ))}
            </div>
            <div className="rounded-sm border border-fd-line p-3">
              <h3 className="font-semibold text-white mb-2">Missing / Needs Review</h3>
              {estimate.missing_specs.length === 0 && <p className="text-sm text-slate-400">No missing specs detected.</p>}
              {estimate.missing_specs.map((item, idx) => (
                <p key={idx} className="text-sm text-fd-amber mb-1">
                  - {item.part_id || 'part'}: {item.field} ({item.message})
                </p>
              ))}
            </div>
          </div>

          <div className="flex flex-wrap gap-3">
            <button onClick={approveEstimate} className="btn-primary" disabled={loading}>
              <CheckCircleIcon className="h-4 w-4 mr-2 inline" />
              Approve &amp; Create Quote
            </button>
            <button onClick={exportInternalEstimate} className="btn-secondary" disabled={loading}>
              <ArrowDownTrayIcon className="h-4 w-4 mr-2 inline" />
              Export Internal Estimate
            </button>
            <button onClick={generateCustomerPdf} className="btn-secondary" disabled={loading}>
              Generate Customer Quote PDF
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
