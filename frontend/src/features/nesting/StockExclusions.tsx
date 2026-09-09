import React, { useState } from 'react';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { bounds, svgPath, type Loop } from './lib/nesting';
import { type StockExclusion, validateStockExclusions } from './lib/stock-exclusions';
import type { SheetOption } from './lib/quoting';
import { formatIn, inToMm, mmToIn, parseInches } from './lib/units';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from './ui/dialog';

const field = z.string().refine(value => Number.isFinite(parseInches(value)), 'Enter decimal inches or a fraction.');
const schema = z
  .object({
    label: z.string().trim().min(1, 'Enter an area label.').max(120),
    reason: z.string().trim().min(1, 'Explain why this material is unavailable.').max(1000),
    kind: z.enum(['rectangle', 'circle', 'polygon']),
    x: z.string(),
    y: z.string(),
    width: z.string(),
    height: z.string(),
    radius: z.string(),
    clearance: field.refine(
      value => parseInches(value) >= 0 && parseInches(value) <= 100,
      'Added clearance must be 0–100 inches.'
    ),
  })
  .superRefine((value, context) => {
    const keys =
      value.kind === 'polygon'
        ? []
        : value.kind === 'circle'
          ? (['x', 'y', 'radius'] as const)
          : (['x', 'y', 'width', 'height'] as const);
    for (const key of keys)
      if (!Number.isFinite(parseInches(value[key])))
        context.addIssue({ code: 'custom', path: [key], message: 'Enter decimal inches or a fraction.' });
  });
type Values = z.infer<typeof schema>;
// Show readable inch values while retaining untouched source numbers below.
const inputIn = (value: number) => String(Number(mmToIn(value).toPrecision(12)));
function shapeKind(outline: Loop): Values['kind'] {
  if (outline.type === 'circle') return 'circle';
  const box = bounds(outline);
  return outline.points.length === 4 &&
    outline.points.every(
      point =>
        (point.x === box.x || point.x === box.x + box.width) && (point.y === box.y || point.y === box.y + box.height)
    )
    ? 'rectangle'
    : 'polygon';
}
function initial(region?: StockExclusion): Values {
  const outline = region?.outline,
    box = outline ? bounds(outline) : null;
  return {
    label: region?.label ?? '',
    reason: region?.reason ?? '',
    kind: outline ? shapeKind(outline) : 'rectangle',
    x: outline?.type === 'circle' ? inputIn(outline.cx) : box ? inputIn(box.x) : '0',
    y: outline?.type === 'circle' ? inputIn(outline.cy) : box ? inputIn(box.y) : '0',
    width: box ? inputIn(box.width) : '1',
    height: box ? inputIn(box.height) : '1',
    radius: outline?.type === 'circle' ? inputIn(outline.r) : '0.5',
    clearance: inputIn(region?.clearance ?? 0),
  };
}
function buildOutline(value: Values, original?: StockExclusion): Loop {
  const previous = initial(original);
  if (original && (['kind', 'x', 'y', 'width', 'height', 'radius'] as const).every(key => value[key] === previous[key]))
    return original.outline;
  if (value.kind === 'polygon') {
    if (!original) throw new Error('An existing polygon is required.');
    return original.outline;
  }
  const box = original ? bounds(original.outline) : undefined;
  const source = original?.outline;
  const unchanged = (key: 'x' | 'y' | 'width' | 'height' | 'radius', exact?: number) =>
    original && value.kind === previous.kind && value[key] === previous[key] && exact !== undefined
      ? exact
      : inToMm(parseInches(value[key]));
  const x = unchanged('x', source?.type === 'circle' ? source.cx : box?.x),
    y = unchanged('y', source?.type === 'circle' ? source.cy : box?.y);
  if (value.kind === 'circle')
    return { type: 'circle', cx: x, cy: y, r: unchanged('radius', source?.type === 'circle' ? source.r : undefined) };
  const w = unchanged('width', box?.width),
    h = unchanged('height', box?.height);
  if (!(w > 0 && h > 0)) throw new Error('Rectangle width and height must be positive.');
  return {
    type: 'poly',
    points: [
      { x, y },
      { x: x + w, y },
      { x: x + w, y: y + h },
      { x, y: y + h },
    ],
  };
}

