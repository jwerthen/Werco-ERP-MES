import React from 'react';
import { Link } from 'react-router-dom';
import { ClipboardDocumentListIcon } from '@heroicons/react/24/outline';
import { CheckCircleIcon as CheckCircleSolid } from '@heroicons/react/24/solid';
import { EmptyState } from '../../../ui';
import { formatCentralDate, formatCentralTime } from '../../../../utils/centralTime';
import { useDashboardWidgetEnv } from '../../DashboardWidgetContext';
import { useCoreDashboard } from './CoreDashboardContext';

/**
 * Recent Completions — its own card markup and body clamp (not a CockpitPanel), as it
 * was before customization; it now sits in a full-width grid cell. `#recent-completions`
 * is the target of the "Operations Completed Today" tile.
 */
export function RecentCompletionsPanel() {
  const { data } = useCoreDashboard();
  const { canOpen } = useDashboardWidgetEnv();
  return (
    <div className="card card-compact flex flex-col min-w-0">
      <div className="card-header !pb-2 !mb-2">
        <div>
          <h2 id="recent-completions" className="card-title">
            Recent Completions
          </h2>
          <p className="card-subtitle">Latest completed operations</p>
        </div>
        {canOpen('/work-orders') && (
          <Link to="/work-orders" className="btn-ghost btn-sm">
            View All
          </Link>
        )}
      </div>

      {data?.recent_completions.length ? (
        <div className="lg:max-h-[clamp(200px,28vh,360px)] lg:overflow-y-auto divide-y divide-fd-line">
          {data.recent_completions.map((completion, index) => (
            <div key={index} className="flex items-center gap-3 py-2 first:pt-0">
              <CheckCircleSolid className="h-4 w-4 flex-shrink-0 text-fd-green" />
              <div className="min-w-0 flex-1">
                <span className="font-semibold text-white">{completion.work_order_number || '-'}</span>
                <span className="ml-2 truncate text-sm text-slate-400">
                  {completion.operation_name || 'Operation completed'}
                  {completion.operator_name ? ` · ${completion.operator_name}` : ''}
                </span>
              </div>
              <div className="flex flex-shrink-0 items-center gap-3 text-right text-xs tabular-nums">
                <span className="w-16 text-slate-300">
                  {completion.completed_at ? formatCentralDate(completion.completed_at, { year: undefined }) : '-'}
                </span>
                <span className="w-16 text-slate-400">
                  {completion.completed_at ? formatCentralTime(completion.completed_at) : '-'}
                </span>
                <span className="w-16 font-semibold text-white">{completion.quantity_complete} units</span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={ClipboardDocumentListIcon}
          title="No recent completions"
          description="Completed operations will appear here as work finishes."
        />
      )}
    </div>
  );
}
