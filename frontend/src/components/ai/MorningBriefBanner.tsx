import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { SparklesIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { AIRecommendation } from '../../types/aiLearning';

/**
 * Dashboard banner for today's morning_brief recommendation (Phase 3).
 * Hidden when no pending brief exists.
 */
export function MorningBriefBanner() {
  const [brief, setBrief] = useState<AIRecommendation | null>(null);

  const load = useCallback(async () => {
    try {
      const rows = await api.getAIRecommendations({ status: 'pending', limit: 20 });
      const found = rows.find((r) => r.recommendation_type === 'morning_brief') || null;
      setBrief(found);
    } catch {
      setBrief(null);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  if (!brief) return null;

  const firstLine = (brief.summary || '').split('\n')[0];

  return (
    <div className="rounded-lg border border-cyan-500/30 bg-gradient-to-r from-cyan-500/10 to-slate-950/40 px-4 py-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex min-w-0 items-start gap-3">
          <span className="mt-0.5 rounded-md border border-cyan-500/40 bg-cyan-500/15 p-1.5 text-cyan-200">
            <SparklesIcon className="h-5 w-5" />
          </span>
          <div className="min-w-0">
            <p className="text-sm font-semibold text-white">{brief.title}</p>
            <p className="mt-0.5 text-sm text-slate-300 line-clamp-2">{firstLine}</p>
          </div>
        </div>
        <Link
          to="/action-inbox"
          className="shrink-0 rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 py-2 text-sm font-medium text-cyan-100 hover:bg-cyan-500/20"
        >
          Review actions
        </Link>
      </div>
    </div>
  );
}