function AreaForm({
  option,
  region,
  onSave,
  onCancel,
}: {
  option: SheetOption;
  region?: StockExclusion;
  onSave: (region: StockExclusion) => void;
  onCancel: () => void;
}) {
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: initial(region) });
  const [error, setError] = useState('');
  const kind = form.watch('kind');
  const immutablePolygon = region && shapeKind(region.outline) === 'polygon';
  const dimensions: { key: 'x' | 'y' | 'width' | 'height' | 'radius' | 'clearance'; label: string }[] = [
    ...(kind === 'polygon'
      ? []
      : [
          { key: 'x' as const, label: kind === 'circle' ? 'Center X (in)' : 'Left X (in)' },
          { key: 'y' as const, label: kind === 'circle' ? 'Center Y (in)' : 'Bottom Y (in)' },
          ...(kind === 'circle'
            ? [{ key: 'radius' as const, label: 'Radius (in)' }]
            : [
                { key: 'width' as const, label: 'Size along X (in)' },
                { key: 'height' as const, label: 'Size along Y (in)' },
              ]),
        ]),
    { key: 'clearance', label: 'Added clearance (in)' },
  ];
  return (
    <form
      className="stock-exclusion-form"
      onSubmit={form.handleSubmit(values => {
        try {
          const changed = {
            id: region?.id ?? crypto.randomUUID(),
            label: values.label,
            reason: values.reason,
            outline: buildOutline(values, region),
            clearance:
              region && values.clearance === initial(region).clearance
                ? region.clearance
                : inToMm(parseInches(values.clearance)),
          };
          validateStockExclusions([changed], option.width, option.height);
          onSave(changed);
        } catch (cause) {
          setError(cause instanceof Error ? cause.message : 'Cannot save this excluded area.');
        }
      })}
    >
      <label className="field-label">
        Area label
        <input {...form.register('label')} maxLength={120} />
      </label>
      {form.formState.errors.label && <p role="alert">{form.formState.errors.label.message}</p>}
      <label className="field-label">
        Reason this material is unavailable
        <textarea {...form.register('reason')} rows={2} maxLength={1000} />
      </label>
      {form.formState.errors.reason && <p role="alert">{form.formState.errors.reason.message}</p>}
      <label className="field-label">
        Area shape
        <select {...form.register('kind')} disabled={!!immutablePolygon}>
          <option value="rectangle">Rectangle</option>
          <option value="circle">Circle</option>
          {immutablePolygon && <option value="polygon">Existing polygon — geometry retained</option>}
        </select>
      </label>
      {immutablePolygon && <p>The exact polygon stays unchanged. Edit its label, reason or added clearance here.</p>}
      <div className="two-fields">
        {dimensions.map(({ key, label }) => (
          <label className="field-label" key={key}>
            {label}
            <input inputMode="decimal" {...form.register(key)} />
            {form.formState.errors[key] && <span role="alert">{form.formState.errors[key]?.message}</span>}
          </label>
        ))}
      </div>
      <p className="helper inset-free">
        Origin is the sheet’s lower-left corner. The complete area must stay inside this {formatIn(option.height)} ×{' '}
        {formatIn(option.width)} in stock size. Added clearance is separate from the part’s half-gap envelope and
        numerical protection.
      </p>
      {error && (
        <p role="alert" className="team-draft-error">
          {error}
        </p>
      )}
      <div className="team-draft-actions">
        <button className="primary" type="submit">
          {region ? 'Update excluded area' : 'Add excluded area'}
        </button>
        <button className="secondary" type="button" onClick={onCancel}>
          Cancel area edit
        </button>
      </div>
    </form>
  );
}

export function StockExclusionOverlay({ exclusions = [] }: { exclusions?: StockExclusion[] }) {
  return (
    <g className="stock-exclusion-overlay" pointerEvents="none">
      {exclusions.map(region => (
        <path
          key={region.id}
          d={svgPath([region.outline])}
          fill="#f87171"
          fillOpacity={0.35}
          stroke="#f87171"
          strokeWidth={1.2}
        >
          <title>
            {region.label}: {region.reason}. Added clearance {formatIn(region.clearance)} in, plus part half-gap and
            numerical guards. Displayed boundary is the entered unavailable area.
          </title>
        </path>
      ))}
    </g>
  );
}
const escapeXml = (value: string) =>
  value.replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[char]!);
