import React, { useEffect, useRef, useState } from 'react';
import { Button, FormField } from '../ui';
import type { InchPoint, ObservedShape, ObservedZone } from '../../types/stockPiece';
import { observedShapeSchema, stockPieceEvidenceSchema, emptyEvidence } from '../../validation/stockPiece';

const ringText = (points: InchPoint[]) => points.map(p => `${p.x}, ${p.y}`).join('\n');
function RingEditor({
  label,
  points,
  onChange,
}: {
  label: string;
  points: InchPoint[];
  onChange: (points: InchPoint[]) => void;
}) {
  const [text, setText] = useState(() => ringText(points));
  const emitted = useRef(points);
  useEffect(() => {
    if (points !== emitted.current) setText(ringText(points));
  }, [points]);
  return (
    <FormField label={label} help="One X, Y pair per line, in inches. At least 3 points; the closing edge is implied.">
      {field => (
        <textarea
          {...field}
          className="input font-mono min-h-28"
          rows={4}
          maxLength={65000}
          value={text}
          onChange={event => {
            const next = event.target.value;
            setText(next);
            const parsed = next.trim()
              ? next.split('\n').map(line => {
                  const comma = line.indexOf(',');
                  return { x: comma < 0 ? line : line.slice(0, comma), y: comma < 0 ? '' : line.slice(comma + 1) };
                })
              : [];
            emitted.current = parsed;
            onChange(parsed);
          }}
        />
      )}
    </FormField>
  );
}
export function StockPieceShapeEditor({
  value,
  onChange,
  zone = false,
  unknownDisabled = false,
}: {
  value: ObservedShape;
  onChange: (value: ObservedShape) => void;
  zone?: boolean;
  unknownDisabled?: boolean;
}) {
  const kinds = zone ? (['circle', 'polygon'] as const) : (['unknown', 'rectangle', 'circle', 'polygon'] as const);
  const update = (name: string, number: string) => onChange({ ...value, [name]: number });
  return (
    <div className="space-y-3">
      <FormField label={zone ? 'Zone shape' : 'Reported shape'}>
        {field => (
          <select
            {...field}
            className="input"
            value={value.kind}
            onChange={event => {
              const kind = event.target.value;
              onChange(
                kind === 'rectangle'
                  ? { kind, width: '', height: '' }
                  : kind === 'circle'
                    ? { kind, cx: '0', cy: '0', r: '' }
                    : kind === 'polygon'
                      ? { kind, outer: [], holes: [] }
                      : { kind: 'unknown' }
              );
            }}
          >
            {kinds.map(kind => (
              <option key={kind} value={kind} disabled={kind === 'unknown' && unknownDisabled}>
                {kind === 'unknown' ? 'Unknown / not measured' : kind[0].toUpperCase() + kind.slice(1)}
              </option>
            ))}
          </select>
        )}
      </FormField>
      {value.kind === 'rectangle' && (
        <div className="grid grid-cols-2 gap-3">
          {(['width', 'height'] as const).map(name => (
            <FormField key={name} label={`${name === 'width' ? 'Horizontal X' : 'Vertical Y'} (in)`} required>
              {field => (
                <input
                  {...field}
                  className="input"
                  value={value[name]}
                  onChange={e => update(name, e.target.value)}
                  maxLength={80}
                />
              )}
            </FormField>
          ))}
        </div>
      )}
      {value.kind === 'circle' && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          {(['cx', 'cy', 'r'] as const).map(name => (
            <FormField
              key={name}
              label={`${name === 'r' ? 'Radius' : name === 'cx' ? 'Center X' : 'Center Y'} (in)`}
              required
            >
              {field => (
                <input
                  {...field}
                  className="input"
                  value={value[name]}
                  onChange={e => update(name, e.target.value)}
                  maxLength={80}
                />
              )}
            </FormField>
          ))}
        </div>
      )}
      {value.kind === 'polygon' && (
        <>
          <RingEditor
            label={zone ? 'Zone boundary points' : 'Outer boundary points'}
            points={value.outer}
            onChange={outer => onChange({ ...value, outer })}
          />
          {!zone &&
            value.holes.map((hole, index) => (
              <div key={index} className="border border-slate-700 p-3 space-y-2">
                <RingEditor
                  label={`Hole ${index + 1} points`}
                  points={hole}
                  onChange={points =>
                    onChange({ ...value, holes: value.holes.map((old, i) => (i === index ? points : old)) })
                  }
                />
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => onChange({ ...value, holes: value.holes.filter((_, i) => i !== index) })}
                >
                  Remove hole {index + 1}
                </Button>
              </div>
            ))}
          {!zone && (
            <Button
              variant="secondary"
              size="sm"
              disabled={value.holes.length >= 16}
              onClick={() => onChange({ ...value, holes: [...value.holes, []] })}
            >
              Add hole
            </Button>
          )}
        </>
      )}
    </div>
  );
}
const path = (points: InchPoint[]) => points.map((p, i) => `${i ? 'L' : 'M'}${p.x} ${p.y}`).join(' ') + ' Z';
export function StockPieceGeometryPreview({ shape, zones }: { shape: ObservedShape; zones: ObservedZone[] }) {
  const parsed = stockPieceEvidenceSchema.safeParse({
    ...emptyEvidence(),
    measurement_method: 'preview',
    geometry: shape,
    unavailable_zones: zones,
  });
  if (!parsed.success)
    return <p className="text-sm text-amber-300">Complete valid measurement fields to preview the reported outline.</p>;
  const valid = parsed.data.geometry;
  if (valid.kind === 'unknown')
    return (
      <div className="border border-dashed border-slate-600 p-6 text-slate-400">No measured outline recorded.</div>
    );
  const bounds = (v: Exclude<ObservedShape, { kind: 'unknown' }>) => {
    if (v.kind === 'rectangle') return [0, 0, Number(v.width), Number(v.height)];
    if (v.kind === 'circle')
      return [
        Number(v.cx) - Number(v.r),
        Number(v.cy) - Number(v.r),
        Number(v.cx) + Number(v.r),
        Number(v.cy) + Number(v.r),
      ];
    const points = [v.outer, ...v.holes].flat();
    return [
      Math.min(...points.map(p => Number(p.x))),
      Math.min(...points.map(p => Number(p.y))),
      Math.max(...points.map(p => Number(p.x))),
      Math.max(...points.map(p => Number(p.y))),
    ];
  };
  const all = [
    bounds(valid),
    ...parsed.data.unavailable_zones.map(z =>
      bounds(z.outline.kind === 'circle' ? z.outline : { kind: 'polygon', outer: z.outline.pts, holes: [] })
    ),
  ];
  const minX = Math.min(...all.map(b => b[0])),
    minY = Math.min(...all.map(b => b[1]));
  const maxX = Math.max(...all.map(b => b[2])),
    maxY = Math.max(...all.map(b => b[3]));
  const pad = Math.max(maxX - minX, maxY - minY, 0.001) * 0.05;
  const draw = (v: Exclude<ObservedShape, { kind: 'unknown' }>) =>
    v.kind === 'rectangle' ? (
      <rect x={0} y={0} width={v.width} height={v.height} vectorEffect="non-scaling-stroke" />
    ) : v.kind === 'circle' ? (
      <circle cx={v.cx} cy={v.cy} r={v.r} vectorEffect="non-scaling-stroke" />
    ) : (
      <path d={[path(v.outer), ...v.holes.map(path)].join(' ')} fillRule="evenodd" vectorEffect="non-scaling-stroke" />
    );
  return (
    <figure className="space-y-2">
      <svg
        role="img"
        aria-label="Reported piece outline and unavailable zones in inches"
        viewBox={`${minX - pad} ${minY - pad} ${maxX - minX + pad * 2} ${maxY - minY + pad * 2}`}
        className="w-full h-56 bg-slate-950 border border-slate-700"
      >
        <g transform={`translate(0 ${minY + maxY}) scale(1 -1)`}>
          <g fill="#1b4d9c" stroke="#93c5fd" strokeWidth={1.5}>
            {draw(valid)}
          </g>
          {parsed.data.unavailable_zones.map(z => (
            <g key={z.id} fill="#f59e0b" fillOpacity={0.45} stroke="#fbbf24" strokeWidth={1.5}>
              {draw(z.outline.kind === 'circle' ? z.outline : { kind: 'polygon', outer: z.outline.pts, holes: [] })}
            </g>
          ))}
        </g>
      </svg>
      <figcaption className="text-xs text-slate-400">
        Reported geometry only. Blue: outline; amber: reported unavailable zones. X right, Y up. Shape validity and
        physical eligibility remain unverified.
      </figcaption>
    </figure>
  );
}

export function shapeIsValid(shape: ObservedShape) {
  return observedShapeSchema.safeParse(shape).success;
}
