import React, { useEffect, useRef, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Link } from 'react-router-dom';
import { isAxiosError } from 'axios';
import api from '../../services/api';
import type { HankCapabilities } from '../../types/hankTasks';
import type { HankHandoff, HankHandoffCreate } from '../../types/hankWork';
import { formatCentralDateTime } from '../../utils/centralTime';
import EntityPicker from '../operations/EntityPicker';
import { FormField } from '../ui/FormField';
import { LoadingButton } from '../ui/LoadingButton';
import { HankDocumentPicker, HankDocumentChoice } from './HankDocumentPicker';
import { HankSourceFile } from './HankSourceFile';
import { isHankReadOnlySession } from './hankSession';
import { useHankSessionGuard } from './useHankSessionGuard';

const id = z.string().regex(/^[1-9]\d*$/, 'Choose a record.');
const schema = z.object({
  work_order_id: id,
  recipient_id: id,
  summary: z.string().trim().min(1, 'Describe the handoff.').max(1000),
  completed_work: z.string().max(3000),
  remaining_work: z.string().max(3000),
  problems: z.string().max(3000),
  quantity_remaining: z
    .string()
    .refine(value => !value || (Number.isFinite(Number(value)) && Number(value) >= 0), 'Enter a nonnegative quantity.'),
});
type Values = z.infer<typeof schema>;
export function HankHandoffs({
  initialId,
  workOrderId,
  onNavigate,
  onBusyChange,
}: {
  initialId?: number;
  workOrderId?: number;
  onNavigate: () => void;
  onBusyChange?: (busy: boolean) => void;
}) {
  const [cap, setCap] = useState<HankCapabilities | null>(null);
  const [people, setPeople] = useState<Array<{ id: number; name: string; role: string }>>([]);
  const [peopleError, setPeopleError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [rows, setRows] = useState<HankHandoff[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [direction, setDirection] = useState<'all' | 'sent' | 'received'>('all');
  const [listLoading, setListLoading] = useState(false);
  const [mode, setMode] = useState<'list' | 'new' | 'detail'>(initialId ? 'detail' : 'list');
  const [selectedId, setSelectedId] = useState(initialId);
  const [handoff, setHandoff] = useState<HankHandoff | null>(null);
  const [documents, setDocuments] = useState<HankDocumentChoice[]>([]);
  const [pending, setPending] = useState<HankHandoffCreate | null>(null);
  const [photo, setPhoto] = useState<File | null>(null);
  const [pendingPhoto, setPendingPhoto] = useState<{ file: File; key: string; version: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const { current, controller, release, changed } = useHankSessionGuard();
  const flight = useRef(false);
  const loadedId = useRef<number | undefined>(undefined);
  const busyCallback = useRef(onBusyChange);
  busyCallback.current = onBusyChange;
  const {
    register,
    control,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      work_order_id: String(workOrderId || ''),
      recipient_id: '',
      summary: '',
      completed_work: '',
      remaining_work: '',
      problems: '',
      quantity_remaining: '',
    },
  });
  useEffect(() => () => busyCallback.current?.(false), []);
  const navigationLocked = busy || !!pending || !!pendingPhoto;
  useEffect(() => {
    busyCallback.current?.(navigationLocked);
  }, [navigationLocked]);
  useEffect(() => {
    if (!flight.current && initialId) {
      loadedId.current = undefined;
      setSelectedId(initialId);
      setMode('detail');
    }
  }, [initialId]);
  useEffect(() => {
    const request = controller();
    setPeopleError(false);
    Promise.all([api.getHankCapabilities(request.signal), api.getHankHandoffPeople(undefined, request.signal)])
      .then(([capabilities, result]) => {
        if (current() && !request.signal.aborted) {
          setCap(capabilities);
          setPeople(result.people);
        }
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setPeopleError(true);
      })
      .finally(() => release(request));
    return () => request.abort();
  }, [attempt, current, controller, release]);
  useEffect(() => {
    if (mode !== 'list') return;
    const request = controller();
    setListLoading(true);
    setError('');
    api
      .getHankHandoffs({ direction, limit: 20 }, request.signal)
      .then(result => {
        if (current() && !request.signal.aborted) {
          setRows(result.handoffs);
          setCursor(result.has_more ? result.next_before_id : null);
        }
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setError('Handoffs could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setListLoading(false);
      });
    return () => request.abort();
  }, [mode, direction, attempt, current, controller, release]);
  useEffect(() => {
    if (mode !== 'detail' || !selectedId || loadedId.current === selectedId) return;
    const request = controller();
    setHandoff(null);
    setListLoading(true);
    setError('');
    api
      .getHankHandoff(selectedId, request.signal)
      .then(result => {
        if (current() && !request.signal.aborted) {
          loadedId.current = result.id;
          setHandoff(result);
        }
      })
      .catch(() => {
        if (current() && !request.signal.aborted) setError('This handoff could not be loaded.');
      })
      .finally(() => {
        release(request);
        if (current() && !request.signal.aborted) setListLoading(false);
      });
    return () => request.abort();
  }, [mode, selectedId, attempt, current, controller, release]);
  const run = async (
    action: 'create' | 'refresh' | 'acknowledge' | 'complete' | 'cancel' | 'photo',
    body?: HankHandoffCreate
  ) => {
    if (!current() || flight.current || !cap || (action !== 'refresh' && (!cap.can_watch || isHankReadOnlySession())))
      return;
    if (action !== 'create' && !handoff) return;
    if (needsRefresh && action !== 'refresh') return;
    flight.current = true;
    const request = controller();
    setBusy(true);
    busyCallback.current?.(true);
    setError('');
    try {
      let result: HankHandoff;
      if (action === 'create' && body) result = await api.createHankHandoff(body, request.signal);
      else if (action === 'refresh' && handoff) result = await api.getHankHandoff(handoff.id, request.signal);
      else if (action === 'photo' && handoff && (pendingPhoto || photo)) {
        const upload = pendingPhoto || { file: photo!, key: crypto.randomUUID(), version: handoff.version };
        setPendingPhoto(upload);
        const data = new FormData();
        data.append('expected_company_id', String(cap.company_id));
        data.append('expected_version', String(upload.version));
        data.append('request_key', upload.key);
        data.append('file', upload.file);
        result = await api.attachHankHandoffPhoto(handoff.id, data, request.signal);
      } else if (handoff && ['acknowledge', 'complete', 'cancel'].includes(action))
        result = await api.commandHankHandoff(
          handoff.id,
          action as 'acknowledge' | 'complete' | 'cancel',
          { expected_company_id: cap.company_id, expected_version: handoff.version },
          request.signal
        );
      else return;
      if (!current() || request.signal.aborted || result.company_id !== cap.company_id) return;
      loadedId.current = result.id;
      setHandoff(result);
      setPending(null);
      setNeedsRefresh(false);
      if (action === 'create') {
        setSelectedId(result.id);
        setMode('detail');
      }
      if (action === 'photo') {
        setPendingPhoto(null);
        setPhoto(null);
      }
    } catch (cause) {
      if (!current() || request.signal.aborted) return;
      const status = isAxiosError(cause) ? cause.response?.status : undefined;
      const detail: unknown = isAxiosError(cause) ? cause.response?.data?.detail : undefined;
      setError(
        typeof detail === 'string'
          ? detail
          : 'The request was not confirmed. Recover its saved status before continuing.'
      );
      const refused = status && status >= 400 && status < 500 && status !== 408 && status !== 429;
      if (action === 'create' && refused) setPending(null);
      if (action === 'photo' && refused) setPendingPhoto(null);
      if (action !== 'create' && action !== 'refresh' && (!refused || status === 409)) setNeedsRefresh(true);
    } finally {
      release(request);
      flight.current = false;
      if (current()) {
        setBusy(false);
      }
    }
  };
  const create = handleSubmit(values => {
    if (!cap || !current() || flight.current || pending) return;
    const body: HankHandoffCreate = {
      expected_company_id: cap.company_id,
      request_key: crypto.randomUUID(),
      work_order_id: Number(values.work_order_id),
      recipient_id: Number(values.recipient_id),
      summary: values.summary,
      completed_work: values.completed_work,
      remaining_work: values.remaining_work,
      problems: values.problems,
      quantity_remaining: values.quantity_remaining ? Number(values.quantity_remaining) : null,
      document_ids: documents.map(item => item.id),
    };
    setPending(body);
    void run('create', body);
  });
  const loadMore = async () => {
    if (!cursor || listLoading || !current()) return;
    const request = controller();
    setListLoading(true);
    setError('');
    try {
      const result = await api.getHankHandoffs({ direction, limit: 20, before_id: cursor }, request.signal);
      if (current() && !request.signal.aborted) {
        setRows(previous => [...previous, ...result.handoffs]);
        setCursor(result.has_more ? result.next_before_id : null);
      }
    } catch {
      if (current()) setError('Older handoffs could not be loaded.');
    } finally {
      release(request);
      if (current()) setListLoading(false);
    }
  };
  if (changed)
    return (
      <p role="alert" className="text-sm text-fd-amber">
        Your session changed. Reopen Hank to see your handoffs.
      </p>
    );
  const canCreate = !!cap?.can_watch && !isHankReadOnlySession();
  return (
    <section aria-label="Hank handoffs" className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-fd-ink flex-1">Shift handoffs</h3>
        <button
          type="button"
          className="btn text-xs"
          disabled={navigationLocked || !canCreate}
          onClick={() => {
            loadedId.current = undefined;
            setNeedsRefresh(false);
            setPending(null);
            setDocuments([]);
            reset({
              work_order_id: String(workOrderId || ''),
              recipient_id: '',
              summary: '',
              completed_work: '',
              remaining_work: '',
              problems: '',
              quantity_remaining: '',
            });
            setMode('new');
            setHandoff(null);
            setPendingPhoto(null);
            setPhoto(null);
            setError('');
          }}
        >
          New handoff
        </button>
        {mode !== 'list' && (
          <button
            type="button"
            className="text-xs text-fd-blue underline"
            disabled={navigationLocked}
            onClick={() => setMode('list')}
          >
            Your handoffs
          </button>
        )}
      </div>
      <p className="text-xs text-fd-mute">
        Share a named job handoff with one coworker. It stays visible to its sender and recipient.
      </p>
      {peopleError && (
        <p role="alert" className="text-xs text-fd-red">
          Handoff access or coworkers could not be loaded.{' '}
          <button type="button" className="underline" onClick={() => setAttempt(value => value + 1)}>
            Retry handoff access
          </button>
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}{' '}
          {mode !== 'new' && !handoff && (
            <button type="button" className="underline" onClick={() => setAttempt(value => value + 1)}>
              Retry handoff loading
            </button>
          )}
        </p>
      )}
      {listLoading && (
        <p role="status" className="text-xs text-fd-mute">
          Loading handoffs…
        </p>
      )}
      {mode === 'list' && (
        <>
          <FormField label="Handoff direction">
            {field => (
              <select
                {...field}
                value={direction}
                className="input w-full"
                onChange={event => setDirection(event.target.value as typeof direction)}
              >
                <option value="all">All your handoffs</option>
                <option value="received">Received</option>
                <option value="sent">Sent</option>
              </select>
            )}
          </FormField>
          {rows.map(item => (
            <button
              key={item.id}
              type="button"
              className="block w-full text-left border border-slate-700 p-3 space-y-1"
              onClick={() => {
                loadedId.current = undefined;
                setSelectedId(item.id);
                setMode('detail');
                setNeedsRefresh(false);
              }}
            >
              <span className="block text-sm text-fd-blue">{item.summary}</span>
              <span className="block text-xs text-fd-mute">
                {item.work_order_number} · {item.sender.name} → {item.recipient.name} · {item.status}
              </span>
            </button>
          ))}
          {!listLoading && !error && !rows.length && <p className="text-xs text-fd-mute">No handoffs in this view.</p>}
          {cursor && (
            <button type="button" className="btn text-xs" disabled={listLoading} onClick={() => void loadMore()}>
              Older handoffs
            </button>
          )}
        </>
      )}
      {mode === 'new' && (
        <form aria-label="Create handoff" onSubmit={create} className="space-y-4">
          <fieldset disabled={busy || !!pending || !canCreate} className="space-y-3">
            <FormField label="Work order" required error={errors.work_order_id?.message}>
              {field => (
                <Controller
                  name="work_order_id"
                  control={control}
                  render={({ field: input }) => (
                    <EntityPicker
                      {...field}
                      kind="workOrder"
                      value={input.value}
                      onChange={input.onChange}
                      disabled={busy || !!pending}
                    />
                  )}
                />
              )}
            </FormField>
            <FormField label="Hand off to" required error={errors.recipient_id?.message}>
              {field => (
                <select {...field} {...register('recipient_id')} className="input w-full">
                  <option value="">Choose coworker…</option>
                  {people.map(person => (
                    <option key={person.id} value={person.id}>
                      {person.name} · {person.role}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            {(['summary', 'completed_work', 'remaining_work', 'problems'] as const).map(name => (
              <FormField
                key={name}
                label={name.replace(/_/g, ' ')}
                required={name === 'summary'}
                error={errors[name]?.message}
              >
                {field => (
                  <textarea
                    {...field}
                    {...register(name)}
                    className="input w-full h-auto"
                    rows={name === 'summary' ? 2 : 3}
                  />
                )}
              </FormField>
            ))}
            <FormField label="Quantity remaining" error={errors.quantity_remaining?.message}>
              {field => (
                <input
                  {...field}
                  {...register('quantity_remaining')}
                  type="number"
                  min="0"
                  step="any"
                  className="input w-full"
                />
              )}
            </FormField>
            <HankDocumentPicker
              value=""
              label="Document to include"
              disabled={busy || !!pending || documents.length >= 10}
              onChange={(_value, row) => {
                if (row && !documents.some(item => item.id === row.id)) setDocuments(previous => [...previous, row]);
              }}
            />
            {documents.map(document => (
              <div key={document.id} className="flex items-center justify-between gap-2 text-xs text-fd-body">
                <span>
                  {document.document_number} · {document.title}
                </span>
                <button
                  type="button"
                  className="underline"
                  onClick={() => setDocuments(items => items.filter(item => item.id !== document.id))}
                >
                  Remove
                </button>
              </div>
            ))}
          </fieldset>
          <p className="text-xs text-fd-mute">
            Sending creates a private in-app notification for the selected coworker.
          </p>
          {pending ? (
            <LoadingButton type="button" size="sm" loading={busy} onClick={() => void run('create', pending)}>
              Retry same handoff
            </LoadingButton>
          ) : (
            <LoadingButton type="submit" size="sm" loading={busy} disabled={!canCreate}>
              Send handoff
            </LoadingButton>
          )}
        </form>
      )}
      {mode === 'detail' && handoff && (
        <div className="space-y-3">
          <h4 className="text-sm font-semibold text-fd-ink">{handoff.summary}</h4>
          <p className="text-xs text-fd-mute">
            {handoff.sender.name} → {handoff.recipient.name} · {handoff.status} · Updated{' '}
            {formatCentralDateTime(handoff.updated_at)}
          </p>
          <Link
            to={`/work-orders/${handoff.work_order_id}`}
            onClick={onNavigate}
            className="text-xs text-fd-blue underline"
          >
            {handoff.work_order_number}
          </Link>
          {(['completed_work', 'remaining_work', 'problems'] as const).map(name => (
            <div key={name}>
              <p className="text-xs font-semibold text-fd-ink">{name.replace(/_/g, ' ')}</p>
              <p className="text-xs text-fd-body whitespace-pre-wrap">{handoff[name] || 'None recorded.'}</p>
            </div>
          ))}
          {handoff.quantity_remaining != null && (
            <p className="text-xs text-fd-body">Quantity remaining: {handoff.quantity_remaining}</p>
          )}
          <div className="flex flex-wrap gap-2">
            {handoff.document_references.map(reference => (
              <Link
                key={reference.id}
                to={reference.url}
                onClick={onNavigate}
                className="text-xs text-fd-blue underline"
              >
                {reference.label}
              </Link>
            ))}
          </div>
          {handoff.attachments.map(attachment => (
            <HankSourceFile
              key={attachment.id}
              filename={attachment.filename}
              load={signal => api.getHankHandoffPhoto(handoff.id, attachment.id, signal)}
            />
          ))}
          {needsRefresh && (
            <p className="text-xs text-fd-amber">
              Refresh the handoff before another change. Retry an uncertain photo with the same request after
              refreshing.
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {handoff.can_acknowledge && (
              <LoadingButton
                type="button"
                size="sm"
                disabled={busy || needsRefresh || !!pendingPhoto}
                onClick={() => void run('acknowledge')}
              >
                Acknowledge handoff
              </LoadingButton>
            )}
            {handoff.can_complete && (
              <LoadingButton
                type="button"
                size="sm"
                disabled={busy || needsRefresh || !!pendingPhoto}
                onClick={() => void run('complete')}
              >
                Mark handoff finished
              </LoadingButton>
            )}
            {handoff.can_cancel && (
              <LoadingButton
                type="button"
                size="sm"
                variant="secondary"
                disabled={busy || needsRefresh || !!pendingPhoto}
                onClick={() => void run('cancel')}
              >
                Cancel handoff
              </LoadingButton>
            )}
            <LoadingButton type="button" size="sm" variant="ghost" loading={busy} onClick={() => void run('refresh')}>
              Refresh handoff
            </LoadingButton>
          </div>
          {canCreate && (pendingPhoto || ['open', 'acknowledged'].includes(handoff.status)) && (
            <div className="space-y-2 border-t border-slate-700 pt-3">
              <FormField
                label="Add a handoff photo"
                help="JPEG or PNG, up to 10 MB. Shared with this handoff’s participants."
              >
                {field => (
                  <input
                    {...field}
                    type="file"
                    accept="image/jpeg,image/png"
                    disabled={busy || needsRefresh || !!pendingPhoto}
                    className="input w-full"
                    onChange={event => {
                      const file = event.target.files?.[0];
                      if (!file) return;
                      if (!['image/jpeg', 'image/png'].includes(file.type) || file.size > 10 * 1024 * 1024) {
                        setError('Choose a JPEG or PNG photo up to 10 MB.');
                        return;
                      }
                      setPhoto(file);
                      setError('');
                    }}
                  />
                )}
              </FormField>
              <LoadingButton
                type="button"
                size="sm"
                disabled={busy || needsRefresh || (!photo && !pendingPhoto)}
                onClick={() => void run('photo')}
              >
                {pendingPhoto ? 'Retry same photo' : 'Attach photo'}
              </LoadingButton>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
