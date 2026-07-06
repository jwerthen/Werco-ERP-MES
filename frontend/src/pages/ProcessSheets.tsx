import React, { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import {
  ArrowLeftIcon,
  ClipboardDocumentCheckIcon,
  LockClosedIcon,
  MagnifyingGlassIcon,
  PencilIcon,
  PlusIcon,
  TrashIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { useAuth } from '../context/AuthContext';
import { hasPermission } from '../utils/permissions';
import { formatCentralDateTime } from '../utils/centralTime';
import { useDebouncedValue } from '../hooks/useDebouncedValue';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import {
  Button,
  ConfirmDialog,
  DataTable,
  DataTableColumn,
  ErrorState,
  FormField,
  LoadingButton,
  Modal,
  StatusBadge,
  useToast,
} from '../components/ui';
import ProcessSheetStepModal from '../components/processSheets/ProcessSheetStepModal';
import { processSheetSchema, ProcessSheetFormData } from '../validation/schemas';
import {
  ProcessSheet,
  ProcessSheetListItem,
  ProcessSheetStatus,
  ProcessSheetStep,
} from '../types/processSheet';

const STATUS_OPTIONS: Array<{ value: '' | ProcessSheetStatus; label: string }> = [
  { value: '', label: 'All Statuses' },
  { value: 'draft', label: 'Draft' },
  { value: 'released', label: 'Released' },
  { value: 'obsolete', label: 'Obsolete' },
];

/** Instrument-panel chips for the typed step kinds (page-local: types, not statuses). */
const TYPE_BADGE: Record<string, string> = {
  measurement: 'bg-blue-500/20 text-blue-300',
  checkbox: 'bg-emerald-500/20 text-emerald-300',
  list: 'bg-cyan-500/20 text-cyan-300',
  value: 'bg-indigo-500/20 text-indigo-300',
  photo: 'bg-purple-500/20 text-purple-300',
  file: 'bg-slate-500/20 text-slate-300',
  instruction: 'bg-amber-500/20 text-amber-300',
};

function StepTypeBadge({ type }: { type: string }) {
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium capitalize ${
        TYPE_BADGE[type] || 'bg-slate-800/50 text-slate-400'
      }`}
    >
      {type}
    </span>
  );
}

/** One-line summary of a step's per-type config for the steps table. */
function stepConfigSummary(step: ProcessSheetStep): string {
  const config = step.config;
  switch (step.step_type) {
    case 'measurement':
      if (!config) return '—';
      return `${config.lsl} – ${config.usl} ${config.unit ?? ''} (nom ${config.nominal})`.trim();
    case 'list': {
      const count = config?.options?.length ?? 0;
      return `${count} option${count === 1 ? '' : 's'}`;
    }
    case 'photo':
    case 'file':
      return config?.hint || '—';
    default:
      return '—';
  }
}

interface SheetFormModalProps {
  open: boolean;
  onClose: () => void;
  /** When set, edits this DRAFT sheet's header; otherwise creates a new sheet. */
  sheet: ProcessSheet | null;
  onSaved: (sheetId: number) => void;
}

/** Create a sheet, or edit a DRAFT sheet's title/description. */
function SheetFormModal({ open, onClose, sheet, onSaved }: SheetFormModalProps) {
  const isEdit = Boolean(sheet);
  const { showToast } = useToast();
  const [saving, setSaving] = useState(false);
  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty },
  } = useForm<ProcessSheetFormData>({
    resolver: zodResolver(processSheetSchema),
    defaultValues: { title: '', description: '' },
  });

  useEffect(() => {
    if (!open) return;
    reset({ title: sheet?.title ?? '', description: sheet?.description ?? '' });
  }, [open, sheet, reset]);

  const { confirmDiscard } = useUnsavedChanges(open && isDirty);
  const handleCancel = () => {
    if (confirmDiscard()) onClose();
  };

  const onSubmit = async (data: ProcessSheetFormData) => {
    setSaving(true);
    try {
      if (isEdit && sheet) {
        await api.updateProcessSheet(sheet.id, {
          title: data.title,
          // description is a NULLABLE column — explicit null clears it.
          description: data.description?.trim() ? data.description.trim() : null,
        });
        showToast('success', 'Process sheet updated');
        onSaved(sheet.id);
      } else {
        const created = await api.createProcessSheet({
          title: data.title,
          description: data.description?.trim() ? data.description.trim() : undefined,
        });
        showToast('success', `Created ${created.sheet_number} Rev ${created.revision}`);
        onSaved(created.id);
      }
      onClose();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to save process sheet');
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal open={open} onClose={handleCancel} size="lg" ariaLabelledBy="process-sheet-form-title">
      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <h3 id="process-sheet-form-title" className="text-lg font-semibold text-white">
          {isEdit ? 'Edit Process Sheet' : 'New Process Sheet'}
        </h3>
        {!isEdit && (
          <p className="text-sm text-slate-400">
            Creates a DRAFT sheet at Rev A with an auto-assigned number. Add typed steps, then release it to make it
            attachable to routing operations.
          </p>
        )}
        <FormField label="Title" required error={errors.title?.message}>
          {(field) => (
            <input
              {...field}
              type="text"
              {...register('title')}
              className={errors.title ? 'input input-error' : 'input'}
              placeholder="e.g. Final Inspection — Bracket Weldment"
            />
          )}
        </FormField>
        <FormField label="Description" error={errors.description?.message}>
          {(field) => <textarea {...field} rows={3} {...register('description')} className="input" />}
        </FormField>
        <div className="flex justify-end gap-3 pt-2">
          <Button variant="secondary" onClick={handleCancel} disabled={saving}>
            Cancel
          </Button>
          <LoadingButton type="submit" loading={saving} loadingText="Saving...">
            {isEdit ? 'Save Changes' : 'Create Sheet'}
          </LoadingButton>
        </div>
      </form>
    </Modal>
  );
}

const listColumns: Array<DataTableColumn<ProcessSheetListItem>> = [
  {
    key: 'sheet_number',
    header: 'Sheet #',
    sortable: true,
    accessor: (sheet) => sheet.sheet_number,
    render: (sheet) => <span className="font-semibold text-werco-600">{sheet.sheet_number}</span>,
  },
  {
    key: 'revision',
    header: 'Rev',
    sortable: true,
    accessor: (sheet) => sheet.revision,
  },
  {
    key: 'title',
    header: 'Title',
    sortable: true,
    accessor: (sheet) => sheet.title,
    render: (sheet) => <span className="text-surface-900">{sheet.title}</span>,
  },
  {
    key: 'status',
    header: 'Status',
    sortable: true,
    accessor: (sheet) => sheet.status,
    render: (sheet) => <StatusBadge status={sheet.status} />,
  },
  {
    key: 'step_count',
    header: 'Steps',
    sortable: true,
    align: 'right',
    accessor: (sheet) => sheet.step_count,
  },
  {
    key: 'updated_at',
    header: 'Updated',
    sortable: true,
    accessor: (sheet) => sheet.updated_at,
    csv: (sheet) => formatCentralDateTime(sheet.updated_at),
    render: (sheet) => <span className="text-sm text-surface-600">{formatCentralDateTime(sheet.updated_at)}</span>,
  },
];

interface ReleaseDialogState {
  open: boolean;
  prior: ProcessSheetListItem | null;
  obsoletePrior: boolean;
}

export default function ProcessSheetsPage() {
  const { user } = useAuth();
  const { showToast } = useToast();
  const [searchParams, setSearchParams] = useSearchParams();

  // Backend role split (server stays the source of truth — 403/409 details are
  // surfaced verbatim): author = Admin/Manager/Supervisor/Quality; release =
  // Admin/Manager/Quality.
  const canAuthor = hasPermission(user?.role, 'process_sheets:author') || user?.is_superuser === true;
  const canRelease = hasPermission(user?.role, 'process_sheets:release') || user?.is_superuser === true;

  const statusFilter = (searchParams.get('status') ?? '') as '' | ProcessSheetStatus;
  const sheetParam = searchParams.get('sheet');
  const selectedId = sheetParam ? parseInt(sheetParam, 10) : null;

  const [sheets, setSheets] = useState<ProcessSheetListItem[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState(false);
  const [search, setSearch] = useState('');
  const debouncedSearch = useDebouncedValue(search.trim(), 300);

  const [selected, setSelected] = useState<ProcessSheet | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState(false);

  const [sheetModal, setSheetModal] = useState<{ open: boolean; sheet: ProcessSheet | null }>({
    open: false,
    sheet: null,
  });
  const [stepModal, setStepModal] = useState<{ open: boolean; step: ProcessSheetStep | null }>({
    open: false,
    step: null,
  });
  const [stepToDelete, setStepToDelete] = useState<ProcessSheetStep | null>(null);
  const [confirmDeleteSheet, setConfirmDeleteSheet] = useState(false);
  const [confirmObsolete, setConfirmObsolete] = useState(false);
  const [releaseDialog, setReleaseDialog] = useState<ReleaseDialogState>({
    open: false,
    prior: null,
    obsoletePrior: true,
  });
  const [releasing, setReleasing] = useState(false);
  // Serializes the other lifecycle calls (obsolete / new revision / deletes) —
  // all NON-optimistic: buttons disable in flight and the UI reflects only
  // what the server returns.
  const [actionPending, setActionPending] = useState(false);

  const loadSheets = useCallback(async () => {
    setListLoading(true);
    try {
      const rows = await api.getProcessSheets({
        status: statusFilter || undefined,
        search: debouncedSearch || undefined,
        limit: 500,
      });
      setSheets(rows);
      setListError(false);
    } catch (err) {
      console.error('Failed to load process sheets:', err);
      setListError(true);
    } finally {
      setListLoading(false);
    }
  }, [statusFilter, debouncedSearch]);

  useEffect(() => {
    loadSheets();
  }, [loadSheets]);

  const loadDetail = useCallback(async (id: number) => {
    setDetailLoading(true);
    setDetailError(false);
    try {
      const sheet = await api.getProcessSheet(id);
      setSelected(sheet);
    } catch (err) {
      console.error('Failed to load process sheet:', err);
      setDetailError(true);
    } finally {
      setDetailLoading(false);
    }
  }, []);

  // URL-param selection (`?sheet=<id>`) is the source of truth so links from
  // Routing (and reloads) land on the right sheet.
  useEffect(() => {
    if (selectedId && !Number.isNaN(selectedId)) {
      loadDetail(selectedId);
    } else {
      setSelected(null);
    }
  }, [selectedId, loadDetail]);

  const openSheet = useCallback(
    (id: number | null) => {
      const next = new URLSearchParams(searchParams);
      if (id) {
        next.set('sheet', String(id));
      } else {
        next.delete('sheet');
      }
      setSearchParams(next);
    },
    [searchParams, setSearchParams]
  );

  const setStatusFilter = (value: string) => {
    const next = new URLSearchParams(searchParams);
    if (value) {
      next.set('status', value);
    } else {
      next.delete('status');
    }
    setSearchParams(next);
  };

  const refreshAll = useCallback(async () => {
    await loadSheets();
    if (selectedId && !Number.isNaN(selectedId)) await loadDetail(selectedId);
  }, [loadSheets, loadDetail, selectedId]);

  // ---- lifecycle actions (all non-optimistic) ------------------------------

  const openReleaseDialog = async () => {
    if (!selected) return;
    setActionPending(true);
    try {
      // Settled release UX: detect a still-released prior revision of the same
      // sheet family and offer (pre-checked) to obsolete it after releasing.
      const family = await api.getProcessSheets({ search: selected.sheet_number });
      const prior =
        family.find(
          (s) => s.sheet_number === selected.sheet_number && s.id !== selected.id && s.status === 'released'
        ) ?? null;
      setReleaseDialog({ open: true, prior, obsoletePrior: Boolean(prior) });
    } catch (err) {
      console.error('Failed to load the sheet family for release:', err);
      showToast('error', 'Could not check for a released prior revision — try again.');
    } finally {
      setActionPending(false);
    }
  };

  const handleReleaseConfirm = async () => {
    if (!selected) return;
    const { prior, obsoletePrior } = releaseDialog;
    setReleasing(true);
    try {
      await api.releaseProcessSheet(selected.id);
    } catch (err: any) {
      // Release refused (409 non-draft, 400 no steps, 403 role) — reflect only
      // the server's state.
      showToast('error', err?.response?.data?.detail || 'Failed to release process sheet');
      setReleasing(false);
      setReleaseDialog((d) => ({ ...d, open: false }));
      await refreshAll();
      return;
    }
    if (obsoletePrior && prior) {
      try {
        await api.obsoleteProcessSheet(prior.id);
        showToast('success', `Released Rev ${selected.revision}; Rev ${prior.revision} is now obsolete`);
      } catch (err: any) {
        // Partial outcome: the release DID happen; say exactly what failed.
        showToast(
          'error',
          `Released Rev ${selected.revision}, but failed to obsolete Rev ${prior.revision}: ` +
            `${err?.response?.data?.detail || 'request failed'}`
        );
      }
    } else {
      showToast('success', `Released ${selected.sheet_number} Rev ${selected.revision}`);
    }
    setReleasing(false);
    setReleaseDialog((d) => ({ ...d, open: false }));
    await refreshAll();
  };

  const handleObsolete = async () => {
    if (!selected) return;
    setConfirmObsolete(false);
    setActionPending(true);
    try {
      await api.obsoleteProcessSheet(selected.id);
      showToast('success', `${selected.sheet_number} Rev ${selected.revision} marked obsolete`);
      await refreshAll();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to obsolete process sheet');
    } finally {
      setActionPending(false);
    }
  };

  const handleNewRevision = async () => {
    if (!selected) return;
    setActionPending(true);
    try {
      const created = await api.newProcessSheetRevision(selected.id);
      showToast('success', `Created draft Rev ${created.revision} of ${created.sheet_number}`);
      await loadSheets();
      openSheet(created.id);
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to create a new revision');
    } finally {
      setActionPending(false);
    }
  };

  const handleDeleteSheet = async () => {
    if (!selected) return;
    setConfirmDeleteSheet(false);
    setActionPending(true);
    try {
      await api.deleteProcessSheet(selected.id);
      showToast('success', `Deleted ${selected.sheet_number} Rev ${selected.revision}`);
      openSheet(null);
      await loadSheets();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to delete process sheet');
    } finally {
      setActionPending(false);
    }
  };

  const handleDeleteStep = async () => {
    if (!selected || !stepToDelete) return;
    const step = stepToDelete;
    setStepToDelete(null);
    setActionPending(true);
    try {
      await api.deleteProcessSheetStep(selected.id, step.id);
      showToast('success', `Deleted step ${step.sequence}`);
      await refreshAll();
    } catch (err: any) {
      showToast('error', err?.response?.data?.detail || 'Failed to delete step');
    } finally {
      setActionPending(false);
    }
  };

  // ---- derived -------------------------------------------------------------

  const isDraft = selected?.status === 'draft';
  const isReleased = selected?.status === 'released';
  const isObsolete = selected?.status === 'obsolete';
  const nextSequence = selected?.steps.length
    ? Math.max(...selected.steps.map((s) => s.sequence)) + 10
    : 10;

  const stepColumns: Array<DataTableColumn<ProcessSheetStep>> = [
    {
      key: 'sequence',
      header: 'Seq',
      accessor: (step) => step.sequence,
      render: (step) => <span className="font-medium tabular-nums">{step.sequence}</span>,
    },
    {
      key: 'step_type',
      header: 'Type',
      accessor: (step) => step.step_type,
      render: (step) => <StepTypeBadge type={step.step_type} />,
    },
    {
      key: 'label',
      header: 'Step',
      accessor: (step) => step.label,
      render: (step) => (
        <div>
          <p className="font-medium text-surface-900">{step.label}</p>
          {step.instruction_text && <p className="text-xs text-surface-500 line-clamp-2">{step.instruction_text}</p>}
        </div>
      ),
    },
    {
      key: 'config',
      header: 'Spec',
      accessor: (step) => stepConfigSummary(step),
      render: (step) => (
        <div className="flex items-center gap-2 text-sm text-surface-600">
          <span>{stepConfigSummary(step)}</span>
          {step.requires_gauge && (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase bg-blue-500/10 text-blue-300">
              Gauge
            </span>
          )}
          {step.spc_characteristic_id != null && (
            <span className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-medium uppercase bg-cyan-500/10 text-cyan-300">
              SPC
            </span>
          )}
        </div>
      ),
    },
    {
      key: 'is_required',
      header: 'Required',
      align: 'center',
      accessor: (step) => (step.is_required ? 'yes' : 'no'),
      render: (step) =>
        step.is_required ? (
          <span className="text-emerald-400 text-sm font-medium">Yes</span>
        ) : (
          <span className="text-surface-500 text-sm">—</span>
        ),
    },
    ...(isDraft && canAuthor
      ? [
          {
            key: 'actions',
            header: '',
            className: 'w-20',
            render: (step: ProcessSheetStep) => (
              <div className="flex items-center justify-end gap-1">
                <button
                  onClick={() => setStepModal({ open: true, step })}
                  className="p-1.5 text-slate-500 hover:text-werco-primary"
                  title="Edit step"
                  aria-label={`Edit step ${step.sequence}`}
                >
                  <PencilIcon className="h-4 w-4" aria-hidden="true" />
                </button>
                <button
                  onClick={() => setStepToDelete(step)}
                  className="p-1.5 text-slate-500 hover:text-red-500"
                  title="Delete step"
                  aria-label={`Delete step ${step.sequence}`}
                >
                  <TrashIcon className="h-4 w-4" aria-hidden="true" />
                </button>
              </div>
            ),
          } as DataTableColumn<ProcessSheetStep>,
        ]
      : []),
  ];

  // ---- render ---------------------------------------------------------------

  if (selectedId) {
    return (
      <div className="space-y-5">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={() => openSheet(null)} className="flex items-center gap-1.5">
            <ArrowLeftIcon className="h-4 w-4" aria-hidden="true" />
            Process Sheets
          </Button>
        </div>

        {detailLoading ? (
          <div className="flex items-center justify-center h-64">
            <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-werco-primary" />
          </div>
        ) : detailError || !selected ? (
          <ErrorState
            message="Could not load this process sheet."
            onRetry={() => loadDetail(selectedId)}
            className="h-64"
          />
        ) : (
          <>
            <div className="card">
              <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h1 className="text-xl font-bold text-white">
                      {selected.sheet_number} <span className="text-slate-400 font-medium">Rev {selected.revision}</span>
                    </h1>
                    <StatusBadge status={selected.status} />
                  </div>
                  <p className="mt-1 text-base text-surface-900">{selected.title}</p>
                  {selected.description && <p className="mt-1 text-sm text-slate-400">{selected.description}</p>}
                  <div className="mt-2 flex flex-wrap gap-4 text-xs text-slate-500">
                    {selected.effective_date && (
                      <span>Effective {formatCentralDateTime(selected.effective_date)}</span>
                    )}
                    {selected.obsolete_date && <span>Obsoleted {formatCentralDateTime(selected.obsolete_date)}</span>}
                    <span>Updated {formatCentralDateTime(selected.updated_at)}</span>
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  {isDraft && canAuthor && (
                    <>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => setSheetModal({ open: true, sheet: selected })}
                        disabled={actionPending}
                        className="flex items-center gap-1.5"
                      >
                        <PencilIcon className="h-4 w-4" aria-hidden="true" />
                        Edit Details
                      </Button>
                      <Button
                        variant="danger"
                        size="sm"
                        onClick={() => setConfirmDeleteSheet(true)}
                        disabled={actionPending}
                        className="flex items-center gap-1.5"
                      >
                        <TrashIcon className="h-4 w-4" aria-hidden="true" />
                        Delete
                      </Button>
                    </>
                  )}
                  {isDraft && canRelease && (
                    <LoadingButton
                      size="sm"
                      loading={actionPending}
                      onClick={openReleaseDialog}
                      disabled={selected.steps.length === 0}
                      title={selected.steps.length === 0 ? 'Add at least one step before releasing' : undefined}
                    >
                      Release
                    </LoadingButton>
                  )}
                  {isReleased && canRelease && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() => setConfirmObsolete(true)}
                      disabled={actionPending}
                    >
                      Obsolete
                    </Button>
                  )}
                  {(isReleased || isObsolete) && canAuthor && (
                    <LoadingButton size="sm" loading={actionPending} onClick={handleNewRevision}>
                      New Revision
                    </LoadingButton>
                  )}
                </div>
              </div>

              {(isReleased || isObsolete) && (
                <div className="mt-4 flex items-start gap-2 border border-werco-primary/40 bg-werco-primary/5 px-3 py-2 text-xs text-slate-300">
                  <LockClosedIcon className="h-4 w-4 flex-shrink-0 mt-0.5 text-werco-primary" aria-hidden="true" />
                  <span>
                    {isReleased
                      ? 'Released — content is locked. Create a new revision to change the steps; work orders snapshot the released content at creation.'
                      : 'Obsolete — retained for traceability. Existing work-order snapshots are unaffected; create a new revision to continue this sheet.'}
                  </span>
                </div>
              )}
            </div>

            <div className="card card-flush overflow-hidden">
              <div className="flex items-center justify-between px-4 py-3 border-b border-fd-line">
                <h2 className="text-sm font-semibold text-white">
                  Steps <span className="text-slate-500 tabular-nums">({selected.steps.length})</span>
                </h2>
                {isDraft && canAuthor && (
                  <Button
                    size="sm"
                    onClick={() => setStepModal({ open: true, step: null })}
                    disabled={actionPending}
                    className="flex items-center gap-1.5"
                  >
                    <PlusIcon className="h-4 w-4" aria-hidden="true" />
                    Add Step
                  </Button>
                )}
              </div>
              <DataTable
                columns={stepColumns}
                data={[...selected.steps].sort((a, b) => a.sequence - b.sequence)}
                rowKey={(step) => step.id}
                className="border-0"
                empty={{
                  icon: ClipboardDocumentCheckIcon,
                  title: 'No steps yet',
                  description: isDraft
                    ? 'Add typed steps — measurements, checkboxes, photo evidence — that operators complete on the shop floor.'
                    : 'This sheet has no steps.',
                  action:
                    isDraft && canAuthor
                      ? { label: 'Add Step', onClick: () => setStepModal({ open: true, step: null }) }
                      : undefined,
                }}
              />
            </div>
          </>
        )}

        {selected && (
          <>
            <SheetFormModal
              open={sheetModal.open}
              onClose={() => setSheetModal({ open: false, sheet: null })}
              sheet={sheetModal.sheet}
              onSaved={() => refreshAll()}
            />
            <ProcessSheetStepModal
              open={stepModal.open}
              onClose={() => setStepModal({ open: false, step: null })}
              sheetId={selected.id}
              step={stepModal.step}
              defaultSequence={nextSequence}
              onSaved={() => refreshAll()}
            />

            {/* Release confirm — the settled obsolete-prior-by-default UX. */}
            <Modal
              open={releaseDialog.open}
              onClose={() => {
                if (!releasing) setReleaseDialog((d) => ({ ...d, open: false }));
              }}
              size="md"
              closeOnBackdrop={!releasing}
              closeOnEscape={!releasing}
              ariaLabelledBy="release-sheet-dialog-title"
            >
              <h3 id="release-sheet-dialog-title" className="text-lg font-semibold text-white">
                Release {selected.sheet_number} Rev {selected.revision}?
              </h3>
              <p className="mt-2 text-sm text-slate-300">
                Releasing locks this sheet&rsquo;s content and makes it attachable to routing operations.
              </p>
              {releaseDialog.prior && (
                <div className="mt-4 border border-amber-500/40 bg-amber-500/10 px-3 py-3 text-sm text-amber-200">
                  <p className="font-medium">Rev {releaseDialog.prior.revision} is currently released.</p>
                  <label className="mt-2 flex items-center gap-2 text-slate-200">
                    <input
                      type="checkbox"
                      checked={releaseDialog.obsoletePrior}
                      onChange={(e) => setReleaseDialog((d) => ({ ...d, obsoletePrior: e.target.checked }))}
                      disabled={releasing}
                      className="rounded border-slate-600 bg-slate-800"
                    />
                    Obsolete Rev {releaseDialog.prior.revision} after releasing
                  </label>
                  <p className="mt-1 text-xs text-amber-200/70">
                    Uncheck to keep both revisions released during a deliberate transition period.
                  </p>
                </div>
              )}
              <div className="mt-6 flex justify-end gap-3">
                <Button
                  variant="secondary"
                  onClick={() => setReleaseDialog((d) => ({ ...d, open: false }))}
                  disabled={releasing}
                >
                  Cancel
                </Button>
                <LoadingButton loading={releasing} loadingText="Releasing..." onClick={handleReleaseConfirm}>
                  {releaseDialog.prior && releaseDialog.obsoletePrior
                    ? `Release & Obsolete Rev ${releaseDialog.prior.revision}`
                    : 'Release'}
                </LoadingButton>
              </div>
            </Modal>

            <ConfirmDialog
              open={confirmObsolete}
              title={`Obsolete ${selected.sheet_number} Rev ${selected.revision}?`}
              message="The sheet can no longer be attached to routing operations. Existing work-order snapshots are unaffected."
              confirmLabel="Obsolete"
              variant="warning"
              onConfirm={handleObsolete}
              onCancel={() => setConfirmObsolete(false)}
            />
            <ConfirmDialog
              open={confirmDeleteSheet}
              title={`Delete ${selected.sheet_number} Rev ${selected.revision}?`}
              message="Deletes this draft sheet (kept for audit/restore). Released sheets are obsoleted instead of deleted."
              confirmLabel="Delete"
              variant="danger"
              onConfirm={handleDeleteSheet}
              onCancel={() => setConfirmDeleteSheet(false)}
            />
            <ConfirmDialog
              open={Boolean(stepToDelete)}
              title={`Delete step ${stepToDelete?.sequence ?? ''}?`}
              message={`Removes "${stepToDelete?.label ?? ''}" from this draft sheet.`}
              confirmLabel="Delete"
              variant="danger"
              onConfirm={handleDeleteStep}
              onCancel={() => setStepToDelete(null)}
            />
          </>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-5 sm:space-y-6">
      <div className="page-header mb-0">
        <div className="min-w-0">
          <h1 className="page-title">Process Sheets</h1>
          <p className="page-subtitle">
            Typed, revision-controlled operation steps — attach released sheets to routing operations
          </p>
        </div>
        {canAuthor && (
          <div className="page-actions w-full sm:w-auto">
            <Button
              onClick={() => setSheetModal({ open: true, sheet: null })}
              className="w-full sm:w-auto flex items-center justify-center"
            >
              <PlusIcon className="h-5 w-5 mr-2 flex-shrink-0" aria-hidden="true" />
              New Process Sheet
            </Button>
          </div>
        )}
      </div>

      <div className="card rounded-sm border-fd-line p-2.5 sm:p-3">
        <div className="grid grid-cols-1 xs:grid-cols-2 lg:grid-cols-[minmax(18rem,1fr)_11rem] gap-2 sm:gap-3">
          <div className="relative min-w-0">
            <MagnifyingGlassIcon className="h-5 w-5 absolute left-4 top-1/2 -translate-y-1/2 text-surface-400" />
            <input
              type="text"
              placeholder="Search by sheet number or title..."
              aria-label="Search process sheets"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input pl-11"
            />
          </div>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="input px-3 text-sm sm:px-4 sm:text-base"
            aria-label="Status filter"
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value || 'all'} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <DataTable
        columns={listColumns}
        data={sheets}
        rowKey={(sheet) => sheet.id}
        onRowClick={(sheet) => openSheet(sheet.id)}
        loading={listLoading}
        error={listError}
        onRetry={loadSheets}
        defaultSort={{ key: 'sheet_number', dir: 'desc' }}
        pageSize={25}
        csvExport={{ filename: 'process-sheets' }}
        empty={{
          icon: ClipboardDocumentCheckIcon,
          title: 'No process sheets found',
          description: canAuthor
            ? 'Create a process sheet to author typed, revision-controlled operation steps.'
            : 'Adjust your filters, or ask an author to create the first process sheet.',
          action: canAuthor
            ? { label: 'New Process Sheet', onClick: () => setSheetModal({ open: true, sheet: null }) }
            : undefined,
        }}
      />

      <SheetFormModal
        open={sheetModal.open}
        onClose={() => setSheetModal({ open: false, sheet: null })}
        sheet={sheetModal.sheet}
        onSaved={(sheetId) => {
          loadSheets();
          openSheet(sheetId);
        }}
      />
    </div>
  );
}
