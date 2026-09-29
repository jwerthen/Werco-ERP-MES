import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowPathIcon,
  CheckCircleIcon,
  ClockIcon,
  LightBulbIcon,
  PlusIcon,
  WrenchScrewdriverIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { FormField } from '../components/ui/FormField';
import { Modal } from '../components/ui/Modal';
import { PageHeader } from '../components/ui/PageHeader';
import { useToast } from '../components/ui/Toast';
import { getHankSessionScope, isHankReadOnlySession, subscribeHankSession } from '../components/ai/hankSession';
import { useAuth } from '../context/AuthContext';
import useUnsavedChanges from '../hooks/useUnsavedChanges';
import continuousImprovement from '../services/continuousImprovement';
import type {
  ImprovementCreate,
  ImprovementDetail,
  ImprovementList,
  ImprovementMetadata,
  ImprovementPriority,
  ImprovementStatus,
  ImprovementSuggestion,
} from '../types/continuousImprovement';
import { toDisplayString } from '../utils/apiError';
import { formatCentralDate, formatCentralDateTime, getCentralTodayISODate } from '../utils/centralTime';

const PAGE_SIZE = 25;
const statusLabels: Record<ImprovementStatus, string> = {
  new: 'New',
  under_review: 'Under Review',
  approved: 'Approved',
  in_progress: 'In Progress',
  implemented: 'Implemented',
  on_hold: 'On Hold',
  declined: 'Declined',
};
const statusStyles: Record<ImprovementStatus, string> = {
  new: 'bg-blue-500/15 text-blue-300 border-blue-500/30',
  under_review: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  approved: 'bg-cyan-500/15 text-cyan-300 border-cyan-500/30',
  in_progress: 'bg-violet-500/15 text-violet-300 border-violet-500/30',
  implemented: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  on_hold: 'bg-slate-500/15 text-slate-300 border-slate-500/30',
  declined: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
};
const fieldLabels: Record<string, string> = {
  title: 'Title',
  problem: 'Problem / current condition',
  proposed_solution: 'Proposed solution',
  expected_benefit: 'Expected benefit',
  category: 'Category',
  priority: 'Priority',
  area: 'Shop area',
  owner_id: 'Owner',
  owner_name: 'Owner',
  target_date: 'Target date',
  status: 'Status',
  implementation_notes: 'Implementation results',
  implemented_at: 'Implemented',
  reviewed_at: 'First reviewed',
};

interface Draft {
  title: string;
  problem: string;
  proposed_solution: string;
  expected_benefit: string;
  category: string;
  priority: ImprovementPriority;
  area: string;
  owner_id: string;
  target_date: string;
  status: ImprovementStatus;
  implementation_notes: string;
  change_note: string;
}
const newDraft = (): Draft => ({
  title: '',
  problem: '',
  proposed_solution: '',
  expected_benefit: '',
  category: '',
  priority: 'medium',
  area: '',
  owner_id: '',
  target_date: '',
  status: 'new',
  implementation_notes: '',
  change_note: '',
});
const draftFrom = (item: ImprovementDetail): Draft => ({
  title: item.title,
  problem: item.problem,
  proposed_solution: item.proposed_solution,
  expected_benefit: item.expected_benefit,
  category: item.category,
  priority: item.priority,
  area: item.area ?? '',
  owner_id: String(item.owner_id ?? ''),
  target_date: item.target_date ?? '',
  status: item.status,
  implementation_notes: item.implementation_notes ?? '',
  change_note: '',
});
const createPayload = (draft: Draft): ImprovementCreate => ({
  title: draft.title.trim(),
  problem: draft.problem.trim(),
  proposed_solution: draft.proposed_solution.trim(),
  expected_benefit: draft.expected_benefit.trim(),
  category: draft.category,
  priority: draft.priority,
  area: draft.area.trim() || null,
  owner_id: draft.owner_id ? Number(draft.owner_id) : null,
  target_date: draft.target_date || null,
});
const reference = (id: number) => `CI-${String(id).padStart(4, '0')}`;
const errorText = (error: unknown, fallback: string) => {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  return detail ? toDisplayString(detail) : fallback;
};
const isConflict = (error: unknown) => (error as { response?: { status?: number } })?.response?.status === 409;
const isOverdue = (item: ImprovementSuggestion) =>
  Boolean(
    item.target_date &&
    item.target_date < getCentralTodayISODate() &&
    !['implemented', 'declined'].includes(item.status)
  );

function Timestamp({ value }: { value: string | null }) {
  return value ? (
    <time dateTime={value}>{formatCentralDateTime(value, { second: '2-digit', timeZoneName: 'short' })}</time>
  ) : (
    <span>—</span>
  );
}

function Status({ value }: { value: ImprovementStatus }) {
  return (
    <span
      className={`inline-flex whitespace-nowrap rounded border px-2 py-0.5 text-xs font-medium ${statusStyles[value]}`}
    >
      {statusLabels[value]}
    </span>
  );
}

