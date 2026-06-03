import React from 'react';
import { ArrowPathIcon, SparklesIcon } from '@heroicons/react/24/outline';
import { AIRecommendation } from '../../types/aiLearning';
import { AISuggestionCard } from './AISuggestionCard';

interface CopilotPanelProps {
  title?: string;
  subtitle?: string;
  recommendations: AIRecommendation[];
  loading?: boolean;
  disabled?: boolean;
  emptyMessage?: string;
  onRefresh?: () => void | Promise<void>;
  onAccept?: (recommendation: AIRecommendation) => void | Promise<void>;
  onDismiss?: (recommendation: AIRecommendation) => void | Promise<void>;
  onFeedback?: (recommendation: AIRecommendation, feedback: string) => void | Promise<void>;
}

export function CopilotPanel({
  title = 'AI Copilot',
  subtitle,
  recommendations,
  loading = false,
  disabled = false,
  emptyMessage = 'No AI recommendations right now.',
  onRefresh,
  onAccept,
  onDismiss,
  onFeedback,
}: CopilotPanelProps) {
  return (
    <section className="space-y-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <SparklesIcon className="h-5 w-5 text-cyan-300" />
            <h2 className="text-lg font-semibold text-white">{title}</h2>
          </div>
          {subtitle && <p className="mt-1 text-sm text-slate-400">{subtitle}</p>}
        </div>
        {onRefresh && (
          <button
            type="button"
            onClick={onRefresh}
            disabled={loading}
            className="inline-flex items-center justify-center rounded-lg border border-slate-700 px-3 py-2 text-sm font-medium text-slate-300 hover:border-cyan-500/60 hover:text-cyan-200 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ArrowPathIcon className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            Refresh
          </button>
        )}
      </div>
      {loading ? (
        <div className="rounded-lg border border-slate-700 bg-[#151b28] p-6 text-sm text-slate-300">
          Loading AI recommendations...
        </div>
      ) : recommendations.length ? (
        <div className="space-y-3">
          {recommendations.map((recommendation) => (
            <AISuggestionCard
              key={recommendation.id}
              recommendation={recommendation}
              disabled={disabled}
              onAccept={onAccept}
              onDismiss={onDismiss}
              onFeedback={onFeedback}
            />
          ))}
        </div>
      ) : (
        <div className="rounded-lg border border-slate-700 bg-[#151b28] p-6 text-sm text-slate-400">
          {emptyMessage}
        </div>
      )}
    </section>
  );
}
