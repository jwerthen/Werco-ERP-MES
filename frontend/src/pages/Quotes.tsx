import { PageHeader } from '../components/ui/PageHeader';
import { DocumentDeliveryComposer } from '../components/DocumentDeliveryComposer';
import { useAuth } from '../context/AuthContext';
import React, { useEffect, useRef, useState } from 'react';
import api from '../services/api';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { formatCentralDate } from '../utils/centralTime';
import {
  PlusIcon,
  PaperAirplaneIcon,
  ArrowRightIcon,
  DocumentTextIcon,
} from '@heroicons/react/24/outline';
import { Modal } from '../components/ui/Modal';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { FormField } from '../components/ui/FormField';
import {
  LoadingButton,
  useToast,
  DataTable,
  DataTableColumn,
  StatusBadge,
  MobileDataCard,
  Button,
} from '../components/ui';

interface QuoteLine {
  id: number;
  line_number: number;
  part_id?: number;
  part_number?: string;
  description: string;
  quantity: number;
  unit_price: number;
  line_total: number;
  work_order_id?: number;
  work_order_number?: string;
  notes?: string;
  material_cost?: number;
  labor_hours?: number;
  labor_cost?: number;
}

interface Quote {
  id: number;
  quote_number: string;
  revision: string;
  customer_name: string;
  customer_contact?: string;
  customer_email?: string;
  customer_phone?: string;
  customer_po?: string;
  payment_terms?: string;
  notes?: string;
  internal_notes?: string;
  status: string;
  quote_date: string;
  updated_at?: string;
  valid_until?: string;
  subtotal: number;
  total: number;
  lead_time_days?: number;
  lines: QuoteLine[];
  work_order_id?: number;
  fabrication_quote_id?: number | null;
}

interface QuoteDraft {
  expected_updated_at?: string;
  customer_name: string;
  customer_contact: string;
  customer_email: string;
  customer_phone: string;
  customer_po?: string;
  valid_until?: string;
  lead_time_days: number;
  payment_terms: string;
  notes: string;
  internal_notes?: string;
  lines: Array<{
    id?: number;
    part_id: number;
    description: string;
    quantity: number;
    unit_price: number;
    labor_hours: number;
    material_cost?: number;
    labor_cost?: number;
    notes?: string;
  }>;
}
interface ConversionLine {
  line_id: number;
  line_number: number;
  part_number?: string;
  part_id?: number;
  description: string;
  quantity: number;
  work_order_id?: number;
  eligible: boolean;
  outcome: string;
}
const emptyDraft = (): QuoteDraft => ({
  customer_name: '',
  customer_contact: '',
  customer_email: '',
  customer_phone: '',
  lead_time_days: 14,
  payment_terms: 'Net 30',
  notes: '',
  lines: [],
});
const quoteStatuses = ['open', 'all', 'draft', 'pending', 'sent', 'accepted', 'rejected', 'converted', 'expired'];

