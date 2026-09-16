import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { ArrowRight, Calculator, CheckCircle2, Download, FilePlus2, FolderOpen, Layers3, RefreshCw, Save, ShieldCheck } from 'lucide-react';
import api from '../services/api';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { PageHeader } from '../components/ui/PageHeader';
import { Button } from '../components/ui/Button';
import type { CustomerNameOption } from '../types/api';
import { fabricationQuoteApi } from '../features/fabrication-quote/api';
import { DecimalField, Empty, Field, SelectField, TextField } from '../features/fabrication-quote/components';
import { AssemblyEditor, HardwareEditor, MaterialsEditor, OperationsEditor } from '../features/fabrication-quote/PlanEditors';
import { CostSummary, ReviewPanel } from '../features/fabrication-quote/ReviewPanel';
import { downloadBlob, downloadJson, SourcesPanel } from '../features/fabrication-quote/SourcesPanel';
import { NestingPanel } from '../features/fabrication-quote/NestingPanel';
import { HistoryActualsPanel } from '../features/fabrication-quote/HistoryActualsPanel';
import { inputErrors, isEditable, mayApprove, readableError, snapshot, writeFromRecord } from '../features/fabrication-quote/model';
import { currencies, emptyPlan, newId } from '../features/fabrication-quote/types';
import type { CalculationResult, QuoteFile, QuotePlan, QuoteRecord, QuoteSummary, QuoteWrite } from '../features/fabrication-quote/types';
import '../features/fabrication-quote/fabrication-quote.css';

type Tab = 'assembly' | 'processes' | 'material' | 'hardware' | 'sources' | 'review' | 'history';
const tabs: { id: Tab; label: string; short: string }[] = [{ id: 'assembly', label: 'Assembly & demand', short: 'Assembly' }, { id: 'processes', label: 'Process route', short: 'Processes' }, { id: 'material', label: 'Material & nesting', short: 'Material' }, { id: 'hardware', label: 'Hardware & offers', short: 'Hardware' }, { id: 'sources', label: 'Source package', short: 'Sources' }, { id: 'review', label: 'Cost & review', short: 'Review' }, { id: 'history', label: 'History & actuals', short: 'History' }];
const libraryPageSize = 30;
const freshWrite = (): QuoteWrite => ({ title: '', customer_id: null, plan: emptyPlan() });