function SuggestionFields({
  draft,
  setDraft,
  metadata,
  existingStatus,
  disabled,
}: {
  draft: Draft;
  setDraft: React.Dispatch<React.SetStateAction<Draft>>;
  metadata: ImprovementMetadata;
  existingStatus?: ImprovementStatus;
  disabled: boolean;
}) {
  const change = (field: keyof Draft, value: string) => setDraft(previous => ({ ...previous, [field]: value }));
  const category = metadata.categories.find(option => option.value === draft.category);
  return (
    <fieldset disabled={disabled} className="space-y-4">
      <FormField label="Title" required help="Give the improvement a short, specific name.">
        {field => (
          <input
            {...field}
            required
            maxLength={200}
            className="input w-full"
            value={draft.title}
            onChange={event => change('title', event.target.value)}
            placeholder="Prevent reversed parts at the assembly fixture"
          />
        )}
      </FormField>
      <div className="grid gap-4 sm:grid-cols-2">
        <FormField label="Category" required help={category?.description}>
          {field => (
            <select
              {...field}
              required
              className="input w-full"
              value={draft.category}
              onChange={event => change('category', event.target.value)}
            >
              <option value="">Select a category</option>
              {metadata.categories.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <FormField label="Priority">
          {field => (
            <select
              {...field}
              className="input w-full"
              value={draft.priority}
              onChange={event => change('priority', event.target.value)}
            >
              {metadata.priorities.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <FormField label="Shop area">
          {field => (
            <input
              {...field}
              maxLength={150}
              className="input w-full"
              value={draft.area}
              onChange={event => change('area', event.target.value)}
              placeholder="e.g. Welding / Cell 2"
            />
          )}
        </FormField>
        <FormField label="Owner">
          {field => (
            <select
              {...field}
              className="input w-full"
              value={draft.owner_id}
              onChange={event => change('owner_id', event.target.value)}
            >
              <option value="">Unassigned</option>
              {draft.owner_id && !metadata.owners.some(owner => String(owner.id) === draft.owner_id) && (
                <option value={draft.owner_id}>Current owner (unavailable)</option>
              )}
              {metadata.owners.map(owner => (
                <option key={owner.id} value={owner.id}>
                  {owner.name}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <FormField label="Target date">
          {field => (
            <input
              {...field}
              type="date"
              className="input w-full"
              value={draft.target_date}
              onChange={event => change('target_date', event.target.value)}
            />
          )}
        </FormField>
        {existingStatus && (
          <FormField label="Status">
            {field => (
              <select
                {...field}
                className="input w-full"
                value={draft.status}
                onChange={event => change('status', event.target.value)}
              >
                {metadata.statuses
                  .filter(option => option.value !== 'new' || existingStatus === 'new')
                  .map(option => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
              </select>
            )}
          </FormField>
        )}
      </div>
      <FormField
        label="Problem / current condition"
        required
        help="What happens today? Include the waste, defect, delay, or risk you observed."
      >
        {field => (
          <textarea
            {...field}
            required
            rows={3}
            maxLength={10000}
            className="input h-auto w-full"
            value={draft.problem}
            onChange={event => change('problem', event.target.value)}
          />
        )}
      </FormField>
      <FormField
        label="Proposed solution"
        required
        help="Describe a practical change or trial. For Poka-yoke, explain how the mistake would be prevented or detected."
      >
        {field => (
          <textarea
            {...field}
            required
            rows={3}
            maxLength={10000}
            className="input h-auto w-full"
            value={draft.proposed_solution}
            onChange={event => change('proposed_solution', event.target.value)}
          />
        )}
      </FormField>
      <FormField
        label="Expected benefit"
        required
        help="How will you know it worked? Include a baseline and target when possible, such as minutes per setup or defects per batch."
      >
        {field => (
          <textarea
            {...field}
            required
            rows={3}
            maxLength={10000}
            className="input h-auto w-full"
            value={draft.expected_benefit}
            onChange={event => change('expected_benefit', event.target.value)}
          />
        )}
      </FormField>
      {existingStatus && (
        <>
          <FormField
            label="Implementation results"
            required={draft.status === 'implemented'}
            help="Record what changed, the outcome, and any follow-up needed. Required to mark an improvement implemented."
          >
            {field => (
              <textarea
                {...field}
                required={draft.status === 'implemented'}
                rows={3}
                maxLength={10000}
                className="input h-auto w-full"
                value={draft.implementation_notes}
                onChange={event => change('implementation_notes', event.target.value)}
              />
            )}
          </FormField>
          <FormField
            label="Change note"
            required={draft.status !== existingStatus}
            help="Explain the decision or next step. Status changes require a note in the activity history."
          >
            {field => (
              <textarea
                {...field}
                required={draft.status !== existingStatus}
                rows={2}
                maxLength={5000}
                className="input h-auto w-full"
                value={draft.change_note}
                onChange={event => change('change_note', event.target.value)}
              />
            )}
          </FormField>
        </>
      )}
    </fieldset>
  );
}

/** A session change unmounts company data and pending editors immediately. */
export default function ContinuousImprovement() {
  const { user } = useAuth();
  const [, refreshSession] = useState(0);
  useEffect(() => subscribeHankSession(() => refreshSession(value => value + 1)), []);
  return (
    <ImprovementWorkspace
      key={`${user?.company_id}:${user?.id}:${getHankSessionScope()}`}
      readOnly={isHankReadOnlySession()}
    />
  );
}

function ImprovementWorkspace({ readOnly }: { readOnly: boolean }) {
  const { showToast } = useToast();
  const [metadata, setMetadata] = useState<ImprovementMetadata | null>(null);
  const [data, setData] = useState<ImprovementList | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('');
  const [category, setCategory] = useState('');
  const [priority, setPriority] = useState('');
  const [owner, setOwner] = useState('');
  const [page, setPage] = useState(0);
  const [creating, setCreating] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<ImprovementDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Draft>(newDraft);
  const [initialDraft, setInitialDraft] = useState<Draft>(newDraft);
  const [comment, setComment] = useState('');
  const [actionError, setActionError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [latest, setLatest] = useState<ImprovementDetail | null>(null);
  const [saving, setSaving] = useState(false);
  const busy = useRef(false);
  const alive = useRef(true);
  const detailSequence = useRef(0);
  const canManage = Boolean(metadata?.can_manage && !readOnly);
  const dirty = ((creating || editing) && JSON.stringify(draft) !== JSON.stringify(initialDraft)) || !!comment.trim();
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      ++detailSequence.current;
    };
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQuery(search.trim());
      setPage(0);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    let current = true;
    setLoading(true);
    setError('');
    Promise.all([
      continuousImprovement.metadata(),
      continuousImprovement.list({
        q: query || undefined,
        status: (status || undefined) as ImprovementStatus | undefined,
        category: category || undefined,
        priority: (priority || undefined) as ImprovementPriority | undefined,
        owner_id: owner ? Number(owner) : undefined,
        skip: page * PAGE_SIZE,
        limit: PAGE_SIZE,
      }),
    ])
      .then(([options, result]) => {
        if (!current) return;
        setMetadata(options);
        setData(result);
        // A concurrent status change can make the final filtered page disappear.
        if (page > 0 && page * PAGE_SIZE >= result.total) setPage(Math.max(0, Math.ceil(result.total / PAGE_SIZE) - 1));
      })
      .catch(cause => {
        if (current) setError(errorText(cause, 'Could not load suggestions. Please try again.'));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [query, status, category, priority, owner, page, reload]);

  const openDetail = useCallback(async (id: number) => {
    const sequence = ++detailSequence.current;
    setSelectedId(id);
    setDetail(null);
    setEditing(false);
    setComment('');
    setActionError('');
    setConflict(false);
    setLatest(null);
    setDetailError('');
    setDetailLoading(true);
    try {
      const result = await continuousImprovement.detail(id);
      if (alive.current && detailSequence.current === sequence) setDetail(result);
    } catch (cause) {
      if (alive.current && detailSequence.current === sequence)
        setDetailError(errorText(cause, 'Could not load this suggestion.'));
    } finally {
      if (alive.current && detailSequence.current === sequence) setDetailLoading(false);
    }
  }, []);

  const closeDialog = () => {
    if (busy.current || !confirmDiscard()) return;
    ++detailSequence.current;
    setCreating(false);
    setSelectedId(null);
    setDetail(null);
    setEditing(false);
    setComment('');
    setActionError('');
  };
  const beginCreate = () => {
    if (!canManage) return;
    const clean = newDraft();
    setDraft(clean);
    setInitialDraft(clean);
    setActionError('');
    setCreating(true);
    setConflict(false);
  };
  const beginEdit = () => {
    if (!detail || !canManage) return;
    const clean = draftFrom(detail);
    setDraft(clean);
    setInitialDraft(clean);
    setActionError('');
    setEditing(true);
    setConflict(false);
    setLatest(null);
  };
  const cancelEdit = () => {
    if (busy.current || !confirmDiscard()) return;
    setEditing(false);
    setActionError('');
    setConflict(false);
    setLatest(null);
  };

  const saveSuggestion = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy.current || !canManage || conflict || (!creating && !detail)) return;
    const payload = createPayload(draft);
    if (
      !payload.title ||
      !payload.problem ||
      !payload.proposed_solution ||
      !payload.expected_benefit ||
      !payload.category
    ) {
      setActionError('Enter a title, category, problem, proposed solution, and expected benefit.');
      return;
    }
    if (!creating && draft.status !== detail?.status && !draft.change_note.trim()) {
      setActionError('Add a change note explaining the status decision.');
      return;
    }
    if (!creating && draft.status === 'implemented' && !draft.implementation_notes.trim()) {
      setActionError('Record implementation results before marking this suggestion implemented.');
      return;
    }
    busy.current = true;
    setSaving(true);
    setActionError('');
    try {
      const result = creating
        ? await continuousImprovement.create(payload)
        : await continuousImprovement.update(detail!.id, {
            ...payload,
            expected_version: detail!.version,
            status: draft.status,
            implementation_notes: draft.implementation_notes.trim() || null,
            change_note: draft.change_note.trim() || undefined,
          });
      if (!alive.current) return;
      markSaved();
      setCreating(false);
      setEditing(false);
      setDetail(result);
      setSelectedId(result.id);
      setComment('');
      setReload(value => value + 1);
      showToast('success', creating ? `${reference(result.id)} submitted.` : 'Suggestion updated.');
    } catch (cause) {
      if (!alive.current) return;
      setConflict(isConflict(cause));
      setActionError(
        isConflict(cause)
          ? 'This suggestion changed while you were editing. Your draft is preserved. Review the latest version before saving.'
          : errorText(cause, 'Could not save the suggestion. Your entries are preserved.')
      );
    } finally {
      busy.current = false;
      if (alive.current) setSaving(false);
    }
  };

  const postComment = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy.current || !canManage || !detail || !comment.trim() || conflict) return;
    busy.current = true;
    setSaving(true);
    setActionError('');
    try {
      const result = await continuousImprovement.comment(detail.id, detail.version, comment.trim());
      if (!alive.current) return;
      markSaved();
      setDetail(result);
      setComment('');
      setReload(value => value + 1);
      showToast('success', 'Comment added to the activity history.');
    } catch (cause) {
      if (!alive.current) return;
      setConflict(isConflict(cause));
      setActionError(
        isConflict(cause)
          ? 'This suggestion changed. Your comment is preserved. Review the latest version before posting.'
          : errorText(cause, 'Could not add your comment. Your text is preserved.')
      );
    } finally {
      busy.current = false;
      if (alive.current) setSaving(false);
    }
  };

  const reviewLatest = async () => {
    if (!detail || busy.current) return;
    busy.current = true;
    setSaving(true);
    try {
      const result = await continuousImprovement.detail(detail.id);
      if (alive.current) setLatest(result);
    } catch (cause) {
      if (alive.current)
        setActionError(errorText(cause, 'Could not load the latest version. Your draft is preserved.'));
    } finally {
      busy.current = false;
      if (alive.current) setSaving(false);
    }
  };

  const optionLabel = (type: 'categories' | 'priorities', value: string) =>
    metadata?.[type].find(option => option.value === value)?.label ?? value;
  const historyValue = (field: string, value: unknown): string => {
    if (value === null || value === undefined || value === '') return 'Not set';
    if (field === 'status') return statusLabels[value as ImprovementStatus] ?? String(value);
    if (field === 'category') return optionLabel('categories', String(value));
    if (field === 'priority') return optionLabel('priorities', String(value));
    if (field === 'target_date') return formatCentralDate(String(value));
    if (field.endsWith('_at')) return formatCentralDateTime(String(value), { timeZoneName: 'short' });
    return toDisplayString(value);
  };
  const filtersActive = Boolean(search || status || category || priority || owner);
  const setFilter = (setter: React.Dispatch<React.SetStateAction<string>>, value: string) => {
    setter(value);
    setPage(0);
  };
  const total = data ? Object.values(data.status_counts).reduce((sum, count) => sum + count, 0) : 0;

  return (
    <div className="space-y-5">
      <PageHeader
        title="Continuous Improvement"
        description="Turn shop-floor observations into lasting improvements. Capture the idea, review the change, and record the result."
        metadata={
          <>
            <span>Lean manufacturing · Kaizen · Poka-yoke</span>
            <span>All times shown in Central Time</span>
          </>
        }
        actions={
          <>
            <button
              type="button"
              className="btn-secondary"
              disabled={loading}
              onClick={() => setReload(value => value + 1)}
            >
              <ArrowPathIcon aria-hidden="true" className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              Refresh
            </button>
            {canManage && (
              <button type="button" className="btn-primary" onClick={beginCreate}>
                <PlusIcon aria-hidden="true" className="mr-2 h-4 w-4" />
                New suggestion
              </button>
            )}
          </>
        }
      />

      <MiniStatStrip className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {(
          [
            {
              key: 'new',
              label: 'New suggestions',
              subtitle: 'Ready for the first review',
              icon: LightBulbIcon,
              bg: 'bg-blue-500/15',
              color: 'text-blue-300',
            },
            {
              key: 'under_review',
              label: 'Under review',
              subtitle: 'Assessing the idea and impact',
              icon: ClockIcon,
              bg: 'bg-amber-500/15',
              color: 'text-amber-300',
            },
            {
              key: 'in_progress',
              label: 'In progress',
              subtitle: 'Changes being put into practice',
              icon: WrenchScrewdriverIcon,
              bg: 'bg-violet-500/15',
              color: 'text-violet-300',
            },
            {
              key: 'implemented',
              label: 'Implemented',
              subtitle: 'Completed improvements',
              icon: CheckCircleIcon,
              bg: 'bg-emerald-500/15',
              color: 'text-emerald-300',
            },
          ] as const
        ).map(tile => (
          <MiniStat
            key={tile.key}
            icon={tile.icon}
            iconBg={tile.bg}
            iconColor={tile.color}
            label={tile.label}
            value={data?.status_counts[tile.key] ?? '—'}
            subtitle={tile.subtitle}
            active={status === tile.key}
            onClick={() => setFilter(setStatus, status === tile.key ? '' : tile.key)}
          />
        ))}
      </MiniStatStrip>

      {metadata && !canManage && (
        <p className="rounded border border-fd-line bg-fd-panel px-4 py-3 text-sm text-slate-400">
          {readOnly
            ? 'This company session is read-only.'
            : 'Managers and administrators can submit suggestions, manage reviews, and add comments.'}{' '}
          You can browse suggestions and their activity history.
        </p>
      )}

      <section className="card !p-0" aria-label="Suggestion register">
        <div className="space-y-4 border-b border-fd-line p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="font-semibold text-white">
              Suggestion register{' '}
              <span className="ml-2 text-sm font-normal text-slate-400">{data ? `${total} total` : ''}</span>
            </h2>
            <p className="text-xs text-slate-400">Summary counts cover all suggestions</p>
          </div>
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Search suggestions" className="sm:col-span-2">
              {field => (
                <input
                  {...field}
                  type="search"
                  maxLength={200}
                  className="input w-full"
                  placeholder="Search titles, ideas, or shop areas"
                  value={search}
                  onChange={event => setSearch(event.target.value)}
                />
              )}
            </FormField>
            {(total > 0 || filtersActive) && <>
            <FormField label="Filter by status">
              {field => (
                <select
                  {...field}
                  className="input w-full"
                  value={status}
                  onChange={event => setFilter(setStatus, event.target.value)}
                >
                  <option value="">All statuses</option>
                  {Object.entries(statusLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                      {data ? ` (${data.status_counts[value as ImprovementStatus]})` : ''}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Filter by category">
              {field => (
                <select
                  {...field}
                  className="input w-full"
                  value={category}
                  onChange={event => setFilter(setCategory, event.target.value)}
                >
                  <option value="">All categories</option>
                  {metadata?.categories.map(option => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Filter by priority">
              {field => (
                <select
                  {...field}
                  className="input w-full"
                  value={priority}
                  onChange={event => setFilter(setPriority, event.target.value)}
                >
                  <option value="">All priorities</option>
                  {metadata?.priorities.map(option => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            <FormField label="Filter by owner">
              {field => (
                <select
                  {...field}
                  className="input w-full"
                  value={owner}
                  onChange={event => setFilter(setOwner, event.target.value)}
                >
                  <option value="">All owners</option>
                  {metadata?.owners.map(person => (
                    <option key={person.id} value={person.id}>
                      {person.name}
                    </option>
                  ))}
                </select>
              )}
            </FormField>
            </>}
          </div>
          {filtersActive && (
            <button
              type="button"
              className="text-xs text-blue-300 hover:underline"
              onClick={() => {
                setSearch('');
                setQuery('');
                setStatus('');
                setCategory('');
                setPriority('');
                setOwner('');
                setPage(0);
              }}
            >
              Clear filters
            </button>
          )}
        </div>
        {error ? (
          <div className="p-6">
            <p role="alert" className="text-sm text-red-300">
              {error}
            </p>
            <button className="btn-secondary mt-3" onClick={() => setReload(value => value + 1)}>
              Try again
            </button>
          </div>
        ) : loading ? (
          <p role="status" className="p-10 text-center text-slate-400">
            Loading suggestions…
          </p>
        ) : !data?.items.length ? (
          <div className="space-y-2 px-6 py-12 text-center">
            <LightBulbIcon aria-hidden="true" className="mx-auto h-8 w-8 text-slate-500" />
            <h3 className="font-semibold text-white">
              {filtersActive ? 'No matching suggestions' : 'Start with one improvement'}
            </h3>
            <p className="mx-auto max-w-md text-sm text-slate-400">
              {filtersActive
                ? 'Try a different search or clear the filters.'
                : 'Capture a recurring mistake, wasted motion, or a better way to work. Each suggestion starts as New and keeps a timestamped history.'}
            </p>
            {canManage && !filtersActive && (
              <button className="btn-primary mt-3" onClick={beginCreate}>
                Submit the first suggestion
              </button>
            )}
          </div>
        ) : (
          <>
            <div className="hidden overflow-x-auto md:block">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">Continuous improvement suggestions, newest submitted first</caption>
                <thead className="bg-fd-panel text-xs uppercase tracking-wide text-slate-400">
                  <tr>
                    {['Suggestion', 'Status / Priority', 'Owner / Target', 'Submitted', 'Updated'].map(label => (
                      <th key={label} scope="col" className="px-4 py-3 font-medium">
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-fd-line">
                  {data.items.map(item => (
                    <tr key={item.id} className="hover:bg-white/[0.025]">
                      <td className="max-w-sm px-4 py-4">
                        <span className="font-mono text-xs text-slate-500">{reference(item.id)}</span>
                        <button
                          className="mt-1 block text-left font-medium text-blue-300 hover:underline [overflow-wrap:anywhere]"
                          onClick={() => void openDetail(item.id)}
                        >
                          {item.title}
                        </button>
                        <p className="mt-1 text-xs text-slate-400">
                          {optionLabel('categories', item.category)}
                          {item.area ? ` · ${item.area}` : ''}
                        </p>
                      </td>
                      <td className="px-4 py-4">
                        <Status value={item.status} />
                        <p className={`mt-2 text-xs ${item.priority === 'high' ? 'text-amber-300' : 'text-slate-400'}`}>
                          {optionLabel('priorities', item.priority)} priority
                        </p>
                      </td>
                      <td className="px-4 py-4">
                        <span className="text-slate-200">{item.owner_name || 'Unassigned'}</span>
                        <p className={`mt-2 text-xs ${isOverdue(item) ? 'text-red-300' : 'text-slate-400'}`}>
                          {item.target_date
                            ? `${isOverdue(item) ? 'Overdue · ' : ''}${formatCentralDate(item.target_date)}`
                            : 'No target date'}
                        </p>
                      </td>
                      <td className="px-4 py-4 text-xs text-slate-400">
                        <Timestamp value={item.created_at} />
                        <p className="mt-1">{item.created_by_name}</p>
                      </td>
                      <td className="px-4 py-4 text-xs text-slate-400">
                        <Timestamp value={item.updated_at} />
                        <p className="mt-1">{item.updated_by_name}</p>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="divide-y divide-fd-line md:hidden">
              {data.items.map(item => (
                <article key={item.id} className="space-y-3 p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-mono text-xs text-slate-500">{reference(item.id)}</span>
                    <Status value={item.status} />
                  </div>
                  <button
                    className="text-left font-medium text-blue-300 hover:underline [overflow-wrap:anywhere]"
                    onClick={() => void openDetail(item.id)}
                  >
                    {item.title}
                  </button>
                  <p className="text-xs text-slate-400">
                    {optionLabel('categories', item.category)} · {optionLabel('priorities', item.priority)} priority
                    {item.area ? ` · ${item.area}` : ''}
                  </p>
                  <dl className="grid grid-cols-2 gap-2 text-xs">
                    <div>
                      <dt className="text-slate-500">Owner</dt>
                      <dd className="text-slate-200">{item.owner_name || 'Unassigned'}</dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Target</dt>
                      <dd className={isOverdue(item) ? 'text-red-300' : 'text-slate-200'}>
                        {item.target_date
                          ? `${isOverdue(item) ? 'Overdue · ' : ''}${formatCentralDate(item.target_date)}`
                          : 'Not set'}
                      </dd>
                    </div>
                    <div className="col-span-2 text-slate-400">
                      <dt className="inline text-slate-500">Submitted: </dt>
                      <dd className="inline">
                        <Timestamp value={item.created_at} />
                      </dd>
                    </div>
                    <div className="col-span-2 text-slate-400">
                      <dt className="inline text-slate-500">Updated: </dt>
                      <dd className="inline">
                        <Timestamp value={item.updated_at} />
                      </dd>
                    </div>
                  </dl>
                </article>
              ))}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-fd-line px-4 py-3">
              <p className="text-xs text-slate-400">
                Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, data.total)} of {data.total}
              </p>
              <div className="flex items-center gap-3">
                <button className="btn-secondary" disabled={page === 0} onClick={() => setPage(value => value - 1)}>
                  Previous
                </button>
                <span className="text-xs text-slate-400">Page {page + 1}</span>
                <button
                  className="btn-secondary"
                  disabled={(page + 1) * PAGE_SIZE >= data.total}
                  onClick={() => setPage(value => value + 1)}
                >
                  Next
                </button>
              </div>
            </div>
          </>
        )}
      </section>

      <Modal
        open={creating || selectedId !== null}
        onClose={closeDialog}
        closeOnBackdrop={false}
        closeOnEscape={!saving}
        size="5xl"
        ariaLabel={creating ? 'New suggestion' : 'Suggestion details'}
      >
        <div className="mb-5 flex items-start justify-between gap-4 border-b border-fd-line pb-4">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-wider text-slate-500">
              {creating ? 'Capture an improvement' : selectedId ? reference(selectedId) : ''}
            </p>
            <h2 className="mt-1 text-xl font-semibold text-white [overflow-wrap:anywhere]">
              {creating ? 'New suggestion' : editing ? 'Edit suggestion' : detail?.title || 'Suggestion details'}
            </h2>
            {creating && (
              <p className="mt-2 text-sm text-slate-400">
                Starts as New. Submission time and author are recorded automatically.
              </p>
            )}
          </div>
          <button
            type="button"
            className="rounded p-1 text-slate-400 hover:text-white"
            aria-label="Close suggestion"
            disabled={saving}
            onClick={closeDialog}
          >
            <XMarkIcon aria-hidden="true" className="h-6 w-6" />
          </button>
        </div>
        {detailLoading && (
          <p role="status" className="py-10 text-center text-slate-400">
            Loading suggestion…
          </p>
        )}
        {detailError && (
          <div>
            <p role="alert" className="text-sm text-red-300">
              {detailError}
            </p>
            <button className="btn-secondary mt-3" onClick={() => selectedId && void openDetail(selectedId)}>
              Retry suggestion
            </button>
          </div>
        )}
        {actionError && (
          <p
            role="alert"
            className="mb-4 rounded border border-amber-500/30 bg-amber-500/10 p-3 text-sm text-amber-200"
          >
            {actionError}
          </p>
        )}
        {conflict && (
          <div className="mb-5 space-y-3 rounded border border-fd-line p-4">
            {!latest ? (
              <button type="button" className="btn-secondary" disabled={saving} onClick={() => void reviewLatest()}>
                Review latest version
              </button>
            ) : (
              <>
                <h3 className="font-semibold text-white">Latest saved version · {latest.version}</h3>
                <p className="text-xs text-slate-400">
                  Updated by {latest.updated_by_name} · <Timestamp value={latest.updated_at} />
                </p>
                <dl className="grid max-h-72 gap-3 overflow-y-auto text-sm sm:grid-cols-2">
                  {Object.entries({
                    title: latest.title,
                    status: latest.status,
                    category: latest.category,
                    priority: latest.priority,
                    owner_name: latest.owner_name,
                    area: latest.area,
                    target_date: latest.target_date,
                    problem: latest.problem,
                    proposed_solution: latest.proposed_solution,
                    expected_benefit: latest.expected_benefit,
                    implementation_notes: latest.implementation_notes,
                  }).map(([field, value]) => (
                    <div key={field}>
                      <dt className="text-xs text-slate-500">{fieldLabels[field]}</dt>
                      <dd className="whitespace-pre-wrap text-slate-200 [overflow-wrap:anywhere]">
                        {historyValue(field, value)}
                      </dd>
                    </div>
                  ))}
                </dl>
                <p className="text-xs text-slate-400">
                  Your draft is still below. Reconcile any differences before continuing. Fields you have not edited
                  will use the latest saved values.
                </p>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() => {
                    if (editing) {
                      const baseline = draftFrom(latest);
                      // Carry only this editor's changes across the reviewed snapshot;
                      // untouched fields must not roll back another manager's work.
                      const merged = Object.fromEntries(
                        (Object.keys(draft) as Array<keyof Draft>).map(field => [
                          field,
                          draft[field] === initialDraft[field] ? baseline[field] : draft[field],
                        ])
                      ) as unknown as Draft;
                      setDraft(merged);
                      setInitialDraft(baseline);
                    }
                    setDetail(latest);
                    setConflict(false);
                    setLatest(null);
                    setActionError('');
                  }}
                >
                  I reviewed the latest version; keep my draft
                </button>
              </>
            )}
          </div>
        )}
        {(creating || editing) && metadata && canManage ? (
          <form onSubmit={event => void saveSuggestion(event)}>
            <SuggestionFields
              draft={draft}
              setDraft={setDraft}
              metadata={metadata}
              existingStatus={creating ? undefined : detail?.status}
              disabled={saving}
            />
            <div className="mt-6 flex justify-end gap-3 border-t border-fd-line pt-4">
              <button
                type="button"
                className="btn-secondary"
                disabled={saving}
                onClick={creating ? closeDialog : cancelEdit}
              >
                Cancel
              </button>
              <button type="submit" className="btn-primary" disabled={saving || conflict}>
                {saving ? 'Saving…' : creating ? 'Submit suggestion' : 'Save changes'}
              </button>
            </div>
          </form>
        ) : (
          detail && (
            <>
              <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
                <div className="flex flex-wrap items-center gap-2">
                  <Status value={detail.status} />
                  <span className="text-sm text-slate-400">
                    {optionLabel('categories', detail.category)} · {optionLabel('priorities', detail.priority)} priority
                  </span>
                </div>
                {canManage && (
                  <button
                    type="button"
                    className="btn-secondary"
                    disabled={saving || conflict || !!comment.trim()}
                    onClick={beginEdit}
                  >
                    Edit suggestion
                  </button>
                )}
              </div>
              <dl className="mb-6 grid gap-4 rounded border border-fd-line bg-black/10 p-4 sm:grid-cols-2 lg:grid-cols-4">
                {[
                  { label: 'Owner', value: detail.owner_name || 'Unassigned' },
                  { label: 'Shop area', value: detail.area || 'Not specified' },
                  {
                    label: 'Target date',
                    value: detail.target_date ? formatCentralDate(detail.target_date) : 'Not set',
                  },
                  { label: 'Submitted by', value: detail.created_by_name },
                ].map(field => (
                  <div key={field.label}>
                    <dt className="text-xs text-slate-500">{field.label}</dt>
                    <dd className="mt-1 text-sm text-slate-200 [overflow-wrap:anywhere]">{field.value}</dd>
                  </div>
                ))}
                {[
                  { label: 'Submitted', value: detail.created_at },
                  { label: 'Updated', value: detail.updated_at },
                  { label: 'First reviewed', value: detail.reviewed_at },
                  { label: 'Implemented', value: detail.implemented_at },
                ].map(field => (
                  <div key={field.label}>
                    <dt className="text-xs text-slate-500">{field.label}</dt>
                    <dd className="mt-1 text-xs text-slate-300">
                      <Timestamp value={field.value} />
                    </dd>
                  </div>
                ))}
              </dl>
              <div className="space-y-5">
                {[
                  { label: 'Problem / current condition', value: detail.problem },
                  { label: 'Proposed solution', value: detail.proposed_solution },
                  { label: 'Expected benefit', value: detail.expected_benefit },
                  ...(detail.implementation_notes
                    ? [{ label: 'Implementation results', value: detail.implementation_notes }]
                    : []),
                ].map(field => (
                  <section key={field.label}>
                    <h3 className="mb-1 text-sm font-semibold text-white">{field.label}</h3>
                    <p className="whitespace-pre-wrap text-sm leading-relaxed text-slate-300 [overflow-wrap:anywhere]">
                      {field.value}
                    </p>
                  </section>
                ))}
              </div>
              <section className="mt-7 border-t border-fd-line pt-5">
                <h3 className="font-semibold text-white">Activity history</h3>
                <p className="mt-1 text-xs text-slate-500">
                  Recorded changes and comments, with author and time. Times are shown in Central Time.
                </p>
                <ol className="mt-4 space-y-4">
                  {detail.history.map(activity => (
                    <li key={activity.id} className="border-l-2 border-fd-line pl-4">
                      <div className="flex flex-wrap items-center justify-between gap-1">
                        <p className="text-sm font-medium text-slate-200">
                          {
                            {
                              submitted: 'Suggestion submitted',
                              updated: 'Suggestion updated',
                              status_changed: 'Status changed',
                              comment: 'Comment added',
                            }[activity.kind]
                          }
                        </p>
                        <span className="text-xs text-slate-500">
                          <Timestamp value={activity.created_at} />
                        </span>
                      </div>
                      <p className="mt-0.5 text-xs text-slate-400">{activity.actor_name}</p>
                      {activity.body && (
                        <p className="mt-2 whitespace-pre-wrap text-sm text-slate-300 [overflow-wrap:anywhere]">
                          {activity.body}
                        </p>
                      )}
                      {Object.keys(activity.changes).length > 0 && (
                        <details className="mt-2 text-xs text-slate-400">
                          <summary className="cursor-pointer text-blue-300">View recorded changes</summary>
                          <dl className="mt-2 space-y-2">
                            {Object.entries(activity.changes)
                              .filter(([field]) => field !== 'owner_id' || !activity.changes.owner_name)
                              .map(([field, change]) => (
                                <div key={field}>
                                  <dt className="font-medium text-slate-300">
                                    {fieldLabels[field] || field.replace(/_/g, ' ')}
                                  </dt>
                                  <dd className="whitespace-pre-wrap [overflow-wrap:anywhere]">
                                    <span className="text-slate-500">From: </span>
                                    {historyValue(field, change.from)}
                                    <br />
                                    <span className="text-slate-500">To: </span>
                                    {historyValue(field, change.to)}
                                  </dd>
                                </div>
                              ))}
                          </dl>
                        </details>
                      )}
                    </li>
                  ))}
                </ol>
              </section>
              {canManage && (
                <form className="mt-6 border-t border-fd-line pt-4" onSubmit={event => void postComment(event)}>
                  <FormField
                    label="Add a comment"
                    help="Use comments for review notes, trial observations, and follow-up. Comments become part of the permanent history."
                  >
                    {field => (
                      <textarea
                        {...field}
                        rows={3}
                        maxLength={5000}
                        required
                        disabled={saving}
                        className="input h-auto w-full"
                        value={comment}
                        onChange={event => setComment(event.target.value)}
                      />
                    )}
                  </FormField>
                  <div className="mt-3 flex justify-end">
                    <button type="submit" className="btn-primary" disabled={saving || conflict || !comment.trim()}>
                      {saving ? 'Posting…' : 'Post comment'}
                    </button>
                  </div>
                </form>
              )}
            </>
          )
        )}
      </Modal>
    </div>
  );
}
