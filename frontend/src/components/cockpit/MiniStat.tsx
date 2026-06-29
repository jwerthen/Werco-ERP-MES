import React from 'react';
import { Link } from 'react-router-dom';

export interface MiniStatProps {
  icon: React.ElementType;
  /** Tailwind bg class for the small icon chip, e.g. "bg-fd-green/15". */
  iconBg: string;
  /** Tailwind text-color class for the icon, e.g. "text-fd-green". */
  iconColor: string;
  label: string;
  value: number | string;
  valueColor?: string;
  subtitle?: string;
  /** Navigate on click. Takes precedence over onClick. */
  href?: string;
  /** Act as a filter/toggle button (used when there's no href). */
  onClick?: () => void;
  /** Highlight the tile when its filter/segment is active. */
  active?: boolean;
}

/**
 * Compact KPI tile — small inline icon + uppercase label + tabular value, in the
 * instrument-panel aesthetic (sharp corners, hairline border, tight padding).
 * This is the shared replacement for the bulky big-stat-icon KPI cards across
 * the app; it was extracted from the Dashboard cockpit so every page's KPI strip
 * is consistent.
 *
 * Renders as a <Link> when `href` is set, a <button> when `onClick` is set
 * (clickable filter tiles), otherwise a static tile.
 */
export function MiniStat({
  icon: Icon,
  iconBg,
  iconColor,
  label,
  value,
  valueColor,
  subtitle,
  href,
  onClick,
  active,
}: MiniStatProps) {
  const content = (
    <div
      className={`card card-compact !p-2.5 flex flex-col gap-1 min-w-0 h-full transition-colors hover:border-fd-line-bright ${
        active ? '!border-fd-blue' : ''
      }`}
    >
      <div className="flex items-center gap-1.5">
        <span className={`flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-sm ${iconBg}`}>
          <Icon className={`h-3.5 w-3.5 ${iconColor}`} />
        </span>
        <p className="stat-label !text-[10px] uppercase tracking-wide truncate">{label}</p>
      </div>
      <p className={`stat-value !text-xl ${valueColor || ''}`}>{value}</p>
      {subtitle && <p className="text-[10px] text-slate-500 leading-tight truncate">{subtitle}</p>}
    </div>
  );

  if (href) {
    return (
      <Link to={href} className="block h-full">
        {content}
      </Link>
    );
  }

  if (onClick) {
    return (
      <button type="button" onClick={onClick} aria-pressed={active} className="block h-full w-full text-left">
        {content}
      </button>
    );
  }

  return content;
}

/**
 * Responsive container for a row of MiniStats. Defaults to the Dashboard's
 * 2 / 3 / 5-up wrapping grid; pass `className` to override the column count.
 */
export function MiniStatStrip({ children, className }: { children: React.ReactNode; className?: string }) {
  return <div className={className || 'grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2'}>{children}</div>;
}
