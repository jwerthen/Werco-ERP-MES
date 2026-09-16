import React, { useState } from 'react';

type Contour = { id?: unknown; vertices_mm?: unknown; length_mm?: unknown; closed?: unknown; is_hole?: unknown };
export function DxfPreview({ geometry }: { geometry: Record<string, unknown> }) {
  const [selected, setSelected] = useState<number | null>(null);
  const contours = (Array.isArray(geometry.contours) ? geometry.contours : []) as Contour[];
  const bounds = geometry.bounds_mm;
  if (!Array.isArray(bounds) || bounds.length !== 4 || !bounds.every(v => typeof v === 'number' && Number.isFinite(v)) || !contours.length) return null;
  const width = Math.max(bounds[2] - bounds[0], 1); const height = Math.max(bounds[3] - bounds[1], 1); const margin = Math.max(width, height) * 0.03;
  const chosen = selected === null ? null : contours[selected];
  return <div className="fq-dxf-preview"><div className="fq-model-toolbar"><strong>DXF contour inspection</strong><span>{width.toLocaleString()} × {height.toLocaleString()} mm envelope</span></div>
    <svg viewBox={`${bounds[0] - margin} ${bounds[1] - margin} ${width + margin * 2} ${height + margin * 2}`} role="img" aria-label="DXF contour preview. Select a contour from the list below for its measured length.">
      <g transform={`translate(0 ${bounds[1] + bounds[3]}) scale(1 -1)`}>{contours.map((contour, i) => { const points = contour.vertices_mm; if (!Array.isArray(points) || !points.every(p => Array.isArray(p) && p.length >= 2 && p.every(n => typeof n === 'number' && Number.isFinite(n)))) return null; const coordinates = points.map(p => `${p[0]},${p[1]}`).join(' '); return <polyline key={i} points={coordinates + (contour.closed && points.length ? ` ${points[0][0]},${points[0][1]}` : '')} fill="none" stroke={selected === i ? 'var(--fd-amber)' : contour.is_hole ? 'var(--fd-body)' : 'var(--fd-cyan)'} strokeWidth={Math.max(width, height) / (selected === i ? 180 : 350)} onClick={() => setSelected(i)}><title>Contour {i + 1} · {String(contour.length_mm ?? '?')} mm</title></polyline>; })}</g>
    </svg><div className="fq-contour-list" aria-label="DXF contours">{contours.map((contour, i) => <button key={i} type="button" className="fq-text-button" aria-pressed={selected === i} onClick={() => setSelected(i)}>Contour {i + 1} · {contour.closed ? 'closed' : 'open'}</button>)}</div>
    {chosen && <p className="fq-hint">Contour {selected! + 1}: {String(chosen.length_mm ?? 'Unknown')} mm measured length · {chosen.is_hole ? 'candidate hole' : 'unconfirmed role'}</p>}
    <p className="fq-hint">Curves in this display are approximated. Verify the original layers and contour roles before using measured length or a conservative blank envelope.</p>
  </div>;
}