export function stockExclusionsSvg(exclusions: StockExclusion[] = []) {
  return exclusions
    .map(
      region =>
        `<path d="${svgPath([region.outline])}" fill="#fecaca" stroke="#dc2626" stroke-width="0.5"><title>${escapeXml(region.label)}: ${escapeXml(region.reason)}. Added clearance ${formatIn(region.clearance)} in; part envelope and numerical guards also apply.</title></path>`
    )
    .join('');
}

export default function StockExclusions({
  option,
  onChange,
}: {
  option: SheetOption;
  onChange: (regions: StockExclusion[]) => void;
}) {
  const [open, setOpen] = useState(false),
    [editing, setEditing] = useState<StockExclusion | 'new' | null>(null);
  const [error, setError] = useState('');
  const regions = option.exclusions ?? [];
  function save(changed: StockExclusion) {
    const next =
      editing === 'new' ? [...regions, changed] : regions.map(region => (region.id === changed.id ? changed : region));
    validateStockExclusions(next, option.width, option.height);
    onChange(next);
    setEditing(null);
    setError('');
  }
  return (
    <>
      <button
        className="secondary compact"
        onClick={() => {
          setOpen(true);
          setError('');
        }}
        aria-label={`Excluded areas for ${formatIn(option.height)} by ${formatIn(option.width)} inch stock ${option.id}`}
      >
        Excluded areas · {regions.length}
      </button>
      <Dialog
        open={open}
        onOpenChange={value => {
          setOpen(value);
          if (!value) setEditing(null);
        }}
      >
        <DialogContent className="stock-exclusion-dialog">
          <DialogHeader>
            <DialogTitle>Excluded stock areas</DialogTitle>
            <DialogDescription>
              Imperial quote inputs for {formatIn(option.height)} × {formatIn(option.width)} in stock. These areas
              repeat on every hypothetical sheet of this size option.
            </DialogDescription>
          </DialogHeader>
          <p>
            No individual piece is identified or reserved. Red shapes show entered unavailable material; clearance and
            guarded part envelopes also restrict placement.
          </p>
          <svg
            viewBox={`0 0 ${option.width} ${option.height}`}
            role="img"
            aria-label="Entered excluded areas on stock sheet"
            className="stock-exclusion-preview"
          >
            <rect
              width={option.width}
              height={option.height}
              fill="#0f172a"
              stroke="#94a3b8"
              strokeWidth={option.width / 500}
            />
            <g transform={`translate(0 ${option.height}) scale(1 -1)`}>
              <StockExclusionOverlay exclusions={regions} />
            </g>
          </svg>
          {editing ? (
            <AreaForm
              key={editing === 'new' ? 'new' : editing.id}
              option={option}
              region={editing === 'new' ? undefined : editing}
              onSave={save}
              onCancel={() => setEditing(null)}
            />
          ) : (
            <>
              <button className="primary compact" disabled={regions.length >= 16} onClick={() => setEditing('new')}>
                New excluded area
              </button>
              {!regions.length && <p>No excluded areas assigned to this stock option.</p>}
              {regions.map(region => (
                <article key={region.id} className="team-draft-item">
                  <div>
                    <strong>{region.label}</strong>
                    <p>{region.reason}</p>
                    <p>
                      Added clearance {formatIn(region.clearance)} in ·{' '}
                      {region.outline.type === 'circle' ? 'Circle' : 'Closed polygon'}
                    </p>
                  </div>
                  <div className="team-draft-actions">
                    <button
                      className="secondary compact"
                      onClick={() => setEditing(region)}
                      aria-label={`Edit excluded area ${region.label}`}
                    >
                      Edit
                    </button>
                    <button
                      className="secondary compact"
                      aria-label={`Remove excluded area ${region.label}`}
                      onClick={() => {
                        try {
                          onChange(regions.filter(value => value.id !== region.id));
                          setError('');
                        } catch (cause) {
                          setError(cause instanceof Error ? cause.message : 'Cannot remove this area.');
                        }
                      }}
                    >
                      Remove
                    </button>
                  </div>
                </article>
              ))}
            </>
          )}
          {error && (
            <p role="alert" className="team-draft-error">
              {error}
            </p>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
