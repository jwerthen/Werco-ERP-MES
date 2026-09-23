import React, { useEffect, useMemo, useRef, useState } from 'react';
import { isAxiosError } from 'axios';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { HankIntakeFile, HankIntakePurchaseOrderDraft } from '../../types/hankIntake';
import type { Part } from '../../types';
import { ComboBox } from '../ui/ComboBox';
import type { HankCapabilities, HankTask, HankTaskCreate } from '../../types/hankTasks';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { HankSourceFile } from './HankSourceFile';
import { HankTaskWorkflow } from './HankTaskWorkflow';
import { hankEvidenceLabel } from './hankDocumentEvidence';
import { useHankSessionGuard } from './useHankSessionGuard';

type ReviewedLine = { part_id: string; quantity: string; unit_price: string; unit_of_measure: string };
const UNIT_ALIASES: Record<string, string> = {
  ea: 'each',
  pc: 'each',
  pcs: 'each',
  piece: 'each',
  pieces: 'each',
  ft: 'feet',
  foot: 'feet',
  in: 'inches',
  inch: 'inches',
  lb: 'pounds',
  lbs: 'pounds',
  pound: 'pounds',
  kg: 'kilograms',
  kilogram: 'kilograms',
  sheet: 'sheets',
  gal: 'gallons',
  gallon: 'gallons',
  l: 'liters',
  liter: 'liters',
};
const normalizedUnit = (value: string | null | undefined) => {
  const unit = (value || '').trim().toLowerCase();
  return UNIT_ALIASES[unit] || unit;
};
const validId = (value: string) => /^[1-9]\d*$/.test(value) && Number.isSafeInteger(Number(value));
const validDate = (value: string) =>
  !value ||
  (/^\d{4}-\d{2}-\d{2}$/.test(value) &&
    Number.isFinite(Date.parse(`${value}T00:00:00Z`)) &&
    new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) === value);

