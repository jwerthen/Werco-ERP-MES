import React, { useEffect, useMemo, useState } from 'react';
import { Link, useLocation, useSearchParams } from 'react-router-dom';
import api from '../../services/api';
import { JobTimelineResponse, TimelineCategory } from '../../types/jobPlanning';
import { Button, Spinner } from '../ui';
import { centralWallClockToUtcISO, formatCentralDateTime } from '../../utils/centralTime';

const categories: TimelineCategory[] = ['job', 'production', 'labor', 'material', 'quality', 'blocker', 'audit'];

export default function JobTimelinePanel({ workOrderId }: { workOrderId: number }) {
  const location = useLocation();
  const [query, setQuery] = useSearchParams();
  const [open, setOpen] = useState(
    () => location.hash.startsWith('#event-') || location.hash === '#job-timeline' || query.has('timeline_category')
  );
  const rawCategory = query.get('timeline_category') as TimelineCategory;
  const category = categories.includes(rawCategory) ? rawCategory : '';
  const actor = useMemo(() => {
    const id = Number(query.get('timeline_actor'));
    return Number.isSafeInteger(id) && id > 0 ? { id, name: query.get('timeline_actor_name') || `Actor #${id}` } : null;
  }, [query]);
  const since = /^\d{4}-\d{2}-\d{2}$/.test(query.get('timeline_since') || '') ? query.get('timeline_since')! : '';
  const updateFilters = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(query);
    Object.entries(changes).forEach(([key, value]) => {
      if (value) next.set(key, value);
      else next.delete(key);
    });
    setQuery(next, { replace: true });
  };
  const setCategory = (value: TimelineCategory | '') => updateFilters({ timeline_category: value });
  const setActor = (value: { id: number; name: string } | null) =>
    updateFilters({ timeline_actor: value ? String(value.id) : null, timeline_actor_name: value?.name || null });
  const setSince = (value: string) => updateFilters({ timeline_since: value });
  const [cursors, setCursors] = useState<Array<string | undefined>>([undefined]);
  const [reload, setReload] = useState(0);
  const [data, setData] = useState<JobTimelineResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    if (location.hash.startsWith('#event-') || location.hash === '#job-timeline') setOpen(true);
  }, [location.hash]);
  useEffect(() => {
    if (data && location.hash.startsWith('#event-'))
      document.getElementById(location.hash.slice(1))?.scrollIntoView?.({ block: 'nearest' });
  }, [data, location.hash]);
  useEffect(() => {
    setData(null);
    setCursors([undefined]);
  }, [workOrderId, category, actor, since]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setLoading(true);
    setFailed(false);
    api
      .getWorkOrderTimeline(workOrderId, {
        category: category || undefined,
        actor_id: actor?.id,
        cursor,
        start_at: since ? centralWallClockToUtcISO(`${since}T00:00`) || undefined : undefined,
      })
      .then(result => {
        if (active) setData(result);
      })
      .catch(() => {
        if (active) setFailed(true);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [workOrderId, open, category, actor, since, cursor, reload]);
  return (
    <section id="job-timeline" className="card p-4 scroll-mt-20">
      <Button variant="secondary" aria-expanded={open} onClick={() => setOpen(!open)}>
        Job timeline
      </Button>
      {open && (
        <div className="mt-3 space-y-3">
          <div className="flex flex-wrap items-end gap-3">
            <div>
              <label className="label" htmlFor="timeline-category">
                Events
              </label>
              <select
                id="timeline-category"
                className="input"
                value={category}
                onChange={event => setCategory(event.target.value as TimelineCategory | '')}
              >
                <option value="">All recorded events</option>
                {categories.map(value => (
                  <option key={value} value={value}>
                    {value[0].toUpperCase() + value.slice(1)}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="label" htmlFor="timeline-since">
                Since (Central)
              </label>
              <input
                id="timeline-since"
                type="date"
                className="input"
                value={since}
                onChange={event => setSince(event.target.value)}
              />
            </div>
            {actor && (
              <Button variant="secondary" onClick={() => setActor(null)}>
                Clear actor: {actor.name}
              </Button>
            )}
            <Button
              variant="secondary"
              onClick={() => {
                setCursors([undefined]);
                setReload(value => value + 1);
              }}
              disabled={loading}
            >
              Refresh timeline
            </Button>
          </div>
          {loading && (
            <div className="flex gap-2 items-center">
              <Spinner size="sm" /> Loading events…
            </div>
          )}
          {failed && (
            <div role="alert" className="text-amber-200">
              Timeline could not be refreshed. {data ? 'Visible events are stale.' : 'History is unavailable.'}{' '}
              <Button variant="secondary" onClick={() => setReload(value => value + 1)}>
                Retry timeline
              </Button>
            </div>
          )}
          {data && (
            <>
              <p className="text-xs text-slate-400">{data.coverage}</p>
              {!loading && data.items.length === 0 && (
                <p className="text-sm text-slate-300">No recorded events match these filters.</p>
              )}
              <ol className="space-y-3">
                {data.items.map(entry => (
                  <li
                    id={`event-${entry.id}`}
                    key={entry.id}
                    className="border-l-2 border-slate-600 pl-3 text-sm scroll-mt-20"
                  >
                    <div className="flex flex-wrap justify-between gap-1">
                      <strong>{entry.title}</strong>
                      <time dateTime={entry.occurred_at} className="text-slate-400">
                        {formatCentralDateTime(entry.occurred_at)} CT
                      </time>
                    </div>
                    {entry.detail && <p className="text-slate-300 whitespace-pre-wrap break-words">{entry.detail}</p>}
                    <div className="flex flex-wrap gap-x-2 text-xs mt-1">
                      {entry.actor_id && entry.actor_name ? (
                        <button
                          className="text-fd-link underline"
                          onClick={() => setActor({ id: entry.actor_id!, name: entry.actor_name! })}
                        >
                          {entry.actor_name}
                        </button>
                      ) : (
                        <span className="text-slate-400">Actor unavailable</span>
                      )}
                      <span className="text-slate-400">
                        {entry.evidence === 'audit'
                          ? 'Audit evidence'
                          : entry.evidence === 'telemetry'
                            ? 'Supplemental event (best effort)'
                            : 'Business record'}{' '}
                        · {entry.id}
                      </span>
                      <Link className="text-fd-link underline" to={entry.source_url}>
                        {entry.source_label}
                      </Link>
                    </div>
                  </li>
                ))}
              </ol>
            </>
          )}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="secondary"
              disabled={loading || cursors.length === 1}
              onClick={() => setCursors(value => value.slice(0, -1))}
            >
              Newer events
            </Button>
            <span className="text-xs text-slate-400">Page {cursors.length}</span>
            <Button
              variant="secondary"
              disabled={loading || failed || !data?.next_cursor}
              onClick={() => {
                if (data?.next_cursor) setCursors(value => [...value, data.next_cursor!]);
              }}
            >
              Older events
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
