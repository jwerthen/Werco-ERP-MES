import React, { useId } from 'react';
import { XMarkIcon } from '@heroicons/react/24/outline';
import api from '../../services/api';
import { Modal } from '../ui/Modal';
import { targetText } from '../../utils/maintenance';
import type { MaintenanceTargetFields } from '../../types/maintenance';
import { MaintenanceLogTimeline } from './MaintenanceLogTimeline';
import { useMaintenanceLogs } from './useMaintenanceLogs';

/** Whose maintenance history to show: a work center or an equipment asset, with its display label. */
export interface MaintenanceHistoryTarget {
  kind: 'work_center' | 'asset';
  id: number;
  label: string;
}

/** The history target of a schedule, work order or asset row, or null when it names none. */
export const historyTargetFor = (row: Partial<MaintenanceTargetFields>): MaintenanceHistoryTarget | null => {
  if (row.target_type === 'asset') {
    return row.asset_id != null ? { kind: 'asset', id: row.asset_id, label: targetText(row) } : null;
  }
  return row.work_center_id != null ? { kind: 'work_center', id: row.work_center_id, label: targetText(row) } : null;
};

interface Props {
  target: MaintenanceHistoryTarget | null;
  onClose: () => void;
}

/**
 * A machine's or an equipment asset's maintenance history: every log entry filed against it,
 * newest first (`GET /maintenance/history/{work_center_id}` or
 * `GET /maintenance/assets/{asset_id}/history`). Read-only; entries are filed from a work order.
 */
export function MaintenanceHistoryModal({ target, onClose }: Props) {
  const titleId = useId();
  const key = target ? `${target.kind}:${target.id}` : null;
  const { logs, loading, error, reload } = useMaintenanceLogs(key, async () => {
    if (!target) return [];
    if (target.kind === 'asset') return (await api.getMaintenanceAssetHistory(target.id)).logs;
    return (await api.getMaintenanceHistory(target.id)).logs;
  });

  return (
    <Modal open={!!target} onClose={onClose} size="2xl" ariaLabelledBy={titleId}>
      {target && (
        <div className="space-y-3">
          <div className="flex items-start justify-between gap-3 border-b border-slate-700 pb-3">
            <div className="min-w-0">
              <h2 id={titleId} className="text-lg font-semibold text-white">
                Maintenance history: {target.label}
              </h2>
              <p className="text-xs uppercase tracking-wide text-slate-400">
                {target.kind === 'asset' ? 'Equipment asset' : 'Work center'}
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close dialog"
              className="text-slate-400 hover:text-white"
            >
              <XMarkIcon className="h-5 w-5" aria-hidden="true" />
            </button>
          </div>
          <MaintenanceLogTimeline
            logs={logs}
            loading={loading}
            error={error}
            onRetry={reload}
            label={`Maintenance history for ${target.label}`}
            emptyDescription="Entries filed against its maintenance work orders, and their starts and completions, appear here."
          />
        </div>
      )}
    </Modal>
  );
}

export default MaintenanceHistoryModal;
