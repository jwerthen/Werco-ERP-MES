import React from 'react';
import { SparklesIcon } from '@heroicons/react/24/outline';
import { AIRecommendation } from '../../types/aiLearning';
import { ConfidenceBadge } from './ConfidenceBadge';
import { FeedbackButtons } from './FeedbackButtons';
import { WhyThisSuggestion } from './WhyThisSuggestion';

interface AISuggestionCardProps {
  recommendation: AIRecommendation;
  disabled?: boolean;
  onAccept?: (recommendation: AIRecommendation) => void | Promise<void>;
  onDismiss?: (recommendation: AIRecommendation) => void | Promise<void>;
  onFeedback?: (recommendation: AIRecommendation, feedback: string) => void | Promise<void>;
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
  onAccept,
  onDismiss,
  onFeedback,
}: AISuggestionCardProps) {
  return (
    <article className="rounded-lg border border-slate-700 bg-[#151b28] p-4">
      <div className="flex items-start gap-3">
        <div className="rounded-lg border border-cyan-500/30 bg-cyan-500/10 p-2 text-cyan-200">
          <SparklesIcon className="h-5 w-5" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="font-semibold text-white">{recommendation.title}</h3>
            <ConfidenceBadge score={recommendation.confidence_score} />
            <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${priorityStyles[recommendation.priority] || priorityStyles.info}`}>
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
              onAccept={onAccept ? () => onAccept(recommendation) : undefined}
              onDismiss={onDismiss ? () => onDismiss(recommendation) : undefined}
              onFeedback={onFeedback ? (feedback) => onFeedback(recommendation, feedback) : undefined}
            />
          )}
        </div>
      </div>
    </article>
  );
}
