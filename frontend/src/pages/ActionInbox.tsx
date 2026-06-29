import React, { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
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
import { MiniStat, MiniStatStrip } from '../components/cockpit';
import { AISuggestionCard, ConfidenceBadge, FeedbackButtons, WhyThisSuggestion } from '../components/ai';
import { AIRecommendation } from '../types/aiLearning';

type Severity = 'high' | 'medium' | 'low' | 'info';
type ItemSource = 'setup' | 'master-data' | 'notification' | 'ai';
type FilterKey = 'open' | 'high' | 'ai' | 'master-data' | 'setup' | 'notifications' | 'dismissed';

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

interface NotificationLog {
  id?: number | string;
  event_type?: string;
  title?: string;
  subject?: string;
  message?: string;
  body?: string;
  status?: string;
  sent_at?: string;
  created_at?: string;
  error_message?: string;
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
  notification: 'Notification',
  ai: 'AI',
};

const filterLabels: Record<FilterKey, string> = {
  open: 'Open',
  high: 'High',
  ai: 'AI',
  'master-data': 'Master Data',
  setup: 'Setup',
  notifications: 'Notifications',
  dismissed: 'Dismissed',
};

const formatDate = (value?: string) => {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleString();
};

const getStoredDismissed = () => {
  try {
    return new Set(JSON.parse(localStorage.getItem('actionInboxDismissed') || '[]') as string[]);
  } catch {
    return new Set<string>();
  }
};

export default function ActionInbox() {
  const [health, setHealth] = useState<SetupHealth | null>(null);
  const [notifications, setNotifications] = useState<NotificationLog[]>([]);
  const [aiRecommendations, setAiRecommendations] = useState<AIRecommendation[]>([]);
  const [notificationsAvailable, setNotificationsAvailable] = useState(true);
  const [aiAvailable, setAiAvailable] = useState(true);
  const [loading, setLoading] = useState(true);
  const [actioningId, setActioningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<FilterKey>('open');
  const [dismissed, setDismissed] = useState<Set<string>>(() => getStoredDismissed());

  const persistDismissed = (next: Set<string>) => {
    setDismissed(next);
    localStorage.setItem('actionInboxDismissed', JSON.stringify(Array.from(next)));
  };

  const loadInbox = async () => {
    setLoading(true);
    setError(null);

    const [healthResult, notificationResult, aiResult] = await Promise.allSettled([
      api.getSetupHealth(),
      api.getNotificationLogs({ limit: 25 }),
      api.getAIRecommendations({ status: 'pending', limit: 25 }),
    ]);

    if (healthResult.status === 'fulfilled') {
      setHealth(healthResult.value);
    } else {
      setError('Setup health is unavailable. Action Inbox is showing notification activity only.');
    }

    if (notificationResult.status === 'fulfilled') {
      const payload = notificationResult.value;
      const rows = Array.isArray(payload) ? payload : payload?.items || payload?.results || payload?.logs || [];
      setNotifications(rows);
      setNotificationsAvailable(true);
    } else {
      setNotifications([]);
      setNotificationsAvailable(false);
    }

    if (aiResult.status === 'fulfilled') {
      setAiRecommendations(Array.isArray(aiResult.value) ? aiResult.value : []);
      setAiAvailable(true);
    } else {
      setAiRecommendations([]);
      setAiAvailable(false);
    }

    setLoading(false);
  };

  useEffect(() => {
    loadInbox();
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
  const queueRecommendations = useMemo(
    () => (heroVisible ? rankedRecommendations.slice(3) : rankedRecommendations),
    [heroVisible, rankedRecommendations]
  );

  const items = useMemo<InboxItem[]>(() => {
    const setupItems: InboxItem[] = (health?.steps || [])
      .filter((step) => step.status !== 'complete')
      .map((step) => ({
        id: `setup:${step.key}`,
        source: 'setup',
        severity: 'medium',
        title: step.label,
        detail: step.reason || 'This setup step is not complete.',
        count: step.count,
        href: step.href,
      }));

    const masterDataItems: InboxItem[] = (health?.issues || []).map((issue) => ({
      id: `master-data:${issue.key}`,
      source: 'master-data',
      severity: issue.severity,
      title: issue.title,
      detail: issue.detail,
      count: issue.count,
      href: issue.href,
    }));

    const notificationItems: InboxItem[] = notifications.map((notification, index) => {
      const status = (notification.status || '').toLowerCase();
      const failed = status.includes('fail') || Boolean(notification.error_message);
      return {
        id: `notification:${notification.id || index}`,
        source: 'notification',
        severity: failed ? 'high' : 'info',
        title: notification.title || notification.subject || notification.event_type || 'Notification',
        detail: notification.error_message || notification.message || notification.body || status || 'Notification activity was recorded.',
        timestamp: notification.sent_at || notification.created_at,
      };
    });

    // While the hero is visible the top 3 render above; otherwise every recommendation joins the queue.
    const aiItems: InboxItem[] = queueRecommendations.map((recommendation) => ({
      id: `ai:${recommendation.id}`,
      source: 'ai',
      severity: recommendation.priority === 'high' ? 'high' : recommendation.priority === 'low' ? 'low' : recommendation.priority === 'info' ? 'info' : 'medium',
      title: recommendation.title,
      detail: recommendation.summary,
      timestamp: recommendation.created_at,
      recommendation,
    }));

    return [...aiItems, ...masterDataItems, ...setupItems, ...notificationItems];
  }, [queueRecommendations, health, notifications]);

  const filteredItems = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();

    return items.filter((item) => {
      const isDismissed = dismissed.has(item.id);
      if (filter === 'open' && isDismissed) return false;
      if (filter === 'dismissed' && !isDismissed) return false;
      if (filter === 'high' && item.severity !== 'high') return false;
      if (filter === 'ai' && item.source !== 'ai') return false;
      if (filter === 'master-data' && item.source !== 'master-data') return false;
      if (filter === 'setup' && item.source !== 'setup') return false;
      if (filter === 'notifications' && item.source !== 'notification') return false;

      if (!normalizedQuery) return true;
      return `${item.title} ${item.detail} ${sourceLabels[item.source]}`.toLowerCase().includes(normalizedQuery);
    });
  }, [dismissed, filter, items, query]);

  const openCount = items.filter((item) => !dismissed.has(item.id)).length;
  const highCount = items.filter((item) => item.severity === 'high' && !dismissed.has(item.id)).length;
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

  const acceptAIRecommendation = async (recommendation: AIRecommendation) => {
    setActioningId(recommendation.id);
    try {
      await api.acceptAIRecommendation(recommendation.id, 'Accepted from Action Inbox');
      setAiRecommendations((current) => current.filter((item) => item.id !== recommendation.id));
    } finally {
      setActioningId(null);
    }
  };

  const dismissAIRecommendation = async (recommendation: AIRecommendation) => {
    setActioningId(recommendation.id);
    try {
      await api.dismissAIRecommendation(recommendation.id, 'Dismissed from Action Inbox');
      setAiRecommendations((current) => current.filter((item) => item.id !== recommendation.id));
    } finally {
      setActioningId(null);
    }
  };

  const sendAIFeedback = async (recommendation: AIRecommendation, feedback: string) => {
    setActioningId(recommendation.id);
    try {
      await api.sendAIRecommendationFeedback(recommendation.id, { feedback });
    } finally {
      setActioningId(null);
    }
  };

  const snoozeAIRecommendation = async (recommendation: AIRecommendation, days: number) => {
    setActioningId(recommendation.id);
    try {
      await api.snoozeAIRecommendation(recommendation.id, days, 'Snoozed from Action Inbox');
      setAiRecommendations((current) => current.filter((item) => item.id !== recommendation.id));
    } finally {
      setActioningId(null);
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col xl:flex-row xl:items-center xl:justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <BellAlertIcon className="h-8 w-8 text-cyan-300" />
            <h1 className="text-2xl font-bold text-white">Action Inbox</h1>
          </div>
          <p className="text-slate-400 mt-1">A focused queue for AI recommendations, setup gaps, master-data blockers, and notification activity.</p>
        </div>
        <button onClick={loadInbox} className="btn-secondary flex items-center justify-center" disabled={loading}>
          <ArrowPathIcon className={`h-5 w-5 mr-2 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      <MiniStatStrip className="grid grid-cols-2 lg:grid-cols-4 gap-2">
        <MiniStat
          icon={InboxIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="Open Actions"
          value={openCount}
        />
        <MiniStat
          icon={ExclamationTriangleIcon}
          iconBg={highCount > 0 ? 'bg-fd-red/15' : 'bg-fd-green/15'}
          iconColor={highCount > 0 ? 'text-fd-red' : 'text-fd-green'}
          label="High Priority"
          value={highCount}
          valueColor={highCount > 0 ? 'text-fd-red' : undefined}
        />
        <MiniStat
          icon={SparklesIcon}
          iconBg="bg-fd-cyan/15"
          iconColor="text-fd-cyan"
          label="AI Suggestions"
          value={aiCount}
          valueColor="text-fd-cyan"
        />
        <div className="card card-compact !p-2.5 flex flex-col gap-1 min-w-0 h-full">
          <div className="flex items-center gap-1.5">
            <span className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-sm bg-fd-cyan/15">
              <CheckCircleIcon className="h-3.5 w-3.5 text-fd-cyan" />
            </span>
            <p className="stat-label !text-[10px] uppercase tracking-wide truncate">Setup Progress</p>
          </div>
          <p className="stat-value !text-xl tabular-nums">{setupProgress}%</p>
          <span className="block h-1.5 w-full rounded-sm bg-fd-line overflow-hidden">
            <span className="block h-full rounded-sm bg-fd-cyan" style={{ width: `${setupProgress}%` }} />
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
                disabled={actioningId === recommendation.id}
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
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search actions..."
              className="w-full rounded-sm border border-fd-line bg-slate-950/70 py-2 pl-10 pr-3 text-white placeholder:text-slate-500 focus:border-fd-blue focus:outline-none focus:ring-1 focus:ring-fd-blue"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <FunnelIcon className="h-5 w-5 text-slate-500" />
            {(Object.keys(filterLabels) as FilterKey[]).map((key) => (
              <button
                key={key}
                onClick={() => setFilter(key)}
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
        {(!notificationsAvailable || !aiAvailable) && (
          <div className="mt-2.5 flex flex-wrap items-center gap-2 border-t border-fd-line pt-2.5 text-[11px] text-slate-500">
            <span className="uppercase tracking-wide text-[10px] text-slate-600">Unavailable</span>
            {!notificationsAvailable && (
              <span className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-950/60 px-2 py-0.5">
                <ExclamationTriangleIcon className="h-3 w-3 text-fd-amber" />
                Notifications
              </span>
            )}
            {!aiAvailable && (
              <span className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-slate-950/60 px-2 py-0.5">
                <ExclamationTriangleIcon className="h-3 w-3 text-fd-amber" />
                AI recommendations
              </span>
            )}
            <span className="text-slate-600">— this source is hidden from the queue.</span>
          </div>
        )}
      </div>

      <div className="rounded-lg border border-slate-700 bg-[#151b28] overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center gap-3 py-16 text-slate-300">
            <ArrowPathIcon className="h-6 w-6 animate-spin" />
            Loading action inbox...
          </div>
        ) : filteredItems.length ? (
          <div className="divide-y divide-slate-800">
            {filteredItems.map((item) => {
              const isDismissed = dismissed.has(item.id);
              return (
                <div key={item.id} className="flex items-start justify-between gap-4 p-4 transition-colors hover:bg-slate-900/50">
                  <div className="flex min-w-0 gap-3">
                    <div className={`mt-1 rounded-lg border p-2 ${severityStyles[item.severity]}`}>
                      {item.severity === 'high' ? <ExclamationTriangleIcon className="h-5 w-5" /> : <InboxIcon className="h-5 w-5" />}
                    </div>
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className={`font-semibold ${isDismissed ? 'text-slate-500 line-through' : 'text-white'}`}>{item.title}</h2>
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
                            disabled={actioningId === item.recommendation.id}
                            onAccept={() => acceptAIRecommendation(item.recommendation as AIRecommendation)}
                            onDismiss={() => dismissAIRecommendation(item.recommendation as AIRecommendation)}
                            onFeedback={(feedback) => sendAIFeedback(item.recommendation as AIRecommendation, feedback)}
                            onSnooze={(days) => snoozeAIRecommendation(item.recommendation as AIRecommendation, days)}
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
                      <Link to={item.href} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-cyan-500/60 hover:text-cyan-200">
                        Open
                      </Link>
                    )}
                    {item.source === 'ai' ? null : isDismissed ? (
                      <button onClick={() => restoreItem(item.id)} className="rounded-lg border border-slate-700 px-3 py-2 text-sm text-slate-300 hover:border-cyan-500/60 hover:text-cyan-200">
                        Restore
                      </button>
                    ) : (
                      <button onClick={() => dismissItem(item.id)} className="rounded-lg border border-slate-700 p-2 text-slate-400 hover:border-slate-500 hover:text-white" title="Dismiss">
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
              {filter === 'dismissed' ? 'Dismissed actions will appear here.' : 'There are no matching open actions.'}
            </p>
          </div>
        )}
      </div>

    </div>
  );
}
