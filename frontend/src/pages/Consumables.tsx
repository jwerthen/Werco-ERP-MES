import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { Controller, useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  ArrowDownTrayIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  ClipboardDocumentCheckIcon,
  CubeIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import { Button, DataTable, DataTableColumn, EmptyState, ErrorState, FormField, useToast } from '../components/ui';
import { ComboBox } from '../components/ui/ComboBox';
import { PageHeader } from '../components/ui/PageHeader';
import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from '../components/ai/hankSession';
import { useAuth } from '../context/AuthContext';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import consumables from '../services/consumables';
import type {
  ConsumableCheckout,
  ConsumableCheckoutCreate,
  ConsumableHistoryFilters,
  ConsumableMetadata,
  ConsumablePage,
  ConsumableStock,
} from '../types/consumables';
import { toDisplayString } from '../utils/apiError';
import { formatCentralDateTime, getCentralTodayISODate } from '../utils/centralTime';
import { consumableCheckoutSchema, ConsumableCheckoutForm } from '../validation/consumables';

const PAGE_SIZE = 25;
const STOCK_PAGE_SIZE = 20;
const blankForm = (): ConsumableCheckoutForm => ({ employee_id: '', inventory_item_id: '', quantity: '1', notes: '' });
const quantityText = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 20 });
const errorText = (error: unknown, fallback: string) => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return detail ? toDisplayString(detail) : fallback;
};
const pageNumber = (value: string | null) => (value && /^\d+$/.test(value) ? Math.min(Number(value), 100000) : 0);
const positiveId = (value: string | null) => (value && /^[1-9]\d*$/.test(value) ? Number(value) : undefined);
const wholeUnit = (unit: string) =>
  ['each', 'ea', 'piece', 'pieces', 'pcs', 'pair', 'pairs', 'box', 'boxes', 'pack', 'packs', 'roll', 'rolls'].includes(
    unit.toLowerCase()
  );

