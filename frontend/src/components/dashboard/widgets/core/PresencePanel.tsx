import React, { useEffect, useMemo, useRef } from 'react';
import { UsersIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState } from '../../../ui';
import type { SignedInUserStatus } from '../../../../types';
import { formatCentralTime } from '../../../../utils/centralTime';
import { useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { useCoreDashboard } from './CoreDashboardContext';
import { getRoleBadgeClass, sortActiveAssignments } from './shared';

/**
 * Signed In Right Now — presence. On-the-job users are chips that jump to (and cycle
 * through) their Live Shop Activity rows; idle users are rows under `#idle-operators`,
 * the target of the "Signed In, Idle" tile and the Live Activity empty state.
 */
export function PresencePanel() {
  const { data, shopError } = useCoreDashboard();
  const { isVisible } = useDashboardWidgetEnv();
  const liveActivityVisible = isVisible('live_activity');
  const activeAssignments = useMemo(() => sortActiveAssignments(data?.active_assignments), [data?.active_assignments]);
  const signedInUsers = data?.signed_in_users || [];
  const idleSignedInUsers = signedInUsers.filter(user => !user.has_active_job);
  const onJobSignedInUsers = signedInUsers.filter(user => user.has_active_job);
  // Crew-station world: one operator can hold SEVERAL open entries at once, so
  // keep every assignment (in the sorted render order), not just the first.
  const timeEntriesByUserId = useMemo(() => {
    const map = new Map<number, number[]>();
    activeAssignments.forEach(assignment => {
      const existing = map.get(assignment.user.id);
      if (existing) {
        existing.push(assignment.time_entry_id);
      } else {
        map.set(assignment.user.id, [assignment.time_entry_id]);
      }
    });
    return map;
  }, [activeAssignments]);
  // Per-user cursor so repeated chip clicks cycle through that operator's
  // assignments (pure navigation state — a ref avoids pointless re-renders).
  // Reset only when the underlying assignment SET changes, not on every poll
  // (the memo above gets a fresh identity each refresh even when nothing moved).
  const chipCycleRef = useRef(new Map<number, number>());
  const assignmentFingerprint = useMemo(
    () =>
      Array.from(timeEntriesByUserId.entries())
        .map(([userId, ids]) => `${userId}:${ids.join('.')}`)
        .join('|'),
    [timeEntriesByUserId]
  );
  useEffect(() => {
    chipCycleRef.current.clear();
  }, [assignmentFingerprint]);

  const chipClassName =
    'inline-flex items-center gap-1 rounded-sm border border-fd-line bg-fd-panel px-2 py-0.5 text-xs font-medium text-slate-200';

  return (
    <CockpitPanel
      title="Signed In Right Now"
      subtitle={shopError ? 'Last verified user presence' : 'Connected user presence'}
      footer={`${signedInUsers.length} signed in`}
      headerExtra={
        <div className="flex items-center gap-1.5 text-[10px] font-semibold tabular-nums">
          <span className="rounded-sm bg-slate-800/60 px-1.5 py-0.5 text-slate-300">
            {data?.summary.signed_in_users || 0} in
          </span>
          <span className="rounded-sm bg-fd-green/15 px-1.5 py-0.5 text-emerald-400">
            {data?.summary.checked_in_users || 0} job
          </span>
          <span className="rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-amber-400">
            {idleSignedInUsers.length} idle
          </span>
        </div>
      }
    >
      {signedInUsers.length ? (
        <div className="space-y-3">
          {onJobSignedInUsers.length > 0 && (
            <div>
              <p className="mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                On a job ({onJobSignedInUsers.length})
              </p>
              <div className="flex flex-wrap gap-1">
                {onJobSignedInUsers.map(user => {
                  const timeEntryIds = timeEntriesByUserId.get(user.id) || [];
                  const title = `${user.name} · ${user.role.replace('_', ' ')}${user.active_work_orders.length ? ` · ${user.active_work_orders.join(', ')}` : ''}${timeEntryIds.length > 1 ? ` · ${timeEntryIds.length} jobs` : ''}`;
                  const body = (
                    <>
                      <span className="h-1.5 w-1.5 rounded-full bg-fd-green" />
                      {user.name}
                    </>
                  );
                  if (!liveActivityVisible) {
                    return (
                      <span key={user.id} title={title} className={chipClassName}>
                        {body}
                      </span>
                    );
                  }
                  return (
                    <button
                      key={user.id}
                      type="button"
                      onClick={() => {
                        if (timeEntryIds.length === 0) return;
                        // Cycle: each click jumps to the operator's NEXT assignment.
                        const cursor = chipCycleRef.current.get(user.id) ?? 0;
                        const timeEntryId = timeEntryIds[cursor % timeEntryIds.length];
                        chipCycleRef.current.set(user.id, (cursor + 1) % timeEntryIds.length);
                        document
                          .getElementById(`assign-${timeEntryId}`)
                          ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                      }}
                      title={title}
                      className={`${chipClassName} transition-colors hover:border-fd-line-bright hover:text-white`}
                    >
                      {body}
                    </button>
                  );
                })}
              </div>
            </div>
          )}

          {idleSignedInUsers.length > 0 && (
            <div
              id="idle-operators"
              tabIndex={-1}
              aria-label="Idle operators"
              className="divide-y divide-fd-line border-t border-fd-line scroll-mt-4"
            >
              {idleSignedInUsers.map(user => (
                <IdleUserRow key={user.id} user={user} />
              ))}
            </div>
          )}

          {idleSignedInUsers.length > 0 && (
            <div className="rounded-sm border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-300">
              <p className="font-semibold">Signed in but not on a job</p>
              <p className="mt-0.5">{idleSignedInUsers.map(user => user.name).join(', ')}</p>
            </div>
          )}
        </div>
      ) : (
        <EmptyState
          icon={UsersIcon}
          title="No active signed-in sessions"
          description="Users currently signed into the ERP will appear here."
        />
      )}
    </CockpitPanel>
  );
}

function IdleUserRow({ user }: { user: SignedInUserStatus }) {
  return (
    <div className="flex items-center gap-2 py-1.5">
      <div className="flex min-w-0 flex-1 items-center gap-1.5">
        <span className="truncate font-medium text-slate-200">{user.name}</span>
        <span className="flex-shrink-0 text-[10px] text-slate-400 tabular-nums">{user.employee_id}</span>
        <span
          className={`flex-shrink-0 rounded-sm px-1 py-0.5 text-[10px] font-medium ${getRoleBadgeClass(user.role)}`}
        >
          {user.role.replace('_', ' ')}
        </span>
      </div>
      <span className="flex-shrink-0 rounded-sm bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-semibold text-amber-400">
        Signed In
      </span>
      <span className="w-16 flex-shrink-0 text-right text-[11px] text-slate-400 tabular-nums">
        {user.connected_since ? formatCentralTime(user.connected_since) : 'Unknown'}
      </span>
    </div>
  );
}
