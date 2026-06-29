/**
 * ErrorState Component
 *
 * A compact, inline error block for section-level load failures (not full-page).
 * Accent-red iconography, a short message, and an optional Retry button that
 * re-runs the failed fetch. Instrument-panel aesthetic: sharp corners, hairline
 * border, dense layout.
 *
 * Usage:
 *   <ErrorState
 *     message="Could not load work orders."
 *     onRetry={loadWorkOrders}
 *   />
 */

import React from 'react';
import { ExclamationTriangleIcon } from '@heroicons/react/24/outline';

interface ErrorStateProps {
  /** Headline for the error block. */
  title?: string;
  /** Optional detail message under the title. */
  message?: string;
  /** When provided, renders a Retry button that calls this handler. */
  onRetry?: () => void;
  /** Label for the retry button. */
  retryLabel?: string;
  className?: string;
}

export const ErrorState: React.FC<ErrorStateProps> = ({
  title = "Couldn't load this",
  message,
  onRetry,
  retryLabel = 'Retry',
  className = '',
}) => {
  return (
    <div
      className={`flex items-start gap-3 px-4 py-3 border border-[#f04438]/40 bg-[#f04438]/5 rounded-sm ${className}`}
      role="alert"
      data-testid="error-state"
    >
      <ExclamationTriangleIcon
        className="h-5 w-5 text-[#f04438] shrink-0 mt-0.5"
        aria-hidden="true"
      />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-semibold text-slate-200">{title}</p>
        {message && <p className="mt-0.5 text-xs text-slate-400">{message}</p>}
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="mt-2 inline-flex items-center px-3 py-1.5 text-xs font-mono font-semibold uppercase tracking-wider text-[#f04438] border border-[#f04438]/50 rounded-sm hover:bg-[#f04438]/10 transition-colors duration-150"
          >
            {retryLabel}
          </button>
        )}
      </div>
    </div>
  );
};

export default ErrorState;