export default function Quotes() {
  const navigate = useNavigate();
  const { user } = useAuth();
  const canEmail = !!user?.is_superuser || ['admin', 'manager', 'supervisor'].includes(user?.role || '');
  const [emailQuoteId, setEmailQuoteId] = useState<number | null>(null);
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [fabricationQuoteId, setFabricationQuoteId] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const [formError, setFormError] = useState('');
  const [sendingIds, setSendingIds] = useState<Set<number>>(new Set());
  const sendingRef = useRef(new Set<number>());
  const [conversionLines, setConversionLines] = useState<ConversionLine[]>([]);
  const [conversionIds, setConversionIds] = useState<number[]>([]);
  const [conversionLoading, setConversionLoading] = useState(false);
  const [conversionError, setConversionError] = useState('');
  const [acknowledgeUnlinked, setAcknowledgeUnlinked] = useState(false);
  const conversionRequest = useRef(0);
  const convertPendingRef = useRef(false);
  const [convertTarget, setConvertTarget] = useState<number | null>(null);
  const [convertPending, setConvertPending] = useState(false);
  const [selectedQuote, setSelectedQuote] = useState<Quote | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(false);
  const requestedDetailId = useRef<number | null>(null);

  const [newQuote, setNewQuote] = useState<QuoteDraft>(emptyDraft);
  const [initialDraft, setInitialDraft] = useState(() => JSON.stringify(emptyDraft()));
  const { confirmDiscard, markSaved } = useUnsavedChanges(showCreateModal && JSON.stringify(newQuote) !== initialDraft);
  const listRequest = useRef(0);
  const detailRequest = useRef(0);
  const status = quoteStatuses.includes(searchParams.get('status') || '') ? searchParams.get('status')! : 'open';
  const query = searchParams.get('q') || '';
  const updateFilter = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    setSearchParams(next, { replace: key === 'q' });
  };
  useEffect(() => {
    void loadData();
  }, [status, query]);
  const closeEditor = () => {
    if (savingRef.current || !confirmDiscard()) return;
    setShowCreateModal(false);
  };
  const openEditor = (quote: Quote) => {
    const draft = quote
      ? {
          ...emptyDraft(),
          expected_updated_at: quote.updated_at,
          customer_name: quote.customer_name,
          customer_contact: quote.customer_contact || '',
          customer_email: quote.customer_email || '',
          customer_phone: quote.customer_phone || '',
          lead_time_days: quote.lead_time_days ?? 14,
          customer_po: quote.customer_po || '',
          valid_until: quote.valid_until?.slice(0, 10) || '',
          payment_terms: quote.payment_terms || '',
          notes: quote.notes || '',
          internal_notes: quote.internal_notes,
          lines: quote.lines.map(line => ({ ...line, part_id: line.part_id || 0, labor_hours: line.labor_hours || 0 })),
        }
      : emptyDraft();
    setNewQuote(draft);
    setInitialDraft(JSON.stringify(draft));
    setEditId(quote.id);
    setFabricationQuoteId(quote.fabrication_quote_id || null);
    setFormError('');
    setShowCreateModal(true);
  };

  // Once-per-id latch for the `?id=` deep-link fallback fetch below. Required,
  // not cosmetic: `quotes` is in the effect's dependency array.
  const deepLinkedQuoteIdRef = useRef<number | null>(null);

  useEffect(() => {
    const requestedId = Number(searchParams.get('id') || 0);
    if (requestedDetailId.current !== requestedId) {
      requestedDetailId.current = requestedId;
      detailRequest.current += 1;
      setSelectedQuote(null);
      setDetailError(false);
      setDetailLoading(false);
    }
    if (!requestedId) {
      detailRequest.current += 1;
      deepLinkedQuoteIdRef.current = null;
      setSelectedQuote(null);
      return;
    }
    const quote = quotes.find(q => q.id === requestedId);
    if (quote) {
      detailRequest.current += 1;
      setSelectedQuote(quote);
      return;
    }
    // Not in the list: list_quotes caps at 100 and excludes CONVERTED/EXPIRED,
    // so a `quote.expiring` notification clicked after the quote actually
    // expired would otherwise land here and silently do nothing.
    if (loading || loadError || deepLinkedQuoteIdRef.current === requestedId) return;
    deepLinkedQuoteIdRef.current = requestedId;
    void loadDeepLinkedQuote(requestedId);
  }, [quotes, searchParams, loading, loadError]);

  /**
   * `GET /quotes/{id}` returns the same QuoteResponse shape the list returns,
   * so the row satisfies the local Quote interface with no field mapping. The
   * detail panel renders from `selectedQuote` alone, so it displays correctly
   * even though the row is not in the list to highlight.
   */
  const loadDeepLinkedQuote = async (quoteId: number) => {
    const request = ++detailRequest.current;
    setDetailLoading(true);
    setDetailError(false);
    try {
      const detail: Quote = await api.getQuote(quoteId);
      if (request === detailRequest.current) setSelectedQuote(detail);
    } catch (err) {
      console.error('Failed to load deep-linked quote:', err);
      if (request === detailRequest.current) {
        setDetailError(true);
        showToast('error', 'Quote could not be loaded. Check access or try again.');
      }
    } finally {
      if (request === detailRequest.current) setDetailLoading(false);
    }
  };

  const loadData = async () => {
    const request = ++listRequest.current;
    setLoading(true);
    setLoadError(false);
    try {
      const rows = await api.getQuotes({ status: status === 'open' ? undefined : status, search: query || undefined });
      if (request === listRequest.current) setQuotes(rows);
    } catch {
      if (request === listRequest.current) setLoadError(true);
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  };

  const handleCreate = async (e: React.FormEvent) => {
    e.preventDefault();
    if (savingRef.current || !editId) return;
    if (
      !newQuote.customer_name.trim() ||
      newQuote.lines.length === 0 ||
      newQuote.lines.some(
        line =>
          !line.description.trim() ||
          !Number.isFinite(line.quantity) ||
          line.quantity <= 0 ||
          !Number.isFinite(line.unit_price) ||
          line.unit_price < 0
      )
    ) {
      setFormError('Enter a customer and at least one line with a description, positive quantity and valid price.');
      return;
    }
    savingRef.current = true;
    setSaving(true);
    setFormError('');
    try {
      const commercialFields = {
        expected_updated_at: newQuote.expected_updated_at,
        customer_name: newQuote.customer_name,
        customer_contact: newQuote.customer_contact,
        customer_email: newQuote.customer_email,
        customer_phone: newQuote.customer_phone,
        customer_po: newQuote.customer_po,
        lead_time_days: newQuote.lead_time_days,
        payment_terms: newQuote.payment_terms,
        notes: newQuote.notes,
        internal_notes: newQuote.internal_notes,
      };
      const result = await api.updateQuote(editId, {
        ...commercialFields,
        valid_until: newQuote.valid_until || null,
      });
      // Mark clean before navigation; a successful save must not trigger the leave guard.
      markSaved();
      setInitialDraft(JSON.stringify(newQuote));
      setShowCreateModal(false);
      setEditId(null);
      showToast('success', 'Quote draft updated');
      selectQuote(result);
      void loadData();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      setFormError(
        typeof detail === 'string'
          ? detail
          : detail?.message || 'Could not save quote. Your entries have been kept. Refresh the quote before retrying if another editor has changed it.'
      );
      if (err.response?.status === 409) void loadData();
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  const handleSend = async (quoteId: number) => {
    if (sendingRef.current.has(quoteId)) return;
    sendingRef.current.add(quoteId);
    setSendingIds(new Set(sendingRef.current));
    try {
      await api.sendQuote(quoteId);
      showToast('success', 'Quote marked as sent. No customer message was sent by this action.');
      void loadData();
    } catch (err: any) {
      showToast('error', err.response?.data?.detail || 'Failed to mark quote as sent');
    } finally {
      sendingRef.current.delete(quoteId);
      setSendingIds(new Set(sendingRef.current));
    }
  };

  const handleConvert = async (quoteId: number) => {
    const request = ++conversionRequest.current;
    setConvertTarget(quoteId);
    setConversionLines([]);
    setConversionIds([]);
    setAcknowledgeUnlinked(false);
    setConversionLoading(true);
    setConversionError('');
    try {
      const plan = await api.getQuoteConversionPlan(quoteId);
      if (request !== conversionRequest.current) return;
      setConversionLines(plan.lines);
      setConversionIds(
        plan.lines.filter((line: ConversionLine) => line.eligible).map((line: ConversionLine) => line.line_id)
      );
    } catch {
      if (request === conversionRequest.current)
        setConversionError('Could not load the conversion plan. Retry before converting.');
    } finally {
      if (request === conversionRequest.current) setConversionLoading(false);
    }
  };

  const handleConfirmConvert = async () => {
    if (convertTarget === null || convertPendingRef.current || !conversionIds.length || conversionLoading) return;
    convertPendingRef.current = true;
    setConvertPending(true);
    setConversionError('');
    try {
      const result = await api.convertQuote(convertTarget, {
        line_ids: conversionIds,
        acknowledge_unlinked: acknowledgeUnlinked,
      });
      showToast(
        'success',
        result.work_orders?.length > 1
          ? `${result.work_orders.length} work orders created`
          : `Work Order ${result.work_order_number} created!`
      );
      setConvertTarget(null);
      void loadData();
      // Conversion has committed. A subsequent refresh failure must not invite
      // the user to retry the already successful mutation.
      try {
        const detail = await api.getQuote(convertTarget);
        selectQuote(detail);
      } catch {
        showToast('warning', 'Work orders were created. Refresh the quote to see its production links.');
      }
    } catch (err: any) {
      setConversionError(
        typeof err.response?.data?.detail === 'string'
          ? err.response.data.detail
          : 'Could not convert quote. Review the current lines and retry.'
      );
    } finally {
      convertPendingRef.current = false;
      setConvertPending(false);
    }
  };

  const clearSelectedQuote = () => {
    setSelectedQuote(null);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete('id');
    setSearchParams(nextParams);
  };

  const selectQuote = (quote: Quote) => {
    // A fresh mutation/detail response must also replace its cached list row;
    // the URL-selection effect reads that cache on the next render.
    setQuotes(previous => previous.map(row => (row.id === quote.id ? quote : row)));
    setSelectedQuote(quote);
    const nextParams = new URLSearchParams(searchParams);
    nextParams.set('id', String(quote.id));
    setSearchParams(nextParams);
  };

  const calculateTotal = () => {
    return newQuote.lines.reduce((sum, line) => sum + line.quantity * line.unit_price, 0);
  };

  // Row actions (Send / Convert / WO-created) — shared by table + mobile cards.
  const renderRowActions = (q: Quote) => (
    <div className="flex flex-wrap justify-center gap-2">
      {canEmail && <button type="button" className="text-fd-link hover:underline text-sm" aria-label={`Email ${q.quote_number}`} onClick={event => { event.stopPropagation(); setEmailQuoteId(q.id); }}>Email</button>}
      {['draft', 'pending'].includes(q.status) && (
        <button
          onClick={e => {
            e.stopPropagation();
            handleSend(q.id);
          }}
          className="text-fd-link hover:text-blue-200"
          title="Mark as sent (no email is sent)"
          aria-label="Mark as sent"
          disabled={sendingIds.has(q.id)}
        >
          <PaperAirplaneIcon className="h-5 w-5" aria-hidden="true" />
        </button>
      )}
      {(q.status === 'sent' || q.status === 'accepted') &&
        (q.lines.some(line => line.part_id && !line.work_order_id) || !q.work_order_id) && (
          <button
            onClick={e => {
              e.stopPropagation();
              handleConvert(q.id);
            }}
            className="text-green-600 hover:text-green-300"
            title="Convert to Work Order"
            aria-label="Convert to Work Order"
          >
            <ArrowRightIcon className="h-5 w-5" aria-hidden="true" />
          </button>
        )}
      {q.work_order_id && (
        <Link
          onClick={e => e.stopPropagation()}
          to={`/work-orders/${q.work_order_id}`}
          className="text-sm text-fd-link"
        >
          View work order
        </Link>
      )}
    </div>
  );

  const columns: DataTableColumn<Quote>[] = [
    {
      key: 'quote_number',
      header: 'Quote #',
      sortable: true,
      accessor: q => q.quote_number,
      csv: q => `${q.quote_number} Rev ${q.revision}`,
      render: q => (
        <>
          <span className="font-medium text-fd-link">{q.quote_number}</span>
          <span className="text-slate-400 text-sm ml-1">Rev {q.revision}</span>
        </>
      ),
    },
    {
      key: 'customer_name',
      header: 'Customer',
      sortable: true,
      accessor: q => q.customer_name,
      render: q => (
        <div>
          <div className="font-medium">{q.customer_name}</div>
          {q.customer_contact && <div className="text-sm text-slate-400">{q.customer_contact}</div>}
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sortable: true,
      accessor: q => q.status,
      render: q => <StatusBadge status={q.status} />,
    },
    {
      key: 'quote_date',
      header: 'Date',
      sortable: true,
      accessor: q => q.quote_date,
      csv: q => formatCentralDate(q.quote_date),
      render: q => formatCentralDate(q.quote_date),
    },
    {
      key: 'valid_until',
      header: 'Valid Until',
      sortable: true,
      accessor: q => q.valid_until ?? '',
      csv: q => (q.valid_until ? formatCentralDate(q.valid_until) : ''),
      render: q => (q.valid_until ? formatCentralDate(q.valid_until) : '-'),
    },
    {
      key: 'total',
      header: 'Total',
      sortable: true,
      align: 'right',
      accessor: q => q.total,
      csv: q => q.total,
      render: q => (
        <span className="font-medium">${q.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}</span>
      ),
    },
    {
      key: 'lines',
      header: 'Lines',
      sortable: true,
      align: 'center',
      accessor: q => q.lines.length,
      render: q => q.lines.length,
    },
    {
      key: 'actions',
      header: 'Actions',
      align: 'center',
      render: renderRowActions,
    },
  ];

  const renderMobileCard = (q: Quote) => (
    <MobileDataCard
      title={`${q.quote_number} Rev ${q.revision}`}
      subtitle={q.customer_contact ? `${q.customer_name} • ${q.customer_contact}` : q.customer_name}
      badge={<StatusBadge status={q.status} />}
      highlight={selectedQuote?.id === q.id}
      onClick={() => selectQuote(q)}
      fields={[
        { label: 'Date', value: formatCentralDate(q.quote_date) },
        { label: 'Valid Until', value: q.valid_until ? formatCentralDate(q.valid_until) : '-' },
        {
          label: 'Total',
          value: `$${q.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}`,
        },
        { label: 'Lines', value: q.lines.length },
      ]}
      actions={renderRowActions(q)}
    />
  );

  return (
    <div className="space-y-6">
      <PageHeader title="Customer Quotes" description="Review approved estimates, prepare customer documents and follow quotes into production." actions={<>
          <Button onClick={() => navigate('/fabrication-quotes')} className="flex items-center">
            <PlusIcon className="h-5 w-5 mr-2" />
            New Quote
          </Button>
      </>} />

      <div className="flex flex-wrap gap-3">
        <input
          aria-label="Search quotes"
          className="input min-w-0 flex-1"
          placeholder="Quote number or customer…"
          value={query}
          onChange={e => updateFilter('q', e.target.value)}
        />
        <select
          aria-label="Quote status"
          className="input w-auto"
          value={status}
          onChange={e => updateFilter('status', e.target.value)}
        >
          {quoteStatuses.map(value => (
            <option key={value} value={value}>
              {value === 'open'
                ? 'Open quotes'
                : value === 'all'
                  ? 'All quote history'
                  : value[0].toUpperCase() + value.slice(1)}
            </option>
          ))}
        </select>
      </div>
      {detailLoading && <p role="status">Loading quote details…</p>}
      {detailError && (
        <div role="alert" className="text-red-300">
          Quote detail is unavailable.{' '}
          <button className="underline" onClick={() => loadDeepLinkedQuote(Number(searchParams.get('id')))}>
            Retry detail
          </button>
        </div>
      )}
      {/* Quotes Table */}
      <div className="card">
        {selectedQuote && (
          <div className="mb-4 rounded-xl border border-werco-500/30 bg-werco-500/10 p-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <p className="text-sm text-slate-400">Selected quote</p>
                <h2 className="text-lg font-semibold text-slate-100">
                  {selectedQuote.quote_number} Rev {selectedQuote.revision}
                </h2>
                <p className="text-sm text-slate-300">{selectedQuote.customer_name}</p>
              </div>
              <div className="text-left sm:text-right">
                <p className="text-xl font-bold text-slate-100">
                  ${selectedQuote.total.toLocaleString(undefined, { minimumFractionDigits: 2 })}
                </p>
                <button onClick={clearSelectedQuote} className="mt-2 text-sm text-werco-300 hover:text-werco-200">
                  Clear selection
                </button>
              </div>
            </div>
            <div className="mt-4 space-y-4">
              <div className="flex flex-wrap gap-3 items-center">
                <StatusBadge status={selectedQuote.status} />
                {['draft', 'pending'].includes(selectedQuote.status) && (
                  <Button variant="secondary" onClick={() => openEditor(selectedQuote)}>
                    Edit draft
                  </Button>
                )}
                {renderRowActions(selectedQuote)}
              </div>
              {selectedQuote.status === 'pending' && (
                <p className="text-sm text-slate-300">
                  Estimate approved. Review the quote lines and terms, then send it using your usual channel and mark it
                  as sent here.
                </p>
              )}
              <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
                <div>
                  <dt className="text-slate-400">Contact</dt>
                  <dd>
                    {selectedQuote.customer_contact || '—'} {selectedQuote.customer_email || ''}
                  </dd>
                </div>
                <div>
                  <dt className="text-slate-400">Payment terms</dt>
                  <dd>{selectedQuote.payment_terms || '—'}</dd>
                </div>
                <div>
                  <dt className="text-slate-400">Lead time</dt>
                  <dd>{selectedQuote.lead_time_days == null ? '—' : `${selectedQuote.lead_time_days} days`}</dd>
                </div>
              </dl>
              <div className="overflow-x-auto">
                <table className="min-w-full text-sm">
                  <caption className="text-left font-semibold pb-2">Quote lines ({selectedQuote.lines.length})</caption>
                  <thead>
                    <tr>
                      {['Line', 'Part / description', 'Quantity', 'Unit price', 'Total', 'Production'].map(label => (
                        <th key={label} className="text-left px-3 py-2">
                          {label}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {selectedQuote.lines.map(line => (
                      <tr key={line.id} className="border-t border-fd-line">
                        <td className="p-3">{line.line_number}</td>
                        <td className="p-3">
                          <div>{line.part_number || 'Custom item'}</div>
                          <div>{line.description}</div>
                          {line.notes && <p className="text-slate-400 whitespace-pre-wrap">{line.notes}</p>}
                        </td>
                        <td className="p-3 tabular-nums">{line.quantity.toLocaleString()}</td>
                        <td className="p-3 tabular-nums">${line.unit_price.toFixed(2)}</td>
                        <td className="p-3 tabular-nums">${line.line_total.toFixed(2)}</td>
                        <td className="p-3">
                          {line.work_order_id ? (
                            <Link className="text-fd-link" to={`/work-orders/${line.work_order_id}`}>
                              {line.work_order_number || `Work order ${line.work_order_id}`}
                            </Link>
                          ) : line.part_id ? (
                            'Not converted'
                          ) : (
                            'Quote only'
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {selectedQuote.notes && (
                <div>
                  <h3 className="font-semibold">Notes</h3>
                  <p className="whitespace-pre-wrap text-sm">{selectedQuote.notes}</p>
                </div>
              )}
              {selectedQuote.internal_notes && (
                <details>
                  <summary className="cursor-pointer text-sm text-fd-link">
                    Internal estimate / calculation record
                  </summary>
                  <pre className="whitespace-pre-wrap break-words text-xs p-3">{selectedQuote.internal_notes}</pre>
                </details>
              )}
              {selectedQuote.work_order_id && !selectedQuote.lines.some(line => line.work_order_id) && (
                <p className="text-sm text-slate-400">
                  Historical conversion: the original work-order link is retained; per-line mapping was not recorded.
                </p>
              )}
            </div>
          </div>
        )}
        <DataTable<Quote>
          columns={columns}
          data={quotes}
          rowKey={q => q.id}
          onRowClick={selectQuote}
          loading={loading}
          error={loadError}
          onRetry={loadData}
          defaultSort={{ key: 'quote_date', dir: 'desc' }}
          pageSize={25}
          csvExport={{ filename: 'quotes' }}
          mobileCards={renderMobileCard}
          empty={{
            icon: DocumentTextIcon,
            title: query ? 'No matching quotes' : status === 'open' ? 'No open quotes' : 'No quotes in this view',
            description: 'Build and approve a fabrication estimate, then hand it off for customer review.',
            action: { label: 'New Quote', onClick: () => navigate('/fabrication-quotes') },
          }}
        />
      </div>

      {emailQuoteId && <DocumentDeliveryComposer key={emailQuoteId} entityType="quote" entityId={emailQuoteId} canReconcile={!!user?.is_superuser || ['admin', 'manager'].includes(user?.role || '')} onClose={() => setEmailQuoteId(null)} onAccepted={() => void loadData()} />}
      {/* Customer quote review */}
      <Modal
        open={showCreateModal}
        onClose={closeEditor}
        size="3xl"
        closeOnBackdrop={false}
        closeOnEscape={!saving}
        ariaLabelledBy="quote-editor-title"
      >
        <h3 id="quote-editor-title" className="text-lg font-semibold mb-4">
          Edit quote draft
        </h3>
        <p className="text-sm text-slate-300 mb-4">
          Pricing and quantities are preserved with this customer quote.{' '}
          <Link className="text-fd-link underline" to={fabricationQuoteId ? `/fabrication-quotes?id=${fabricationQuoteId}` : '/fabrication-quotes'}>
            {fabricationQuoteId ? 'Open fabrication estimate to revise' : 'Create a fabrication estimate for new pricing'}
          </Link>.
        </p>
        <form onSubmit={handleCreate} className="space-y-4">
          <fieldset disabled={saving} className="min-w-0 space-y-4">
            {formError && (
              <p role="alert" className="text-red-300 p-3 border border-red-500/30 rounded">
                {formError}
              </p>
            )}
            {/* Customer Info */}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <FormField label="Customer Name" required>
                {field => (
                  <input
                    {...field}
                    type="text"
                    value={newQuote.customer_name}
                    onChange={e => setNewQuote({ ...newQuote, customer_name: e.target.value })}
                    className="input"
                    required
                  />
                )}
              </FormField>
              <FormField label="Contact">
                {field => (
                  <input
                    {...field}
                    type="text"
                    value={newQuote.customer_contact}
                    onChange={e => setNewQuote({ ...newQuote, customer_contact: e.target.value })}
                    className="input"
                  />
                )}
              </FormField>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <FormField label="Email">
                {field => (
                  <input
                    {...field}
                    type="email"
                    value={newQuote.customer_email}
                    onChange={e => setNewQuote({ ...newQuote, customer_email: e.target.value })}
                    className="input"
                  />
                )}
              </FormField>
              <FormField label="Customer Phone">
                {field => (
                  <input
                    {...field}
                    type="tel"
                    className="input"
                    value={newQuote.customer_phone}
                    onChange={event => setNewQuote({ ...newQuote, customer_phone: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Customer PO">
                {field => (
                  <input
                    {...field}
                    className="input"
                    value={newQuote.customer_po || ''}
                    onChange={event => setNewQuote({ ...newQuote, customer_po: event.target.value })}
                  />
                )}
              </FormField>
              <FormField label="Valid Until">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={newQuote.valid_until || ''}
                    onChange={e => setNewQuote({ ...newQuote, valid_until: e.target.value })}
                    className="input"
                  />
                )}
              </FormField>
              <FormField label="Lead Time (days)">
                {field => (
                  <input
                    {...field}
                    type="number"
                    value={newQuote.lead_time_days}
                    onChange={e => setNewQuote({ ...newQuote, lead_time_days: parseInt(e.target.value) })}
                    className="input"
                  />
                )}
              </FormField>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full text-sm text-left">
                <caption className="text-left font-semibold mb-2">Quoted line items (read only)</caption>
                <thead><tr><th className="p-2">Description</th><th className="p-2">Quantity</th><th className="p-2">Unit price</th><th className="p-2">Total</th></tr></thead>
                <tbody>{newQuote.lines.map((line, index) => (
                  <tr key={line.id || index} className="border-t border-fd-line">
                    <td className="p-2">{line.description}</td>
                    <td className="p-2">{line.quantity.toLocaleString()}</td>
                    <td className="p-2">${line.unit_price.toFixed(2)}</td>
                    <td className="p-2">${(line.quantity * line.unit_price).toFixed(2)}</td>
                  </tr>
                ))}</tbody>
              </table>
              <p className="text-right font-semibold mt-3">Total: ${calculateTotal().toLocaleString(undefined, { minimumFractionDigits: 2 })}</p>
            </div>

            <FormField label="Payment terms">
              {field => (
                <input
                  {...field}
                  className="input"
                  value={newQuote.payment_terms}
                  onChange={e => setNewQuote({ ...newQuote, payment_terms: e.target.value })}
                />
              )}
            </FormField>
            <FormField label="Notes">
              {field => (
                <textarea
                  {...field}
                  value={newQuote.notes}
                  onChange={e => setNewQuote({ ...newQuote, notes: e.target.value })}
                  className="input"
                  rows={2}
                />
              )}
            </FormField>

            <div className="flex justify-end gap-3 pt-4 border-t">
              <Button type="button" variant="secondary" onClick={closeEditor} disabled={saving}>
                Cancel
              </Button>
              <LoadingButton type="submit" loading={saving}>
                Save draft
              </LoadingButton>
            </div>
          </fieldset>
        </form>
      </Modal>

      <Modal
        open={convertTarget !== null}
        onClose={() => {
          if (!convertPendingRef.current) {
            conversionRequest.current += 1;
            setConvertTarget(null);
          }
        }}
        size="3xl"
        ariaLabelledBy="conversion-title"
        closeOnBackdrop={!convertPending}
        closeOnEscape={!convertPending}
      >
        <h2 id="conversion-title" className="text-xl font-semibold">
          Review work-order conversion
        </h2>
        <p className="text-sm text-slate-300 my-3">
          Each selected production part line creates a separate draft work order with that line’s quantity. Review and
          add its routing operations before release. Unselected production lines remain available for later conversion.
        </p>
        {conversionLoading && <p role="status">Loading conversion plan…</p>}
        {!conversionLoading && !conversionError && !conversionLines.some(line => line.eligible) && (
          <p role="status" className="text-amber-200 mb-3">
            No lines are ready for production conversion. Review the reasons below; existing work-order links remain on
            the quote.
          </p>
        )}
        {conversionError && (
          <div role="alert" className="text-red-300">
            {conversionError}{' '}
            <button
              className="underline"
              onClick={() => convertTarget && handleConvert(convertTarget)}
              disabled={convertPending}
            >
              Reload plan
            </button>
          </div>
        )}
        <div className="space-y-2">
          {conversionLines.map(line => (
            <label key={line.line_id} className="flex items-start gap-3 border border-fd-line p-3 rounded">
              <input
                type="checkbox"
                aria-label={`Convert line ${line.line_number}`}
                disabled={!line.eligible || convertPending}
                checked={conversionIds.includes(line.line_id)}
                onChange={e =>
                  setConversionIds(ids =>
                    e.target.checked ? [...ids, line.line_id] : ids.filter(id => id !== line.line_id)
                  )
                }
              />
              <span>
                <span className="font-semibold">
                  Line {line.line_number}: {line.part_number || 'Custom item'} · Qty {line.quantity}
                </span>
                <span className="block text-sm">{line.description}</span>
                <span className="block text-xs text-slate-400">{line.outcome}</span>
              </span>
            </label>
          ))}
        </div>
        {conversionLines.some(line => !line.part_id) && (
          <label className="flex gap-3 my-4 text-sm">
            <input
              type="checkbox"
              checked={acknowledgeUnlinked}
              onChange={e => setAcknowledgeUnlinked(e.target.checked)}
              disabled={convertPending}
            />
            Custom/service items remain on the quote and do not create work orders.
          </label>
        )}
        <div className="flex justify-end gap-3 mt-5">
          <Button
            variant="secondary"
            disabled={convertPending}
            onClick={() => {
              conversionRequest.current += 1;
              setConvertTarget(null);
            }}
          >
            Cancel
          </Button>
          <LoadingButton
            loading={convertPending}
            disabled={
              conversionLoading ||
              !!conversionError ||
              !conversionIds.length ||
              (conversionLines.some(line => !line.part_id) && !acknowledgeUnlinked)
            }
            onClick={handleConfirmConvert}
          >
            Create {conversionIds.length} work order{conversionIds.length === 1 ? '' : 's'}
          </LoadingButton>
        </div>
      </Modal>
    </div>
  );
}
