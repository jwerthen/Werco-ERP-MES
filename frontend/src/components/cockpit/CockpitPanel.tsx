import React from 'react';

export interface CockpitPanelProps {
  title: string;
  subtitle?: string;
  /** Footer count; rendered as "<footer> total". */
  footer?: string;
  /** Right-aligned header content (counts, links, toggles). */
  headerExtra?: React.ReactNode;
  className?: string;
  /** Extra classes on the scrolling body (e.g. spacing or to opt out of caps). */
  bodyClassName?: string;
  children: React.ReactNode;
}

/**
 * Shared chrome for a cockpit panel: a `card-compact` column with a tight header,
 * a capped, internally-scrolling body (the cap applies only at lg+ so mobile
 * grows naturally and never traps a nested scroll), and an optional footer count.
 * Extracted from the Dashboard cockpit so analytics/ops pages can lay capped
 * panels side-by-side in a grid instead of stacking unbounded full-width sections.
 */
export function CockpitPanel({ title, subtitle, footer, headerExtra, className, bodyClassName, children }: CockpitPanelProps) {
  return (
    <div className={`card card-compact flex flex-col min-w-0 ${className || ''}`}>
      <div className="card-header !pb-2 !mb-2 gap-3">
        <div className="min-w-0">
          <h2 className="card-title">{title}</h2>
          {subtitle && <p className="card-subtitle truncate">{subtitle}</p>}
        </div>
        {headerExtra && <div className="flex-shrink-0">{headerExtra}</div>}
      </div>
      <div className={`flex-1 lg:max-h-[clamp(280px,38vh,440px)] lg:overflow-y-auto pr-1 ${bodyClassName || ''}`}>
        {children}
      </div>
      {footer && (
        <div className="mt-2 border-t border-fd-line pt-1.5 text-[10px] font-medium uppercase tracking-wide text-slate-500 tabular-nums">
          {footer} total
        </div>
      )}
    </div>
  );
}
