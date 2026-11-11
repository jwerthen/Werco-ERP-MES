/**
 * Shared building blocks for the opt-in dashboard panels. Every panel reads its data
 * through `useDashboardSource` and renders one of five states the same way:
 *
 *   forbidden  → "Not available for your role" (in-panel, never an error banner)
 *   data       → the panel body; with a refresh error the last verified data stays and the
 *                subtitle reads "Last verified · refresh failed"
 *   error, no data → ErrorState whose Retry asks the page for a FORCED refresh
 *   otherwise  → a role="status" loading line (a needed key with no result yet is loading)
 *
 * Links go through `WidgetLink`, which renders plain text when the viewer cannot open the
 * destination (the predicate PrivateRoute enforces), so a panel never offers a dead link.
 */
import React from 'react';
import { Link } from 'react-router-dom';
import { ErrorState } from '../../../ui';
import { variantClass, type StatusVariant } from '../../../../utils/statusColors';
import { useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import type { SourceState } from '../../widgetTypes';

export const FORBIDDEN_TEXT = 'Not available for your role';
export const STALE_TEXT = 'Last verified · refresh failed';

/** True when a refresh failed but the last verified data is still shown. */
export function isStale(state: SourceState<unknown>): boolean {
  return !state.forbidden && state.data != null && Boolean(state.error);
}

/** Subtitle helper: the panel's normal subtitle, or the stale note. */
export function panelSubtitle(states: SourceState<unknown>[], normal: string): string {
  return states.some(isStale) ? STALE_TEXT : normal;
}

/**
 * Two sources a panel needs together (e.g. inspection stats + queue). Forbidden if either
 * is; data only when both have data (stale if either refresh failed); with data missing,
 * an error only when a source has FAILED without data — a source still loading reads as
 * loading, so one fast failure never hides a sibling that is about to arrive.
 */
export function combineSources<A, B>(a: SourceState<A>, b: SourceState<B>): SourceState<[A, B]> {
  const data: [A, B] | null = a.data != null && b.data != null ? [a.data, b.data] : null;
  const aFailed = a.data == null && Boolean(a.error);
  const bFailed = b.data == null && Boolean(b.error);
  const error = data != null ? (a.error ?? b.error) : aFailed ? a.error : bFailed ? b.error : null;
  const updatedAt =
    a.updatedAt && b.updatedAt ? (a.updatedAt.getTime() <= b.updatedAt.getTime() ? a.updatedAt : b.updatedAt) : null;
  return {
    data,
    error,
    loading: data == null && !aFailed && !bFailed,
    forbidden: a.forbidden || b.forbidden,
    updatedAt,
  };
}

export function ForbiddenNotice() {
  return <p className="py-3 text-xs text-slate-400">{FORBIDDEN_TEXT}</p>;
}

interface SourceBodyProps<T> {
  state: SourceState<T>;
  /** e.g. "Loading overdue work orders…" */
  loadingText: string;
  /** ErrorState headline when there is no data to fall back to. */
  errorTitle: string;
  /**
   * The ErrorState's Retry. Defaults to the page's forced refresh; a panel whose source has
   * its own cache passes a handler that clears that cache first and then forces a refresh.
   */
  onRetry?: () => void;
  children: (data: T) => React.ReactNode;
}

export function SourceBody<T>({ state, loadingText, errorTitle, onRetry, children }: SourceBodyProps<T>) {
  const { refresh } = useDashboardWidgetEnv();
  if (state.forbidden) return <ForbiddenNotice />;
  if (state.data != null) return <>{children(state.data)}</>;
  if (state.error) {
    return <ErrorState title={errorTitle} message={state.error} onRetry={onRetry ?? (() => refresh(true))} />;
  }
  return (
    <p role="status" className="py-3 text-xs text-slate-400">
      {loadingText}
    </p>
  );
}

interface WidgetLinkProps {
  href: string;
  children: React.ReactNode;
  className?: string;
  /** Classes for the plain-text fallback (defaults to `className` without hover affordances). */
  textClassName?: string;
  title?: string;
  'aria-label'?: string;
}

/** A link when the viewer may open `href`; otherwise the same content as plain text. */
export function WidgetLink({ href, children, className, textClassName, title, ...rest }: WidgetLinkProps) {
  const { canOpen } = useDashboardWidgetEnv();
  if (!canOpen(href)) {
    return (
      <span className={textClassName ?? className} title={title}>
        {children}
      </span>
    );
  }
  return (
    <Link to={href} className={className} title={title} aria-label={rest['aria-label']}>
      {children}
    </Link>
  );
}

/** A header call-to-action ("View all"). Omitted entirely when the viewer cannot open it. */
export function PanelAction({ href, label, ariaLabel }: { href: string; label: string; ariaLabel?: string }) {
  const { canOpen } = useDashboardWidgetEnv();
  if (!canOpen(href)) return null;
  return (
    <Link to={href} className="btn-ghost btn-sm whitespace-nowrap" aria-label={ariaLabel}>
      {label}
    </Link>
  );
}

/** Work-order number link class (matches the cockpit's Live Shop Activity rows). */
export const WO_LINK_CLASS = 'text-xs font-semibold text-fd-link hover:text-sky-200';
export const WO_TEXT_CLASS = 'text-xs font-semibold text-slate-200';

/**
 * Wallboard rows carry a work-order NUMBER but no id, so they land on the work-order list
 * searched for that number. `cots=1` keeps purchased-part work orders in the search.
 */
export function workOrderSearchHref(woNumber: string): string {
  return `/work-orders?search=${encodeURIComponent(woNumber)}&cots=1`;
}

/** "material_missing" → "Material missing". */
export function humanize(value: string | null | undefined): string {
  if (!value) return '—';
  const words = value.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Compact elapsed duration: "45m", "3h 5m", "2d 4h". */
export function formatDuration(totalMinutes: number): string {
  if (!Number.isFinite(totalMinutes) || totalMinutes < 0) return '—';
  const minutes = Math.floor(totalMinutes);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** A compact header count chip ("12 open"). */
export function CountChip({
  children,
  variant = 'slate',
  title,
}: {
  children: React.ReactNode;
  variant?: StatusVariant;
  title?: string;
}) {
  return (
    <span
      className={`rounded-sm px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${variantClass[variant]}`}
      title={title}
    >
      {children}
    </span>
  );
}

/** One labelled number in a panel's stat grid. */
export function PanelStat({
  label,
  value,
  valueClassName = 'text-white',
  href,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  valueClassName?: string;
  href?: string;
  hint?: string;
}) {
  const body = (
    <>
      <span className="block text-[10px] font-medium uppercase tracking-wide text-slate-400">{label}</span>
      <span className={`block text-lg font-bold tabular-nums leading-tight ${valueClassName}`}>{value}</span>
      {hint && <span className="block truncate text-[10px] text-slate-400">{hint}</span>}
    </>
  );
  const cellClass = 'block min-w-0 rounded-sm border border-fd-line px-2 py-1.5';
  if (!href) return <div className={cellClass}>{body}</div>;
  return (
    <WidgetLink
      href={href}
      className={`${cellClass} transition-colors hover:border-fd-line-bright`}
      textClassName={cellClass}
    >
      {body}
    </WidgetLink>
  );
}
