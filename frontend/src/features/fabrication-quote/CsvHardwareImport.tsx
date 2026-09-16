import React, { useMemo, useState } from 'react';
import { Field, PartField, SelectField, Toggle } from './components';
import { appendCsvHardwareImport, csvHardwareFields, csvImportPlanSignature, csvRows, previewCsvHardwareImport, suggestCsvHardwareMapping } from './csvHardwareImportModel';
import type { CsvHardwareField, CsvHardwarePreview } from './csvHardwareImportModel';
import type { QuoteFile, QuotePlan } from './types';

export interface CsvHardwareImportProps { file: QuoteFile; plan: QuotePlan; onChange: (plan: QuotePlan) => void; editable: boolean }

export function CsvHardwareImport(props: CsvHardwareImportProps) {
  if (props.file.analysis?.kind !== 'csv') return null;
  return <CsvHardwareImportBody key={`${props.file.id}:${props.file.sha256}`} {...props} />;
}

function CsvHardwareImportBody({ file, plan, onChange, editable }: CsvHardwareImportProps) {
  const rows = useMemo(() => csvRows(file), [file]);
  const [headerRow, setHeaderRow] = useState(rows[0]?.row_number ?? 1);
  const headers = rows.find(row => row.row_number === headerRow)?.cells ?? [];
  const [mapping, setMapping] = useState(() => suggestCsvHardwareMapping(headers));
  const [targetPartId, setTargetPartId] = useState('');
  const [includeOffers, setIncludeOffers] = useState(false);
  const [preview, setPreview] = useState<CsvHardwarePreview | null>(null);
  const [notice, setNotice] = useState(''); const [error, setError] = useState(''); const [page, setPage] = useState(0);
  const clearPreview = () => { setPreview(null); setNotice(''); setError(''); setPage(0); };
  const changeMapping = (key: CsvHardwareField, change: Partial<typeof mapping[CsvHardwareField]>) => { clearPreview(); setMapping(old => ({ ...old, [key]: { ...old[key], ...change } })); };
  const stale = !!preview && preview.planSignature !== csvImportPlanSignature(plan);
  const runPreview = () => { setPreview(previewCsvHardwareImport(file, plan, { headerRow, targetPartId, includeOffers, mapping })); setPage(0); setNotice(''); setError(''); };
  const apply = () => {
    if (!editable || !preview || stale) return;
    try { const next = appendCsvHardwareImport(plan, preview); onChange(next); setNotice(`Appended ${preview.rows.length} unreviewed hardware rows. Review quantities, purchasing terms and source disposition before approval.`); setPreview(null); setError(''); } catch (caught) { setError(caught instanceof Error ? caught.message : 'Import could not be applied.'); }
  };
  return <details className="fq-details"><summary>Import CSV hardware and supplier offers</summary>
    <p className="fq-hint">Map exact identities and quantities per made part, then inspect the preview. Repeated identities and multi-row price tiers require manual reconciliation. Importing clears the affected parts’ costing confirmation and this file’s source review.</p>
    {notice && <p className="fq-notice" role="status">{notice}</p>}{error && <p className="fq-alert" role="alert">{error}</p>}
    <fieldset disabled={!editable}>
      <div className="fq-grid fq-grid-2"><SelectField label="CSV header record" value={String(headerRow)} onChange={value => { const next = Number(value); setHeaderRow(next); setMapping(suggestCsvHardwareMapping(rows.find(row => row.row_number === next)?.cells ?? [])); clearPreview(); }} options={rows.slice(0, 20).map(row => ({ value: String(row.row_number), label: `Record ${row.row_number}: ${row.cells.slice(0, 4).join(' · ').slice(0, 140)}` }))} /><PartField label="Target made part when CSV part ID is blank" value={targetPartId} onChange={value => { setTargetPartId(value); clearPreview(); }} parts={plan.parts.filter(part => part.make_or_buy === 'make')} /></div>
      <Toggle label="Include supplier offers" checked={includeOffers} onChange={value => { setIncludeOffers(value); clearPreview(); }} hint="Provide explicit price units, price threshold, currency and ordering terms. Blank dates and freight remain unknown. Offers use an unreviewed 30-day freshness policy." />
      <div className="fq-grid fq-grid-3">{csvHardwareFields.filter(field => includeOffers || !field.offer).map(field => <div key={field.key}>
        <SelectField label={`${field.label} column`} value={mapping[field.key].column === null ? '' : String(mapping[field.key].column)} onChange={value => changeMapping(field.key, { column: value === '' ? null : Number(value) })} options={[{ value: '', label: 'No column / enter a constant' }, ...headers.map((header, index) => ({ value: String(index), label: `${index + 1}. ${header || '(blank header)'}` }))]} />
        {mapping[field.key].column === null && field.key !== 'part_id' && <Field label={`${field.label} constant`}><input type="text" value={mapping[field.key].literal} onChange={event => changeMapping(field.key, { literal: event.target.value })} placeholder="Unknown / not provided" autoComplete="off" /></Field>}
      </div>)}</div>
      <button type="button" className="fq-btn fq-btn-secondary" onClick={runPreview} disabled={!rows.length}>Preview mapped rows</button>
      {preview && <div>
        <h4>Import preview · {preview.rows.length} records</h4>
        {preview.errors.length > 0 && <ul className="fq-alert" role="alert">{preview.errors.map(message => <li key={message}>{message}</li>)}</ul>}
        {stale && <p className="fq-alert" role="alert">The quote changed after this preview. Preview the import again.</p>}
        <div className="fq-table-wrap"><table><caption>Mapped CSV records {page * 25 + 1}–{Math.min((page + 1) * 25, preview.rows.length)}</caption><thead><tr><th>Record</th><th>Target part</th><th>Manufacturer / MPN</th><th>Qty per part</th><th>Supplier price</th><th>Review findings</th></tr></thead><tbody>{preview.rows.slice(page * 25, (page + 1) * 25).map(row => <tr key={row.row}><td>{row.row}</td><td>{row.partId || 'Unknown'}</td><td>{row.manufacturer || 'Unknown'} / {row.mpn || 'Unknown'}</td><td>{row.quantity || 'Unknown'}</td><td>{includeOffers ? `${row.supplier || 'Unknown'} · ${row.currency} ${row.price || 'Unknown'}` : 'No offer imported'}</td><td>{row.errors.length > 0 && <ul>{row.errors.map(message => <li key={message}>{message}</li>)}</ul>}{row.warnings.map(message => <p key={message}>{message}</p>)}{!row.errors.length && <span className="fq-pill fq-pill-amber">Unreviewed</span>}</td></tr>)}</tbody></table></div>
        {preview.rows.length > 25 && <div><button type="button" className="fq-btn fq-btn-secondary" disabled={page === 0} onClick={() => setPage(value => value - 1)}>Previous records</button><button type="button" className="fq-btn fq-btn-secondary" disabled={(page + 1) * 25 >= preview.rows.length} onClick={() => setPage(value => value + 1)}>Next records</button></div>}
        {!preview.canImport && <p className="fq-hint">Resolve all row and mapping errors before appending. No rows will be imported partially.</p>}
        <button type="button" className="fq-btn fq-btn-primary" disabled={!preview.canImport || stale} onClick={apply}>Append {preview.rows.length} hardware rows</button>
      </div>}
    </fieldset>
  </details>;
}