export default function FabricationQuoting() {
  const [params, setParams] = useSearchParams(); const initialId = useRef(Number(params.get('id')) || null);
  const [quotes, setQuotes] = useState<QuoteSummary[]>([]); const [customers, setCustomers] = useState<CustomerNameOption[]>([]);
  const [record, setRecord] = useState<QuoteRecord | null>(null); const [write, setWrite] = useState<QuoteWrite>(freshWrite);
  const [saved, setSaved] = useState(() => snapshot(freshWrite())); const [result, setResult] = useState<CalculationResult | null>(null); const [resultPlan, setResultPlan] = useState('');
  const [tab, setTab] = useState<Tab>('assembly'); const [busy, setBusy] = useState(false); const busyRef = useRef(false);
  const [canWrite, setCanWrite] = useState(false); const [loading, setLoading] = useState(true); const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [search, setSearch] = useState('');
  const [libraryQuery, setLibraryQuery] = useState({ page: 1, search: '' });
  const [libraryTotal, setLibraryTotal] = useState(0); const [libraryLoading, setLibraryLoading] = useState(true); const [libraryError, setLibraryError] = useState('');
  const libraryQueryRef = useRef(libraryQuery); libraryQueryRef.current = libraryQuery;
  const libraryRequest = useRef(0); const debouncedSearch = useDebouncedValue(search, 250);
  const [pending, setPending] = useState<number | 'new' | null>(null); const [reviewNote, setReviewNote] = useState(''); const [conflict, setConflict] = useState(false);
  const createKey = useRef(newId('create')); const mounted = useRef(true);
  const dirty = snapshot(write) !== saved; const draft = !record || isEditable(record.status); const editable = canWrite && draft; const current = !!result && resultPlan === JSON.stringify(write.plan);
  const blockers = result?.issues.filter(i => i.severity === 'blocking').length ?? 0;
  const applyRecord = useCallback((value: QuoteRecord) => { const next = writeFromRecord(value); setRecord(value); setWrite(next); setSaved(snapshot(next)); setResult(value.calculation); setResultPlan(JSON.stringify(next.plan)); setReviewNote(''); setConflict(false); }, []);
  const refreshList = useCallback(async () => {
    const request = ++libraryRequest.current; const query = libraryQueryRef.current;
    setLibraryLoading(true); setLibraryError('');
    try {
      const response = await fabricationQuoteApi.list({ ...query, per_page: libraryPageSize });
      if (!mounted.current || request !== libraryRequest.current) return;
      const lastPage = Math.max(1, Math.ceil(response.total / libraryPageSize));
      if (query.page > lastPage) { setLibraryQuery(value => ({ ...value, page: lastPage })); return; }
      setQuotes(response.items); setLibraryTotal(response.total);
    } catch (e) {
      if (mounted.current && request === libraryRequest.current) setLibraryError(readableError(e));
    } finally {
      if (mounted.current && request === libraryRequest.current) setLibraryLoading(false);
    }
  }, []);
  useEffect(() => {
    setLibraryQuery(value => value.search === debouncedSearch.trim() ? value : { page: 1, search: debouncedSearch.trim() });
  }, [debouncedSearch]);
  useEffect(() => {
    void refreshList();
    return () => { libraryRequest.current += 1; };
  }, [refreshList, libraryQuery]);
  useEffect(() => {
    mounted.current = true; let active = true;
    void (async () => {
      const loaded = await Promise.allSettled([api.getCustomerNames(), initialId.current ? fabricationQuoteApi.get(initialId.current) : Promise.resolve(null), fabricationQuoteApi.capabilities()]);
      if (!active) return;
      const [names, quote, capabilities] = loaded;
      if (capabilities.status === 'fulfilled') setCanWrite(capabilities.value.can_write); else setError('Quote permissions could not be verified. Editing is disabled. Reload to try again.');
      if (names.status === 'fulfilled') setCustomers(names.value); else setNotice('Customer names could not be loaded. Save a draft now, or reload before assigning a customer.');
      if (quote.status === 'fulfilled' && quote.value) applyRecord(quote.value); else if (quote.status === 'rejected') setError(readableError(quote.reason));
      setLoading(false);
    })();
    return () => { active = false; mounted.current = false; };
  }, [applyRecord]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return; busyRef.current = true; setBusy(true); setError(''); setNotice('');
    try { await action(); } catch (e) { if (mounted.current) { setError(readableError(e)); if ((e as { response?: { status?: number } }).response?.status === 409) setConflict(true); } }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const navigate = (target: number | 'new', discard = false) => {
    if (busyRef.current) return;
    if (dirty && !discard) { setPending(target); return; }
    setPending(null);
    if (target === 'new') { const next = freshWrite(); setRecord(null); setWrite(next); setSaved(snapshot(next)); setResult(null); setResultPlan(''); setTab('assembly'); setReviewNote(''); setConflict(false); setError(''); setNotice(''); setParams({}, { replace: true }); createKey.current = newId('create'); return; }
    void run(async () => { const value = await fabricationQuoteApi.get(target); applyRecord(value); setParams({ id: String(value.id) }, { replace: true }); });
  };
  const updatePlan = (plan: QuotePlan) => { setWrite(value => ({ ...value, plan })); setNotice(''); };
  const validInput = () => { const errors = inputErrors(write); if (errors.length) { setError(errors.join('\n')); return false; } return true; };
  const save = () => { if (!validInput() || !editable || conflict) return; void run(async () => { const value = record ? await fabricationQuoteApi.save(record.id, record.revision, write) : await fabricationQuoteApi.create(write, createKey.current); applyRecord(value); setParams({ id: String(value.id) }, { replace: true }); setNotice(`Saved revision ${value.revision}.`); await refreshList(); }); };
  const calculate = () => { if (!validInput()) return; void run(async () => { const plan = write.plan; const calculated = await fabricationQuoteApi.calculate(plan, record?.id); setResult(calculated); setResultPlan(JSON.stringify(plan)); setTab('review'); setNotice('Calculated the current inputs. Save changes before approval.'); }); };
  const upload = async (file: File, units?: string) => { if (!record || dirty || !editable) return; await run(async () => { const value = await fabricationQuoteApi.upload(record.id, record.revision, file, units); applyRecord(value); setNotice('Source saved. Review extracted findings and the original file.'); await refreshList(); }); };
  const download = async (file: QuoteFile) => { if (!record) return; await run(async () => downloadBlob(await fabricationQuoteApi.download(record.id, file.id), file.file_name)); };
  const saveNest = async (input: unknown) => { if (!record || dirty || !editable) return; await run(async () => { const value = await fabricationQuoteApi.saveNest(record.id, record.revision, input); applyRecord(value); setNotice('Nest evidence saved. Apply its material allocation and review its source.'); await refreshList(); }); };
  const approve = () => { if (!mayApprove(record, dirty, busy || conflict || !canWrite, result, current) || !record || !reviewNote.trim()) return; void run(async () => { applyRecord(await fabricationQuoteApi.approve(record.id, record.revision, reviewNote.trim())); setNotice('Approved this frozen quote revision.'); await refreshList(); }); };
  const revise = () => { if (!record || !canWrite) return; void run(async () => { applyRecord(await fabricationQuoteApi.revise(record.id, record.revision)); setNotice('Opened a new draft revision. The prior approval is retained in history.'); await refreshList(); }); };
  const handoff = () => { if (!record || !canWrite || dirty || record.status !== 'approved') return; void run(async () => { const value = await fabricationQuoteApi.handoff(record.id, record.revision); applyRecord(value); setNotice(`Created ERP draft quote ${value.erp_quote_id}.`); await refreshList(); }); };
  const exportPackage = () => { if (!record) return; void run(async () => downloadJson(await fabricationQuoteApi.export(record.id), `fabrication-${record.id}-r${record.revision}.json`)); };
  const libraryPending = libraryLoading || search.trim() !== libraryQuery.search;
  const libraryPages = Math.max(1, Math.ceil(libraryTotal / libraryPageSize));

  return <div className="fq-workspace">
    <div className="fq-page-header"><PageHeader title="Fabrication quotes" description="Estimate assemblies, review manufacturing costs and release approved quotes." actions={<Button disabled={busy || loading || !canWrite} onClick={() => navigate('new')}><FilePlus2 size={17} />New quote</Button>} /></div>
    <div className="fq-layout">
      <aside className="fq-library" aria-label="Quote library">
        <div className="fq-library-heading"><h2>Quote library</h2><button type="button" className="fq-icon-btn" aria-label="Refresh quote library" disabled={busy || libraryPending} onClick={() => void refreshList()}><RefreshCw size={15} /></button></div>
        <TextField label="Find a quote" value={search} onChange={setSearch} placeholder="Search quote titles…" />
        <div className="fq-quote-list" aria-busy={libraryPending}>
          {libraryPending ? <p role="status">Loading quotes…</p> : libraryError ? <div className="fq-library-error" role="alert"><p>{libraryError}</p><button type="button" className="fq-text-button" onClick={() => void refreshList()}>Retry quote library</button></div> : !quotes.length ? <div className="fq-library-empty"><FolderOpen size={24} /><p>{libraryQuery.search ? 'No matching quotes.' : 'Your saved quotes will appear here.'}</p></div> : quotes.map(q => <button type="button" key={q.id} className={`fq-quote-item ${record?.id === q.id ? 'fq-selected' : ''}`} disabled={busy || loading} onClick={() => navigate(q.id)} aria-current={record?.id === q.id ? 'page' : undefined}><strong>{q.title}</strong><span>FQ-{q.id} · R{q.revision}</span><small className={`fq-status-${q.status}`}>{q.status.replace(/_/g, ' ')}</small></button>)}
        </div>
        <nav className="fq-library-pagination" aria-label="Quote library pages">
          <p aria-live="polite">{libraryPending ? 'Loading quote count…' : libraryError ? 'Quote count unavailable' : `${libraryTotal} ${libraryQuery.search ? 'matching ' : ''}${libraryTotal === 1 ? 'quote' : 'quotes'} · Page ${libraryQuery.page} of ${libraryPages}`}</p>
          <div><button type="button" className="fq-btn fq-btn-secondary" disabled={busy || libraryPending || libraryQuery.page === 1} onClick={() => setLibraryQuery(value => ({ ...value, page: value.page - 1 }))}>Previous</button><button type="button" className="fq-btn fq-btn-secondary" disabled={busy || libraryPending || !!libraryError || libraryQuery.page >= libraryPages} onClick={() => setLibraryQuery(value => ({ ...value, page: value.page + 1 }))}>Next</button></div>
        </nav>
        <div className="fq-library-footer"><ShieldCheck size={17} /><span>Estimator approval required for every released quote.</span></div>
      </aside>
      <section className="fq-main" aria-label="Fabrication quote editor" aria-busy={busy || loading}>
        {pending !== null && <div className="fq-alert fq-discard" role="alert"><strong>This draft has unsaved changes.</strong><p>Save your edits first or discard them to open another quote.</p><div className="fq-actions"><button type="button" className="fq-btn fq-btn-secondary" onClick={() => setPending(null)}>Keep editing</button><button type="button" className="fq-btn fq-btn-danger" onClick={() => navigate(pending, true)}>Discard and open</button></div></div>}
        {error && <div className="fq-alert" role="alert">{error}{conflict && record && <button type="button" className="fq-text-button" onClick={() => navigate(record.id)}>Reload saved revision</button>}</div>}
        {!loading && !canWrite && <div className="fq-alert">Read-only access. You can inspect files and calculate scenarios; editing and release actions are unavailable.</div>}
        {notice && <div className="fq-notice" role="status"><CheckCircle2 size={17} />{notice}</div>}
        <section className="fq-document-heading"><div className="fq-document-meta"><span className="fq-document-icon"><Layers3 size={22} /></span><div><span className={`fq-pill ${record?.status === 'approved' || record?.status === 'handed_off' ? 'fq-pill-green' : 'fq-pill-neutral'}`}>{record ? record.status.replace(/_/g, ' ') : 'Unsaved draft'}</span><span className="fq-muted">{record ? `FQ-${record.id} · Revision ${record.revision}` : 'Create a fabrication package'}</span></div>{dirty && <span className="fq-unsaved">Unsaved changes</span>}</div><div className="fq-actions"><button type="button" className="fq-btn fq-btn-secondary" disabled={busy || loading} onClick={calculate}><Calculator size={16} />Calculate</button>{draft ? <button type="button" className="fq-btn fq-btn-primary" disabled={!canWrite || busy || loading || conflict || (!dirty && !!record)} onClick={save}><Save size={16} />{busy ? 'Working…' : 'Save draft'}</button> : <button type="button" className="fq-btn fq-btn-secondary" disabled={busy || !canWrite} onClick={revise}>Create revision</button>}</div></section>
        <fieldset className="fq-title-fields" disabled={!editable || busy || loading}><div className="fq-grid fq-grid-title"><TextField label="Quote title" value={write.title} onChange={title => setWrite({ ...write, title })} placeholder="Assembly or customer RFQ name" /><Field label="Customer"><select value={write.customer_id ?? ''} onChange={e => setWrite({ ...write, customer_id: e.target.value ? Number(e.target.value) : null })}><option value="">Assign a customer</option>{write.customer_id && !customers.some(c => c.id === write.customer_id) && <option value={write.customer_id}>Customer #{write.customer_id}</option>}{customers.map(c => <option value={c.id} key={c.id}>{c.name}</option>)}</select></Field><SelectField label="Quote currency" value={write.plan.currency} onChange={currency => updatePlan({ ...write.plan, currency })} options={currencies} /><DecimalField label="Target gross margin" value={write.plan.target_margin} onChange={target_margin => updatePlan({ ...write.plan, target_margin })} hint="Fraction: 0.25 = 25%" /></div></fieldset>
        <nav className="fq-tabs" aria-label="Quote sections">{tabs.map(t => <button type="button" key={t.id} className={tab === t.id ? 'fq-tab-active' : ''} onClick={() => setTab(t.id)} aria-current={tab === t.id ? 'page' : undefined} title={t.label}>{t.short}{t.id === 'review' && blockers > 0 && <span className="fq-count">{blockers}</span>}{t.id === 'sources' && (record?.files.length ?? 0) > 0 && <span className="fq-count">{record?.files.length}</span>}</button>)}</nav>
        <div className="fq-content-layout"><div className="fq-editor">
          {loading ? <Empty>Loading estimator workspace…</Empty> : <>
            <fieldset disabled={!editable || busy} hidden={!['assembly', 'processes', 'material', 'hardware'].includes(tab)}>{tab === 'assembly' && <AssemblyEditor plan={write.plan} onChange={updatePlan} />}{tab === 'processes' && <OperationsEditor plan={write.plan} onChange={updatePlan} />}{tab === 'material' && <MaterialsEditor plan={write.plan} onChange={updatePlan} />}{tab === 'hardware' && <HardwareEditor plan={write.plan} onChange={updatePlan} />}</fieldset>
            {tab === 'material' && <NestingPanel key={record?.id ?? 'new'} plan={write.plan} files={record?.files ?? []} onChange={updatePlan} onSave={saveNest} canSave={!!record && !dirty && !conflict} editable={editable} busy={busy} />}
            {tab === 'sources' && <SourcesPanel quoteId={record?.id} files={record?.files ?? []} plan={write.plan} onChange={updatePlan} onUpload={upload} onDownload={download} uploadEnabled={!!record && !dirty && !conflict} editable={editable} busy={busy} />}
            {tab === 'review' && <><ReviewPanel plan={write.plan} onChange={updatePlan} result={result} current={current} editable={editable && !busy} quoteId={record?.id} /><section className="fq-approval"><div className="fq-section-heading"><div><div className="fq-eyebrow">ESTIMATOR RELEASE</div><h2>{draft ? 'Approve this revision' : 'Approved package'}</h2></div><ShieldCheck size={25} /></div>{draft ? <><p>Confirm the entire package, operation plan, purchasing terms and commercial assumptions. Approval freezes this revision.</p><fieldset disabled={busy || !canWrite}><TextField label="Estimator release note" value={reviewNote} onChange={setReviewNote} placeholder="What was checked and approved?" /></fieldset><button type="button" className="fq-btn fq-btn-primary" disabled={!mayApprove(record, dirty, busy || conflict || !canWrite, result, current) || !reviewNote.trim()} onClick={approve}><ShieldCheck size={17} />Approve revision</button><p className="fq-hint">Save all changes, calculate current inputs, resolve blocking items and review every source first.</p></> : <><p>Approved {record?.approved_at ? new Date(record.approved_at).toLocaleString() : 'revision'}.</p><div className="fq-actions"><button type="button" className="fq-btn fq-btn-secondary" disabled={busy} onClick={exportPackage}><Download size={16} />Export manufacturing package</button>{record?.status === 'approved' && <button type="button" className="fq-btn fq-btn-primary" disabled={!canWrite || busy || dirty || !write.customer_id} onClick={handoff}>Create ERP draft quote<ArrowRight size={16} /></button>}</div>{record?.erp_quote_id && <p className="fq-hint"><Link className="text-fd-link underline" to={`/quotes?id=${record.erp_quote_id}`}>Review customer quote #{record.erp_quote_id}</Link></p>}{!write.customer_id && <p className="fq-hint">Assign a customer in a new draft revision before ERP handoff.</p>}</>}</section></>}
            {tab === 'history' && (record ? <HistoryActualsPanel record={record} canWrite={canWrite} /> : <Empty>Save the quote to establish its revision history and record actuals.</Empty>)}
          </>}
        </div><CostSummary result={result} currency={write.plan.currency} current={current} /></div>
      </section>
    </div>
  </div>;
}
