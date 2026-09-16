import React, { useId } from 'react';
import { Plus, Trash2 } from 'lucide-react';
import type { Decimal, Evidence, PartDefinition } from './types';

export function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactElement<{ id?: string; 'aria-describedby'?: string }> }) {
  const id = useId();
  return <div className="fq-field"><label htmlFor={id}>{label}</label>{React.cloneElement(children, { id, 'aria-describedby': hint ? `${id}-hint` : undefined })}{hint && <span id={`${id}-hint`} className="fq-hint">{hint}</span>}</div>;
}
export function TextField({ label, value, onChange, hint, placeholder, type = 'text' }: { label: string; value: string | null; onChange: (value: string) => void; hint?: string; placeholder?: string; type?: string }) {
  return <Field label={label} hint={hint}><input type={type} value={value ?? ''} onChange={e => onChange(e.target.value)} placeholder={placeholder} /></Field>;
}
export function DecimalField({ label, value, onChange, hint }: { label: string; value: Decimal; onChange: (value: Decimal) => void; hint?: string }) {
  return <Field label={label} hint={hint}><input type="text" inputMode="decimal" value={value ?? ''} onChange={e => onChange(e.target.value === '' ? null : e.target.value)} placeholder="Unknown" autoComplete="off" /></Field>;
}
export function IntegerField({ label, value, onChange, hint }: { label: string; value: number; onChange: (value: number) => void; hint?: string }) {
  return <Field label={label} hint={hint}><input type="text" inputMode="numeric" value={Number.isFinite(value) ? value : ''} onChange={e => onChange(/^\d+$/.test(e.target.value) ? Number(e.target.value) : NaN)} autoComplete="off" /></Field>;
}
export function SelectField<T extends string>({ label, value, onChange, options, hint }: { label: string; value: T; onChange: (value: T) => void; options: (T | { value: T; label: string })[]; hint?: string }) {
  return <Field label={label} hint={hint}><select value={value} onChange={e => onChange(e.target.value as T)}>{options.map(o => typeof o === 'string' ? <option key={o} value={o}>{o.replace(/_/g, ' ')}</option> : <option key={o.value} value={o.value}>{o.label}</option>)}</select></Field>;
}
export function PartField({ label = 'Part / assembly', value, onChange, parts }: { label?: string; value: string; onChange: (id: string) => void; parts: PartDefinition[] }) {
  return <SelectField label={label} value={value} onChange={onChange} options={[{ value: '', label: 'Select a part' }, ...parts.map(p => ({ value: p.id, label: p.name || p.id }))]} />;
}
export function Toggle({ label, checked, onChange, hint }: { label: string; checked: boolean; onChange: (checked: boolean) => void; hint?: string }) {
  return <label className="fq-check"><input type="checkbox" checked={checked} onChange={e => onChange(e.target.checked)} /><span>{label}{hint && <small>{hint}</small>}</span></label>;
}
export function AddButton({ children, onClick, disabled }: { children: React.ReactNode; onClick: () => void; disabled?: boolean }) { return <button type="button" className="fq-btn fq-btn-secondary" onClick={onClick} disabled={disabled}><Plus size={15} />{children}</button>; }
export function RemoveButton({ label, onClick }: { label: string; onClick: () => void }) { return <button type="button" className="fq-icon-btn" aria-label={label} title={label} onClick={onClick}><Trash2 size={16} /></button>; }
export function Section({ title, description, action, children }: { title: string; description?: string; action?: React.ReactNode; children: React.ReactNode }) { return <section className="fq-section"><div className="fq-section-heading"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{action}</div>{children}</section>; }
export function Empty({ children }: { children: React.ReactNode }) { return <div className="fq-empty">{children}</div>; }
export function Card({ title, index, children, onRemove }: { title: string; index?: number; children: React.ReactNode; onRemove?: () => void }) { const id = useId(); return <article className="fq-card" aria-labelledby={id}><header><h3 id={id}>{index !== undefined && <span className="fq-card-index">{String(index + 1).padStart(2, '0')}</span>}{title}</h3>{onRemove && <RemoveButton label={`Remove ${title}`} onClick={onRemove} />}</header>{children}</article>; }
export function EvidenceEditor({ value, onChange }: { value: Evidence; onChange: (evidence: Evidence) => void }) {
  return <details className="fq-evidence"><summary><span className={`fq-dot ${value.reviewed ? 'fq-dot-green' : ''}`} />{value.reviewed ? 'Evidence reviewed' : 'Evidence needs review'}<span className="fq-muted">{value.source || 'Add a source and review this input'}</span></summary><div className="fq-grid fq-grid-2"><TextField label="Evidence source" value={value.source} onChange={source => onChange({ ...value, source: source || null, reviewed: false })} hint="Drawing page, supplier quote, machine table or measured job." /><SelectField label="Evidence basis" value={value.status} onChange={status => onChange({ ...value, status, reviewed: false })} options={['assumption', 'measured', 'validated']} /><TextField label="Evidence note" value={value.note} onChange={note => onChange({ ...value, note: note || null, reviewed: false })} /></div><Toggle label="I reviewed this evidence and its applicability" checked={value.reviewed} onChange={reviewed => onChange({ ...value, reviewed })} /></details>;
}
