import React, { useId } from 'react';
import type { Part } from './lib/nesting';
import {
  allowedRotations,
  effectiveRotationMode,
  orientationExplanation,
  type GrainAxis,
  type RotationMode,
} from './lib/orientation';

export const sheetGrainLabel = (axis?: GrainAxis) =>
  axis === 'x' ? 'Along sheet length (X)' : axis === 'y' ? 'Along sheet width (Y)' : 'Unknown / not specified';

export function orientationSummary(part: Part, stock: { grainAxis?: GrainAxis }) {
  const angles = allowedRotations(part, stock);
  return angles.length ? angles.map(angle => `${angle}°`).join(' / ') : 'No permitted orientation';
}

export function SheetGrainControl({
  grainAxis,
  onChange,
}: {
  grainAxis?: GrainAxis;
  onChange: (axis?: GrainAxis) => void;
}) {
  const id = useId();
  return (
    <label className="field-label" htmlFor={id}>
      <span>Sheet grain</span>
      <select
        id={id}
        className="orientation-select"
        value={grainAxis ?? ''}
        onChange={event => onChange((event.target.value || undefined) as GrainAxis | undefined)}
      >
        <option value="">Unknown / not specified</option>
        <option value="x">Along sheet length (X)</option>
        <option value="y">Along sheet width (Y)</option>
      </select>
    </label>
  );
}

export default function PartOrientationControls({
  part,
  stock,
  onChange,
}: {
  part: Part;
  stock: { grainAxis?: GrainAxis };
  onChange: (patch: Partial<Part>) => void;
}) {
  const id = useId();
  const blocked = orientationExplanation(part, stock);
  return (
    <div className="orientation-fields">
      <label className="field-label" htmlFor={`${id}-rotation`}>
        <span>Allowed rotation</span>
        <select
          id={`${id}-rotation`}
          aria-label={`Allowed rotation for ${part.name}`}
          className="orientation-select"
          value={effectiveRotationMode(part)}
          onChange={event => {
            const rotationMode = event.target.value as RotationMode;
            onChange({ rotationMode, rotate: rotationMode !== 'fixed' });
          }}
        >
          <option value="fixed">Fixed (0°)</option>
          <option value="half-turn">Half turns (0°, 180°)</option>
          <option value="quarter-turn">Quarter turns (0°, 90°, 180°, 270°)</option>
        </select>
      </label>
      <label className="field-label" htmlFor={`${id}-grain`}>
        <span>Part grain</span>
        <select
          id={`${id}-grain`}
          aria-label={`Part grain for ${part.name}`}
          className="orientation-select"
          value={part.grainAxis ?? ''}
          onChange={event => onChange({ grainAxis: (event.target.value || undefined) as GrainAxis | undefined })}
        >
          <option value="">No grain requirement</option>
          <option value="x">Horizontal in DXF (X)</option>
          <option value="y">Vertical in DXF (Y)</option>
        </select>
      </label>
      <p className={blocked ? 'orientation-warning' : 'orientation-note'}>
        {blocked ?? `Permitted on this stock: ${orientationSummary(part, stock)}. Mirroring prohibited.`}
      </p>
    </div>
  );
}
