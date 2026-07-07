/**
 * EmptyState Component
 *
 * A centered, muted, hairline-bordered placeholder for "no data" sections.
 * Instrument-panel aesthetic: sharp corners, steel-gray text, optional icon
 * and a primary-styled call-to-action.
 *
 * Usage:
 *   <EmptyState
 *     icon={InboxIcon}
 *     title="No work orders"
 *     description="Released work orders will appear here."
 *     action={{ label: 'New Work Order', onClick: handleCreate }}
 *   />
 */

import React from 'react';

interface EmptyStateAction {
  label: string;
  onClick: () => void;
}

interface EmptyStateProps {
  /** Optional heroicon (or any icon component accepting className) rendered above the title. */
  icon?: React.ComponentType<{ className?: string }>;
  /** Primary headline — what's empty. */
  title: string;
  /** Optional supporting copy explaining the empty state or how to fill it. */
  description?: string;
  /** Either a simple { label, onClick } CTA (rendered as a primary button) or arbitrary node. */
  action?: EmptyStateAction | React.ReactNode;
  className?: string;
}

function isActionConfig(action: EmptyStateProps['action']): action is EmptyStateAction {
  return (
    typeof action === 'object' &&
    action !== null &&
    'label' in action &&
    'onClick' in action &&
    typeof (action as EmptyStateAction).onClick === 'function'
  );
}

export const EmptyState: React.FC<EmptyStateProps> = ({
  icon: Icon,
  title,
  description,
  action,
  className = '',
}) => {
  return (
    <div
      className={`flex flex-col items-center justify-center text-center px-6 py-12 border border-dashed border-[#243042] rounded-sm bg-[#10151e]/40 ${className}`}
      data-testid="empty-state"
    >
      {Icon && (
        <Icon className="h-10 w-10 text-slate-600 mb-3" aria-hidden="true" />
      )}
      <p className="text-sm font-semibold text-slate-300">{title}</p>
      {/* slate-400 (not -500): 12px copy on the dark well needs >=4.5:1 (WCAG AA) — 6.7:1 worst case */}
      {description && (
        <p className="mt-1 text-xs text-slate-400 max-w-sm">{description}</p>
      )}
      {action && (
        <div className="mt-4">
          {isActionConfig(action) ? (
            <button type="button" onClick={action.onClick} className="btn-primary">
              {action.label}
            </button>
          ) : (
            action
          )}
        </div>
      )}
    </div>
  );
};

export default EmptyState;
