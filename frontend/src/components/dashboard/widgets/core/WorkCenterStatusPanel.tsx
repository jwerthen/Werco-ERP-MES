import React from 'react';
import { Link } from 'react-router-dom';
import { WrenchScrewdriverIcon } from '@heroicons/react/24/outline';
import { CockpitPanel } from '../../../cockpit';
import { EmptyState, statusVariant } from '../../../ui';
import type { WorkCenterStatus } from '../../../../types';
import { useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { useCoreDashboard } from './CoreDashboardContext';
import { variantDotColor, workCenterTypeColors } from './shared';

/** Work Center Status — station status; "N ppl" jumps to the station's Live Shop Activity group. */
export function WorkCenterStatusPanel() {
  const { data, shopError } = useCoreDashboard();
  const { canOpen, isVisible } = useDashboardWidgetEnv();
  const liveActivityVisible = isVisible('live_activity');
  return (
    <CockpitPanel
      title="Work Center Status"
      subtitle={shopError ? 'Last verified station status' : 'Station status · 30-second refresh'}
      footer={`${data?.work_centers.length || 0} work center${(data?.work_centers.length || 0) === 1 ? '' : 's'}`}
      headerExtra={
        canOpen('/work-centers') ? (
          <Link to="/work-centers" className="btn-ghost btn-sm">
            View All
          </Link>
        ) : undefined
      }
    >
      {data?.work_centers.length ? (
        <div className="divide-y divide-fd-line">
          {data.work_centers.map((wc: WorkCenterStatus) => (
            <WorkCenterRow key={wc.id} wc={wc} linkToLiveActivity={liveActivityVisible} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={WrenchScrewdriverIcon}
          title="No work centers configured"
          description="Configure work centers to monitor station status here."
        />
      )}
    </CockpitPanel>
  );
}

function WorkCenterRow({ wc, linkToLiveActivity }: { wc: WorkCenterStatus; linkToLiveActivity: boolean }) {
  const statusDot = variantDotColor[statusVariant(wc.status)];
  const typeColor = workCenterTypeColors[wc.type] || 'bg-slate-600';

  const scrollToLiveGroup = () => {
    document.getElementById(`wc-live-${wc.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="flex items-center gap-2 py-2 first:pt-0">
      <span className={`h-6 w-1 flex-shrink-0 rounded-sm ${typeColor}`} title={wc.type.replace('_', ' ')} />
      <div
        className={`h-2.5 w-2.5 flex-shrink-0 rounded-full ${statusDot} animate-pulse`}
        title={wc.status.replace('_', ' ')}
        aria-label={`Status: ${wc.status.replace('_', ' ')}`}
      />
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium text-white">{wc.name}</p>
        <p className="truncate text-[11px] capitalize text-slate-400">{wc.type.replace('_', ' ')}</p>
      </div>
      <div className="flex flex-shrink-0 items-center gap-1 text-[10px] tabular-nums">
        <span className="rounded-sm bg-slate-800/60 px-1.5 py-0.5 text-slate-300" title="Active operations">
          {wc.active_operations} act
        </span>
        <span className="rounded-sm bg-slate-800/60 px-1.5 py-0.5 text-slate-300" title="Queued operations">
          {wc.queued_operations} qd
        </span>
        {linkToLiveActivity ? (
          <button
            type="button"
            onClick={scrollToLiveGroup}
            className="rounded-sm bg-fd-blue/15 px-1.5 py-0.5 font-medium text-fd-blue transition-colors hover:bg-fd-blue/25"
            title="Jump to live activity for this work center"
          >
            {wc.active_people_count} ppl
          </button>
        ) : (
          <span
            className="rounded-sm bg-fd-blue/15 px-1.5 py-0.5 font-medium text-fd-blue"
            title="People at this work center"
          >
            {wc.active_people_count} ppl
          </span>
        )}
      </div>
    </div>
  );
}
