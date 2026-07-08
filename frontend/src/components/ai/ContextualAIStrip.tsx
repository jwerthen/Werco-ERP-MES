import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowPathIcon, SparklesIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { AIRecommendation, recommendationIsApplyable } from '../../types/aiLearning';
import { AISuggestionCard } from './AISuggestionCard';

interface ContextualAIStripProps {
  entityType: string;
  entityId: number;
  limit?: number;
  title?: string;
  className?: string;
}

/**
 * Ambient AI strip — loads pending recommendations for a target entity
 * without requiring the user to open Copilot or Action Inbox (Phase 3).
 */
export function ContextualAIStrip({
  entityType,
  entityId,
  limit = 3,
  title = 'AI recommendations',
  className = '',
}: ContextualAIStripProps) {
  const [items, setItems] = useState<AIRecommendation[]>([]);
  const [loading, setLoading] = useState(true);
  const [actioningId, setActioningId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!entityId) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.getAIRecommendations({
        status: 'pending',
        target_entity_type: entityType,
        target_entity_id: entityId,
        limit,
      });
      setItems(data);
    } catch {
      setError('AI suggestions unavailable');
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [entityType, entityId, limit]);

  useEffect(() => {
    load();
  }, [load]);

  const removeLocal = (id: number) => setItems((cur) => cur.filter((r) => r.id !== id));

  const onAccept = async (rec: AIRecommendation) => {
    setActioningId(rec.id);
    setToast(null);
    try {
      const apply = recommendationIsApplyable(rec);
      const result = await api.acceptAIRecommendation(
        rec.id,
        apply ? 'Accepted & applied from context strip' : 'Accepted from context strip',
        apply
      );
      removeLocal(rec.id);
      if (result.apply_error) {
        setToast(`Accepted, but apply failed: ${result.apply_error}`);
      } else if (result.applied) {
        setToast('Accepted and applied');
      }
    } catch {
      setToast('Accept failed');
    } finally {
      setActioningId(null);
    }
  };

  const onDismiss = async (rec: AIRecommendation) => {
    setActioningId(rec.id);
    try {
      await api.dismissAIRecommendation(rec.id, 'Dismissed from context strip');
      removeLocal(rec.id);
    } finally {
      setActioningId(null);
    }
  };

  const onSnooze = async (rec: AIRecommendation, days: number) => {
    setActioningId(rec.id);
    try {
      await api.snoozeAIRecommendation(rec.id, days, 'Snoozed from context strip');
      removeLocal(rec.id);
    } finally {
      setActioningId(null);
    }
  };

  const onFeedback = async (rec: AIRecommendation, feedback: string) => {
    setActioningId(rec.id);
    try {
      await api.sendAIRecommendationFeedback(rec.id, { feedback });
    } finally {
      setActioningId(null);
    }
  };

  if (loading && items.length === 0) {
    return (
      <div className={`rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-sm text-slate-500 ${className}`}>
        <span className="inline-flex items-center gap-2">
          <ArrowPathIcon className="h-4 w-4 animate-spin" />
          Loading AI context…
        </span>
      </div>
    );
  }

  if (error || items.length === 0) {
    return null;
  }

  return (
    <section
      aria-label={title}
      className={`rounded-lg border border-cyan-500/25 bg-cyan-500/5 p-3 ${className}`}
    >
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <SparklesIcon className="h-5 w-5 text-cyan-300" />
          <h2 className="text-sm font-semibold text-white">{title}</h2>
          <span className="rounded-full bg-cyan-500/15 px-2 py-0.5 text-xs text-cyan-200">{items.length}</span>
        </div>
        <Link to="/action-inbox" className="text-xs text-cyan-300 hover:text-cyan-100">
          Open Action Inbox
        </Link>
      </div>
      {toast && (
        <p className="mb-2 rounded border border-slate-700 bg-slate-950/60 px-2 py-1 text-xs text-slate-300">{toast}</p>
      )}
      <div className="grid grid-cols-1 gap-2 xl:grid-cols-3">
        {items.map((rec) => (
          <AISuggestionCard
            key={rec.id}
            recommendation={rec}
            disabled={actioningId === rec.id}
            onAccept={onAccept}
            onDismiss={onDismiss}
            onFeedback={onFeedback}
            onSnooze={onSnooze}
          />
        ))}
      </div>
    </section>
  );
}
