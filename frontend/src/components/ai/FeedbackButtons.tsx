import React, { useState } from 'react';
import { ChatBubbleLeftRightIcon, CheckIcon, ClockIcon, XMarkIcon } from '@heroicons/react/24/outline';

export const SNOOZE_CHOICES: Array<{ label: string; days: number }> = [
  { label: '1 day', days: 1 },
  { label: '3 days', days: 3 },
  { label: '1 week', days: 7 },
];

interface FeedbackButtonsProps {
  disabled?: boolean;
  onAccept?: () => void | Promise<void>;
  onDismiss?: () => void | Promise<void>;
  onFeedback?: (feedback: string) => void | Promise<void>;
  onSnooze?: (days: number) => void | Promise<void>;
}

export function FeedbackButtons({ disabled = false, onAccept, onDismiss, onFeedback, onSnooze }: FeedbackButtonsProps) {
  const [feedbackOpen, setFeedbackOpen] = useState(false);
  const [snoozeOpen, setSnoozeOpen] = useState(false);
  const [feedback, setFeedback] = useState('');

  const chooseSnooze = async (days: number) => {
    if (!onSnooze) return;
    await onSnooze(days);
    setSnoozeOpen(false);
  };

  const submitFeedback = async () => {
    const trimmed = feedback.trim();
    if (!trimmed || !onFeedback) return;
    await onFeedback(trimmed);
    setFeedback('');
    setFeedbackOpen(false);
  };

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {onAccept && (
          <button
            type="button"
            disabled={disabled}
            onClick={onAccept}
            className="inline-flex items-center gap-1 rounded-lg border border-emerald-500/40 bg-emerald-500/10 px-3 py-2 text-sm font-medium text-emerald-200 hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <CheckIcon className="h-4 w-4" />
            Accept
          </button>
        )}
        {onDismiss && (
          <button
            type="button"
            disabled={disabled}
            onClick={onDismiss}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-700 px-3 py-2 text-sm font-medium text-slate-300 hover:border-slate-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-60"
          >
            <XMarkIcon className="h-4 w-4" />
            Dismiss
          </button>
        )}
        {onSnooze && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => setSnoozeOpen((value) => !value)}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-700 px-3 py-2 text-sm font-medium text-slate-300 hover:border-amber-500/60 hover:text-amber-200 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ClockIcon className="h-4 w-4" />
            Snooze
          </button>
        )}
        {onFeedback && (
          <button
            type="button"
            disabled={disabled}
            onClick={() => setFeedbackOpen((value) => !value)}
            className="inline-flex items-center gap-1 rounded-lg border border-slate-700 px-3 py-2 text-sm font-medium text-slate-300 hover:border-cyan-500/60 hover:text-cyan-200 disabled:cursor-not-allowed disabled:opacity-60"
          >
            <ChatBubbleLeftRightIcon className="h-4 w-4" />
            Feedback
          </button>
        )}
      </div>
      {snoozeOpen && onSnooze && (
        <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Snooze for">
          <span className="text-xs uppercase tracking-wide text-slate-500">Snooze for</span>
          {SNOOZE_CHOICES.map((choice) => (
            <button
              key={choice.days}
              type="button"
              disabled={disabled}
              onClick={() => chooseSnooze(choice.days)}
              className="rounded-lg border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-amber-500/60 hover:text-amber-200 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {choice.label}
            </button>
          ))}
        </div>
      )}
      {feedbackOpen && (
        <div className="flex flex-col gap-2 sm:flex-row">
          <input
            value={feedback}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder="What should AI learn from this?"
            aria-label="What should AI learn from this?"
            className="min-w-0 flex-1 rounded-lg border border-slate-700 bg-slate-950/70 px-3 py-2 text-sm text-white placeholder:text-slate-500 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
          />
          <button
            type="button"
            disabled={disabled || !feedback.trim()}
            onClick={submitFeedback}
            className="rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 py-2 text-sm font-medium text-cyan-200 hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-60"
          >
            Send
          </button>
        </div>
      )}
    </div>
  );
}