/** Use a UUID even on browser contexts that expose getRandomValues but not randomUUID. */
function requestId(): string {
  if (globalThis.crypto.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes)
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function Timestamp({ value }: { value: string }) {
  return <time dateTime={value}>{formatCentralDateTime(value, { second: '2-digit', timeZoneName: 'short' })}</time>;
}

const checkoutReference = (id: number) => `CC-${String(id).padStart(6, '0')}`;

/** Switching employee/company clears drafts and stale responses immediately. */
export default function Consumables() {
  const { user } = useAuth();
  const [, refreshSession] = useState(0);
  useEffect(() => subscribeHankSession(() => refreshSession(value => value + 1)), []);
  return (
    <ConsumablesWorkspace
      key={`${user?.company_id}:${user?.id}:${getHankSessionScope()}`}
      readOnly={isHankReadOnlySession()}
    />
  );
}

function ConsumablesWorkspace({ readOnly }: { readOnly: boolean }) {
  const { user } = useAuth();
  const { showToast } = useToast();
  const [params, setParams] = useSearchParams();
  const [metadata, setMetadata] = useState<ConsumableMetadata | null>(null);
  const [metadataError, setMetadataError] = useState('');
  const [stock, setStock] = useState<ConsumablePage<ConsumableStock> | null>(null);
  const [stockLoading, setStockLoading] = useState(true);
  const [stockError, setStockError] = useState('');
  const [history, setHistory] = useState<ConsumablePage<ConsumableCheckout> | null>(null);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyError, setHistoryError] = useState('');
  const [selected, setSelected] = useState<ConsumableStock | null>(null);
  const [receipt, setReceipt] = useState<ConsumableCheckout | null>(null);
  const [reload, setReload] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [pending, setPending] = useState<ConsumableCheckoutCreate | null>(null);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState('');
  const saveBusy = useRef(false);
  const exportBusy = useRef(false);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const {
    register,
    control,
    watch,
    setValue,
    setError,
    clearErrors,
    reset,
    handleSubmit,
    formState: { errors, isDirty },
  } = useForm<ConsumableCheckoutForm>({
    resolver: zodResolver(consumableCheckoutSchema),
    defaultValues: blankForm(),
  });
  const { markSaved, allowInternalNavigation } = useUnsavedChanges(isDirty || Boolean(pending));
  const employeeId = watch('employee_id');
  const quantity = watch('quantity');
  const employee = metadata?.people.find(person => String(person.id) === employeeId);
  const canManage = metadata?.can_manage === true && !readOnly;
  const canExport = metadata?.can_export === true;
  const locked = saving || Boolean(pending);
  const stockQuery = params.get('stock_q') || '';
  const debouncedStockQuery = useDebouncedValue(stockQuery.trim(), 250);
  const lowStockOnly = params.get('low_stock') === 'true';
  const stockPage = pageNumber(params.get('stock_page'));
  const historyQuery = params.get('q') || '';
  const debouncedHistoryQuery = useDebouncedValue(historyQuery.trim(), 250);
  const historySearchPending = historyQuery.trim() !== debouncedHistoryQuery;
  const historyEmployeeId = params.get('employee_id') || '';
  const historyPartId = params.get('part_id') || '';
  const dateFrom = params.get('date_from') || '';
  const dateTo = params.get('date_to') || '';
  const historyPage = pageNumber(params.get('page'));
  const invalidDates = Boolean(dateFrom && dateTo && dateFrom > dateTo);
  const historyFilters = useMemo<ConsumableHistoryFilters>(
    () => ({
      q: debouncedHistoryQuery || undefined,
      employee_id: positiveId(historyEmployeeId),
      part_id: positiveId(historyPartId),
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
    }),
    [debouncedHistoryQuery, historyEmployeeId, historyPartId, dateFrom, dateTo]
  );

  const changeParam = (key: string, value: string, pageKey = 'page') => {
    allowInternalNavigation(() =>
      setParams(
        previous => {
          const next = new URLSearchParams(previous);
          value ? next.set(key, value) : next.delete(key);
          if (key !== pageKey) next.delete(pageKey);
          return next;
        },
        { replace: true }
      )
    );
  };

  useEffect(() => {
    let active = true;
    setMetadataError('');
    consumables
      .metadata()
      .then(result => {
        if (active) setMetadata(result);
      })
      .catch(error => {
        if (active) setMetadataError(errorText(error, 'Unable to load employees and checkout permissions.'));
      });
    return () => {
      active = false;
    };
  }, [reload]);

  useEffect(() => {
    let active = true;
    setStockLoading(true);
    setStockError('');
    consumables
      .stock({
        q: debouncedStockQuery || undefined,
        low_stock: lowStockOnly,
        skip: stockPage * STOCK_PAGE_SIZE,
        limit: STOCK_PAGE_SIZE,
      })
      .then(result => {
        if (active) setStock(result);
      })
      .catch(error => {
        if (active) setStockError(errorText(error, 'Unable to load consumable stock.'));
      })
      .finally(() => {
        if (active) setStockLoading(false);
      });
    return () => {
      active = false;
    };
  }, [debouncedStockQuery, lowStockOnly, stockPage, reload]);

  useEffect(() => {
    let active = true;
    if (invalidDates) {
      setHistoryLoading(false);
      return;
    }
    setHistoryLoading(true);
    setHistoryError('');
    consumables
      .checkouts({ ...historyFilters, skip: historyPage * PAGE_SIZE, limit: PAGE_SIZE })
      .then(result => {
        if (active) setHistory(result);
      })
      .catch(error => {
        if (active) setHistoryError(errorText(error, 'Unable to load checkout history.'));
      })
      .finally(() => {
        if (active) setHistoryLoading(false);
      });
    return () => {
      active = false;
    };
  }, [historyFilters, historyPage, reload, invalidDates]);

  const chooseStock = (item: ConsumableStock) => {
    if (!canManage || locked || !item.can_checkout || item.inventory_item_id === null) return;
    setSelected(item);
    setValue('inventory_item_id', String(item.inventory_item_id), { shouldDirty: true, shouldValidate: true });
    clearErrors('quantity');
    setSaveError('');
  };

  const submitCheckout = async (values: ConsumableCheckoutForm) => {
    if (saveBusy.current || !canManage) return;
    // An uncertain outcome must recover the exact original command before a new one can be started.
    let payload = pending;
    if (!payload) {
      if (!selected || selected.inventory_item_id === null || !selected.can_checkout) {
        setError('inventory_item_id', { message: 'Select available stock before checking out.' });
        return;
      }
      if (!employee?.is_active) {
        setError('employee_id', { message: 'Select an active employee.' });
        return;
      }
      if (wholeUnit(selected.unit_of_measure) && !Number.isInteger(Number(values.quantity))) {
        setError('quantity', { message: `Enter a whole number of ${selected.unit_of_measure}.` });
        return;
      }
      if (Number(values.quantity) > selected.quantity_available) {
        setError('quantity', {
          message: `Only ${quantityText(selected.quantity_available)} ${selected.unit_of_measure} available at this location.`,
        });
        return;
      }
      payload = {
        inventory_item_id: selected.inventory_item_id,
        employee_id: Number(values.employee_id),
        quantity: Number(values.quantity),
        notes: values.notes.trim() || null,
        request_id: requestId(),
      };
    }
    saveBusy.current = true;
    setSaving(true);
    setSaveError('');
    setPending(payload);
    try {
      const result = await consumables.checkout(payload);
      if (!alive.current) return;
      setPending(null);
      setReceipt(result);
      setSelected(null);
      markSaved();
      reset({ ...blankForm(), employee_id: String(result.employee_id) });
      showToast('success', 'Consumable checkout recorded.');
      // Reads are independent effects. A refresh failure cannot turn a committed checkout into a failed save.
      setReload(value => value + 1);
    } catch (error) {
      if (!alive.current) return;
      const status = (error as { response?: { status?: number } })?.response?.status;
      const uncertain = !status || status >= 500 || status === 408;
      if (!uncertain) setPending(null);
      setSaveError(
        uncertain
          ? 'The server has not confirmed this checkout. Keep these details unchanged and use Retry checkout to safely confirm it.'
          : errorText(error, 'Checkout was not recorded. Review the details and try again.')
      );
    } finally {
      saveBusy.current = false;
      if (alive.current) setSaving(false);
    }
  };

  const exportHistory = async () => {
    if (exportBusy.current || invalidDates || historySearchPending || !canExport) return;
    exportBusy.current = true;
    setExporting(true);
    setExportError('');
    try {
      const csv = await consumables.exportCsv(historyFilters);
      if (!alive.current) return;
      const url = URL.createObjectURL(new Blob(['\uFEFF', csv], { type: 'text/csv;charset=utf-8;' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = `consumable-checkouts-${getCentralTodayISODate()}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (alive.current) setExportError(errorText(error, 'Unable to export checkout history. Try again.'));
    } finally {
      exportBusy.current = false;
      if (alive.current) setExporting(false);
    }
  };

  const historyColumns: DataTableColumn<ConsumableCheckout>[] = [
    {
      key: 'issued_at',
      header: 'When · Central',
      render: row => (
        <span className="whitespace-nowrap text-xs">
          <Timestamp value={row.issued_at} />
          <span className="mt-1 block font-mono text-slate-400">{checkoutReference(row.id)}</span>
        </span>
      ),
    },
    {
      key: 'employee',
      header: 'Employee',
      render: row => (
        <div>
          <p className="font-medium text-white">{row.employee_name}</p>
          <p className="text-xs text-slate-400">{row.department}</p>
        </div>
      ),
    },
    {
      key: 'item',
      header: 'Consumable',
      render: row => (
        <div className="min-w-44">
          <p className="font-medium">{row.part_name}</p>
          <p className="font-mono text-xs text-slate-400">{row.part_number}</p>
        </div>
      ),
    },
    {
      key: 'quantity',
      header: 'Quantity',
      align: 'right',
      render: row => (
        <span className="whitespace-nowrap font-semibold text-white">
          {quantityText(row.quantity)} <span className="text-xs font-normal text-slate-400">{row.unit_of_measure}</span>
        </span>
      ),
    },
    {
      key: 'location',
      header: 'From location',
      render: row => (
        <div>
          <p>{row.location || 'Not specified'}</p>
          {row.lot_number && <p className="text-xs text-slate-400">Lot {row.lot_number}</p>}
        </div>
      ),
    },
    { key: 'issuer', header: 'Issued by', render: row => row.issued_by_name },
    {
      key: 'notes',
      header: 'Notes',
      render: row => <p className="min-w-36 max-w-72 whitespace-pre-wrap break-words text-xs">{row.notes || 'None'}</p>,
    },
  ];
  const historyHasFilters = Boolean(historyQuery || historyEmployeeId || historyPartId || dateFrom || dateTo);
  const selectedQuantity = Number(quantity);
  const remaining =
    selected && Number.isFinite(selectedQuantity) ? selected.quantity_available - selectedQuantity : null;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Consumables"
        description="Record every issue from the supply room: employee, item, quantity, and time."
        actions={
          <>
            <Link to="/materials?type=consumable" className="btn-secondary">
              Manage consumables
            </Link>
            {canManage && (
              <Link to="/warehouse?tab=inventory" className="btn-secondary">
                Receive stock
              </Link>
            )}
            <Button
              variant="ghost"
              aria-label="Refresh consumables"
              disabled={saving}
              onClick={() => setReload(value => value + 1)}
            >
              <ArrowPathIcon className="h-4 w-4" aria-hidden="true" />
            </Button>
          </>
        }
      />

      {metadataError && (
        <ErrorState
          title="Checkout unavailable"
          message={metadataError}
          onRetry={() => setReload(value => value + 1)}
        />
      )}
      {!metadata && !metadataError && (
        <p role="status" className="text-sm text-slate-400">
          Loading employees and checkout permissions…
        </p>
      )}
      {metadata && !canManage && (
        <p className="border border-fd-line bg-fd-panel p-3 text-sm text-slate-300">
          You can review supplies and checkout history. An authorized supply attendant records checkouts.
        </p>
      )}

      <div className={canManage ? 'grid items-start gap-5 xl:grid-cols-[minmax(0,1.15fr)_minmax(22rem,0.85fr)]' : ''}>
        <section className="min-w-0 border border-fd-line bg-fd-panel" aria-labelledby="consumable-stock-title">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-fd-line px-4 py-3">
            <div>
              <h2 id="consumable-stock-title" className="text-base font-semibold text-white">
                Supplies & stock
              </h2>
              <p className="text-xs text-slate-400">
                {canManage
                  ? 'Choose the item and location you are issuing from.'
                  : 'Current quantities by item and location.'}
              </p>
            </div>
            {stock && !stockError && (
              <span className="text-xs text-slate-400">{stock.total.toLocaleString()} stock records</span>
            )}
          </div>
          <div className="space-y-3 p-4">
            <FormField label="Search consumables">
              {field => (
                <input
                  {...field}
                  className="input w-full"
                  value={stockQuery}
                  maxLength={200}
                  placeholder="Name, item number, location, or lot…"
                  onChange={event => changeParam('stock_q', event.target.value, 'stock_page')}
                />
              )}
            </FormField>
            <label className="inline-flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={lowStockOnly}
                onChange={event => changeParam('low_stock', event.target.checked ? 'true' : '', 'stock_page')}
              />
              Low stock only
            </label>
            {stockError ? (
              <ErrorState
                title="Stock could not be refreshed"
                message={stockError}
                onRetry={() => setReload(value => value + 1)}
              />
            ) : stockLoading ? (
              <p role="status" className="py-8 text-center text-sm text-slate-400">
                Loading consumables…
              </p>
            ) : stock?.items.length ? (
              <div className="max-h-[25rem] divide-y divide-fd-line overflow-y-auto border border-fd-line">
                {stock.items.map(item => {
                  const isSelected =
                    item.inventory_item_id !== null && item.inventory_item_id === selected?.inventory_item_id;
                  const location = item.location || 'No location';
                  return (
                    <div
                      key={`${item.part_id}:${item.inventory_item_id ?? 'none'}`}
                      className={isSelected ? 'bg-blue-500/10' : ''}
                    >
                      <button
                        type="button"
                        disabled={!canManage || locked || !item.can_checkout}
                        onClick={() => chooseStock(item)}
                        aria-label={`Select ${item.part_name} at ${location}`}
                        aria-pressed={isSelected}
                        className={`flex w-full items-start justify-between gap-3 p-3 text-left transition-colors enabled:hover:bg-slate-700/30 disabled:cursor-default ${isSelected ? 'border-l-2 border-blue-400' : 'border-l-2 border-transparent'}`}
                      >
                        <span className="min-w-0">
                          <span className="block break-words text-sm font-semibold text-white">{item.part_name}</span>
                          <span className="mt-1 block break-words font-mono text-xs text-slate-400">
                            {item.part_number} · {location}
                            {item.lot_number ? ` · Lot ${item.lot_number}` : ''}
                          </span>
                          {item.unavailable_reason && (
                            <span className="mt-1 block text-xs text-amber-300">{item.unavailable_reason}</span>
                          )}
                        </span>
                        <span className="shrink-0 text-right">
                          <span className="block text-lg font-semibold tabular-nums text-white">
                            {quantityText(item.quantity_available)}
                          </span>
                          <span className="block text-xs text-slate-400">{item.unit_of_measure} available</span>
                          {item.is_low_stock && (
                            <span className="mt-1 block text-xs font-medium text-amber-300">Low stock</span>
                          )}
                          {isSelected && (
                            <span className="mt-1 block text-xs font-semibold text-blue-300">Selected</span>
                          )}
                        </span>
                      </button>
                      <div className="flex items-center justify-between gap-2 px-3 pb-2">
                        <span className="text-[11px] text-slate-400">
                          On hand: {quantityText(item.quantity_on_hand)} {item.unit_of_measure}
                        </span>
                        <button
                          type="button"
                          className="text-xs text-blue-300 hover:underline"
                          aria-label={`View checkout history for ${item.part_name}`}
                          onClick={() => changeParam('part_id', String(item.part_id))}
                        >
                          View history
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            ) : (
              <EmptyState
                icon={CubeIcon}
                title={stockQuery || lowStockOnly ? 'No matching consumables' : 'Set up your consumables'}
                description={
                  stockQuery || lowStockOnly
                    ? 'Try another search or clear the low-stock filter.'
                    : 'Create items with type Consumable in Materials & Supplies, then receive the opening stock count.'
                }
                action={
                  !(stockQuery || lowStockOnly) ? (
                    <Link to="/materials?type=consumable" className="btn-primary">
                      Open consumable catalog
                    </Link>
                  ) : undefined
                }
              />
            )}
            {stock && stock.total > STOCK_PAGE_SIZE && (
              <div className="flex items-center justify-between gap-2 text-xs text-slate-400">
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={stockLoading || stockPage === 0}
                  onClick={() => changeParam('stock_page', String(stockPage - 1), 'stock_page')}
                >
                  Previous supplies
                </Button>
                <span>
                  Page {stockPage + 1} of {Math.ceil(stock.total / STOCK_PAGE_SIZE)}
                </span>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={stockLoading || (stockPage + 1) * STOCK_PAGE_SIZE >= stock.total}
                  onClick={() => changeParam('stock_page', String(stockPage + 1), 'stock_page')}
                >
                  Next supplies
                </Button>
              </div>
            )}
          </div>
        </section>

        {canManage && (
          <section className="min-w-0 border border-fd-line bg-fd-panel" aria-labelledby="consumable-checkout-title">
            <div className="flex items-center gap-2 border-b border-fd-line px-4 py-3">
              <ClipboardDocumentCheckIcon className="h-5 w-5 text-blue-300" aria-hidden="true" />
              <div>
                <h2 id="consumable-checkout-title" className="text-base font-semibold text-white">
                  Check out supplies
                </h2>
                <p className="text-xs text-slate-400">
                  One item per entry. Keep the employee selected for the next item.
                </p>
              </div>
            </div>
            <form className="space-y-4 p-4" onSubmit={handleSubmit(submitCheckout)} noValidate>
              {receipt && (
                <div
                  role="status"
                  className="flex items-start gap-2 border border-emerald-500/30 bg-emerald-500/10 p-3 text-sm text-emerald-200"
                >
                  <CheckCircleIcon className="mt-0.5 h-5 w-5 shrink-0" aria-hidden="true" />
                  <div>
                    <p className="font-semibold">Checkout recorded · {checkoutReference(receipt.id)}</p>
                    <p>
                      {quantityText(receipt.quantity)} {receipt.unit_of_measure} of {receipt.part_name} issued to{' '}
                      {receipt.employee_name}.
                    </p>
                    <p className="mt-1 text-xs">
                      <Timestamp value={receipt.issued_at} /> · Recorded by {receipt.issued_by_name}
                    </p>
                  </div>
                </div>
              )}
              <Controller
                name="employee_id"
                control={control}
                render={({ field }) => (
                  <FormField
                    label="Employee receiving supplies"
                    required
                    error={errors.employee_id?.message}
                    help="Search by employee name or department."
                  >
                    {wiring => (
                      <ComboBox
                        id={wiring.id}
                        ariaDescribedBy={wiring['aria-describedby']}
                        options={(metadata?.people || [])
                          .filter(person => person.is_active)
                          .map(person => ({
                            value: String(person.id),
                            label: person.name,
                            hint: person.department || undefined,
                          }))}
                        value={field.value}
                        onChange={field.onChange}
                        disabled={locked}
                        placeholder="Search employee name…"
                        emptyOptionLabel="Select employee"
                      />
                    )}
                  </FormField>
                )}
              />
              <div>
                <p className="label">Consumable & location</p>
                {selected ? (
                  <div className="border border-blue-400/30 bg-blue-500/5 p-3">
                    <p className="font-semibold text-white">{selected.part_name}</p>
                    <p className="mt-1 text-xs text-slate-400">
                      {selected.part_number} · {selected.location || 'No location'}
                      {selected.lot_number ? ` · Lot ${selected.lot_number}` : ''}
                    </p>
                    <p className="mt-2 text-sm text-blue-200">
                      {quantityText(selected.quantity_available)} {selected.unit_of_measure} available
                    </p>
                    {selected.is_low_stock && (
                      <p className="mt-1 flex items-center gap-1 text-xs text-amber-300">
                        <ExclamationTriangleIcon className="h-4 w-4" aria-hidden="true" />
                        Low stock: {quantityText(selected.part_quantity_on_hand)} {selected.unit_of_measure} on hand
                        across locations; reorder point {quantityText(selected.reorder_point)}.
                      </p>
                    )}
                  </div>
                ) : (
                  <p className="border border-dashed border-fd-line p-4 text-sm text-slate-400">
                    Select an item from Supplies & stock.
                  </p>
                )}
                {errors.inventory_item_id && (
                  <p role="alert" className="mt-1 text-xs text-red-300">
                    {errors.inventory_item_id.message}
                  </p>
                )}
              </div>
              <div className="grid grid-cols-2 items-end gap-3">
                <FormField
                  label="Quantity"
                  required
                  error={errors.quantity?.message}
                  help={selected ? `Issue in ${selected.unit_of_measure}.` : 'Select an item to see its unit.'}
                >
                  {field => (
                    <input
                      {...field}
                      {...register('quantity')}
                      type="number"
                      inputMode="decimal"
                      min={selected && wholeUnit(selected.unit_of_measure) ? 1 : 0.000001}
                      step={selected && wholeUnit(selected.unit_of_measure) ? 1 : 'any'}
                      disabled={locked}
                      className="input w-full"
                    />
                  )}
                </FormField>
                <div className="pb-5 text-right">
                  <p className="text-xs text-slate-400">Available after checkout</p>
                  <p
                    className={`mt-1 text-xl font-semibold tabular-nums ${remaining !== null && remaining < 0 ? 'text-red-300' : 'text-white'}`}
                  >
                    {remaining !== null ? `${quantityText(remaining)} ${selected?.unit_of_measure}` : 'Select stock'}
                  </p>
                </div>
              </div>
              <FormField
                label="Notes (optional)"
                error={errors.notes?.message}
                help="Job number, purpose, or anything the supply attendant should retain."
              >
                {field => (
                  <textarea
                    {...field}
                    {...register('notes')}
                    rows={2}
                    maxLength={2000}
                    disabled={locked}
                    className="input h-auto w-full"
                    placeholder="For example: replacement grinding discs for welding"
                  />
                )}
              </FormField>
              {saveError && (
                <p role="alert" className="border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200">
                  {saveError}
                </p>
              )}
              <div className="space-y-2 border-t border-fd-line pt-4">
                <Button
                  type="submit"
                  className="min-h-11 w-full"
                  disabled={saving || (!pending && Boolean(stockError || metadataError))}
                >
                  {saving ? 'Recording checkout…' : pending ? 'Retry checkout' : 'Record checkout'}
                </Button>
                <p className="text-xs text-slate-400">
                  {user
                    ? `Recorded by ${[user.first_name, user.last_name].filter(Boolean).join(' ') || 'your signed-in account'}. `
                    : ''}
                  The issue date and time are saved automatically.
                </p>
              </div>
            </form>
          </section>
        )}
      </div>

      <section className="space-y-3" aria-labelledby="consumable-history-title">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 id="consumable-history-title" className="text-lg font-semibold text-white">
              Checkout history
            </h2>
            <p className="text-xs text-slate-400">
              {history && !historyError && !invalidDates
                ? `${history.total.toLocaleString()} matching checkouts. `
                : ''}
              Newest first. Dates and times use Central time.
            </p>
          </div>
          {canExport && (
            <Button
              variant="secondary"
              onClick={exportHistory}
              disabled={exporting || invalidDates || historyLoading || historySearchPending}
            >
              <ArrowDownTrayIcon className="mr-1 h-4 w-4" aria-hidden="true" />
              {exporting ? 'Exporting…' : 'Export CSV'}
            </Button>
          )}
        </div>
        <div className="grid gap-3 border border-fd-line bg-fd-panel p-4 md:grid-cols-2 xl:grid-cols-4">
          <FormField label="Search checkout history">
            {field => (
              <input
                {...field}
                value={historyQuery}
                maxLength={200}
                className="input w-full"
                placeholder="Item, employee, location, or notes…"
                onChange={event => changeParam('q', event.target.value)}
              />
            )}
          </FormField>
          <FormField label="Filter by employee">
            {field => (
              <ComboBox
                id={field.id}
                value={historyEmployeeId}
                options={(metadata?.people || []).map(person => ({
                  value: String(person.id),
                  label: `${person.name}${person.is_active ? '' : ' (inactive)'}`,
                  hint: person.department || undefined,
                }))}
                onChange={value => changeParam('employee_id', value)}
                emptyOptionLabel="All employees"
                placeholder="All employees"
              />
            )}
          </FormField>
          <FormField label="From date">
            {field => (
              <input
                {...field}
                type="date"
                value={dateFrom}
                className="input w-full"
                onChange={event => changeParam('date_from', event.target.value)}
              />
            )}
          </FormField>
          <FormField label="Through date" error={invalidDates ? 'Choose a date on or after the From date.' : undefined}>
            {field => (
              <input
                {...field}
                type="date"
                value={dateTo}
                className="input w-full"
                onChange={event => changeParam('date_to', event.target.value)}
              />
            )}
          </FormField>
          {historyHasFilters && (
            <div className="flex flex-wrap items-center gap-3 md:col-span-2 xl:col-span-4">
              {historyPartId && (
                <span className="text-xs text-blue-200">
                  Filtered to{' '}
                  {stock?.items.find(item => String(item.part_id) === historyPartId)?.part_name ||
                    history?.items.find(item => String(item.part_id) === historyPartId)?.part_name ||
                    'selected consumable'}
                </span>
              )}
              <button
                type="button"
                className="text-xs text-blue-300 hover:underline"
                onClick={() =>
                  allowInternalNavigation(() =>
                    setParams(
                      previous => {
                        const next = new URLSearchParams(previous);
                        ['q', 'employee_id', 'part_id', 'date_from', 'date_to', 'page'].forEach(key =>
                          next.delete(key)
                        );
                        return next;
                      },
                      { replace: true }
                    )
                  )
                }
              >
                Clear history filters
              </button>
            </div>
          )}
        </div>
        {exportError && <ErrorState title="Export unavailable" message={exportError} onRetry={exportHistory} />}
        {!invalidDates && (
          <DataTable
            columns={historyColumns}
            data={history?.items || []}
            rowKey={row => row.id}
            loading={historyLoading}
            error={historyError || false}
            onRetry={() => setReload(value => value + 1)}
            manualSorting
            empty={{
              title: historyHasFilters ? 'No checkouts match these filters' : 'No checkouts recorded yet',
              description: historyHasFilters
                ? 'Adjust the employee, date range, or search to see other entries.'
                : 'Recorded checkouts will show who received each supply, how much, and when.',
            }}
            serverPagination={{
              page: historyPage + 1,
              pageSize: PAGE_SIZE,
              loading: historyLoading,
              hasNext: (historyPage + 1) * PAGE_SIZE < (history?.total || 0),
              onPageChange: page => changeParam('page', String(page - 1)),
            }}
            mobileCards={row => (
              <article className="space-y-3 border border-fd-line bg-fd-panel p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <h3 className="break-words font-semibold text-white">{row.part_name}</h3>
                    <p className="font-mono text-xs text-slate-400">{row.part_number}</p>
                  </div>
                  <span className="shrink-0 font-semibold text-white">
                    {quantityText(row.quantity)} {row.unit_of_measure}
                  </span>
                </div>
                <dl className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <dt className="text-xs text-slate-400">Employee</dt>
                    <dd>
                      {row.employee_name}
                      {row.department && <span className="block text-xs text-slate-400">{row.department}</span>}
                    </dd>
                  </div>
                  <div>
                    <dt className="text-xs text-slate-400">Issued by</dt>
                    <dd>{row.issued_by_name}</dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="text-xs text-slate-400">When · Central</dt>
                    <dd>
                      <Timestamp value={row.issued_at} />
                      <span className="mt-1 block font-mono text-xs text-slate-400">{checkoutReference(row.id)}</span>
                    </dd>
                  </div>
                  <div className="col-span-2">
                    <dt className="text-xs text-slate-400">From location</dt>
                    <dd>
                      {row.location || 'Not specified'}
                      {row.lot_number ? ` · Lot ${row.lot_number}` : ''}
                    </dd>
                  </div>
                  {row.notes && (
                    <div className="col-span-2">
                      <dt className="text-xs text-slate-400">Notes</dt>
                      <dd className="whitespace-pre-wrap break-words">{row.notes}</dd>
                    </div>
                  )}
                </dl>
              </article>
            )}
          />
        )}
      </section>
    </div>
  );
}
