import React from 'react';

interface ConfidenceBadgeProps {
  score?: number;
}

const styleForScore = (score: number) => {
  if (score >= 0.75) return 'border-emerald-500/40 bg-emerald-500/10 text-emerald-200';
  if (score >= 0.5) return 'border-cyan-500/40 bg-cyan-500/10 text-cyan-200';
  return 'border-amber-500/40 bg-amber-500/10 text-amber-200';
};

export function ConfidenceBadge({ score = 0 }: ConfidenceBadgeProps) {
  const safeScore = Math.max(0, Math.min(score, 1));
  const percent = Math.round(safeScore * 100);

  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${styleForScore(safeScore)}`}>
      {percent}% confidence
    </span>
  );
}
