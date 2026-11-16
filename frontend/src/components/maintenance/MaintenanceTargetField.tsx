import React from 'react';
import EntityPicker from '../operations/EntityPicker';
import { ComboBox } from '../ui/ComboBox';
import { FormField } from '../ui/FormField';
import type { MaintenanceAsset, MaintenanceTargetType } from '../../types/maintenance';

export interface MaintenanceTarget {
  targetType: MaintenanceTargetType;
  workCenterId: string;
  assetId: string;
}

export const BLANK_TARGET: MaintenanceTarget = { targetType: 'work_center', workCenterId: '', assetId: '' };

/** The exactly-one-target payload the server expects (`work_center_id` XOR `asset_id`). */
export const targetPayload = (target: MaintenanceTarget): { work_center_id?: number; asset_id?: number } =>
  target.targetType === 'asset'
    ? target.assetId
      ? { asset_id: Number(target.assetId) }
      : {}
    : target.workCenterId
      ? { work_center_id: Number(target.workCenterId) }
      : {};

export const hasTarget = (target: MaintenanceTarget): boolean =>
  target.targetType === 'asset' ? !!target.assetId : !!target.workCenterId;

interface Props {
  value: MaintenanceTarget;
  onChange: (next: MaintenanceTarget) => void;
  /** Active equipment assets to choose from. */
  assets: MaintenanceAsset[];
  /** Radio-group name; must be unique on the page. */
  name: string;
}

/**
 * Maintenance target picker: a production WORK CENTER or a non-production EQUIPMENT ASSET —
 * exactly one, which is what the server (and migration 124's CHECK) holds every schedule,
 * work order and log to. Switching kind clears the other id so a stale one is never sent.
 */
export function MaintenanceTargetField({ value, onChange, assets, name }: Props) {
  const assetOptions = assets.map(asset => ({
    value: String(asset.id),
    label: `${asset.asset_tag} ${asset.name}`,
    hint: asset.asset_type || undefined,
  }));
  return (
    <div className="space-y-2">
      <fieldset>
        <legend className="block text-sm font-medium text-slate-300 mb-1">Maintains</legend>
        <div className="flex gap-4 text-sm">
          {(
            [
              ['work_center', 'Work center'],
              ['asset', 'Equipment asset'],
            ] as Array<[MaintenanceTargetType, string]>
          ).map(([kind, label]) => (
            <label key={kind} className="flex items-center gap-2">
              <input
                type="radio"
                name={name}
                value={kind}
                checked={value.targetType === kind}
                onChange={() => onChange({ targetType: kind, workCenterId: '', assetId: '' })}
              />
              <span>{label}</span>
            </label>
          ))}
        </div>
      </fieldset>
      {value.targetType === 'work_center' ? (
        <FormField label="Work Center" required labelClassName="block text-sm font-medium text-slate-300 mb-1">
          {field => (
            <EntityPicker
              {...field}
              kind="workCenter"
              value={value.workCenterId}
              onChange={next => onChange({ ...value, workCenterId: next })}
            />
          )}
        </FormField>
      ) : (
        <FormField
          label="Equipment Asset"
          required
          labelClassName="block text-sm font-medium text-slate-300 mb-1"
          help={assets.length === 0 ? 'No equipment assets yet — add one on the Assets tab.' : undefined}
        >
          {field => (
            <ComboBox
              id={field.id}
              ariaDescribedBy={field['aria-describedby']}
              options={assetOptions}
              value={value.assetId}
              onChange={next => onChange({ ...value, assetId: next })}
              placeholder="Search by tag or name…"
              emptyOptionLabel="Select…"
              noResultsLabel="No matching assets"
              disabled={assets.length === 0}
            />
          )}
        </FormField>
      )}
    </div>
  );
}

export default MaintenanceTargetField;
