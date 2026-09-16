import React, { useEffect, useRef, useState } from 'react';
import { fabricationQuoteApi } from './api';
import { DecimalField, Empty, PartField, SelectField, TextField } from './components';
import { inputErrors, readableError } from './model';
import { operationFromProfile } from './profiles';
import type { ProcessProfile, QuotePlan } from './types';

export function ProcessProfilesPanel({ plan, onChange }: { plan: QuotePlan; onChange: (plan: QuotePlan) => void }) {
  const [profiles, setProfiles] = useState<ProcessProfile[]>([]); const [selected, setSelected] = useState('');
  const [partId, setPartId] = useState(plan.parts[0]?.id || ''); const [operationId, setOperationId] = useState('');
  const [name, setName] = useState(''); const [machine, setMachine] = useState(''); const [material, setMaterial] = useState('');
  const [thickness, setThickness] = useState<string | null>(null); const [note, setNote] = useState(''); const [mode, setMode] = useState('new');
  const [error, setError] = useState(''); const [notice, setNotice] = useState(''); const [busy, setBusy] = useState(false); const lock = useRef(false);
  const selectedProfile = profiles.find(p => p.key === selected); const operation = plan.operations.find(o => o.id === operationId);
  useEffect(() => { let active = true; void fabricationQuoteApi.profiles().then(result => { if (active) setProfiles(result.items); }).catch(e => { if (active) setError(readableError(e)); }); return () => { active = false; }; }, []);
  const apply = () => {
    if (!selectedProfile || !plan.parts.some(p => p.id === partId)) return;
    try { onChange({ ...plan, operations: [...plan.operations, operationFromProfile(selectedProfile, partId, plan.currency)] }); setNotice('Operation copied. Fill the blank part geometry, run allowances and costs, then review its applicability.'); setError(''); } catch (e) { setError(readableError(e)); }
  };
  const save = async () => {
    if (lock.current || !operation) return;
    if (!name.trim() || !note.trim()) { setError('Provide a profile name and evidence note.'); return; }
    if (thickness !== null && (!/^\d+(?:\.\d{1,9})?$/.test(thickness) || Number(thickness) <= 0)) { setError('Thickness must be a positive decimal or blank.'); return; }
    const errors = inputErrors({ title: name, customer_id: null, plan: { ...plan, operations: [operation], hardware: [], assumptions: [] } });
    if (errors.length) { setError(errors.join('\n')); return; }
    if (mode === 'revision' && !selectedProfile) { setError('Select the existing profile to revise.'); return; }
    lock.current = true; setBusy(true); setError(''); setNotice('');
    try {
      const result = await fabricationQuoteApi.saveProfile({ ...(mode === 'revision' && selectedProfile ? { key: selectedProfile.key, expected_revision: selectedProfile.revision } : {}), name: name.trim(), process: operation.process, machine: machine.trim() || null, material: material.trim() || null, thickness_mm: thickness, currency: plan.currency, template: operation, evidence_note: note.trim() });
      setProfiles(old => [result, ...old.filter(p => p.key !== result.key)]); setSelected(result.key); setNotice(`Saved ${result.name}, revision ${result.revision}. Existing quotes retain their copied inputs.`);
    } catch (e) { setError(readableError(e)); } finally { lock.current = false; setBusy(false); }
  };
  return <details className="fq-profile-library fq-details"><summary>Process template library <span className="fq-muted">{profiles.length} available</span></summary>
    <p className="fq-hint">Reusable machine and shop inputs with immutable revisions. Applying a profile copies candidate data; every part still requires its own geometry and review.</p>
    {error && <div className="fq-alert" role="alert">{error}</div>}{notice && <div className="fq-notice" role="status">{notice}</div>}
    <fieldset disabled={busy}><div className="fq-grid fq-grid-2"><SelectField label="Library profile" value={selected} onChange={value => { setSelected(value); const p = profiles.find(item => item.key === value); if (p) { setName(p.name); setMachine(p.machine || ''); setMaterial(p.material || ''); setThickness(p.thickness_mm); setNote(p.evidence_note); } }} options={[{ value: '', label: 'Choose a process profile' }, ...profiles.map(p => ({ value: p.key, label: `${p.name} · R${p.revision} · ${p.currency}` }))]} /><PartField label="Apply profile to part" parts={plan.parts} value={partId} onChange={setPartId} /></div>
      {selectedProfile && <><div className="fq-fact-grid"><div><span>Machine</span><strong>{selectedProfile.machine || 'Unspecified'}</strong></div><div><span>Material / thickness</span><strong>{selectedProfile.material || 'Unspecified'} · {selectedProfile.thickness_mm ? `${selectedProfile.thickness_mm} mm` : 'Unknown thickness'}</strong></div><div><span>Evidence</span><p>{selectedProfile.evidence_note}</p></div></div>{selectedProfile.currency !== plan.currency && <p className="fq-alert">Currency mismatch: this profile uses {selectedProfile.currency}; this quote uses {plan.currency}.</p>}<details className="fq-details"><summary>Inspect saved process inputs</summary><pre className="fq-json">{JSON.stringify(selectedProfile.template, null, 2)}</pre></details></>}
      <button type="button" className="fq-btn fq-btn-secondary" disabled={!selectedProfile || !plan.parts.some(p => p.id === partId) || selectedProfile.currency !== plan.currency} onClick={apply}>Copy profile into quote</button>
      {!profiles.length && <Empty>No profiles available. Save reviewed shop inputs from an operation below.</Empty>}
      <details className="fq-details"><summary>Save an operation to the library</summary><div className="fq-grid fq-grid-3"><SelectField label="Operation to save" value={operationId} onChange={setOperationId} options={[{ value: '', label: 'Choose an operation' }, ...plan.operations.map(o => ({ value: o.id, label: o.name || o.id }))]} /><SelectField label="Library save action" value={mode} onChange={setMode} options={[{ value: 'new', label: 'Create a new profile' }, { value: 'revision', label: 'Append revision to selected profile' }]} /><TextField label="Profile name" value={name} onChange={setName} /><TextField label="Profile machine" value={machine} onChange={setMachine} /><TextField label="Profile material" value={material} onChange={setMaterial} /><DecimalField label="Profile thickness (mm)" value={thickness} onChange={setThickness} /><TextField label="Profile evidence note" value={note} onChange={setNote} hint="Record data source, measured trials and the conditions for using these inputs." /></div><button type="button" className="fq-btn fq-btn-secondary" disabled={!operation || !name.trim() || !note.trim() || (mode === 'revision' && !selectedProfile)} onClick={() => void save()}>{busy ? 'Saving profile…' : 'Save process profile'}</button><p className="fq-hint">Currency: {plan.currency}. Unknown rates stay unknown. Saving a profile does not approve this quote.</p></details>
    </fieldset>
  </details>;
}
