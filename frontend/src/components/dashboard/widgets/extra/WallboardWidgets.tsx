/**
 * Panels fed by ONE shared `GET /shop-floor/wallboard` request (the `wallboard` source):
 * Blocked Jobs, Shipping Outlook, Today's Output and Late Jobs. The wallboard payload is
 * bounded and read-only, and carries the definitions the TV board uses:
 *  - blocked = an OPEN or ACKNOWLEDGED blocker on a live (non-deleted, non-terminal) work
 *    order; `blocked_total` counts blockers, oldest first;
 *  - late / shipping use the PROMISE date (`must_ship_by`, else `due_date`) against Central
 *    today — not the due-date "overdue" of the Overdue tile and list.
 *
 * Wallboard rows carry a work-order number but no id, so row links land on the work-order
 * list searched for that number. The server sends no customer names to roles that may not
 * see them, and these panels render none.
 */
import React from 'react';
import { CheckCircleIcon, ClockIcon, ShieldCheckIcon, TruckIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState, StatusBadge } from '../../../ui';
import { formatCentralDate } from '../../../../utils/centralTime';
import type { WallboardResponse } from '../../../../types/wallboard';
import { useDashboardSource } from '../../DashboardWidgetContext';
import {
  CountChip,
  PanelAction,
  PanelStat,
  SourceBody,
  WidgetLink,
  WO_LINK_CLASS,
  WO_TEXT_CLASS,
  formatDuration,
  humanize,
  panelSubtitle,
  workOrderSearchHref,
} from './shared';

const LOADING_TEXT = 'Loading shop status…';
const ERROR_TITLE = 'Shop status is unavailable';

function formatQuantity(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/\.?0+$/, '');
}

