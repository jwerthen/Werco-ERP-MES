import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { useSearchParams } from 'react-router-dom';
import api from '../../services/api';
import { useAuth } from '../../context/AuthContext';
import { useUnsavedChanges } from '../../hooks/useUnsavedChanges';
import { CycleCountDetail, CycleCountLine, CycleCountReview, CycleCountSummary } from '../../types/cycleCount';
import { formatCentralDate, formatCentralDateTime, getCentralTodayISODate } from '../../utils/centralTime';
import {
  Button,
  ComboBox,
  DataTable,
  DataTableColumn,
  EmptyState,
  ErrorState,
  FormField,
  LoadingButton,
  Modal,
  StatusBadge,
  useToast,
} from '../ui';

type Counter = { id: number; name: string };
type Location = { code: string; warehouse: string };
type PartOption = { id: number; part_number: string; name: string };
type Props = { locations: Location[]; parts: PartOption[]; onPosted?: () => void };
const quantity = (value: number | null) =>
  value === null ? '—' : value.toLocaleString('en-US', { maximumFractionDigits: 4 });
function errorMessage(error: unknown) {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return typeof detail === 'string' ? detail : 'Could not save this change. Please try again.';
}

function NewCount({
  locations,
  parts,
  counters,
  onClose,
  onCreated,
}: Props & { counters: Counter[]; onClose: () => void; onCreated: (id: number) => void }) {
  const { showToast } = useToast();
  const {
    register,
    control,
    handleSubmit,
    watch,
    formState: { isDirty, isSubmitting, errors },
  } = useForm({
    shouldUnregister: true,
    defaultValues: {
      scope: 'location',
      location_code: '',
      warehouse: '',
      part_id: '',
      scheduled_date: getCentralTodayISODate(),
      assigned_to: '',
      notes: '',
    },
  });
  const { confirmDiscard, markSaved } = useUnsavedChanges(isDirty);
  const scope = watch('scope');
  const close = () => {
    if (!isSubmitting && confirmDiscard()) onClose();
  };
  return (
    <Modal open onClose={close} ariaLabel="Schedule cycle count" size="lg">
      <form
        onSubmit={handleSubmit(async values => {
          try {
            const result = await api.createCycleCount({
              scheduled_date: values.scheduled_date,
              ...(scope === 'location'
                ? { location_code: values.location_code }
                : scope === 'warehouse'
                  ? { warehouse: values.warehouse }
                  : { part_id: Number(values.part_id) }),
              assigned_to: values.assigned_to ? Number(values.assigned_to) : undefined,
              notes: values.notes,
            });
            markSaved();
            onCreated(result.id);
          } catch (error) {
            showToast('error', errorMessage(error));
          }
        })}
        className="space-y-4"
      >
        <h3 className="text-lg font-semibold text-slate-100">Schedule cycle count</h3>
        <p className="text-sm text-slate-400">
          Choose the stock to count and the person responsible. Quantities are saved per item; adjustments need a
          separate review.
        </p>
        <FormField label="Count scope">
          {field => (
            <select {...field} {...register('scope')} className="input">
              <option value="location">One location</option>
              <option value="warehouse">Entire warehouse</option>
              <option value="part">One part across locations</option>
            </select>
          )}
        </FormField>
        {scope === 'location' && (
          <FormField label="Location" required error={errors.location_code?.message}>
            {field => (
              <select
                {...field}
                {...register('location_code', { required: scope === 'location' ? 'Select a location' : false })}
                className="input"
              >
                <option value="">Select location</option>
                {locations.map(location => (
                  <option key={location.code} value={location.code}>
                    {location.code} · {location.warehouse}
                  </option>
                ))}
              </select>
            )}
          </FormField>
        )}
        {scope === 'warehouse' && (
          <FormField label="Warehouse" required error={errors.warehouse?.message}>
            {field => (
              <select
                {...field}
                {...register('warehouse', { required: scope === 'warehouse' ? 'Select a warehouse' : false })}
                className="input"
              >
                <option value="">Select warehouse</option>
                {Array.from(new Set(locations.map(location => location.warehouse))).map(name => (
                  <option key={name}>{name}</option>
                ))}
              </select>
            )}
          </FormField>
        )}
        {scope === 'part' && (
          <FormField label="Part" required error={errors.part_id?.message}>
            {field => (
              <Controller
                control={control}
                name="part_id"
                rules={{ required: scope === 'part' ? 'Select a part' : false }}
                render={({ field: input }) => (
                  <ComboBox
                    {...field}
                    value={input.value}
                    onChange={input.onChange}
                    options={parts.map(part => ({ value: String(part.id), label: part.part_number, hint: part.name }))}
                  />
                )}
              />
            )}
          </FormField>
        )}
        <div className="grid gap-4 sm:grid-cols-2">
          <FormField label="Scheduled date" required error={errors.scheduled_date?.message}>
            {field => (
              <input
                {...field}
                {...register('scheduled_date', { required: 'Choose a date' })}
                type="date"
                className="input"
              />
            )}
          </FormField>
          <FormField label="Assigned counter">
            {field => (
              <select {...field} {...register('assigned_to')} className="input">
                <option value="">Unassigned</option>
                {counters.map(counter => (
                  <option key={counter.id} value={counter.id}>
                    {counter.name}
                  </option>
                ))}
              </select>
            )}
          </FormField>
        </div>
        <FormField label="Instructions">
          {field => <textarea {...field} {...register('notes')} className="input" rows={3} />}
        </FormField>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" type="button" onClick={close} disabled={isSubmitting}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={isSubmitting}>
            Create count
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

function CountEntry({
  countId,
  line,
  onClose,
  onSaved,
}: {
  countId: number;
  line: CycleCountLine;
  onClose: () => void;
  onSaved: (next: boolean) => Promise<void>;
}) {
  const { showToast } = useToast();
  const {
    register,
    handleSubmit,
    formState: { isDirty, isSubmitting, errors },
  } = useForm({
    defaultValues: {
      counted_quantity: line.counted_quantity === null ? '' : String(line.counted_quantity),
      notes: line.notes || '',
    },
  });
  const { confirmDiscard, markSaved } = useUnsavedChanges(isDirty);
  const [saveNext, setSaveNext] = useState(false);
  const close = () => {
    if (!isSubmitting && confirmDiscard()) onClose();
  };
  return (
    <Modal open onClose={close} ariaLabel={`Count ${line.part_number}`} size="md">
      <form
        className="space-y-4"
        onSubmit={handleSubmit(async values => {
          try {
            await api.recordCycleCount(countId, line.id, {
              counted_quantity: Number(values.counted_quantity),
              notes: values.notes,
              expected_counted_at: line.counted_at,
            });
            markSaved();
            await onSaved(saveNext);
          } catch (error) {
            showToast('error', errorMessage(error));
          }
        })}
      >
        <div>
          <p className="text-xs uppercase text-slate-400">Physical count</p>
          <h3 className="text-xl font-semibold text-slate-100">{line.part_number}</h3>
          <p className="text-sm text-slate-400">{line.part_name}</p>
        </div>
        <div className="border border-fd-line bg-fd-panel p-3 text-sm">
          <p>
            Location <strong className="font-mono text-slate-100">{line.location || 'Unavailable'}</strong>
          </p>
          <p>
            Lot {line.lot_number || 'Not lot tracked'}
            {line.serial_number ? ` · Serial ${line.serial_number}` : ''}
          </p>
        </div>
        <FormField
          label={`Quantity counted (${line.unit_of_measure || 'units'})`}
          required
          error={errors.counted_quantity?.message}
        >
          {field => (
            <input
              {...field}
              {...register('counted_quantity', {
                required: 'Enter the physical quantity, including zero',
                validate: value =>
                  (Number.isFinite(Number(value)) && Number(value) >= 0) || 'Enter a finite quantity of zero or more',
              })}
              type="number"
              inputMode="decimal"
              step="any"
              min="0"
              autoFocus
              className="input min-h-14 text-2xl"
            />
          )}
        </FormField>
        <p className="text-xs text-slate-400">
          Enter what is physically present. Zero records an empty location. Saving a count does not change stock.
        </p>
        <FormField label="Count notes">
          {field => <textarea {...field} {...register('notes')} className="input" rows={2} />}
        </FormField>
        <div className="flex flex-wrap justify-end gap-2">
          <Button type="button" variant="secondary" onClick={close} disabled={isSubmitting}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={isSubmitting} onClick={() => setSaveNext(false)}>
            Save count
          </LoadingButton>
          <LoadingButton type="submit" variant="secondary" loading={isSubmitting} onClick={() => setSaveNext(true)}>
            Save & next
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

export default function CycleCountsPanel({ locations, parts, onPosted }: Props) {
  const { user } = useAuth();
  const { showToast } = useToast();
  const canManage =
    !!user && (user.is_superuser || ['platform_admin', 'admin', 'manager', 'supervisor'].includes(user.role));
  const canCount = !!user && (user.is_superuser || user.role !== 'viewer');
  const [params, setParams] = useSearchParams();
  const selectedId = Number(params.get('cycle_count')) || null;
  const status = params.get('count_status') || '';
  const mine = params.get('count_assignee') === 'mine';
  const page = Math.max(1, Number(params.get('count_page')) || 1);
  const [counts, setCounts] = useState<CycleCountSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [detail, setDetail] = useState<CycleCountDetail | null>(null);
  const [counters, setCounters] = useState<Counter[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [entry, setEntry] = useState<CycleCountLine | null>(null);
  const [review, setReview] = useState<CycleCountReview | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [filter, setFilter] = useState('');
  const [remainingOnly, setRemainingOnly] = useState(false);
  const generation = useRef(0);
  const updateParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== 'count_page') next.delete('count_page');
    setParams(next);
  };
  const load = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setFailed(false);
    try {
      if (selectedId) {
        const result = await api.getCycleCount(selectedId);
        if (request === generation.current) setDetail(result);
      } else {
        const result = await api.getCycleCountWorkspace({
          status: status || undefined,
          assigned_to: mine ? user?.id : undefined,
          offset: (page - 1) * 25,
          limit: 25,
        });
        if (request === generation.current) {
          setCounts(result.items);
          setTotal(result.total);
          setHasMore(result.has_more);
          setDetail(null);
        }
      }
    } catch {
      if (request === generation.current) setFailed(true);
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [selectedId, status, mine, user?.id, page]);
  useEffect(() => {
    void load();
    return () => {
      generation.current += 1;
    };
  }, [load]);
  useEffect(() => {
    if (!canManage) return;
    let active = true;
    api
      .getCycleCountCounters()
      .then(result => {
        if (active) setCounters(result);
      })
      .catch(() => {
        if (active) showToast('error', 'Could not load the counter list. Refresh to retry.');
      });
    return () => {
      active = false;
    };
  }, [canManage, showToast]);
  const run = async (action: () => Promise<void>) => {
    if (busy) return;
    setBusy(true);
    try {
      await action();
    } catch (error) {
      showToast('error', errorMessage(error));
    } finally {
      setBusy(false);
    }
  };
  const listColumns: DataTableColumn<CycleCountSummary>[] = [
    {
      key: 'number',
      header: 'Count',
      render: row => (
        <Button variant="ghost" onClick={() => updateParam('cycle_count', String(row.id))}>
          {row.count_number}
        </Button>
      ),
    },
    {
      key: 'scope',
      header: 'Scope',
      render: row => row.location_code || row.warehouse || (row.part_id ? `Part #${row.part_id}` : 'All stock'),
    },
    { key: 'date', header: 'Scheduled', render: row => formatCentralDate(row.scheduled_date) },
    { key: 'owner', header: 'Counter', render: row => row.assigned_to_name || 'Unassigned' },
    { key: 'progress', header: 'Counted', render: row => `${row.items_counted} / ${row.total_items}` },
    { key: 'status', header: 'Status', render: row => <StatusBadge status={row.status} /> },
  ];
  const lineColumns: DataTableColumn<CycleCountLine>[] = [
    {
      key: 'part',
      header: 'Part',
      render: row => (
        <div>
          <p className="font-mono text-slate-100">{row.part_number}</p>
          <p className="text-xs text-slate-400">{row.part_name}</p>
        </div>
      ),
    },
    {
      key: 'location',
      header: 'Location / lot',
      render: row => (
        <div>
          {row.location || 'Unavailable'}
          <p className="text-xs text-slate-400">
            {row.lot_number || 'No lot'}
            {row.serial_number ? ` · ${row.serial_number}` : ''}
          </p>
        </div>
      ),
    },
    {
      key: 'counted',
      header: 'Physical count',
      render: row => (row.is_counted ? `${quantity(row.counted_quantity)} ${row.unit_of_measure}` : 'Not counted'),
    },
    { key: 'saved', header: 'Saved', render: row => (row.counted_at ? formatCentralDateTime(row.counted_at) : '—') },
    {
      key: 'action',
      header: 'Action',
      render: row =>
        canCount && detail?.status === 'in_progress' ? (
          <Button variant="secondary" onClick={() => setEntry(row)}>
            {row.is_counted ? 'Recount' : 'Count'}
          </Button>
        ) : null,
    },
  ];
  const reviewColumns: DataTableColumn<CycleCountLine>[] = [
    ...lineColumns.slice(0, 2),
    { key: 'expected', header: 'At enrollment', render: row => quantity(row.system_quantity) },
    { key: 'current', header: 'Stock now', render: row => quantity(row.current_quantity) },
    { key: 'physical', header: 'Physical count', render: row => quantity(row.counted_quantity) },
    {
      key: 'delta',
      header: 'Adjustment',
      render: row => (
        <span className={row.posting_delta ? 'font-semibold text-amber-300' : ''}>
          {row.posting_delta > 0 ? '+' : ''}
          {quantity(row.posting_delta)}
        </span>
      ),
    },
  ];
  const visibleLines =
    detail?.items.filter(
      line =>
        (!remainingOnly || !line.is_counted) &&
        [line.part_number, line.part_name, line.location, line.lot_number, line.serial_number].some(value =>
          value?.toLowerCase().includes(filter.toLowerCase())
        )
    ) || [];
  const ready =
    detail?.status === 'in_progress' &&
    detail.items.length > 0 &&
    detail.items.every(line => line.is_counted && !line.requires_recount && line.current_quantity !== null);
  return (
    <section className="space-y-4 py-4" aria-label="Cycle counts">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">
            {selectedId && detail ? detail.count_number : 'Cycle counts'}
          </h2>
          <p className="text-sm text-slate-400">
            Count physical stock, review differences, then post approved adjustments.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="secondary" onClick={() => void load()} disabled={loading || busy}>
            Refresh
          </Button>
          {selectedId ? (
            <Button
              variant="secondary"
              onClick={() => {
                setEntry(null);
                setReview(null);
                updateParam('cycle_count', '');
              }}
            >
              All counts
            </Button>
          ) : (
            canManage && <Button onClick={() => setCreateOpen(true)}>Schedule count</Button>
          )}
        </div>
      </div>
      {failed ? (
        <ErrorState message="Could not load cycle counts." onRetry={() => void load()} />
      ) : !selectedId ? (
        <>
          <div className="flex flex-wrap items-end gap-4">
            <FormField label="Count status">
              {field => (
                <select
                  {...field}
                  value={status}
                  onChange={event => updateParam('count_status', event.target.value)}
                  className="input"
                >
                  <option value="">All statuses</option>
                  <option value="scheduled">Scheduled</option>
                  <option value="in_progress">In progress</option>
                  <option value="completed">Completed</option>
                  <option value="cancelled">Cancelled</option>
                </select>
              )}
            </FormField>
            <label className="flex min-h-11 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={mine}
                onChange={event => updateParam('count_assignee', event.target.checked ? 'mine' : '')}
              />
              Assigned to me
            </label>
            <p className="pb-3 text-xs text-slate-400">{total} count sessions</p>
          </div>
          <DataTable
            columns={listColumns}
            data={counts}
            rowKey={row => row.id}
            loading={loading}
            serverPagination={{
              page,
              pageSize: 25,
              hasNext: hasMore,
              onPageChange: next => updateParam('count_page', String(next)),
            }}
            mobileCards={row => (
              <div className="space-y-2 border border-fd-line bg-fd-panel p-4">
                <div className="flex justify-between gap-2">
                  <Button variant="ghost" onClick={() => updateParam('cycle_count', String(row.id))}>
                    {row.count_number}
                  </Button>
                  <StatusBadge status={row.status} />
                </div>
                <p className="text-sm">
                  {row.location_code || row.warehouse || 'Part count'} · {formatCentralDate(row.scheduled_date)}
                </p>
                <p className="text-sm text-slate-400">
                  {row.assigned_to_name || 'Unassigned'} · {row.items_counted}/{row.total_items} counted
                </p>
              </div>
            )}
            empty={{ title: 'No cycle counts', description: 'Schedule a location, warehouse, or part count to begin.' }}
          />
        </>
      ) : loading ? (
        <p role="status" className="text-sm text-slate-400">
          Loading count…
        </p>
      ) : detail ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-4 border border-fd-line bg-fd-panel p-4">
            <div>
              <div className="flex items-center gap-3">
                <StatusBadge status={detail.status} />
                <span className="text-lg font-mono">
                  {detail.items_counted} / {detail.total_items}
                </span>
                <span className="text-sm text-slate-400">items counted</span>
              </div>
              <p className="mt-2 text-sm text-slate-400">
                {detail.location_code || detail.warehouse || 'Selected part'} · Scheduled{' '}
                {formatCentralDate(detail.scheduled_date)} · {detail.assigned_to_name || 'Unassigned'}
              </p>
              {detail.notes && <p className="mt-2 whitespace-pre-wrap text-sm">{detail.notes}</p>}
            </div>
            <div className="flex flex-wrap gap-2">
              {canCount && detail.status === 'scheduled' && (
                <LoadingButton
                  loading={busy}
                  onClick={() =>
                    void run(async () => {
                      await api.startCycleCount(detail.id);
                      await load();
                    })
                  }
                >
                  Start counting
                </LoadingButton>
              )}
              {canManage && detail.status === 'in_progress' && (
                <LoadingButton
                  loading={busy}
                  disabled={!ready}
                  onClick={() =>
                    void run(async () => {
                      setAcknowledged(false);
                      setReview(await api.reviewCycleCount(detail.id));
                    })
                  }
                >
                  Review adjustments
                </LoadingButton>
              )}
            </div>
          </div>
          {canManage && ['scheduled', 'in_progress'].includes(detail.status) && (
            <FormField label="Assigned counter">
              {field => (
                <select
                  {...field}
                  className="input max-w-sm"
                  value={detail.assigned_to || ''}
                  disabled={busy}
                  onChange={event => {
                    const id = event.target.value ? Number(event.target.value) : null;
                    void run(async () => {
                      setDetail(await api.assignCycleCount(detail.id, id));
                    });
                  }}
                >
                  <option value="">Unassigned</option>
                  {counters.map(counter => (
                    <option key={counter.id} value={counter.id}>
                      {counter.name}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
          )}
          {detail.status === 'completed' && (
            <p role="status" className="text-sm text-emerald-300">
              Completed {formatCentralDateTime(detail.completed_at)} · {detail.items_adjusted} stock rows adjusted.
            </p>
          )}
          {!detail.items.length ? (
            <EmptyState
              title="No stock rows in this count"
              description="This scope had no active nonzero stock when the count was created. Schedule a new count with another scope."
            />
          ) : (
            <>
              <div className="flex flex-wrap items-end gap-4">
                <FormField label="Find part, location, lot or serial">
                  {field => (
                    <input
                      {...field}
                      type="search"
                      className="input"
                      value={filter}
                      onChange={event => setFilter(event.target.value)}
                    />
                  )}
                </FormField>
                <label className="flex min-h-11 items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={remainingOnly}
                    onChange={event => setRemainingOnly(event.target.checked)}
                  />
                  Still to count
                </label>
              </div>
              <DataTable
                columns={lineColumns}
                data={visibleLines}
                rowKey={row => row.id}
                pageSize={25}
                mobileCards={row => (
                  <div className="space-y-2 border border-fd-line bg-fd-panel p-4">
                    <p className="font-mono font-semibold">{row.part_number}</p>
                    <p className="text-sm text-slate-400">
                      {row.location} · Lot {row.lot_number || '—'}
                    </p>
                    <div className="flex items-center justify-between">
                      <span>{row.is_counted ? `Counted ${quantity(row.counted_quantity)}` : 'Not counted'}</span>
                      {canCount && detail.status === 'in_progress' && (
                        <Button onClick={() => setEntry(row)}>{row.is_counted ? 'Recount' : 'Count'}</Button>
                      )}
                    </div>
                  </div>
                )}
                empty={{ title: 'No matching items', description: 'Change the search or clear Still to count.' }}
              />
            </>
          )}
        </>
      ) : null}
      {createOpen && (
        <NewCount
          locations={locations}
          parts={parts}
          counters={counters}
          onClose={() => setCreateOpen(false)}
          onCreated={id => {
            setCreateOpen(false);
            updateParam('cycle_count', String(id));
          }}
        />
      )}
      {entry && detail && (
        <CountEntry
          key={`${entry.id}-${entry.counted_at}`}
          countId={detail.id}
          line={entry}
          onClose={() => setEntry(null)}
          onSaved={async next => {
            try {
              const refreshed = await api.getCycleCount(detail.id);
              setDetail(refreshed);
              setEntry(next ? refreshed.items.find(line => !line.is_counted) || null : null);
              showToast('success', 'Physical count saved');
            } catch {
              setEntry(null);
              setFailed(true);
              showToast(
                'warning',
                'Physical count saved, but the updated list could not load. Refresh before continuing.'
              );
            }
          }}
        />
      )}
      {review && (
        <Modal
          open
          onClose={() => {
            if (!busy) setReview(null);
          }}
          ariaLabel="Review cycle count adjustments"
          size="6xl"
        >
          <div className="space-y-4">
            <h3 className="text-lg font-semibold text-slate-100">Review {review.count_number}</h3>
            <p className="text-sm text-slate-400">
              Posting sets each stock row to its physical count and records the difference from current stock in the
              ledger.
            </p>
            {review.items.some(line => line.stock_changed) && (
              <div role="alert" className="border border-amber-500/50 bg-amber-500/10 p-3 text-sm text-amber-200">
                Stock moved after this count was scheduled. Check the Stock now column and recount affected items if
                their physical quantities are no longer current.
              </div>
            )}
            <DataTable
              columns={reviewColumns}
              data={review.items}
              rowKey={row => row.id}
              pageSize={25}
              csvExport={{ filename: review.count_number }}
            />
            <label className="flex items-start gap-3 text-sm">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={event => setAcknowledged(event.target.checked)}
                className="mt-1"
              />
              I reviewed every physical quantity and adjustment, including any stock movements since enrollment.
            </label>
            <div className="flex justify-end gap-2">
              <Button variant="secondary" disabled={busy} onClick={() => setReview(null)}>
                Back to counting
              </Button>
              <LoadingButton
                disabled={!acknowledged}
                loading={busy}
                onClick={() =>
                  void run(async () => {
                    try {
                      const result = await api.postReviewedCycleCount(review.id, review.review_token);
                      setReview(null);
                      await load();
                      onPosted?.();
                      showToast('success', `Cycle count completed: ${result.items_adjusted} stock rows adjusted`);
                    } catch (error) {
                      setReview(null);
                      await load();
                      throw error;
                    }
                  })
                }
              >
                Post adjustments & complete
              </LoadingButton>
            </div>
          </div>
        </Modal>
      )}
    </section>
  );
}
