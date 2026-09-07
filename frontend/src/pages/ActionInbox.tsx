import { PageHeader } from '../components/ui/PageHeader';
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import {
  ArrowPathIcon,
  BellAlertIcon,
  CheckCircleIcon,
  ChevronRightIcon,
  ExclamationTriangleIcon,
  FunnelIcon,
  InboxIcon,
  MagnifyingGlassIcon,
  SparklesIcon,
  XMarkIcon,
} from '@heroicons/react/24/outline';
import api from '../services/api';
import { ComboBox } from '../components/ui/ComboBox';
import { Modal } from '../components/ui/Modal';
import { useUnsavedChanges } from '../hooks/useUnsavedChanges';
import {
  OperationalInboxItem,
  OperationalInboxResponse,
  OperationalInboxUpdate,
  OperationalSource,
} from '../types/operationsInbox';
import { useOptimisticMutation } from '../hooks/useOptimisticMutation';
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { AISuggestionCard, ConfidenceBadge, FeedbackButtons, WhyThisSuggestion } from '../components/ai';
import { AIRecommendation, recommendationIsApplyable } from '../types/aiLearning';
import { formatCentralDateTime, toDate } from '../utils/centralTime';

type Severity = 'high' | 'medium' | 'low' | 'info';
type ItemSource = 'setup' | 'master-data' | 'ai';
type FilterKey = 'open' | 'high' | 'ai' | 'master-data' | 'setup' | 'dismissed';

interface SetupStep {
  key: string;
  label: string;
  status: 'complete' | 'missing';
  count: number;
  required_count: number;
  href: string;
  reason?: string;
}

interface MasterDataIssue {
  key: string;
  severity: Severity;
  title: string;
  detail: string;
  count: number;
  href: string;
}

interface SetupHealth {
  progress: number;
  counts: Record<string, number>;
  steps: SetupStep[];
  issues: MasterDataIssue[];
}

interface InboxItem {
  id: string;
  source: ItemSource;
  severity: Severity;
  title: string;
  detail: string;
  count?: number;
  href?: string;
  timestamp?: string;
  recommendation?: AIRecommendation;
}

const severityStyles: Record<Severity, string> = {
  high: 'border-red-500/40 bg-red-500/10 text-red-200',
  medium: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  low: 'border-blue-500/40 bg-blue-500/10 text-blue-200',
  info: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-200',
};

const sourceLabels: Record<ItemSource, string> = {
  setup: 'Setup',
  'master-data': 'Master Data',
  ai: 'AI',
};

const filterLabels: Record<FilterKey, string> = {
  open: 'Open',
  high: 'High',
  ai: 'AI',
  'master-data': 'Master Data',
  setup: 'Setup',
  dismissed: 'Dismissed',
};

// Shop-local Central date+time; returns null for an empty/invalid timestamp so
// the caller can conditionally render (kept from the prior local formatter).
const formatDate = (value?: string) => {
  if (!value || !toDate(value)) return null;
  return formatCentralDateTime(value);
};

const getStoredDismissed = (key: string) => {
  try {
    return new Set(JSON.parse(localStorage.getItem(key) || '[]') as string[]);
  } catch {
    return new Set<string>();
  }
};

const operationalLabels: Record<OperationalSource, string> = {
  late_work_order: 'Late work order',
  blocker: 'Work-order blocker',
  low_stock: 'Low stock',
  quality_ncr: 'Quality',
  overdue_po_line: 'Overdue purchase order',
  supplier_follow_up: 'Supplier follow-up',
  mrp_shortage: 'Projected material shortage',
};

