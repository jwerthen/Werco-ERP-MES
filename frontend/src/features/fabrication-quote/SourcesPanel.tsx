import React, { lazy, Suspense, useEffect, useRef, useState } from 'react';
import PdfPreview from '../../components/ui/PdfPreview';
import { DxfPreview } from './DxfPreview';
import { fabricationQuoteApi } from './api';
import { readableError } from './model';
import { CsvHardwareImport } from './CsvHardwareImport';
const StepViewer = lazy(() => import('./StepViewer'));
import { Download, FileText, Upload } from 'lucide-react';
import { Card, Empty, IntegerField, PartField, Section, SelectField, TextField } from './components';
import { newEvidence, newId } from './types';
import type { QuoteFile, QuotePlan, SourceReview } from './types';

export const objectValue = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
export function downloadBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
export function downloadJson(value: unknown, name: string): void { downloadBlob(new Blob([JSON.stringify(value, null, 2)], { type: 'application/json' }), name); }

interface Props { quoteId?: number; files: QuoteFile[]; plan: QuotePlan; onChange: (plan: QuotePlan) => void; onUpload: (file: File, units?: string) => Promise<void>; onDownload: (file: QuoteFile) => Promise<void>; uploadEnabled: boolean; editable: boolean; busy: boolean }
export function SourcesPanel({ quoteId, files, plan, onChange, onUpload, onDownload, uploadEnabled, editable, busy }: Props) {
  const [units, setUnits] = useState('');
  return <>
    <Section title="Source package" description="Original files, extraction findings and explicit requirement review stay with the quote.">
      <div className="fq-upload"><Upload size={24} /><div><h3>Add drawings, models and supplier evidence</h3><p>DXF, STEP, PDF and BOM files. Geometry findings need review before they become costing inputs.</p><span className="fq-hint">{!uploadEnabled ? 'Save the current draft before adding files.' : 'Each upload creates a saved revision. Review or exclude every file before approval.'}</span></div><label className={`fq-btn fq-btn-secondary ${!uploadEnabled || busy ? 'fq-disabled' : ''}`}>Choose file<input aria-label="Upload quote source file" type="file" accept=".dxf,.step,.stp,.pdf,.csv" disabled={!uploadEnabled || busy || !editable} onChange={e => { const file = e.target.files?.[0]; if (file) void onUpload(file, units || undefined); e.target.value = ''; }} /></label></div>
      <div className="fq-compact"><SelectField label="DXF units override" value={units} onChange={setUnits} options={[{ value: '', label: 'Use file declaration' }, { value: 'mm', label: 'Millimeters — reviewed override' }, { value: 'in', label: 'Inches — reviewed override' }, { value: 'cm', label: 'Centimeters — reviewed override' }, { value: 'm', label: 'Meters — reviewed override' }]} hint="Use an override only when the document's unit declaration is missing or incorrect." /></div>
      {!files.length && <Empty><FileText size={28} />No source files attached yet.</Empty>}
      {files.map(file => <SourceCard key={file.id} file={file} quoteId={quoteId} plan={plan} onChange={onChange} editable={editable && !busy} onDownload={() => onDownload(file)} />)}
    </Section>
  </>;
}

