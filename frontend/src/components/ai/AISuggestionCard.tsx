import React from 'react';
import { Link } from 'react-router-dom';
import { ArrowTopRightOnSquareIcon, SparklesIcon } from '@heroicons/react/24/outline';
import { AIRecommendation, recommendationAcceptMode } from '../../types/aiLearning';
import { ConfidenceBadge } from './ConfidenceBadge';
import { FeedbackButtons } from './FeedbackButtons';
import { WhyThisSuggestion } from './WhyThisSuggestion';

interface AISuggestionCardProps {
  recommendation: AIRecommendation;
  disabled?: boolean;
  /** Accept & apply is in flight for this card (server-gated, non-optimistic). */
  accepting?: boolean;
  /** 1-based position in a ranked surface (e.g. the Action Inbox "Top 3 today" hero). */
  rank?: number;
  /**
   * Render an "Open record" link to the server-computed `target_href` (default true).
   * The context strip passes false: it already sits on the record.
   */
  showRecordLink?: boolean;
  onAccept?: (recommendation: AIRecommendation) => void | Promise<void>;
  onDismiss?: (recommendation: AIRecommendation) => void | Promise<void>;
  onFeedback?: (recommendation: AIRecommendation, feedback: string) => void | Promise<void>;
  onSnooze?: (recommendation: AIRecommendation, days: number) => void | Promise<void>;
}

const priorityStyles: Record<string, string> = {
  high: 'border-red-500/40 bg-red-500/10 text-red-200',
  medium: 'border-amber-500/40 bg-amber-500/10 text-amber-200',
  low: 'border-blue-500/40 bg-blue-500/10 text-blue-200',
  info: 'border-cyan-500/40 bg-cyan-500/10 text-cyan-200',
};

export function AISuggestionCard({
  recommendation,
  disabled = false,
  accepting = false,
  rank,
  showRecordLink = true,
  onAccept,
  onDismiss,
  onFeedback,
  onSnooze,
}: AISuggestionCardProps) {
  const applyAction = recommendation.apply_action ?? null;
  const recordHref = showRecordLink ? recommendation.target_href : null;
  // A blocked card has no Accept & apply, so the record is where the work happens:
  // make the way there prominent so the card never dead-ends.
  const blocked = Boolean(applyAction?.blocked_reason);

  return (
    <article className="rounded-lg border border-slate-700 bg-fd-panel p-4">
      <div className="flex items-start gap-3">
        <div className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 p-2 text-cyan-200">
          <SparklesIcon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            {rank !== undefined && (
              <span className="rounded-full border border-cyan-500/40 bg-cyan-500/10 px-2 py-0.5 font-mono text-xs text-cyan-200">
                #{rank}
              </span>
            )}
            <h3 className="font-semibold text-white">{recommendation.title}</h3>
            <ConfidenceBadge score={recommendation.confidence_score} />
            <span
              className={`rounded-full border px-2 py-0.5 text-xs font-medium ${priorityStyles[recommendation.priority] || priorityStyles.info}`}
            >
              {recommendation.priority}
            </span>
          </div>
          <p className="mt-2 text-sm text-slate-300">{recommendation.summary}</p>
          {recommendation.source_module && (
            <p className="mt-2 text-xs uppercase tracking-wide text-slate-500">{recommendation.source_module}</p>
          )}
          <WhyThisSuggestion
            rationale={recommendation.rationale}
            evidence={recommendation.evidence || []}
            impact={recommendation.impact || {}}
          />
          {recommendation.status === 'pending' && (
            <FeedbackButtons
              disabled={disabled}
              accepting={accepting}
              acceptMode={recommendationAcceptMode(recommendation)}
              applySummary={applyAction?.summary}
              applyBlockedReason={applyAction?.blocked_reason}
              applyWarnings={applyAction?.warnings || []}
              onAccept={onAccept ? () => onAccept(recommendation) : undefined}
              onDismiss={onDismiss ? () => onDismiss(recommendation) : undefined}
              onFeedback={onFeedback ? feedback => onFeedback(recommendation, feedback) : undefined}
              onSnooze={onSnooze ? days => onSnooze(recommendation, days) : undefined}
            />
          )}
          {recordHref && (
            <Link
              to={recordHref}
              className={
                blocked
                  ? 'btn-secondary btn-sm mt-3'
                  : 'mt-3 inline-flex items-center gap-1 text-xs text-cyan-300 hover:text-cyan-100'
              }
            >
              <ArrowTopRightOnSquareIcon className="h-4 w-4" aria-hidden="true" />
              Open record
            </Link>
          )}
        </div>
      </div>
    </article>
  );
}
