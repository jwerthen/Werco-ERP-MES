/**
 * DispatchNoticeBanner — the floor's "the planner changed this machine's order"
 * strip (Dispatch Board v2, SPEC §7). One shared component for every floor
 * surface: OperatorKiosk, CrewStationKiosk (variant `kiosk`), ShopFloorSimple
 * and ShopFloor (variant `desktop`). Which notices to show, and the
 * acknowledgement, live in `hooks/useDispatchNotice.ts`.
 *
 * Two strengths, decided by whether the queue's "run next" job changed:
 * - it CHANGED → amber `role="alert"` strip leading with what to run:
 *   "RUN NEXT: WO-1234 · Unit 7 · Op 20 Bend — moved up by Jon W. 10:34 AM";
 * - it did NOT → a calmer `role="status"` strip:
 *   "Queue order updated by Jon W. 10:34 AM — your next job is unchanged".
 *
 * What it must never do (each is a real floor failure mode):
 * - navigate, or take the operator anywhere — it is information only;
 * - reset an idle timer itself — only a real tap on "Got it" does, through the
 *   window-level activity listener every tap already reaches;
 * - replace or hide the offline / idle-countdown / unsaved-pieces strips —
 *   callers stack it BELOW them;
 * - be a 3-second toast — it stays until "Got it" or until the server stops
 *   reporting the notice (12-hour window);
 * - drop keyboard focus to <body> — "Got it" removes the very button that was
 *   pressed, so focus moves to the next strip's "Got it", or, when none is left,
 *   to a visually-hidden anchor that stays mounted where the strips were (it
 *   takes no layout space, so the screen does not shift). Focus only — never a
 *   synthetic pointer event, which would reset the kiosk's idle timer.
 *
 * Text comes only from the frozen notice payload (office-authored identity and
 * the planner's "First L."), so a crew station shows nothing the station token
 * was not already given.
 */

import React, { useLayoutEffect, useRef, useState } from 'react';
import { ArrowUpIcon, QueueListIcon } from '@heroicons/react/24/outline';
import type { DispatchNotice, DispatchNoticeNextUp } from '../../types';
import { Button } from '../ui';
import { formatCentralTime } from '../../utils/centralTime';
import { formatOperationLabel, hasOperationNumber } from '../../utils/operationLabel';
import type { ActiveDispatchNotice } from '../../hooks/useDispatchNotice';

export type DispatchNoticeBannerVariant = 'kiosk' | 'desktop';

interface DispatchNoticeBannerProps {
  notices: ReadonlyArray<ActiveDispatchNotice>;
  onAcknowledge: (notice: DispatchNotice) => void;
  /** `kiosk` = the darker kiosk palette + gloved-tap sizing (render inside `.fd-scope-kiosk`). */
  variant?: DispatchNoticeBannerVariant;
  /**
   * Name the machine on every strip. Always true on screens that can carry
   * notices for several work centers (ShopFloor / ShopFloorSimple); the kiosks
   * name it only when more than one strip is up.
   */
  showWorkCenter?: boolean;
}

const noticeKey = (notice: DispatchNotice) => `${notice.work_center_id}:${notice.id}`;

/** "WO-1234 · Unit 7 · Op 20 Bend" — unit omitted when the work order has none. */
export function describeNextUp(next: DispatchNoticeNextUp): string {
  const parts: string[] = [next.work_order_number || 'Work order'];
  const unit = typeof next.unit_number === 'string' ? next.unit_number.trim() : '';
  if (unit) parts.push(`Unit ${unit}`);
  const name = (next.operation_name || '').trim();
  const operation = hasOperationNumber(next.operation_number)
    ? `${formatOperationLabel(next.operation_number)}${name ? ` ${name}` : ''}`
    : name || 'Operation';
  parts.push(operation);
  return parts.join(' · ');
}

function actorAndTime(notice: DispatchNotice): string {
  const actor = (notice.actor_display || '').trim() || 'the planner';
  const at = formatCentralTime(notice.occurred_at, {}).trim();
  return at && at !== '-' ? `${actor} ${at}` : actor;
}

/**
 * The strip's sentence, split so the job identity can be set large. Joined, the
 * three parts read exactly as the spec's wording.
 */
export function dispatchNoticeText(entry: Pick<ActiveDispatchNotice, 'notice' | 'nextUpChanged'>): {
  lead: string;
  subject: string;
  tail: string;
} {
  const { notice, nextUpChanged } = entry;
  const by = actorAndTime(notice);
  if (!nextUpChanged) {
    return { lead: '', subject: `Queue order updated by ${by}`, tail: ' — your next job is unchanged' };
  }
  const next = notice.next_up_after;
  if (!next) {
    // The job that was next left and nothing READY took its place.
    return { lead: 'RUN NEXT: ', subject: 'nothing queued here now', tail: ` — changed by ${by}` };
  }
  const verb = notice.change_kind === 'moved_in' ? 'moved to this machine' : 'moved up';
  return { lead: 'RUN NEXT: ', subject: describeNextUp(next), tail: ` — ${verb} by ${by}` };
}