function SourceCard({ file, quoteId, plan, onChange, editable, onDownload }: { file: QuoteFile; quoteId?: number; plan: QuotePlan; onChange: (plan: QuotePlan) => void; editable: boolean; onDownload: () => Promise<void> }) {
  const [pdfUrl, setPdfUrl] = useState(''); const [previewBusy, setPreviewBusy] = useState(false); const [previewError, setPreviewError] = useState('');
  const previewMounted = useRef(true);
  useEffect(() => { previewMounted.current = true; return () => { previewMounted.current = false; }; }, []);
  useEffect(() => () => { if (pdfUrl) URL.revokeObjectURL(pdfUrl); }, [pdfUrl]);
  const openPdf = async () => { if (!quoteId || previewBusy) return; setPreviewBusy(true); setPreviewError(''); try { const blob = await fabricationQuoteApi.download(quoteId, file.id); if (previewMounted.current) setPdfUrl(URL.createObjectURL(blob)); } catch (e) { if (previewMounted.current) setPreviewError(readableError(e)); } finally { if (previewMounted.current) setPreviewBusy(false); } };
  const review = plan.source_reviews.find(r => r.file_id === file.id && r.sha256 === file.sha256);
  const [note, setNote] = useState(review?.note || '');
  const [disposition, setDisposition] = useState<SourceReview['disposition']>(review?.disposition || 'reviewed');
  const [partId, setPartId] = useState(plan.parts[0]?.id || '');
  const [pierces, setPierces] = useState(NaN);
  const analysis = objectValue(file.analysis);
  const geometry = objectValue(analysis.geometry);
  const issues = Array.isArray(analysis.issues) ? analysis.issues.map(objectValue) : [];
  const observations = Array.isArray(analysis.observations) ? analysis.observations.map(objectValue) : [];
  const pages = Array.isArray(analysis.pages) ? analysis.pages.map(objectValue) : [];
  const definitions = Array.isArray(geometry.definitions) ? geometry.definitions.map(objectValue) : [];
  const contours = Array.isArray(geometry.contours) ? geometry.contours.map(objectValue) : [];
  const parser = objectValue(analysis.parser);
  const verified = !!review;
  const hasLength = typeof geometry.measured_length_mm === 'number' && geometry.measured_length_mm > 0;
  const applyLaser = () => {
    if (!partId || !hasLength || !review || review.disposition !== 'reviewed' || !Number.isInteger(pierces) || pierces < 0) return;
    onChange({ ...plan, operations: [...plan.operations, { id: newId('op'), part_id: partId, name: `Laser · ${file.file_name}`, process: 'laser', setup_basis: 'per_quote', run_basis: 'per_unit', batch_size: '1', setup_labor_seconds: null, setup_machine_seconds: null, labor_rate_per_hour: null, machine_rate_per_hour: null, consumables_cost_per_run: null, outside_cost_per_run: null, recipe: { kind: 'laser', cuts: [{ cut_length_mm: Number(geometry.measured_length_mm).toFixed(9).replace(/\.?0+$/, ''), speed_mm_per_second: null, pierces, pierce_seconds: null }], noncut_machine_seconds: null, labor_seconds: null, speed_includes_dynamics: false, dynamics_allowance_seconds: null }, evidence: { ...newEvidence(), source: `${file.file_name}; SHA-256 ${file.sha256}`, note: `Reviewed DXF measured length and manually specified ${pierces} pierces. Source review: ${review.note}. Speeds, times, rates and applicability still require review.` } }] });
  };
  return <Card title={file.file_name}>
    <div className="fq-file-meta"><span className={`fq-pill ${verified ? 'fq-pill-green' : 'fq-pill-amber'}`}>{verified ? review.disposition === 'excluded' ? 'Excluded with reason' : 'Source reviewed' : 'Review required'}</span><span>{String(analysis.kind || 'file').toUpperCase()}</span><span>{parser.name ? `${parser.name} ${parser.version ?? ''}` : 'Analysis pending / unavailable'}</span><button type="button" className="fq-text-button" onClick={() => void onDownload()}><Download size={14} />Original file</button></div>
    <div className="fq-hash" title={file.sha256}>SHA-256 {file.sha256}</div>
    {(analysis.kind === 'pdf' || file.file_name.toLowerCase().endsWith('.pdf')) && <div className="fq-drawing-review">{pdfUrl ? <><button type="button" className="fq-text-button" onClick={() => setPdfUrl('')}>Close drawing</button><PdfPreview url={pdfUrl} fileName={file.file_name} title={`Drawing review: ${file.file_name}`} /></> : <button type="button" className="fq-btn fq-btn-secondary" disabled={previewBusy || !quoteId} onClick={() => void openPdf()}>{previewBusy ? 'Loading drawing…' : 'Open drawing for review'}</button>}{previewError && <div className="fq-alert" role="alert">{previewError}</div>}</div>}
    {Array.isArray(geometry.meshes) && geometry.meshes.length > 0 && <Suspense fallback={<p role="status">Loading 3D inspection…</p>}><StepViewer geometry={geometry} /></Suspense>}
    {analysis.kind === 'dxf' && <DxfPreview geometry={geometry} />}
    {issues.length > 0 && <ul className="fq-findings">{issues.map((issue, i) => <li key={i}><strong>{String(issue.code || 'Finding').replace(/_/g, ' ')}</strong><span>{String(issue.message || '')}</span></li>)}</ul>}
    {hasLength && <div className="fq-fact-grid"><div><span>Measured path length</span><strong>{Number(geometry.measured_length_mm).toLocaleString(undefined, { maximumFractionDigits: 3 })} mm</strong></div><div><span>Candidate closed contours</span><strong>{String(geometry.closed_contour_count ?? contours.length)}</strong></div><div><span>Candidate net area</span><strong>{geometry.net_candidate_area_mm2 == null ? 'Unresolved' : `${Number(geometry.net_candidate_area_mm2).toLocaleString(undefined, { maximumFractionDigits: 2 })} mm²`}</strong></div></div>}
    {definitions.length > 0 && <div className="fq-table-wrap"><table><caption>STEP definitions · {String(geometry.leaf_occurrence_count ?? '?')} leaf occurrences</caption><thead><tr><th>Definition</th><th>Geometry</th><th>Volume (mm³)</th></tr></thead><tbody>{definitions.map((definition, i) => <tr key={i}><td>{String(definition.name || definition.id)}</td><td>{definition.is_assembly ? 'Assembly' : `${definition.solid_count ?? 0} solid(s)`}</td><td>{definition.volume_mm3 == null ? 'Unknown' : Number(definition.volume_mm3).toLocaleString()}</td></tr>)}</tbody></table><p className="fq-hint">Keep every occurrence when building the BOM. STEP geometry does not establish make/buy, material or weld requirements. Any flat-pattern candidate still requires estimator review.</p></div>}
    {(observations.length > 0 || pages.length > 0) && <details className="fq-details"><summary>Extracted text & page coverage ({pages.length || observations.length})</summary>{pages.map((page, i) => <div className="fq-page-text" key={i}><h4>Page {String(page.page_number || i + 1)} · {String(page.status || 'needs review')}</h4><pre>{String(page.text || 'No text extracted. Review the original page.')}</pre></div>)}{!pages.length && observations.map((observation, i) => <pre className="fq-page-text" key={i}>{String(observation.text || '')}</pre>)}</details>}
    <details className="fq-details"><summary>Full extraction evidence</summary><pre className="fq-json">{JSON.stringify(analysis, null, 2)}</pre></details>
    <CsvHardwareImport file={file} plan={plan} onChange={onChange} editable={editable} />
    <fieldset disabled={!editable}><div className="fq-grid fq-grid-2"><SelectField label="File disposition" value={disposition} onChange={setDisposition} options={[{ value: 'reviewed', label: 'Reviewed requirements' }, { value: 'excluded', label: 'Excluded from quote scope' }]} /><TextField label="Source review note" value={note} onChange={setNote} hint="Identify the requirements represented in the plan, or explain the exclusion." /></div><button type="button" className="fq-btn fq-btn-secondary" disabled={!note.trim()} onClick={() => onChange({ ...plan, source_reviews: [...plan.source_reviews.filter(r => r.file_id !== file.id), { file_id: file.id, sha256: file.sha256, disposition, note: note.trim() }] })}>Record source review</button>
      {hasLength && <details className="fq-details"><summary>Use reviewed DXF measurements</summary><p className="fq-hint">Measured length includes all supported entities. Verify layers, annotations, open paths and omitted entities against the original. Enter the intended pierce count explicitly. The new operation will still need reviewed machine data.</p><div className="fq-grid fq-grid-2"><PartField parts={plan.parts} value={partId} onChange={setPartId} /><IntegerField label="Reviewed pierce count" value={pierces} onChange={setPierces} /></div><button type="button" className="fq-btn fq-btn-secondary" disabled={!review || review.disposition !== 'reviewed' || !plan.parts.some(p => p.id === partId) || !Number.isInteger(pierces) || pierces < 0} onClick={applyLaser}>Create laser operation from reviewed length</button></details>}
    </fieldset>
  </Card>;
}
