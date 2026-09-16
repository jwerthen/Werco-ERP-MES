import React, { useEffect, useRef, useState } from 'react';
import { fabricationQuoteApi } from './api';
import { DecimalField, Empty, Section, SelectField, TextField } from './components';
import { readableError } from './model';
import { downloadJson, objectValue } from './SourcesPanel';
import type { ActualObservation, QuoteRecord, RevisionSummary } from './types';
import { newId } from './types';

const blankObservation = (record: QuoteRecord): ActualObservation => ({ request_key: newId('actual'), quote_revision: record.revision, operation_id: record.plan.operations[0]?.id || '', observed_on: '', good_quantity: '', scrap_quantity: '', setup_labor_seconds: null, run_labor_seconds: null, machine_seconds: null, observed_cost: null, source: '', note: '', completeness: 'partial' });

export function HistoryActualsPanel({ record, canWrite }: { record: QuoteRecord; canWrite: boolean }) {
  const [revisions, setRevisions] = useState<RevisionSummary[]>([]); const [actuals, setActuals] = useState<Record<string, unknown>[]>([]);
  const [observation, setObservation] = useState<ActualObservation>(() => blankObservation(record));
  const [approvedRevision, setApprovedRevision] = useState(''); const [observedOperations, setObservedOperations] = useState<{ id: string; name: string }[]>([]); const [snapshotLoading, setSnapshotLoading] = useState(false);
  const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [loading, setLoading] = useState(true); const [busy, setBusy] = useState(false); const lock = useRef(false);
  useEffect(() => {
    let active = true; setLoading(true); setError(''); setObservation(blankObservation(record));
    void Promise.allSettled([fabricationQuoteApi.revisions(record.id), fabricationQuoteApi.actuals(record.id)]).then(([history, observations]) => {
      if (!active) return;
      if (history.status === 'fulfilled') { setRevisions(history.value.items); const approved = history.value.items.filter(r => r.action === 'approve').sort((a, b) => b.revision - a.revision); setApprovedRevision(approved.length ? String(approved[0].revision) : ''); } else setError(readableError(history.reason));
      if (observations.status === 'fulfilled') setActuals(observations.value.items); else setError(readableError(observations.reason));
      setLoading(false);
    }); return () => { active = false; };
  }, [record]);
  useEffect(() => {
    let active = true; setObservedOperations([]);
    if (!approvedRevision) { setSnapshotLoading(false); return; }
    setSnapshotLoading(true);
    void fabricationQuoteApi.revision(record.id, Number(approvedRevision)).then(snapshot => {
      if (!active) return;
      const calculation = objectValue(snapshot.calculation); const plan = objectValue(snapshot.plan);
      const definitions = (Array.isArray(plan.operations) ? plan.operations : []).map(objectValue);
      const operations = (Array.isArray(calculation.operation_lines) ? calculation.operation_lines : []).map(objectValue).map(o => ({ id: String(o.id), name: String(definitions.find(d => d.id === o.id)?.name || o.id) }));
      setObservedOperations(operations); setObservation(value => ({ ...value, quote_revision: Number(approvedRevision), operation_id: operations[0]?.id || '' }));
    }).catch(e => { if (active) setError(readableError(e)); }).finally(() => { if (active) setSnapshotLoading(false); });
    return () => { active = false; };
  }, [record, approvedRevision]);
  const run = async (fn: () => Promise<void>) => { if (lock.current) return; lock.current = true; setBusy(true); setError(''); setNotice(''); try { await fn(); } catch (e) { setError(readableError(e)); } finally { lock.current = false; setBusy(false); } };
  const save = () => {
    if (!canWrite || snapshotLoading || observation.quote_revision !== Number(approvedRevision) || !observedOperations.some(o => o.id === observation.operation_id)) return;
    if (!observation.observed_on || !observation.operation_id || !observation.source.trim() || !observation.note.trim() || !/^\d+(?:\.\d{1,9})?$/.test(observation.good_quantity) || !/^\d+(?:\.\d{1,9})?$/.test(observation.scrap_quantity)) { setError('Complete the operation, observation date, quantities, source and note. Enter explicit zero quantities where applicable.'); return; }
    void run(async () => { await fabricationQuoteApi.addActual(record.id, observation); setActuals((await fabricationQuoteApi.actuals(record.id)).items); setObservation({ ...blankObservation(record), quote_revision: Number(approvedRevision), operation_id: observedOperations[0]?.id || '' }); setNotice('Observation recorded. It will not change rates or the approved quote automatically.'); });
  };
  return <>
    {error && <div className="fq-alert" role="alert">{error}</div>}{notice && <div className="fq-notice" role="status">{notice}</div>}
    <Section title="Revision history" description="Saved inputs, approvals and handoffs remain available as immutable snapshots.">{loading ? <p role="status">Loading history…</p> : !revisions.length ? <Empty>No revision history returned.</Empty> : <div className="fq-table-wrap"><table><thead><tr><th>Revision</th><th>Action</th><th>Recorded</th><th>Review note</th><th>Snapshot</th></tr></thead><tbody>{revisions.map(revision => <tr key={revision.revision}><td>R{revision.revision}</td><td>{revision.action.replace(/_/g, ' ')}</td><td>{new Date(revision.created_at).toLocaleString()}</td><td>{revision.note || '—'}</td><td><button type="button" className="fq-text-button" disabled={busy} onClick={() => void run(async () => downloadJson(await fabricationQuoteApi.revision(record.id, revision.revision), `fabrication-${record.id}-r${revision.revision}-snapshot.json`))}>Download</button></td></tr>)}</tbody></table></div>}</Section>
    <Section title="Record actual work" description="Capture observations against a saved operation. Separate person-time, machine occupancy and incomplete observations.">
      {!approvedRevision ? <Empty>Approve an estimate revision before recording actual work.</Empty> : <fieldset disabled={!canWrite || busy || loading || snapshotLoading}><div className="fq-grid fq-grid-3"><SelectField label="Observed quote revision" value={approvedRevision} onChange={setApprovedRevision} options={revisions.filter(r => r.action === 'approve').map(r => ({ value: String(r.revision), label: `Approved revision ${r.revision}` }))} /><SelectField label="Observed operation" value={observation.operation_id} onChange={operation_id => setObservation({ ...observation, operation_id })} options={[{ value: '', label: snapshotLoading ? 'Loading approved operations…' : 'Choose an approved operation' }, ...observedOperations.map(o => ({ value: o.id, label: o.name }))]} /><TextField label="Observation date" type="date" value={observation.observed_on} onChange={observed_on => setObservation({ ...observation, observed_on })} /><DecimalField label="Good quantity observed" value={observation.good_quantity} onChange={value => setObservation({ ...observation, good_quantity: value ?? '' })} /><DecimalField label="Scrap quantity observed" value={observation.scrap_quantity} onChange={value => setObservation({ ...observation, scrap_quantity: value ?? '' })} /><SelectField label="Observation completeness" value={observation.completeness} onChange={completeness => setObservation({ ...observation, completeness })} options={['partial', 'complete']} /><DecimalField label="Actual setup labor (person-seconds)" value={observation.setup_labor_seconds} onChange={setup_labor_seconds => setObservation({ ...observation, setup_labor_seconds })} /><DecimalField label="Actual run labor (person-seconds)" value={observation.run_labor_seconds} onChange={run_labor_seconds => setObservation({ ...observation, run_labor_seconds })} /><DecimalField label="Actual machine occupancy (seconds)" value={observation.machine_seconds} onChange={machine_seconds => setObservation({ ...observation, machine_seconds })} /><DecimalField label={`Observed cost (${record.plan.currency})`} value={observation.observed_cost} onChange={observed_cost => setObservation({ ...observation, observed_cost })} /><TextField label="Observation source" value={observation.source} onChange={source => setObservation({ ...observation, source })} hint="Time sheet, job record or measured trial." /><TextField label="Observation note" value={observation.note} onChange={note => setObservation({ ...observation, note })} hint="Document scope, downtime, rework and crew assumptions." /></div><button type="button" className="fq-btn fq-btn-primary" disabled={!observedOperations.length || snapshotLoading} onClick={save}>{busy ? 'Saving…' : 'Record observation'}</button><p className="fq-hint">Operations come from the selected approved calculation, including operations removed from later drafts. Rates remain unchanged until explicitly reviewed.</p></fieldset>}
    </Section>
    <Section title="Observed results" description="Recorded evidence for future calibration; partial records are kept visible.">{!actuals.length ? <Empty>No actual work recorded yet.</Empty> : <div className="fq-table-wrap"><table><thead><tr><th>Revision / operation</th><th>Date</th><th>Good / scrap</th><th>Coverage</th><th>Source & note</th></tr></thead><tbody>{actuals.map((actual, i) => <tr key={String(actual.id ?? i)}><td>R{String(actual.quote_revision)} · {record.plan.operations.find(o => o.id === actual.operation_id)?.name || String(actual.operation_id)}</td><td>{String(actual.observed_on)}</td><td>{String(actual.good_quantity)} / {String(actual.scrap_quantity)}</td><td>{String(actual.completeness)}</td><td>{String(actual.source)}<small>{String(actual.note)}</small></td></tr>)}</tbody></table></div>}</Section>
  </>;
}