function plural(count: number, singular: string, pluralForm = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

/** Blocked Jobs — `blocked_wos` (oldest first, capped) + the uncapped `blocked_total`. */
export function BlockedJobsWidget() {
  const state = useDashboardSource<WallboardResponse>('wallboard');
  const data = state.forbidden ? null : state.data;
  const total = data ? (data.blocked_total ?? data.blocked_wos.length) : null;

  return (
    <CockpitPanel
      title="Blocked Jobs"
      subtitle={panelSubtitle([state], 'Open or acknowledged blockers · oldest first')}
      footer={total != null ? plural(total, 'open blocker') : undefined}
      headerExtra={
        total != null ? (
          // `blocked_total` counts BLOCKERS (one work order can carry several), and the 24-row
          // cap means distinct work orders cannot be counted here — so the chip says blockers.
          <CountChip variant={total > 0 ? 'red' : 'slate'} title={plural(total, 'open blocker')}>
            {plural(total, 'blocker')}
          </CountChip>
        ) : undefined
      }
    >
      <SourceBody state={state} loadingText={LOADING_TEXT} errorTitle={ERROR_TITLE}>
        {wallboard =>
          wallboard.blocked_wos.length === 0 ? (
            <EmptyState
              icon={ShieldCheckIcon}
              title="No blocked work orders"
              description="Open blockers on live work orders will appear here."
            />
          ) : (
            <ul className="divide-y divide-fd-line">
              {wallboard.blocked_wos.map((row, index) => {
                const ageMinutes = row.age_hours * 60;
                const ageClass =
                  row.age_hours >= 24 ? 'text-red-400' : row.age_hours >= 4 ? 'text-amber-400' : 'text-slate-300';
                return (
                  <li key={`${row.wo_number}-${index}`} className="flex items-center gap-2 py-1.5 first:pt-0">
                    <WidgetLink
                      href={workOrderSearchHref(row.wo_number)}
                      className={WO_LINK_CLASS}
                      textClassName={WO_TEXT_CLASS}
                    >
                      {row.wo_number}
                    </WidgetLink>
                    <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">{humanize(row.category)}</span>
                    <span
                      className={`flex-shrink-0 text-[11px] tabular-nums ${ageClass}`}
                      title="Time since the blocker was reported"
                    >
                      {formatDuration(ageMinutes)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )
        }
      </SourceBody>
    </CockpitPanel>
  );
}

/** Shipping Outlook — the wallboard `ship` block (promise-date based). */
export function ShippingOutlookWidget() {
  const state = useDashboardSource<WallboardResponse>('wallboard');

  return (
    <CockpitPanel
      title="Shipping Outlook"
      subtitle={panelSubtitle([state], 'By promise date · today and the next 7 days')}
      headerExtra={
        state.forbidden ? undefined : (
          <PanelAction href="/warehouse?tab=shipping" label="Shipping" ariaLabel="Open shipping" />
        )
      }
    >
      <SourceBody state={state} loadingText={LOADING_TEXT} errorTitle={ERROR_TITLE}>
        {wallboard => {
          const ship = wallboard.ship;
          if (!ship) {
            return <p className="py-3 text-xs text-slate-400">Shipping figures are unavailable right now.</p>;
          }
          if (ship.due_today === 0 && ship.due_this_week === 0 && !ship.next_due_date) {
            return (
              <EmptyState
                icon={TruckIcon}
                title="Nothing promised to ship this week"
                description="Work orders promised today through the next 7 days appear here."
              />
            );
          }
          return (
            <div className="space-y-2">
              <div className="grid grid-cols-3 gap-1.5">
                <PanelStat
                  label="Due today"
                  value={ship.due_today}
                  valueClassName={ship.due_today > 0 ? 'text-amber-400' : 'text-white'}
                />
                <PanelStat
                  label="Shipped today"
                  value={`${ship.shipped_today}/${ship.due_today}`}
                  valueClassName={
                    ship.due_today > 0 && ship.shipped_today >= ship.due_today ? 'text-emerald-400' : 'text-white'
                  }
                />
                <PanelStat label="Due this week" value={ship.due_this_week} hint="Not yet shipped" />
              </div>
              {ship.due_today_rows.length > 0 && (
                <div>
                  <p className="mb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                    Most still to ship today
                  </p>
                  <ul className="divide-y divide-fd-line">
                    {ship.due_today_rows.map(row => (
                      <li key={row.wo_number} className="flex items-center gap-2 py-1.5 first:pt-0">
                        <WidgetLink
                          href={workOrderSearchHref(row.wo_number)}
                          className={WO_LINK_CLASS}
                          textClassName={WO_TEXT_CLASS}
                        >
                          {row.wo_number}
                        </WidgetLink>
                        <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">
                          {row.part_number || '—'}
                        </span>
                        <span className="flex-shrink-0 text-[11px] font-semibold tabular-nums text-white">
                          {formatQuantity(row.qty_remaining)} left
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {ship.due_today === 0 && ship.next_due_date && (
                <p className="text-xs text-slate-400">
                  Next due {formatCentralDate(ship.next_due_date, { year: undefined })} ·{' '}
                  {plural(ship.next_due_count, 'work order')}
                </p>
              )}
            </div>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}

/** Today's Output — the wallboard `today` block (Central day). */
export function TodaysOutputWidget() {
  const state = useDashboardSource<WallboardResponse>('wallboard');

  return (
    <CockpitPanel title="Today's Output" subtitle={panelSubtitle([state], 'Completed so far today (Central)')}>
      <SourceBody state={state} loadingText={LOADING_TEXT} errorTitle={ERROR_TITLE}>
        {wallboard => {
          const today = wallboard.today;
          if (!today) {
            return (
              <EmptyState
                icon={ClockIcon}
                title="Today's totals are unavailable"
                description="The shop could not total today's work. They return on the next refresh."
              />
            );
          }
          const nothingYet =
            today.ops_completed === 0 &&
            today.pieces_completed === 0 &&
            today.wos_completed === 0 &&
            today.operators_on_clock === 0 &&
            !today.hours_logged &&
            today.receipts === 0 &&
            today.scrap_events === 0;
          if (nothingYet) {
            return (
              <EmptyState
                icon={ClockIcon}
                title="No activity recorded yet today"
                description="Completions, labor and receipts for the Central day appear here as they happen."
              />
            );
          }
          return (
            <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-3">
              <PanelStat label="Operations done" value={today.ops_completed} />
              <PanelStat label="Pieces done" value={formatQuantity(today.pieces_completed)} />
              <PanelStat label="WOs completed" value={today.wos_completed} />
              <PanelStat label="On the clock" value={today.operators_on_clock} hint="Operators" />
              <PanelStat
                label="Hours logged"
                value={today.hours_logged == null ? '—' : `${today.hours_logged.toFixed(1)}h`}
              />
              <PanelStat label="Receipts" value={today.receipts} />
              <PanelStat
                label="Scrap events"
                value={today.scrap_events}
                valueClassName={today.scrap_events > 0 ? 'text-amber-400' : 'text-white'}
              />
            </div>
          );
        }}
      </SourceBody>
    </CockpitPanel>
  );
}

/** Late Jobs — `late_wos` (worst first, capped at 12) + the uncapped `late_total`, by PROMISE date. */
export function LateJobsWidget() {
  const state = useDashboardSource<WallboardResponse>('wallboard');
  const data = state.forbidden ? null : state.data;
  const total = data ? (data.late_total ?? data.late_wos.length) : null;

  return (
    <CockpitPanel
      title="Late Jobs"
      subtitle={panelSubtitle([state], 'By promise date (must-ship-by, else due date)')}
      footer={total != null ? plural(total, 'late work order') : undefined}
      headerExtra={
        total != null ? (
          <CountChip variant={total > 0 ? 'red' : 'slate'} title={plural(total, 'late work order')}>
            {total} late
          </CountChip>
        ) : undefined
      }
    >
      <SourceBody state={state} loadingText={LOADING_TEXT} errorTitle={ERROR_TITLE}>
        {wallboard =>
          wallboard.late_wos.length === 0 ? (
            <EmptyState
              icon={CheckCircleIcon}
              title="No late jobs"
              description="Nothing open is past its promise date."
            />
          ) : (
            <ul className="divide-y divide-fd-line">
              {wallboard.late_wos.map(row => (
                <li key={row.wo_number} className="flex items-center gap-2 py-1.5 first:pt-0">
                  <WidgetLink
                    href={workOrderSearchHref(row.wo_number)}
                    className={WO_LINK_CLASS}
                    textClassName={WO_TEXT_CLASS}
                  >
                    {row.wo_number}
                  </WidgetLink>
                  <span className="min-w-0 flex-1 truncate text-[11px] text-slate-400">{row.part_number || '—'}</span>
                  <span
                    className="w-12 flex-shrink-0 text-right text-[11px] tabular-nums text-slate-300"
                    title="Promise date"
                  >
                    {row.due_date ? formatCentralDate(row.due_date, { year: undefined }) : '—'}
                  </span>
                  <span className="w-14 flex-shrink-0 text-right text-[11px] font-semibold tabular-nums text-red-400">
                    {row.days_late}d late
                  </span>
                  {row.status && <StatusBadge status={row.status} className="flex-shrink-0 !text-[10px]" />}
                </li>
              ))}
            </ul>
          )
        }
      </SourceBody>
    </CockpitPanel>
  );
}
