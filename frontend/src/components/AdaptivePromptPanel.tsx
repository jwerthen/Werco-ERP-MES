import React, { useEffect, useMemo, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ArrowRightIcon, LightBulbIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { AdaptivePrompt } from '../types/aiForward';

const VISIT_KEY = 'wercoAdaptiveVisits';
const DISMISSED_KEY = 'wercoAdaptiveDismissed';
const FAILED_SEARCH_KEY = 'wercoAdaptiveFailedSearches';

const readJson = <T,>(key: string, fallback: T): T => {
  try {
    return JSON.parse(localStorage.getItem(key) || '') as T;
  } catch {
    return fallback;
  }
};

const writeJson = (key: string, value: unknown) => {
  localStorage.setItem(key, JSON.stringify(value));
};

const promptForPath = (path: string, visitCount: number): AdaptivePrompt | null => {
  if (visitCount < 4) return null;
  if (path.startsWith('/work-orders/')) {
    return {
      id: 'work-order-blockers',
      title: 'Capture the blocker',
      detail: 'Report missing material, tooling, quality holds, or engineering questions from this work order.',
      href: path,
      action_label: 'Open blockers',
    };
  }
  if (path.startsWith('/shop-floor')) {
    return {
      id: 'shop-floor-material',
      title: 'Mark why work is stuck',
      detail: 'Use the material blocker action when an operation cannot start because stock is not at the work center.',
      href: '/action-inbox',
      action_label: 'Review actions',
    };
  }
  if (path.startsWith('/scheduling')) {
    return {
      id: 'schedule-recommendations',
      title: 'Let AI surface schedule friction',
      detail: 'Open recommendations to review blockers, hot jobs, and capacity risks before resequencing work.',
      href: '/action-inbox?filter=ai',
      action_label: 'Review AI actions',
    };
  }
  return null;
};

export default function AdaptivePromptPanel() {
  const location = useLocation();
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState<AdaptivePrompt | null>(null);

  const pathKey = useMemo(() => location.pathname, [location.pathname]);

  useEffect(() => {
    const dismissed = readJson<Record<string, boolean>>(DISMISSED_KEY, {});
    const visits = readJson<Record<string, number>>(VISIT_KEY, {});
    const nextVisits = { ...visits, [pathKey]: (visits[pathKey] || 0) + 1 };
    writeJson(VISIT_KEY, nextVisits);

    const nextPrompt = promptForPath(pathKey, nextVisits[pathKey]);
    if (nextPrompt && !dismissed[nextPrompt.id]) {
      setPrompt(nextPrompt);
    }
  }, [pathKey]);

  useEffect(() => {
    const onFriction = (event: Event) => {
      const detail = (event as CustomEvent<{ type?: string; query?: string }>).detail;
      if (detail?.type !== 'failed_search') return;
      const failed = readJson<Record<string, number>>(FAILED_SEARCH_KEY, {});
      const count = (failed[detail.query || 'unknown'] || 0) + 1;
      writeJson(FAILED_SEARCH_KEY, { ...failed, [detail.query || 'unknown']: count });
      const promptId = 'natural-language-search';
      const dismissed = readJson<Record<string, boolean>>(DISMISSED_KEY, {});
      if (count >= 2 && !dismissed[promptId]) {
        setPrompt({
          id: promptId,
          title: 'Ask for the work, not the record',
          detail: 'Try an operational phrase such as late laser jobs waiting on material.',
          href: '/work-orders',
          action_label: 'Open work orders',
        });
      }
    };
    window.addEventListener('werco:friction', onFriction);
    return () => window.removeEventListener('werco:friction', onFriction);
  }, []);

  if (!prompt) return null;

  const dismiss = () => {
    const dismissed = readJson<Record<string, boolean>>(DISMISSED_KEY, {});
    writeJson(DISMISSED_KEY, { ...dismissed, [prompt.id]: true });
    setPrompt(null);
  };

  return (
    <div className="fixed bottom-20 right-4 z-40 w-[min(360px,calc(100vw-2rem))] rounded-lg border border-cyan-500/30 bg-fd-panel shadow-2xl">
      <div className="flex items-start gap-3 p-4">
        <div className="mt-0.5 rounded-lg bg-cyan-500/20 p-2 text-cyan-300">
          <LightBulbIcon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-3">
            <h2 className="text-sm font-semibold text-white">{prompt.title}</h2>
            <button onClick={dismiss} className="rounded p-1 text-slate-400 hover:bg-slate-700 hover:text-white">
              <XMarkIcon className="h-4 w-4" />
            </button>
          </div>
          <p className="mt-1 text-sm leading-5 text-slate-300">{prompt.detail}</p>
          {prompt.href && (
            <button
              onClick={() => {
                navigate(prompt.href!);
                dismiss();
              }}
              className="mt-3 inline-flex items-center text-sm font-medium text-cyan-300 hover:text-cyan-200"
            >
              {prompt.action_label || 'Open'}
              <ArrowRightIcon className="ml-1 h-4 w-4" />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