/** Source review saves a proposal. Existing task confirmation owns every ERP write. */
export function HankDocumentPurchaseOrder({
  file,
  onNavigate,
  onBusyChange,
}: {
  file: HankIntakeFile;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [draft, setDraft] = useState<HankIntakePurchaseOrderDraft | null>(null);
  const [capabilities, setCapabilities] = useState<HankCapabilities | null>(null);
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<HankTaskCreate | null>(null);
  const [task, setTask] = useState<HankTask | null>(null);
  const [vendorId, setVendorId] = useState('');
  const [poNumber, setPONumber] = useState('');
  const [orderDate, setOrderDate] = useState('');
  const [requiredDate, setRequiredDate] = useState('');
  const [readyForReceiving, setReadyForReceiving] = useState(false);
  const [lines, setLines] = useState<ReviewedLine[]>([]);
  const [parts, setParts] = useState<Part[]>([]);
  const partOptions = useMemo(
    () =>
      parts.map(part => ({
        value: String(part.id),
        label: `${part.part_number} — ${part.name}`,
        hint: `Stocking unit ${part.unit_of_measure}`,
      })),
    [parts]
  );
  const flight = useRef(false);
  const callback = useRef(onBusyChange);
  callback.current = onBusyChange;
  const { current, controller, release, changed } = useHankSessionGuard();
  useEffect(() => () => callback.current?.(false), []);
  useEffect(() => {
    callback.current?.(busy || !!pending);
  }, [busy, pending]);
  useEffect(() => {
    const request = controller();
    setLoading(true);
    setError('');
    setDraft(null);
    Promise.all([
      api.getHankIntakePurchaseOrderDraft(file.id, request.signal),
      api.getHankCapabilities(request.signal),
      api.getParts({ active_only: true, item_group: 'all' }),
    ])
      .then(([value, cap, catalog]) => {
        if (!current() || request.signal.aborted) return;
        if (value.company_id !== file.company_id || cap.company_id !== file.company_id) {
          setError('The document belongs to another company. Reopen it from your current workspace.');
          return;
        }
        setDraft(value);
        setCapabilities(cap);
        setParts(catalog);
        setVendorId(value.vendor_id ? String(value.vendor_id) : '');
        setPONumber(value.po_number || '');
        setOrderDate(value.order_date?.slice(0, 10) || '');
        setRequiredDate(value.required_date?.slice(0, 10) || '');
        setReadyForReceiving(value.can_ready_for_receiving);
        setLines(
          value.lines.map(line => ({
            part_id: line.part_id ? String(line.part_id) : '',
            quantity: line.quantity_ordered == null ? '' : String(line.quantity_ordered),
            unit_price: line.unit_price_amount == null ? '' : String(line.unit_price_amount),
            unit_of_measure: line.part_id
              ? line.candidates.find(candidate => candidate.id === line.part_id)?.unit_of_measure || ''
              : '',
          }))
        );
      })
      .catch(cause => {
        if (!current() || request.signal.aborted) return;
        const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
        setError(typeof detail === 'string' ? detail : 'Purchase order suggestions could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setLoading(false);
      });
    return () => request.abort();
  }, [file.id, file.company_id, attempt, current, controller, release]);
  const prepare = async (event?: React.FormEvent) => {
    event?.preventDefault();
    if (
      !current() ||
      flight.current ||
      !draft ||
      !capabilities?.can_write ||
      !capabilities.allowed_kinds.includes('draft_purchase_order') ||
      draft.blocked_reason
    )
      return;
    let proposal = pending;
    if (!proposal) {
      const included = lines;
      if (
        !validId(vendorId) ||
        !poNumber.trim() ||
        poNumber.trim().length > 50 ||
        !validDate(orderDate) ||
        !validDate(requiredDate) ||
        !included.length ||
        included.some(
          line =>
            !line.unit_of_measure.trim() ||
            !validId(line.part_id) ||
            !line.quantity ||
            !Number.isFinite(Number(line.quantity)) ||
            Number(line.quantity) <= 0 ||
            !line.unit_price ||
            !Number.isFinite(Number(line.unit_price)) ||
            Number(line.unit_price) < 0
        )
      ) {
        setError(
          'Review the PO number, vendor, dates, and every included line. Each line needs an existing part, its stocking unit, a quantity greater than zero, and a unit price of zero or more.'
        );
        return;
      }
      proposal = {
        expected_company_id: draft.company_id,
        request_key: crypto.randomUUID(),
        kind: 'draft_purchase_order',
        input: {
          source_intake_file_id: draft.file_id,
          source_intake_version: draft.file_version,
          po_number: poNumber.trim(),
          order_date: orderDate || null,
          required_date: requiredDate || null,
          vendor_id: Number(vendorId),
          ready_for_receiving: readyForReceiving && draft.can_ready_for_receiving,
          lines: included.map((line, index) => ({
            source_line_index: draft.lines[index].source_line_index,
            unit_of_measure: line.unit_of_measure.trim(),
            part_id: Number(line.part_id),
            quantity_ordered: Number(line.quantity),
            unit_price: Number(line.unit_price),
            required_date: requiredDate || null,
          })),
        },
      };
      setPending(proposal);
    }
    flight.current = true;
    setBusy(true);
    setError('');
    const request = controller();
    try {
      const saved = await api.createHankTask(proposal, request.signal);
      if (current() && !request.signal.aborted && saved.company_id === file.company_id) {
        setTask(saved);
        setPending(null);
      }
    } catch (cause) {
      if (!current() || request.signal.aborted) return;
      const status = isAxiosError(cause) ? cause.response?.status : undefined;
      const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      setError(
        typeof detail === 'string'
          ? detail
          : 'The proposal was not confirmed. Retry this same proposal to recover its status.'
      );
      if (status && status >= 400 && status < 500 && status !== 408 && status !== 429) setPending(null);
    } finally {
      release(request);
      flight.current = false;
      if (current()) setBusy(false);
    }
  };
  if (changed)
    return (
      <p role="alert" className="text-xs text-fd-amber">
        Your session changed. Reopen Hank to import the purchase order.
      </p>
    );
  if (task)
    return (
      <HankTaskWorkflow
        initialTask={task}
        onTaskChanged={setTask}
        onNavigate={onNavigate}
        onBusyChange={onBusyChange}
        onStartAnother={() => {
          setTask(null);
          setAttempt(value => value + 1);
        }}
      />
    );
  const disabled =
    busy ||
    !!pending ||
    !capabilities?.can_write ||
    !capabilities.allowed_kinds.includes('draft_purchase_order') ||
    !!draft?.blocked_reason;
  const changeLine = (index: number, update: Partial<ReviewedLine>) =>
    setLines(previous => previous.map((line, item) => (item === index ? { ...line, ...update } : line)));
  const selectPart = (index: number, partId: string) => {
    const unit = parts.find(part => String(part.id) === partId)?.unit_of_measure || '';
    const previousUnit = lines[index]?.unit_of_measure || draft?.lines[index].unit_of_measure;
    const sameUnit = normalizedUnit(unit) && normalizedUnit(unit) === normalizedUnit(previousUnit);
    changeLine(index, {
      part_id: partId,
      unit_of_measure: unit,
      // A different stocking unit needs both values converted by the employee.
      // Keeping the old unit price would silently misprice the imported order.
      ...(!sameUnit ? { quantity: '', unit_price: '' } : {}),
    });
  };
  return (
    <section aria-label="Create purchase order from document" className="space-y-4">
      <h3 className="text-sm font-semibold text-fd-ink break-words">Create purchase order from {file.filename}</h3>
      <p className="text-xs text-fd-mute">
        Verify the source and match the vendor and parts already in the system. Review a saved proposal before creating
        the purchase order.
      </p>
      <HankSourceFile
        filename={file.filename}
        pages={draft?.lines.flatMap(line => line.evidence.map(item => item.page)) || []}
        load={signal => api.getHankIntakeSource(file.id, signal)}
        loadPreview={signal => api.getHankIntakeSourcePreview(file.id, signal)}
      />
      {loading && (
        <p role="status" className="text-xs text-fd-mute">
          Matching the document to purchase order records…
        </p>
      )}
      {error && (
        <p role="alert" className="text-xs text-fd-red">
          {error}
        </p>
      )}
      {pending && (
        <p className="text-xs text-fd-amber">
          The request may have reached the server. Retry the same proposal before changing these values.
        </p>
      )}
      <button
        type="button"
        className="text-xs text-fd-blue underline"
        disabled={loading || busy || !!pending}
        onClick={() => setAttempt(value => value + 1)}
      >
        Refresh purchase order suggestions
      </button>
      {draft && (
        <>
          {draft.warnings.map((warning, index) => (
            <p key={index} className="text-xs text-fd-amber">
              {warning}
            </p>
          ))}
          {draft.blocked_reason && (
            <p role="alert" className="text-xs text-fd-amber">
              {draft.blocked_reason}
            </p>
          )}
          {draft.existing_purchase_orders?.map(po => (
            <Link key={po.id} to={po.href} onClick={onNavigate} className="block text-xs text-fd-blue underline">
              Review existing {po.po_number}
            </Link>
          ))}
          {draft.has_duplicates && (
            <p className="text-xs text-fd-amber">
              Matching source content was uploaded before. Review any existing purchase order before importing.
            </p>
          )}
          <form aria-label="Review purchase order import" onSubmit={event => void prepare(event)} className="space-y-4">
            <fieldset disabled={disabled} className="space-y-3 min-w-0">
              <FormField label="Printed PO number" required>
                {field => (
                  <input
                    {...field}
                    value={poNumber}
                    onChange={event => setPONumber(event.target.value)}
                    maxLength={50}
                    className="input w-full"
                  />
                )}
              </FormField>
              <FormField label="Vendor" required>
                {field => (
                  <EntityPicker {...field} kind="vendor" value={vendorId} onChange={setVendorId} disabled={disabled} />
                )}
              </FormField>
              {!vendorId &&
                draft.vendors.map(vendor => (
                  <button
                    key={vendor.id}
                    type="button"
                    className="block text-xs text-fd-blue underline"
                    onClick={() => setVendorId(String(vendor.id))}
                  >
                    {vendor.code} {vendor.name} · {vendor.reason}
                  </button>
                ))}
              <FormField label="Order date (optional)">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={orderDate}
                    onChange={event => setOrderDate(event.target.value)}
                    className="input w-full"
                  />
                )}
              </FormField>
              <FormField label="Required date (optional)">
                {field => (
                  <input
                    {...field}
                    type="date"
                    value={requiredDate}
                    onChange={event => setRequiredDate(event.target.value)}
                    className="input w-full"
                  />
                )}
              </FormField>
              {draft.lines.map((source, index) => (
                <div key={source.source_line_index} className="space-y-2 border border-slate-700 p-3 min-w-0">
                  <h4 className="text-xs font-semibold text-fd-ink break-words">
                    Line {index + 1}: {source.description || source.part_number || 'Unidentified item'}
                  </h4>

                  <p className="text-xs text-fd-body">
                    Source part {source.part_number || 'unknown'} · Quantity {source.quantity || 'unknown'}{' '}
                    {source.unit_of_measure || '(units unknown)'} · Unit price {source.unit_price || 'unknown'} ·{' '}
                    {source.confidence} confidence
                  </p>
                  {source.warnings.map((warning, item) => (
                    <p key={item} className="text-xs text-fd-amber">
                      {warning}
                    </p>
                  ))}
                  {source.evidence.map((evidence, item) => (
                    <p key={item} className="text-xs text-fd-mute break-words">
                      {hankEvidenceLabel(file, evidence)}: {evidence.excerpt}
                    </p>
                  ))}
                  <>
                    <FormField label={`Part for line ${index + 1}`} required>
                      {field => (
                        <ComboBox
                          id={field.id}
                          ariaDescribedBy={field['aria-describedby']}
                          options={partOptions}
                          value={lines[index]?.part_id || ''}
                          onChange={value => selectPart(index, value)}
                          disabled={disabled}
                        />
                      )}
                    </FormField>
                    {source.candidates.map(candidate => (
                      <p key={candidate.id} className="text-xs text-fd-mute">
                        {candidate.part_number} · Stocking unit {candidate.unit_of_measure}
                      </p>
                    ))}
                    <FormField
                      label={`Stocking unit for line ${index + 1}`}
                      required
                      help="Use the selected ERP part’s unit. Convert the source quantity and price explicitly if its units differ."
                    >
                      {field => (
                        <input
                          {...field}
                          value={lines[index]?.unit_of_measure || ''}
                          maxLength={20}
                          onChange={event => changeLine(index, { unit_of_measure: event.target.value })}
                          className="input w-full"
                        />
                      )}
                    </FormField>
                    <FormField label={`Quantity for line ${index + 1}`} required>
                      {field => (
                        <input
                          {...field}
                          type="number"
                          min="0"
                          step="any"
                          value={lines[index]?.quantity || ''}
                          onChange={event => changeLine(index, { quantity: event.target.value })}
                          className="input w-full"
                        />
                      )}
                    </FormField>
                    <FormField label={`Unit price for line ${index + 1}`} required>
                      {field => (
                        <input
                          {...field}
                          type="number"
                          min="0"
                          step="any"
                          value={lines[index]?.unit_price || ''}
                          onChange={event => changeLine(index, { unit_price: event.target.value })}
                          className="input w-full"
                        />
                      )}
                    </FormField>
                  </>
                </div>
              ))}
              {!draft.lines.length && (
                <p className="text-xs text-fd-amber">
                  No purchase order lines could be extracted. Check the source before creating a PO in Purchasing.
                </p>
              )}
              {draft.can_ready_for_receiving ? (
                <FormField
                  label="Add to Receiving"
                  help="Creates an issued PO that is available on the Receiving list. No vendor email is sent and no material is received yet."
                >
                  {field => (
                    <input
                      {...field}
                      type="checkbox"
                      checked={readyForReceiving}
                      onChange={event => setReadyForReceiving(event.target.checked)}
                      className="checkbox checkbox-sm"
                    />
                  )}
                </FormField>
              ) : (
                <p className="text-xs text-fd-mute">
                  Your role can create a draft PO. A purchasing approver must issue it before it appears in Receiving.
                </p>
              )}
              <p className="text-xs text-fd-mute">
                {readyForReceiving
                  ? 'The final confirmation creates this purchase order and adds it to Receiving.'
                  : 'The final confirmation creates a draft PO for review in Purchasing.'}
              </p>
            </fieldset>
            {pending ? (
              <LoadingButton type="button" size="sm" loading={busy} onClick={() => void prepare()}>
                Retry saving purchase order proposal
              </LoadingButton>
            ) : (
              <LoadingButton type="submit" size="sm" loading={busy} disabled={disabled || !lines.length}>
                Prepare purchase order for review
              </LoadingButton>
            )}
          </form>
        </>
      )}
    </section>
  );
}
