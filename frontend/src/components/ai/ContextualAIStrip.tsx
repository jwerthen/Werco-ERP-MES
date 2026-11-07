import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowPathIcon, SparklesIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { AIRecommendation, AIRecommendationApplyResult, recommendationAcceptMode } from '../../types/aiLearning';
import { useToast } from '../ui/Toast';
import { apiErrorDetail, apiErrorStatus } from '../../utils/apiError';
import { useInFlightIds } from '../../hooks/useInFlightIds';
import { AISuggestionCard } from './AISuggestionCard';

interface ContextualAIStripProps {
  entityType: string;
  entityId: number;
  limit?: number;
  title?: string;
  className?: string;
  /**
   * Called after an Accept & apply succeeds, so the host page reloads the record it
   * shows (the apply changed it server-side; a stale copy would 409 on the next edit).
   */
  onApplied?: (result: AIRecommendationApplyResult) => void;
}

/**
 * Ambient AI strip — loads pending recommendations for a target entity
 * without requiring the user to open Copilot or Action Inbox (Phase 3).
 *
 * Every action here is NON-optimistic: the card leaves only once the server has
 * answered. Accept & apply in particular is server-gated (role, credential, AI egress,
 * live plan + fingerprint), so a refusal keeps the card and toasts the server's detail
 * verbatim; a 409 also reloads, because the card it refers to has changed.
 */
export function ContextualAIStrip({
  entityType,
  entityId,
  limit = 3,
  title = 'AI recommendations',
  className = '',
  onApplied,
}: ContextualAIStripProps) {
  const { showToast } = useToast();
  const [items, setItems] = useState<AIRecommendation[]>([]);
  const [loading, setLoading] = useState(true);
  // Per-card in-flight state, so one card's request never re-enables another's buttons.
  const actioning = useInFlightIds();
  const applying = useInFlightIds();
  const [error, setError] = useState<string | null>(null);

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

  const removeLocal = (id: number) => setItems(cur => cur.filter(r => r.id !== id));

  const reportFailure = (err: unknown, fallback: string) => {
    showToast('error', apiErrorDetail(err, fallback));
    if (apiErrorStatus(err) === 409) {
      void load();
    }
  };

  const onAccept = async (rec: AIRecommendation) => {
    const mode = recommendationAcceptMode(rec);
    if (mode === 'none') return;
    const isApply = mode === 'apply' && Boolean(rec.apply_action);
    // Synchronous double-click guard for Accept & apply on THIS card.
    if (isApply && !applying.start(rec.id)) return;
    const owns = actioning.start(rec.id);
    try {
      if (isApply && rec.apply_action) {
        const result = await api.applyAIRecommendation(
          rec.id,
          rec.apply_action.fingerprint,
          'Accepted & applied from context strip'
        );
        removeLocal(rec.id);
        const applied = result.apply_result?.summary;
        showToast('success', `Applied: ${typeof applied === 'string' ? applied : rec.apply_action.summary}`);
        onApplied?.(result);
        // Applying supersedes sibling cards on the same record and can change other
        // cards' plans, so re-read what is still open here.
        void load();
      } else {
        await api.acceptAIRecommendation(
          rec.id,
          mode === 'mark_handled' ? 'Marked handled from context strip' : 'Accepted from context strip'
        );
        removeLocal(rec.id);
      }
    } catch (err) {
      reportFailure(err, mode === 'apply' ? 'Accept & apply failed. Nothing was changed.' : 'Accept failed');
    } finally {
      if (owns) actioning.finish(rec.id);
      if (isApply) applying.finish(rec.id);
    }
  };

  const onDismiss = async (rec: AIRecommendation) => {
    const owns = actioning.start(rec.id);
    try {
      await api.dismissAIRecommendation(rec.id, 'Dismissed from context strip');
      removeLocal(rec.id);
    } catch (err) {
      reportFailure(err, 'Dismiss failed');
    } finally {
      if (owns) actioning.finish(rec.id);
    }
  };

  const onSnooze = async (rec: AIRecommendation, days: number) => {
    const owns = actioning.start(rec.id);
    try {
      await api.snoozeAIRecommendation(rec.id, days, 'Snoozed from context strip');
      removeLocal(rec.id);
    } catch (err) {
      reportFailure(err, 'Snooze failed');
    } finally {
      if (owns) actioning.finish(rec.id);
    }
  };

  const onFeedback = async (rec: AIRecommendation, feedback: string) => {
    const owns = actioning.start(rec.id);
    try {
      await api.sendAIRecommendationFeedback(rec.id, { feedback });
    } finally {
      if (owns) actioning.finish(rec.id);
    }
  };

  if (loading && items.length === 0) {
    return (
      <div
        className={`rounded-lg border border-slate-800 bg-slate-950/40 px-3 py-2 text-sm text-slate-500 ${className}`}
      >
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
    <section aria-label={title} className={`rounded-lg border border-cyan-500/25 bg-cyan-500/5 p-3 ${className}`}>
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
      <div className="grid grid-cols-1 gap-2 xl:grid-cols-3">
        {items.map(rec => (
          <AISuggestionCard
            key={rec.id}
            recommendation={rec}
            disabled={actioning.ids.has(rec.id) || applying.ids.has(rec.id)}
            accepting={applying.ids.has(rec.id)}
            showRecordLink={false}
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