export default function DispatchNoticeBanner({
  notices,
  onAcknowledge,
  variant = 'desktop',
  showWorkCenter = false,
}: DispatchNoticeBannerProps) {
  const stripsRef = useRef<HTMLDivElement | null>(null);
  const anchorRef = useRef<HTMLSpanElement | null>(null);
  /** The strip whose "Got it" was just pressed, and where it sat. */
  const pendingFocusRef = useRef<{ key: string; index: number } | null>(null);
  /**
   * The focus target for "nothing left to acknowledge". Mounted by the first
   * "Got it" and kept for the life of the screen, so a screen that never showed
   * a notice renders exactly what it did before (nothing).
   */
  const [anchorMounted, setAnchorMounted] = useState(false);

  // Every commit: once the acknowledged strip is gone, put focus where the
  // keyboard user can carry on — the next strip's "Got it" (the one that slid
  // into its place, else the one above), or the anchor. Only focus the removed
  // button dropped is recovered; focus the user has since moved is left alone.
  useLayoutEffect(() => {
    const pending = pendingFocusRef.current;
    if (!pending) return;
    pendingFocusRef.current = null;
    if (notices.some(entry => noticeKey(entry.notice) === pending.key)) return;
    const active = document.activeElement;
    if (active && active !== document.body) return;
    const buttons = stripsRef.current?.querySelectorAll<HTMLButtonElement>('[data-notice-ack]');
    const next = buttons && buttons.length > 0 ? buttons[Math.min(pending.index, buttons.length - 1)] : null;
    (next ?? anchorRef.current)?.focus();
  });

  const acknowledge = (notice: DispatchNotice, index: number) => {
    pendingFocusRef.current = { key: noticeKey(notice), index };
    setAnchorMounted(true);
    onAcknowledge(notice);
  };

  const kiosk = variant === 'kiosk';
  const nameMachine = showWorkCenter || notices.length > 1;

  return (
    <>
      {anchorMounted && (
        // Visually hidden and absolutely positioned: no box, no gap, no shift.
        <span ref={anchorRef} tabIndex={-1} className="sr-only" data-testid="dispatch-notice-focus-anchor">
          {notices.length === 0
            ? 'Queue changes acknowledged'
            : `${notices.length} queue change${notices.length === 1 ? '' : 's'} to acknowledge`}
        </span>
      )}
      {notices.length > 0 && (
        <div ref={stripsRef} data-testid="dispatch-notice-banner" className={kiosk ? undefined : 'space-y-2'}>
          {notices.map((entry, index) => {
            const { notice, nextUpChanged, observedChanges } = entry;
            const { lead, subject, tail } = dispatchNoticeText(entry);
            const machine = notice.work_center_code || `Work center #${notice.work_center_id}`;
            const urgent = nextUpChanged;
            const Icon = urgent ? ArrowUpIcon : QueueListIcon;

            const tone = urgent
              ? 'border-fd-amber bg-fd-amber/15 text-fd-amber'
              : kiosk
                ? 'border-fd-line-bright bg-fd-panel text-fd-body'
                : 'border-fd-line bg-fd-panel text-fd-body';
            const frame = kiosk
              ? `border-b px-5 py-4 min-[1100px]:px-6 ${tone}`
              : `rounded-sm border px-3 py-2.5 ${tone}`;

            return (
              <div
                key={noticeKey(notice)}
                data-testid="dispatch-notice"
                data-notice-id={notice.id}
                data-kind={urgent ? 'next-up' : 'order'}
                className={`flex flex-wrap items-center gap-x-4 gap-y-2 ${frame}`}
              >
                <Icon aria-hidden="true" className={`shrink-0 ${kiosk ? 'h-7 w-7' : 'h-5 w-5'}`} />
                <div role={urgent ? 'alert' : 'status'} className="min-w-0 flex-1">
                  <p
                    className={
                      kiosk ? (urgent ? 'text-xl font-semibold leading-snug' : 'text-lg leading-snug') : 'text-sm'
                    }
                  >
                    {nameMachine && (
                      <span
                        className={`mr-2 inline-block rounded-[3px] border border-current px-1.5 py-0.5 align-middle font-mono font-bold uppercase tracking-[0.08em] ${
                          kiosk ? 'text-xs' : 'text-[10px]'
                        }`}
                      >
                        {machine}
                      </span>
                    )}
                    {nameMachine && ' '}
                    {lead && <span className="font-mono font-bold uppercase tracking-[0.06em]">{lead}</span>}
                    <span className={urgent ? 'font-mono font-bold text-fd-ink' : 'font-semibold'}>{subject}</span>
                    <span className={urgent ? '' : 'text-fd-mute'}>{tail}</span>
                  </p>
                  {observedChanges > 1 && (
                    <p
                      data-testid="dispatch-notice-change-count"
                      className={`mt-0.5 font-mono uppercase tracking-[0.08em] ${kiosk ? 'text-sm' : 'text-[11px]'} ${
                        urgent ? 'text-fd-amber/90' : 'text-fd-mute'
                      }`}
                    >
                      Changed {observedChanges} times since you last checked
                    </p>
                  )}
                </div>
                <Button
                  variant="secondary"
                  size={kiosk ? 'md' : 'sm'}
                  aria-label={`Got it — ${machine} queue change`}
                  data-notice-ack=""
                  onClick={() => acknowledge(notice, index)}
                  className={kiosk ? 'min-h-16 min-w-36 shrink-0 px-6 text-lg' : 'min-h-11 shrink-0 px-4'}
                >
                  Got it
                </Button>
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
