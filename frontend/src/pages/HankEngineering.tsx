import React, { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { usePermissions } from '../hooks/usePermissions';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from '../components/ai/hankSession';
import { FormField } from '../components/ui/FormField';
import PdfPreview from '../components/ui/PdfPreview';
import api from '../services/api';
import type { WorkCenter } from '../types';
import {
  blankComponent,
  blankOperation,
  blankPart,
  issues,
  key,
  restriction,
  TYPES,
  UOMS,
  type DrawingPlan,
  type Evidence,
} from '../features/hank-engineering/model';
import { readDrawing } from '../features/hank-engineering/pdf';
import {
  createDraftWO,
  matchEngineering,
  saveBOM,
  saveParts,
  saveRouting,
  type Guard,
  type Receipt,
  type Snapshot,
} from '../features/hank-engineering/entry';

function TextField({
  label,
  value,
  onChange,
  type = 'text',
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
}) {
  return (
    <FormField label={label}>
      {props => (
        <input
          {...props}
          className="input w-full"
          type={type}
          value={value}
          onChange={event => onChange(event.target.value)}
        />
      )}
    </FormField>
  );
}
function Choice({
  label,
  value,
  choices,
  onChange,
}: {
  label: string;
  value: string;
  choices: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <FormField label={label}>
      {props => (
        <select {...props} className="input w-full" value={value} onChange={event => onChange(event.target.value)}>
          <option value="">Review and select</option>
          {choices.map(choice => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      )}
    </FormField>
  );
}
function Check({ label, value, onChange }: { label: string; value: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="flex items-start gap-2 text-sm">
      <input className="mt-1" type="checkbox" checked={value} onChange={event => onChange(event.target.checked)} />
      {label}
    </label>
  );
}
function EvidenceFields({ row, onChange }: { row: Evidence; onChange: (change: Partial<Evidence>) => void }) {
  return (
    <div className="grid gap-3 md:grid-cols-[100px_1fr]">
      <TextField
        label="Source page"
        type="number"
        value={String(row.page)}
        onChange={value => onChange({ page: Number(value), confirmed: false })}
      />
      <TextField
        label="Drawing evidence / approval for supplied values"
        value={row.note}
        onChange={value => onChange({ note: value, confirmed: false })}
      />
      <div className="md:col-span-2">
        <Check
          label="Confirmed against source; supplied values have explicit approval recorded above"
          value={row.confirmed}
          onChange={confirmed => onChange({ confirmed })}
        />
      </div>
    </div>
  );
}
const choices = (values: string[]) => values.map(value => ({ value, label: value.replace('_', ' ') }));

export default function HankEngineering() {
  const [scope, setScope] = useState(getHankSessionScope());
  useEffect(() => subscribeHankSession(() => setScope(getHankSessionScope())), []);
  return <EngineeringWorkspace key={scope || 'signed-out'} scope={scope} />;
}

function EngineeringWorkspace({ scope }: { scope: string | null }) {
  const { canAll, can } = usePermissions();
  const [plan, setPlan] = useState<DrawingPlan | null>(null);
  const [pdfUrl, setPdfUrl] = useState('');
  const [centers, setCenters] = useState<WorkCenter[]>([]);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [receipts, setReceipts] = useState<Receipt[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const [started, setStarted] = useState(false);
  const [message, setMessage] = useState('');
  const lock = useRef(false);
  const mounted = useRef(true);
  const canWrite = !!scope && !isHankReadOnlySession() && canAll(['parts:create', 'boms:create', 'routings:create']);
  const { confirmDiscard } = useUnsavedChanges(!!plan);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(
    () => () => {
      if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    },
    [pdfUrl]
  );
  const guard: Guard = () => {
    if (!mounted.current || !scope || getHankSessionScope() !== scope || isHankReadOnlySession())
      throw new Error('Session changed. Reopen Engineering in the current company.');
  };
  const edit = (change: Partial<DrawingPlan>) => {
    if (!plan || started || lock.current) return;
    setPlan({ ...plan, ...change, reviewed: change.reviewed ?? false });
    setSnapshot(null);
    setMessage('');
  };
  const record = (receipt: Receipt) => {
    guard();
    setReceipts(rows => [...rows.filter(row => !(row.kind === receipt.kind && row.id === receipt.id)), receipt]);
  };
  const run = async (action: 'match' | 'parts' | 'bom' | 'routing' | 'wo') => {
    if (!plan || lock.current || !canWrite || (uncertain && action !== 'match')) return;
    lock.current = true;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      guard();
      if (action === 'match') {
        setSnapshot(await matchEngineering(plan, guard));
        setMessage('Current records matched. Review missing records and existing links below.');
      } else {
        if (!snapshot) throw new Error('Match existing records before saving.');
        setStarted(true);
        await { parts: saveParts, bom: saveBOM, routing: saveRouting, wo: createDraftWO }[action](plan, guard, record);
        guard();
        setSnapshot(await matchEngineering(plan, guard));
        setMessage(
          action === 'wo'
            ? 'Draft Work Order saved and read back. No production release was requested.'
            : 'Saved records were read back and verified.'
        );
      }
    } catch (failure) {
      if (mounted.current && getHankSessionScope() === scope) {
        setError(
          failure instanceof Error ? failure.message : 'The action failed. Inspect existing records before retrying.'
        );
        if (action !== 'match') setUncertain(true);
      }
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const upload = async (file?: File) => {
    if (!file || lock.current || (plan && !confirmDiscard())) return;
    lock.current = true;
    setBusy(true);
    setError('');
    try {
      guard();
      const next = await readDrawing(file);
      guard();
      setPlan(next);
      setPdfUrl(URL.createObjectURL(file));
      setSnapshot(null);
      setReceipts([]);
      setStarted(false);
      setUncertain(false);
      if (!restriction.test(next.pages.join('\n'))) {
        const rows = await api.getWorkCenters();
        guard();
        setCenters(rows);
      }
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : 'Could not read this drawing.');
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  };
  const blocked = !!plan && restriction.test([...plan.pages, ...plan.requirements.map(row => row.text)].join('\n'));
  const problems = plan ? issues(plan) : [];
  const exportReview = () => {
    if (!plan || blocked || !plan.screened) return;
    const blob = new Blob([JSON.stringify({ ...plan, receipts }, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'bom.json';
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <main className="space-y-5 max-w-7xl mx-auto p-4">
      <header>
        <h1 className="text-2xl font-semibold">Hank Drawing Engineering</h1>
        <p className="text-fd-mute mt-2">
          Read a drawing, review its evidence, match existing records, then save Parts, BOM and Routing. Create a Draft
          Work Order only when requested.
        </p>
      </header>
      <p className="text-sm">
        PDF reading stays on this device. Every extracted value starts unconfirmed. Check all sheets and restrictive
        markings before entering any information into ERP. No cloud OCR, pricing, stock changes or Work Order release.
      </p>
      {!canWrite && (
        <p role="alert">
          Engineering entry requires a signed-in author with Parts, BOM and Routing create permissions. Read-only and
          kiosk sessions cannot enter records.
        </p>
      )}
      <FormField label="Fabrication, assembly or detail PDF (up to 25 pages)">
        {props => (
          <input
            {...props}
            type="file"
            accept=".pdf,application/pdf"
            disabled={busy || !canWrite || started}
            onChange={event => {
              void upload(event.target.files?.[0]);
              event.target.value = '';
            }}
          />
        )}
      </FormField>
      {busy && <p role="status">Working. Keep this page open.</p>}
      {error && (
        <p role="alert" className="text-red-500">
          {error}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      {blocked && (
        <p role="alert">
          Restriction marking found. Stop and ask Jon. This drawing has not been uploaded and entry/export is blocked.
        </p>
      )}
      {plan && !blocked && (
        <>
          <section className="grid gap-4 lg:grid-cols-2" aria-label="Drawing evidence">
            <PdfPreview url={pdfUrl} fileName={plan.filename} title="Source drawing" />
            <div className="space-y-3">
              <h2 className="text-lg font-semibold">Source and review flags</h2>
              <p>
                {plan.filename} · {plan.pages.length} pages
              </p>
              <p className="break-all text-xs">SHA-256: {plan.sha256}</p>
              <ul className="list-disc pl-5 text-sm">
                {plan.flags.map((flag, i) => (
                  <li key={i}>{flag}</li>
                ))}
              </ul>
              {plan.pages.map((text, i) => (
                <details key={i}>
                  <summary>Extracted page {i + 1}</summary>
                  <pre className="whitespace-pre-wrap text-xs max-h-48 overflow-auto">
                    {text || 'No native text. Review the rendered sheet.'}
                  </pre>
                </details>
              ))}
            </div>
          </section>
          <fieldset disabled={busy || started || !canWrite} className="space-y-6 disabled:opacity-75">
            <legend className="text-lg font-semibold">Review drawing information</legend>
            <div className="grid gap-3 md:grid-cols-2">
              {(['number', 'revision', 'title', 'customer'] as const).map(field => (
                <TextField
                  key={field}
                  label={`Drawing ${field}`}
                  value={plan[field]}
                  onChange={value => edit({ [field]: value })}
                />
              ))}
            </div>
            <Check
              label="I inspected every page: no ITAR, CUI, do-not-transmit or uncertain restrictions; ERP entry is allowed"
              value={plan.screened}
              onChange={screened => edit({ screened })}
            />
            <section className="space-y-3" aria-label="Reviewed parts">
              <h2 className="text-lg font-semibold">Parts</h2>
              <p className="text-sm">
                Use printed PNs. A proposed PN needs Jon's specific approval in its evidence. Unknown revisions,
                make/buy choices and units remain blank. Raw material SKU = grade only; put the full specification in
                Description.
              </p>
              {plan.parts.map((part, i) => {
                const update = (change: Partial<typeof part>) =>
                  edit({
                    parts: plan.parts.map((row, index) =>
                      index === i ? { ...row, ...change, confirmed: change.confirmed ?? false } : row
                    ),
                  });
                return (
                  <fieldset key={i} className="border border-fd-line rounded p-3 space-y-3">
                    <legend>
                      Part {i + 1} · {part.confirmed ? 'Confirmed' : 'Needs review'}
                    </legend>
                    <div className="grid gap-3 md:grid-cols-3">
                      <TextField label="Part number" value={part.pn} onChange={pn => update({ pn })} />
                      <TextField label="Revision" value={part.revision} onChange={revision => update({ revision })} />
                      <TextField label="Part name" value={part.name} onChange={name => update({ name })} />
                      <Choice
                        label="Make / buy type"
                        value={part.type}
                        choices={choices(TYPES)}
                        onChange={type => update({ type: type as typeof part.type })}
                      />
                      <Choice
                        label="Stocking unit"
                        value={part.uom}
                        choices={choices(UOMS)}
                        onChange={uom => update({ uom })}
                      />
                      <TextField
                        label="Material grade (raw material only)"
                        value={part.grade}
                        onChange={grade => update({ grade })}
                      />
                    </div>
                    <TextField
                      label="Description / full material specification"
                      value={part.description}
                      onChange={description => update({ description })}
                    />
                    <EvidenceFields row={part} onChange={update} />
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => edit({ parts: plan.parts.filter((_, index) => index !== i) })}
                    >
                      Remove part {i + 1}
                    </button>
                  </fieldset>
                );
              })}
              <button
                type="button"
                className="btn-secondary"
                onClick={() => edit({ parts: [...plan.parts, blankPart()] })}
              >
                Add part
              </button>
              <Choice
                label="Owner part for BOM, Routing and requested Work Order"
                value={plan.owner}
                choices={plan.parts.filter(p => p.pn).map(p => ({ value: p.pn, label: p.pn }))}
                onChange={owner => edit({ owner })}
              />
            </section>
            <section className="space-y-3" aria-label="Reviewed components">
              <h2 className="text-lg font-semibold">BOM quantities per one parent</h2>
              <p className="text-sm">Leave empty for a single-piece job without a BOM. Job quantity is separate.</p>
              {plan.components.map((line, i) => {
                const update = (change: Partial<typeof line>) =>
                  edit({
                    components: plan.components.map((row, index) =>
                      index === i ? { ...row, ...change, confirmed: change.confirmed ?? false } : row
                    ),
                  });
                return (
                  <fieldset key={i} className="border border-fd-line rounded p-3 space-y-3">
                    <legend>
                      Component {i + 1} · {line.confirmed ? 'Confirmed' : 'Needs review'}
                    </legend>
                    <div className="grid gap-3 md:grid-cols-3">
                      <Choice
                        label="Component PN"
                        value={line.pn}
                        choices={plan.parts
                          .filter(p => p.pn && key(p.pn) !== key(plan.owner))
                          .map(p => ({ value: p.pn, label: p.pn }))}
                        onChange={pn => update({ pn })}
                      />
                      <TextField
                        label="Quantity per parent"
                        value={line.quantity}
                        onChange={quantity => update({ quantity })}
                      />
                      <Choice
                        label="BOM unit"
                        value={line.uom}
                        choices={choices(UOMS)}
                        onChange={uom => update({ uom })}
                      />
                    </div>
                    <EvidenceFields row={line} onChange={update} />
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => edit({ components: plan.components.filter((_, index) => index !== i) })}
                    >
                      Remove component {i + 1}
                    </button>
                  </fieldset>
                );
              })}
              <button
                type="button"
                className="btn-secondary"
                onClick={() => edit({ components: [...plan.components, blankComponent()] })}
              >
                Add component
              </button>
            </section>
            <section className="space-y-3" aria-label="Manufacturing requirements">
              <h2 className="text-lg font-semibold">Manufacturing requirements</h2>
              <p className="text-sm">
                These are literal drawing notes, not inferred processes. Confirm material, finish, weld, tolerance and
                inspection requirements. Include each applicable requirement in the operation instructions.
              </p>
              {plan.requirements.map((req, i) => {
                const update = (change: Partial<typeof req>) =>
                  edit({
                    requirements: plan.requirements.map((row, index) =>
                      index === i ? { ...row, ...change, confirmed: change.confirmed ?? false } : row
                    ),
                  });
                return (
                  <fieldset key={i} className="border border-fd-line rounded p-3 space-y-3">
                    <legend>Requirement {i + 1}</legend>
                    <TextField label="Requirement" value={req.text} onChange={text => update({ text })} />
                    <EvidenceFields row={req} onChange={update} />
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => edit({ requirements: plan.requirements.filter((_, index) => index !== i) })}
                    >
                      Remove non-requirement {i + 1}
                    </button>
                  </fieldset>
                );
              })}
              <button
                type="button"
                className="btn-secondary"
                onClick={() =>
                  edit({ requirements: [...plan.requirements, { text: '', page: 1, note: '', confirmed: false }] })
                }
              >
                Add requirement
              </button>
            </section>
            <section className="space-y-3" aria-label="Reviewed routing">
              <h2 className="text-lg font-semibold">Routing owned by {plan.owner || 'selected part'}</h2>
              <p className="text-sm">
                Review the process sequence, exact work centers and approved times. No process, center substitution or
                time is inferred. Titles use PN - description. Mark inspection points from the drawing. Outside
                processing requires its normal Routing review.
              </p>
              {plan.operations.map((op, i) => {
                const update = (change: Partial<typeof op>) =>
                  edit({
                    operations: plan.operations.map((row, index) =>
                      index === i ? { ...row, ...change, confirmed: change.confirmed ?? false } : row
                    ),
                  });
                return (
                  <fieldset key={i} className="border border-fd-line rounded p-3 space-y-3">
                    <legend>
                      Operation {op.sequence} · {op.confirmed ? 'Confirmed' : 'Needs review'}
                    </legend>
                    <div className="grid gap-3 md:grid-cols-3">
                      <TextField
                        label="Sequence"
                        value={String(op.sequence)}
                        onChange={sequence => update({ sequence: Number(sequence) })}
                      />
                      <Choice
                        label="Operation part"
                        value={op.component}
                        choices={plan.parts.filter(p => p.pn).map(p => ({ value: p.pn, label: p.pn }))}
                        onChange={component => update({ component })}
                      />
                      <TextField label="Operation description" value={op.name} onChange={name => update({ name })} />
                      <Choice
                        label="Work center"
                        value={op.center}
                        choices={centers.map(center => ({
                          value: String(center.id),
                          label: `${center.code} - ${center.name}`,
                        }))}
                        onChange={center => update({ center })}
                      />
                      <TextField
                        label="Approved setup minutes"
                        value={op.setup}
                        onChange={setup => update({ setup })}
                      />
                      <TextField
                        label="Approved run minutes per unit"
                        value={op.run}
                        onChange={run => update({ run })}
                      />
                    </div>
                    <Check
                      label="Drawing requires an inspection point"
                      value={op.inspection}
                      onChange={inspection => update({ inspection })}
                    />
                    <TextField
                      label="Work instructions / manufacturing requirements"
                      value={op.instructions}
                      onChange={instructions => update({ instructions })}
                    />
                    <EvidenceFields row={op} onChange={update} />
                    <button
                      type="button"
                      className="btn-secondary"
                      onClick={() => edit({ operations: plan.operations.filter((_, index) => index !== i) })}
                    >
                      Remove operation {op.sequence}
                    </button>
                  </fieldset>
                );
              })}
              <button
                type="button"
                className="btn-secondary"
                onClick={() =>
                  edit({ operations: [...plan.operations, blankOperation((plan.operations.length + 1) * 10)] })
                }
              >
                Add operation
              </button>
            </section>
            <section className="space-y-3">
              <h2 className="text-lg font-semibold">Authorization and unresolved information</h2>
              <TextField
                label="Review notes: missing facts, conflicts, assumptions and explicit resolutions / entry authorization"
                value={plan.reviewNotes}
                onChange={reviewNotes => edit({ reviewNotes })}
              />
              <Check
                label="A new Draft Work Order was explicitly requested"
                value={plan.requestedWO}
                onChange={requestedWO => edit({ requestedWO })}
              />
              {plan.requestedWO && (
                <div className="grid gap-3 md:grid-cols-2">
                  <TextField
                    label="Authorized job quantity"
                    value={plan.quantity}
                    onChange={quantity => edit({ quantity })}
                  />
                  <TextField
                    label="Job quantity and WO authorization source"
                    value={plan.quantitySource}
                    onChange={quantitySource => edit({ quantitySource })}
                  />
                </div>
              )}
              <Check
                label="All sheets, values and omissions reviewed; no unresolved assumptions or conflicts; Engineering entry authorized"
                value={plan.reviewed}
                onChange={reviewed => edit({ reviewed })}
              />
            </section>
          </fieldset>
          <section className="space-y-3" aria-label="Engineering entry">
            <h2 className="text-lg font-semibold">Match, then enter through this signed-in screen</h2>
            {problems.length > 0 && (
              <ul className="list-disc pl-5 text-sm" aria-label="Review blockers">
                {problems.map(problem => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            )}
            <div className="flex flex-wrap gap-2">
              <button
                className="btn-primary"
                disabled={busy || !canWrite || !!problems.length}
                onClick={() => void run('match')}
              >
                Match existing records
              </button>
              <button className="btn-secondary" disabled={busy || !plan.screened} onClick={exportReview}>
                Download review and receipts
              </button>
            </div>
            {snapshot && (
              <>
                <ul className="list-disc pl-5">
                  {plan.parts.map(part => (
                    <li key={part.pn}>
                      {part.pn}: {snapshot.parts.has(key(part.pn)) ? 'exact match' : 'missing; will create'}{' '}
                      {snapshot.parts.has(key(part.pn)) && (
                        <Link
                          target="_blank"
                          rel="noopener noreferrer"
                          to={`/parts/${snapshot.parts.get(key(part.pn))!.id}`}
                        >
                          Open part
                        </Link>
                      )}
                    </li>
                  ))}
                </ul>
                <p>
                  BOM:{' '}
                  {snapshot.bom
                    ? `${snapshot.bom.status}, matched`
                    : plan.components.length
                      ? 'missing'
                      : 'not requested'}{' '}
                  · Routing: {snapshot.routing ? `${snapshot.routing.status}, matched` : 'missing'}
                </p>
                <div className="flex flex-wrap gap-2">
                  <button
                    className="btn-primary"
                    disabled={busy || uncertain || !canWrite}
                    onClick={() => void run('parts')}
                  >
                    Save missing Parts
                  </button>
                  <button
                    className="btn-primary"
                    disabled={
                      busy ||
                      uncertain ||
                      !canWrite ||
                      !plan.components.length ||
                      snapshot.parts.size !== plan.parts.length
                    }
                    onClick={() => void run('bom')}
                  >
                    Save Draft BOM
                  </button>
                  <button
                    className="btn-primary"
                    disabled={busy || uncertain || !canWrite || snapshot.parts.size !== plan.parts.length}
                    onClick={() => void run('routing')}
                  >
                    Save Draft Routing
                  </button>
                  {plan.requestedWO && (
                    <button
                      className="btn-primary"
                      disabled={
                        busy ||
                        uncertain ||
                        !canWrite ||
                        !can('work_orders:create') ||
                        snapshot.routing?.status !== 'released' ||
                        (!!plan.components.length && snapshot.bom?.status !== 'released') ||
                        receipts.some(r => r.kind === 'work_order')
                      }
                      onClick={() => void run('wo')}
                    >
                      Create requested Draft Work Order
                    </button>
                  )}
                </div>
                <p className="text-sm">
                  Open the saved BOM and Routing in their normal screens to review and authorize Engineering release.
                  Return here and match again before creating the Draft WO. Released or conflicting records are never
                  overwritten.
                </p>
                {snapshot.bom && (
                  <Link
                    className="mr-4 underline"
                    target="_blank"
                    rel="noopener noreferrer"
                    to={`/bom?id=${snapshot.bom.id}`}
                  >
                    Review BOM
                  </Link>
                )}
                {snapshot.routing && (
                  <Link
                    className="underline"
                    target="_blank"
                    rel="noopener noreferrer"
                    to={`/routing?id=${snapshot.routing.id}`}
                  >
                    Review Routing
                  </Link>
                )}
              </>
            )}
            {uncertain && (
              <p role="alert">
                Entry stopped. Some records may already be saved. Open the receipts and match existing records before
                any manual recovery. Automatic retry is disabled to prevent duplicates.
              </p>
            )}
          </section>
          {receipts.length > 0 && (
            <section aria-label="Saved record receipts">
              <h2 className="text-lg font-semibold">Observed records</h2>
              <ul>
                {receipts.map(receipt => (
                  <li key={`${receipt.kind}:${receipt.id}`}>
                    <Link className="underline" target="_blank" rel="noopener noreferrer" to={receipt.href}>
                      {receipt.label}
                    </Link>{' '}
                    · {receipt.action}
                    {receipt.status ? ` · ${receipt.status}` : ''}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </main>
  );
}