/** Live source records are separate from optional local setup/recommendation dismissals. */
export function OperationalQueue({ scope }: { scope: string }) {
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<OperationalInboxResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [actionError, setActionError] = useState('');
  const [pending, setPending] = useState<string | null>(null);
  const [editing, setEditing] = useState<OperationalInboxItem | null>(null);
  const [owner, setOwner] = useState('');
  const [nextAction, setNextAction] = useState('');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(0);
  const sequence = useRef(0);
  const saving = useRef(false);
  const activeScope = useRef(scope);
  activeScope.current = scope;
  const editingRef = useRef(editing);
  editingRef.current = editing;
  const requestedOwner = params.get('operationsOwner');
  const ownerFilter = requestedOwner === 'mine' || requestedOwner === 'unassigned' ? requestedOwner : 'all';
  const snoozed = params.get('operationsView') === 'snoozed';
  const userId = Number(scope.split(':').pop());
  const dirty = !!editing && (owner !== String(editing.owner_id ?? '') || nextAction !== editing.next_action);
  const { confirmDiscard, markSaved } = useUnsavedChanges(dirty);

  const load = async () => {
    const current = ++sequence.current;
    setLoading(true);
    setError('');
    try {
      const result = await api.getOperationalInbox();
      if (current === sequence.current) setData(result);
    } catch {
      if (current === sequence.current)
        setError(
          'Operational issues could not be refreshed. Any issues below are last verified results; refresh before changing an action.'
        );
    } finally {
      if (current === sequence.current) setLoading(false);
    }
  };
  useEffect(() => {
    setData(null);
    setEditing(null);
    setActionError('');
    setPending(null);
    saving.current = false;
    void load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible' && !saving.current && !editingRef.current) void load();
    }, 60_000);
    return () => {
      ++sequence.current;
      window.clearInterval(timer);
    };
  }, [scope]);

  const setView = (key: string, value: string) => {
    setPage(0);
    setParams(previous => {
      const next = new URLSearchParams(previous);
      if (value === 'all' || value === 'active') next.delete(key);
      else next.set(key, value);
      return next;
    });
  };
  const filtered = (data?.items ?? []).filter(item => {
    if (Boolean(item.snoozed_until) !== snoozed) return false;
    if (ownerFilter === 'mine' && item.owner_id !== userId) return false;
    if (ownerFilter === 'unassigned' && item.owner_id !== null) return false;
    return `${item.title} ${item.detail} ${item.owner_name ?? ''} ${item.next_action}`
      .toLowerCase()
      .includes(search.trim().toLowerCase());
  });
  const lastPage = Math.max(0, Math.ceil(filtered.length / 20) - 1);
  const currentPage = Math.min(page, lastPage);
  const visible = filtered.slice(currentPage * 20, currentPage * 20 + 20);
  const stale = loading || !!error;

  const update = async (
    item: OperationalInboxItem,
    patch: Omit<OperationalInboxUpdate, 'expected_version' | 'occurrence'>,
    close = false
  ) => {
    if (saving.current || stale) return;
    saving.current = true;
    const mutationScope = scope;
    setPending(item.key);
    setActionError('');
    ++sequence.current; // Invalidate a response begun before this mutation.
    try {
      const saved = await api.updateOperationalInbox(item.source_kind, item.source_id, {
        ...patch,
        expected_version: item.version,
        occurrence: item.occurrence,
      });
      if (activeScope.current !== mutationScope) return;
      setData(previous =>
        previous ? { ...previous, items: previous.items.map(row => (row.key === saved.key ? saved : row)) } : previous
      );
      if (close) {
        markSaved();
        setEditing(null);
      }
    } catch (err: unknown) {
      if (activeScope.current !== mutationScope) return;
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      setActionError(
        typeof detail === 'string' ? detail : 'Action was not saved. Your changes are retained. Refresh and retry.'
      );
    } finally {
      if (activeScope.current === mutationScope) {
        saving.current = false;
        setPending(null);
      }
    }
  };
  const closeEdit = () => {
    if (!pending && confirmDiscard()) setEditing(null);
  };

  return (
    <section
      aria-labelledby="operational-inbox-heading"
      className="rounded-lg border border-fd-line bg-fd-panel p-4 space-y-4 min-w-0"
    >
      <div className="flex flex-wrap justify-between gap-3">
        <div className="min-w-0">
          <h2 id="operational-inbox-heading" className="text-xl font-semibold text-white">
            Operations needing attention
          </h2>
          <p className="mt-1 text-sm text-fd-body">
            Assign the next action here. Resolve the underlying issue in its workflow.
          </p>
          {data && (
            <p className="text-xs text-fd-mute mt-1">
              Last verified {formatDate(data.checked_at)} · {data.items.length} issues in the loaded scope
            </p>
          )}
        </div>
        <button className="btn-secondary" onClick={() => void load()} disabled={loading || !!pending}>
          {' '}
          {loading ? 'Refreshing operations…' : 'Refresh operations'}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-sm text-amber-200">
          {error}
        </p>
      )}
      {!!data?.truncated_sources.length && (
        <p role="status" className="text-sm text-amber-200">
          Showing the newest 1,000 issues per category. Some categories have more records; open their source workflow
          for the complete list. Counts describe only loaded issues.
        </p>
      )}
      {actionError && !editing && (
        <p role="alert" className="text-sm text-red-200">
          {actionError}
        </p>
      )}
      <div className="flex flex-wrap gap-2" aria-label="Operational assignment filters">
        {(['all', 'mine', 'unassigned'] as const).map(value => (
          <button
            key={value}
            aria-pressed={ownerFilter === value}
            onClick={() => setView('operationsOwner', value)}
            className="btn-secondary"
          >
            {value === 'all' ? 'Everyone' : value === 'mine' ? 'Mine' : 'Unassigned'}
          </button>
        ))}
        <button aria-pressed={!snoozed} onClick={() => setView('operationsView', 'active')} className="btn-secondary">
          Active issues
        </button>
        <button aria-pressed={snoozed} onClick={() => setView('operationsView', 'snoozed')} className="btn-secondary">
          Snoozed
        </button>
      </div>
      <input
        aria-label="Search operational issues"
        placeholder="Search operational issues…"
        value={search}
        onChange={event => {
          setSearch(event.target.value);
          setPage(0);
        }}
        className="input w-full"
      />
      {loading && !data ? (
        <p role="status">Loading operational issues…</p>
      ) : !data ? (
        <p>Operational scope is unavailable. Refresh to try again.</p>
      ) : visible.length === 0 ? (
        <p className="text-fd-body">
          No operational issues match this view{error ? '; current source results are unverified.' : '.'}
        </p>
      ) : (
        <div className="space-y-3" aria-busy={loading}>
          {visible.map(item => (
            <article key={item.key} aria-label={item.title} className="rounded border border-fd-line p-3 min-w-0">
              <div className="flex flex-wrap items-center gap-2 text-xs text-fd-body">
                <span>{operationalLabels[item.source_kind]}</span>
                <span className={item.severity === 'high' ? 'text-red-200' : 'text-amber-200'}>
                  {item.severity === 'high' ? 'High priority' : 'Needs review'}
                </span>
                {item.acknowledged && <span className="text-emerald-200">Acknowledged · issue remains active</span>}
              </div>
              <h3 className="font-semibold text-white mt-1 break-words">{item.title}</h3>
              <p className="text-sm text-fd-body mt-1 break-words whitespace-pre-wrap">{item.detail}</p>
              <dl className="text-sm mt-2 space-y-1">
                <div>
                  <dt className="inline text-fd-mute">Owner: </dt>
                  <dd className="inline text-fd-body">{item.owner_name ?? 'Unassigned'}</dd>
                </div>
                <div>
                  <dt className="inline text-fd-mute">Next action: </dt>
                  <dd className="inline text-fd-body break-words">{item.next_action || item.suggested_action}</dd>
                </div>
              </dl>
              {item.snoozed_until && (
                <p className="text-xs text-amber-200 mt-2">
                  Snoozed until {formatDate(item.snoozed_until)}. A changed issue returns automatically.
                </p>
              )}
              <div className="flex flex-wrap gap-2 mt-3">
                <Link to={item.href} className="btn-secondary">
                  Open workflow
                </Link>
                {item.can_manage && (
                  <>
                    <button
                      className="btn-secondary"
                      disabled={stale || !!pending}
                      onClick={() => {
                        setEditing(item);
                        setOwner(String(item.owner_id ?? ''));
                        setNextAction(item.next_action);
                        setActionError('');
                      }}
                    >
                      Assign / next action
                    </button>
                    {!item.acknowledged && (
                      <button
                        className="btn-secondary"
                        disabled={stale || !!pending}
                        onClick={() => void update(item, { acknowledge: true })}
                      >
                        Acknowledge
                      </button>
                    )}
                    <button
                      className="btn-secondary"
                      disabled={stale || !!pending}
                      onClick={() => void update(item, { snooze_hours: item.snoozed_until ? 0 : 24 })}
                    >
                      {item.snoozed_until ? 'Return to active' : 'Snooze 24 hours'}
                    </button>
                  </>
                )}
                {pending === item.key && (
                  <span role="status" className="text-sm text-fd-body self-center">
                    Saving action…
                  </span>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {data && (
        <div className="flex flex-wrap items-center gap-3 text-sm text-fd-body">
          <span>
            {filtered.length} matching loaded issues · Page {currentPage + 1} of {lastPage + 1}
          </span>
          <button className="btn-secondary" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>
            Previous issues
          </button>
          <button className="btn-secondary" disabled={currentPage >= lastPage} onClick={() => setPage(currentPage + 1)}>
            Next issues
          </button>
        </div>
      )}
      <Modal open={!!editing} onClose={closeEdit} ariaLabelledBy="inbox-assignment-title" size="md">
        <h2 id="inbox-assignment-title" className="text-xl font-semibold text-white">
          Assign next action
        </h2>
        <p className="text-sm text-fd-body my-3">{editing?.title}</p>
        {actionError && (
          <p role="alert" className="text-red-200 mb-3">
            {actionError}
          </p>
        )}
        <form
          onSubmit={event => {
            event.preventDefault();
            if (editing)
              void update(editing, { owner_id: owner ? Number(owner) : null, next_action: nextAction }, true);
          }}
          className="space-y-4"
        >
          <div>
            <label className="block text-sm mb-1" htmlFor="inbox-action-owner">
              Owner
            </label>
            <ComboBox
              id="inbox-action-owner"
              value={owner}
              onChange={setOwner}
              emptyOptionLabel="Unassigned"
              disabled={!!pending}
              options={(data?.assignees ?? [])
                .filter(user => editing && user.sources.includes(editing.source_kind))
                .map(user => ({ value: String(user.id), label: user.name }))}
            />
          </div>
          <div>
            <label className="block text-sm mb-1" htmlFor="inbox-next-action">
              Next action
            </label>
            <textarea
              id="inbox-next-action"
              value={nextAction}
              onChange={event => setNextAction(event.target.value)}
              maxLength={500}
              rows={3}
              disabled={!!pending}
              className="input w-full"
              placeholder={editing?.suggested_action}
            />
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            <button type="button" className="btn-secondary" disabled={!!pending} onClick={closeEdit}>
              Cancel
            </button>
            <button type="submit" className="btn-primary" disabled={!!pending || stale}>
              {pending ? 'Saving…' : 'Save action'}
            </button>
          </div>
        </form>
      </Modal>
    </section>
  );
}

export default function ActionInbox() {
  const [params, setParams] = useSearchParams();
  const request = useRef(0);
  const [hasLoaded, setHasLoaded] = useState(false);
  const userScope = (() => {
    try {
      const user = JSON.parse(sessionStorage.getItem('user') ?? '{}');
      return `${user.company_id ?? 'workspace'}:${user.id ?? 'anonymous'}`;
    } catch {
      return 'anonymous';
    }
  })();
  const dismissedKey = `actionInboxDismissed:${userScope}`;
  const [health, setHealth] = useState<SetupHealth | null>(null);
  const [aiRecommendations, setAiRecommendations] = useState<AIRecommendation[]>([]);
  const [aiAvailable, setAiAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [actioningId, setActioningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const requestedFilter = params.get('filter');
  const filter: FilterKey =
    requestedFilter && Object.prototype.hasOwnProperty.call(filterLabels, requestedFilter)
      ? (requestedFilter as FilterKey)
      : 'open';
  const setFilter = (value: FilterKey) =>
    setParams(previous => {
      const next = new URLSearchParams(previous);
      if (value === 'open') next.delete('filter');
      else next.set('filter', value);
      return next;
    });
  const [dismissed, setDismissed] = useState<Set<string>>(() => getStoredDismissed(dismissedKey));

  const persistDismissed = (next: Set<string>) => {
    setDismissed(next);
    try {
      localStorage.setItem(dismissedKey, JSON.stringify(Array.from(next)));
    } catch {
      /* Optional local preference. */
    }
  };

  const loadInbox = async () => {
    const seq = ++request.current;
    setLoading(true);
    setError(null);

    const [healthResult, aiResult] = await Promise.allSettled([
      api.getSetupHealth(),
      api.getAIRecommendations({ status: 'pending', limit: 25 }),
    ]);

    if (seq !== request.current) return;
    setHasLoaded(true);
    if (healthResult.status === 'fulfilled') {
      setHealth(healthResult.value);
    } else {
      setError('Setup health could not be refreshed. Any setup and master-data items below are last verified results.');
    }

    if (aiResult.status === 'fulfilled') {
      setAiRecommendations(Array.isArray(aiResult.value) ? aiResult.value : []);
      setAiAvailable(true);
    } else {
      setAiAvailable(false);
    }

    setLoading(false);
  };

  useEffect(() => {
    loadInbox();
    return () => {
      ++request.current;
    };
  }, []);

  // Backend already returns recommendations sorted by the deterministic score; re-sort
  // defensively client-side so the "Top 3 today" hero is stable even on stale caches.
  const rankedRecommendations = useMemo(
    () => [...aiRecommendations].sort((a, b) => (b.score ?? 0) - (a.score ?? 0)),
    [aiRecommendations]
  );
  // The hero only renders on the default view. With a non-default filter or an active search,
  // it would disagree with the queue (showing recommendations the filtered list excludes), so
  // it is hidden and every recommendation folds back into the queue for filtering/searching.
  const heroVisible = filter === 'open' && query.trim() === '';
  const topThree = useMemo(
    () => (heroVisible ? rankedRecommendations.slice(0, 3) : []),
    [heroVisible, rankedRecommendations]
  );
  const items = useMemo<InboxItem[]>(() => {
    const setupItems: InboxItem[] = (health?.steps || [])
      .filter(step => step.status !== 'complete')
      .map(step => ({
        id: `setup:${step.key}`,
        source: 'setup',
        severity: 'medium',
        title: step.label,
        detail: step.reason || 'This setup step is not complete.',
        count: step.count,
        href: step.href,
      }));

    const masterDataItems: InboxItem[] = (health?.issues || []).map(issue => ({
      id: `master-data:${issue.key}`,
      source: 'master-data',
      severity: issue.severity,
      title: issue.title,
      detail: issue.detail,
      count: issue.count,
      href: issue.href,
    }));

    // While the hero is visible the top 3 render above; otherwise every recommendation joins the queue.
    const aiItems: InboxItem[] = rankedRecommendations.map(recommendation => ({
      id: `ai:${recommendation.id}`,
      source: 'ai',
      severity:
        recommendation.priority === 'high'
          ? 'high'
          : recommendation.priority === 'low'
            ? 'low'
            : recommendation.priority === 'info'
              ? 'info'
              : 'medium',
      title: recommendation.title,
      detail: recommendation.summary,
      timestamp: recommendation.created_at,
      recommendation,
    }));

    return [...aiItems, ...masterDataItems, ...setupItems];
  }, [rankedRecommendations, health]);

  const filteredItems = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();

    return items.filter(item => {
      const isDismissed = dismissed.has(item.id);
      if (filter !== 'dismissed' && isDismissed) return false;
      if (
        heroVisible &&
        item.source === 'ai' &&
        topThree.some(recommendation => recommendation.id === item.recommendation?.id)
      )
        return false;
      if (filter === 'dismissed' && !isDismissed) return false;
      if (filter === 'high' && item.severity !== 'high') return false;
      if (filter === 'ai' && item.source !== 'ai') return false;
      if (filter === 'master-data' && item.source !== 'master-data') return false;
      if (filter === 'setup' && item.source !== 'setup') return false;

      if (!normalizedQuery) return true;
      return `${item.title} ${item.detail} ${sourceLabels[item.source]}`.toLowerCase().includes(normalizedQuery);
    });
  }, [dismissed, filter, items, query, heroVisible, topThree]);

  const firstException = items.find(item => item.severity === 'high' && !dismissed.has(item.id));
  const openCount = items.filter(item => !dismissed.has(item.id)).length;
  const highCount = items.filter(item => item.severity === 'high' && !dismissed.has(item.id)).length;
  const aiCount = aiRecommendations.length;
  const setupProgress = health?.progress ?? 0;

  const dismissItem = (id: string) => {
    const next = new Set(dismissed);
    next.add(id);
    persistDismissed(next);
  };

  const restoreItem = (id: string) => {
    const next = new Set(dismissed);
    next.delete(id);
    persistDismissed(next);
  };

  // Accept / dismiss / snooze all optimistically REMOVE the recommendation from the
  // queue the moment the operator acts, then call the server. These actions are
  // effectively never server-rejected (they record a decision on a pending row), so
  // the snappy removal is safe; on the rare failure the hook re-inserts the card at
  // its original position and toasts the verbatim server error — it never reports
  // success for a failed call. The per-call removal descriptor is threaded through
  // run(ctx), so each card's rollback restores THAT card even when two cards are
  // actioned in quick succession (per-card disabled lets two be in flight).
  type RemovalCtx = { recommendation: AIRecommendation; index: number; mutate: () => Promise<unknown> };

  const removalMutation = useOptimisticMutation<unknown, RemovalCtx>({
    applyOptimistic: ({ recommendation }) => {
      setAiRecommendations(current => current.filter(item => item.id !== recommendation.id));
    },
    rollback: ({ recommendation, index }) => {
      // Re-insert the card at its original index so the queue order is preserved.
      setAiRecommendations(current => {
        if (current.some(item => item.id === recommendation.id)) return current;
        const next = [...current];
        next.splice(Math.min(index, next.length), 0, recommendation);
        return next;
      });
    },
    mutate: ({ mutate }) => mutate(),
    errorFallback: 'Action failed. Please try again.',
  });

  const runRemoval = async (recommendation: AIRecommendation, mutate: () => Promise<unknown>) => {
    const index = aiRecommendations.findIndex(item => item.id === recommendation.id);
    setActioningId(recommendation.id);
    try {
      await removalMutation.run({ recommendation, index: index < 0 ? aiRecommendations.length : index, mutate });
    } finally {
      setActioningId(null);
    }
  };

  const acceptAIRecommendation = (recommendation: AIRecommendation) => {
    const apply = recommendationIsApplyable(recommendation);
    // Accept always removes the card; apply errors are non-fatal (status is already accepted).
    return runRemoval(recommendation, () =>
      api.acceptAIRecommendation(
        recommendation.id,
        apply ? 'Accepted & applied from Action Inbox' : 'Accepted from Action Inbox',
        apply
      )
    );
  };

  const dismissAIRecommendation = (recommendation: AIRecommendation) =>
    runRemoval(recommendation, () => api.dismissAIRecommendation(recommendation.id, 'Dismissed from Action Inbox'));

  const snoozeAIRecommendation = (recommendation: AIRecommendation, days: number) =>
    runRemoval(recommendation, () => api.snoozeAIRecommendation(recommendation.id, days, 'Snoozed from Action Inbox'));

  // Feedback does not remove the card from the queue, so it keeps its own simple
  // in-flight state rather than the optimistic-removal path.
  const sendAIFeedback = async (recommendation: AIRecommendation, feedback: string) => {
    setActioningId(recommendation.id);
    try {
      await api.sendAIRecommendationFeedback(recommendation.id, { feedback });
    } finally {
      setActioningId(null);
    }
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Action Inbox"
        description="Operational issues, shared next actions, AI recommendations and setup gaps."
        icon={<BellAlertIcon className="h-8 w-8 text-cyan-300" />}
        actions={
          <button onClick={loadInbox} className="btn-secondary" disabled={loading}>
            <ArrowPathIcon aria-hidden="true" className={`h-5 w-5 mr-2 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        }
      />

      <OperationalQueue scope={userScope} />
      <h2 className="text-xl font-semibold text-white">Recommendations and setup</h2>

      {firstException && !loading && (
        <section
          aria-label="First action to review"
          className="rounded border border-red-500/40 bg-red-500/10 p-4 flex flex-wrap items-center justify-between gap-3"
        >
          <div>
            <p className="text-sm text-red-200">Review first · high priority</p>
            <h2 className="font-semibold text-white">{firstException.title}</h2>
            <p className="text-sm text-fd-body">{firstException.detail}</p>
          </div>
          {firstException.href ? (
            <Link className="btn-primary" to={firstException.href}>
              Review affected records
            </Link>
          ) : (
            <button className="btn-primary" onClick={() => setFilter('high')}>
              Review high-priority actions
            </button>
          )}
        </section>
      )}

      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={InboxIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Recommendation & setup actions"
          value={hasLoaded ? openCount : 'Loading…'}
          subtitle={error || !aiAvailable ? 'Partial · unavailable sources' : 'Includes featured recommendations'}
          onClick={() => setFilter('open')}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={highCount > 0 ? 'bg-fd-red/15' : 'bg-fd-green/15'}
          iconColor={highCount > 0 ? 'text-fd-red' : 'text-fd-green'}
          label="High Priority"
          value={hasLoaded ? highCount : 'Loading…'}
          onClick={() => setFilter('high')}
          valueColor={highCount > 0 ? 'text-fd-red' : undefined}
        />
        <MiniStat
          icon={SparklesIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="AI Suggestions"
          value={aiAvailable && hasLoaded ? aiCount : hasLoaded ? 'Unavailable' : 'Loading…'}
          subtitle="Up to 25 pending recommendations"
          onClick={() => setFilter('ai')}
          valueColor="text-fd-cyan"
        />
        <div className="card card-compact !p-2.5 flex flex-col gap-1 min-w-0 h-full">
          <div className="flex items-center gap-1.5">
            <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-sm bg-fd-cyan/15">
              <CheckCircleIcon className="h-3.5 w-3.5 text-fd-cyan" />
            </span>
            <p className="stat-label !text-[10px] uppercase tracking-wide truncate">Setup Progress</p>
          </div>
          <p className="stat-value !text-xl tabular-nums">{health && !error ? `${setupProgress}%` : 'Unverified'}</p>
          <span className="block h-1.5 w-full rounded-sm bg-fd-line overflow-hidden">
            <span
              className="block h-full rounded-sm bg-fd-cyan"
              style={{ width: `${health && !error ? `${setupProgress}%` : 'Unverified'}` }}
            />
          </span>
        </div>
      </MiniStatStrip>

      {error && (
        <div className="rounded-sm border border-fd-amber/40 bg-fd-amber/10 px-3 py-2.5 text-sm text-fd-amber">
          {error}
        </div>
      )}

      {!loading && topThree.length > 0 && (
        <section aria-label="Top 3 today" className="rounded-lg border border-cyan-500/30 bg-cyan-500/5 p-4">
          <div className="flex items-center gap-2">
            <SparklesIcon className="h-5 w-5 text-cyan-300" />
            <h2 className="text-lg font-semibold text-white">Top 3 today</h2>
            <span className="text-sm text-slate-400">The highest-impact recommendations right now.</span>
          </div>
          <div className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-3">
            {topThree.map((recommendation, index) => (
              <AISuggestionCard
                key={recommendation.id}
                recommendation={recommendation}
                rank={index + 1}
                disabled={!aiAvailable || actioningId === recommendation.id}
                onAccept={acceptAIRecommendation}
                onDismiss={dismissAIRecommendation}
                onFeedback={sendAIFeedback}
                onSnooze={snoozeAIRecommendation}
              />
            ))}
          </div>
        </section>
      )}

      <div className="rounded-sm border border-fd-line bg-fd-panel p-3">
        <div className="flex flex-col lg:flex-row gap-3 lg:items-center lg:justify-between">
          <div className="relative flex-1">
            <MagnifyingGlassIcon className="pointer-events-none absolute left-3 top-1/2 h-5 w-5 -translate-y-1/2 text-slate-500" />
            <input
              value={query}
              onChange={event => setQuery(event.target.value)}
              placeholder="Search actions..."
              aria-label="Search actions"
              className="w-full rounded-sm border border-fd-line bg-slate-950/70 py-2 pl-10 pr-3 text-white placeholder:text-slate-500 focus:border-fd-blue focus:outline-none focus:ring-1 focus:ring-fd-blue"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <FunnelIcon className="h-5 w-5 text-slate-500" />
            {(Object.keys(filterLabels) as FilterKey[]).map(key => (
              <button
                key={key}
                onClick={() => setFilter(key)}
                aria-pressed={filter === key}
                className={`rounded-sm px-3 py-1.5 text-sm font-medium transition-colors ${
                  filter === key
                    ? 'bg-fd-blue/20 text-fd-blue border border-fd-blue/50'
                    : 'bg-slate-900/70 text-slate-300 border border-fd-line hover:border-fd-line-bright'
                }`}
              >
                {filterLabels[key]}
              </button>
            ))}
          </div>
        </div>
        {!aiAvailable && (
          <div className="mt-2.5 flex flex-wrap items-center gap-2 border-t border-fd-line pt-2.5 text-[11px] text-slate-500">
            <span className="uppercase tracking-wide text-[10px] text-slate-600">Unavailable</span>
            <span className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-950/60 px-2 py-0.5">
              <ExclamationTriangleIcon className="h-3 w-3 text-fd-amber" />
              AI recommendations
            </span>
            <span className="text-fd-mute">— any recommendations shown are last verified and may be out of date.</span>
          </div>
        )}
      </div>

      <div className="rounded-lg border border-slate-700 bg-fd-panel overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center gap-3 py-16 text-slate-300">
            <ArrowPathIcon className="h-6 w-6 animate-spin" />
            Loading action inbox...
          </div>
        ) : filteredItems.length ? (
          <div className="divide-y divide-slate-800">
            {filteredItems.map(item => {
              const isDismissed = dismissed.has(item.id);
              return (
                <div
                  key={item.id}
                  className="flex flex-col sm:flex-row items-start justify-between gap-4 p-4 transition-colors hover:bg-slate-900/50"
                >
                  <div className="flex min-w-0 gap-3">
                    <div className={`mt-1 rounded-lg border p-2 ${severityStyles[item.severity]}`}>
                      {item.severity === 'high' ? (
                        <ExclamationTriangleIcon className="h-5 w-5" />
                      ) : (
                        <InboxIcon className="h-5 w-5" />
                      )}
                    </div>
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className={`font-semibold ${isDismissed ? 'text-slate-500 line-through' : 'text-white'}`}>
                          {item.title}
                        </h2>
                        <span className="rounded-full border border-slate-700 bg-slate-950/70 px-2 py-0.5 text-xs text-slate-300">
                          {sourceLabels[item.source]}
                        </span>
                        {item.recommendation && <ConfidenceBadge score={item.recommendation.confidence_score} />}
                        {item.count !== undefined && (
                          <span className="rounded-full bg-slate-800 px-2 py-0.5 text-xs text-slate-300">
                            {item.count}
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-sm text-slate-400">{item.detail}</p>
                      {item.recommendation && (
                        <>
                          <WhyThisSuggestion
                            rationale={item.recommendation.rationale}
                            evidence={item.recommendation.evidence || []}
                            impact={item.recommendation.impact || {}}
                          />
                          <FeedbackButtons
                            disabled={!aiAvailable || actioningId === item.recommendation.id}
                            applyable={recommendationIsApplyable(item.recommendation as AIRecommendation)}
                            onAccept={() => acceptAIRecommendation(item.recommendation as AIRecommendation)}
                            onDismiss={() => dismissAIRecommendation(item.recommendation as AIRecommendation)}
                            onFeedback={feedback => sendAIFeedback(item.recommendation as AIRecommendation, feedback)}
                            onSnooze={days => snoozeAIRecommendation(item.recommendation as AIRecommendation, days)}
                          />
                        </>
                      )}
                      {formatDate(item.timestamp) && (
                        <p className="mt-2 text-xs text-slate-500">{formatDate(item.timestamp)}</p>
                      )}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    {item.href && !isDismissed && (
                      <Link
                        to={item.href}
                        className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-cyan-500/60 hover:text-cyan-200"
                      >
                        Open
                      </Link>
                    )}
                    {item.source === 'ai' ? null : isDismissed ? (
                      <button
                        onClick={() => restoreItem(item.id)}
                        className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-cyan-500/60 hover:text-cyan-200"
                      >
                        Restore
                      </button>
                    ) : (
                      <button
                        onClick={() => dismissItem(item.id)}
                        className="rounded-lg border border-slate-700 p-2 text-slate-400 hover:border-slate-500 hover:text-white"
                        title="Dismiss"
                      >
                        <XMarkIcon className="h-5 w-5" />
                      </button>
                    )}
                    {item.href && !isDismissed && <ChevronRightIcon className="h-5 w-5 text-slate-500" />}
                  </div>
                </div>
              );
            })}
          </div>
        ) : (
          <div className="py-16 text-center">
            <CheckCircleIcon className="mx-auto h-12 w-12 text-emerald-300" />
            <h2 className="mt-4 text-lg font-semibold text-white">No actions in this view</h2>
            <p className="mt-1 text-sm text-slate-400">
              {error || !aiAvailable
                ? 'Some sources are unavailable. Retry before concluding there is no work to review.'
                : filter === 'dismissed'
                  ? 'Dismissed actions will appear here.'
                  : 'There are no matching open actions.'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}
