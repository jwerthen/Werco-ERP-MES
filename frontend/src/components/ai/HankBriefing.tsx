import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import api from '../../services/api';
import type { HankBriefing as Briefing } from '../../types/hank';
import { getHankSessionScope } from './hankSession';
import { formatCentralDateTime } from '../../utils/centralTime';

export function HankBriefing({ onNavigate }: { onNavigate: () => void }) {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState('');
  const [attempt, setAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    const scope = getHankSessionScope();
    let active = true;
    setLoading(true);
    setError('');
    api
      .getHankBriefing(controller.signal)
      .then(result => {
        if (active && scope === getHankSessionScope()) setBriefing(result);
      })
      .catch(() => {
        if (active) setError('Your shift briefing could not be loaded. Try again.');
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt]);

  return (
    <section aria-label="My shift briefing" className="space-y-4" aria-busy={loading}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="font-semibold text-fd-ink">{briefing?.headline || 'Your shift briefing'}</h3>
          <p className="mt-1 text-xs text-fd-mute">Live priorities for your role and assigned work.</p>
        </div>
        <button type="button" onClick={() => setAttempt(value => value + 1)} disabled={loading} className="btn text-xs">
          Refresh
        </button>
      </div>
      {loading && (
        <p role="status" className="text-sm text-fd-mute">
          Checking the shop…
        </p>
      )}
      {error && (
        <p role="alert" className="text-sm text-fd-red">
          {error}
        </p>
      )}
      {briefing && (
        <>
          <div className="text-sm text-fd-body">
            <p>{briefing.summary}</p>
            <p className="mt-1 text-xs text-fd-mute">Checked {formatCentralDateTime(briefing.checked_at)}</p>
            {error && <p className="mt-1 text-xs text-fd-amber">Showing the last successful briefing.</p>}
          </div>
          {briefing.sections.map(section => (
            <section key={section.key} className="space-y-2" aria-label={section.title}>
              <div className="flex justify-between gap-2 text-sm font-semibold text-fd-ink">
                <h4>{section.title}</h4>
                <span>
                  {section.total}
                  {section.truncated ? '+' : ''}
                </span>
              </div>
              <p className="text-xs text-fd-mute">{section.description}</p>
              {section.items.length === 0 && (
                <p className="text-xs text-fd-mute">Nothing needing attention in this check.</p>
              )}
              {section.items.map(item => (
                <article key={item.key} className="p-3 rounded-[3px] border border-slate-700 space-y-1.5">
                  <div className="flex gap-2 items-start justify-between">
                    <Link onClick={onNavigate} to={item.href} className="text-sm text-fd-blue hover:underline">
                      {item.title}
                    </Link>
                    {item.is_mine && (
                      <span className="text-[10px] uppercase text-fd-amber shrink-0">
                        {item.source_kind === 'active_work' ? 'Clocked in' : 'Assigned to you'}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-fd-body">{item.detail}</p>
                  <p className="text-xs text-fd-mute">Next: {item.suggested_action}</p>
                  {item.owner_name && !item.is_mine && <p className="text-xs text-fd-mute">Owner: {item.owner_name}</p>}
                </article>
              ))}
              {section.truncated && (
                <p className="text-xs text-fd-amber">
                  Showing the first {section.items.length}.{' '}
                  <Link
                    to={
                      section.key === 'shipping'
                        ? '/shipping'
                        : section.key === 'my_work'
                          ? '/work-orders'
                          : '/action-inbox'
                    }
                    onClick={onNavigate}
                    className="underline"
                  >
                    {section.key === 'shipping'
                      ? 'Review Shipping'
                      : section.key === 'my_work'
                        ? 'Review work orders'
                        : 'Review Action Inbox'}
                  </Link>
                </p>
              )}
            </section>
          ))}
          {!!briefing.coverage_notes.length && (
            <div className="text-xs text-fd-mute space-y-1">
              {briefing.coverage_notes.map(note => (
                <p key={note}>{note}</p>
              ))}
            </div>
          )}
          <Link to="/action-inbox" onClick={onNavigate} className="inline-block text-sm text-fd-blue hover:underline">
            Open Action Inbox
          </Link>
        </>
      )}
    </section>
  );
}
