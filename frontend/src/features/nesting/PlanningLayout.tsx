import React, { useRef, useState } from 'react';
import type { LeftoverAnalysis } from './lib/leftovers';
import type { InstanceMap } from './lib/remnant-planning';
import { svgPath, transformLoops, transformReferencePaths, type Nest, type Part, type Stock } from './lib/nesting';
import { formatIn, mmToIn } from './lib/units';
import LeftoverReview, { LeftoverOverlay } from './LeftoverReview';
import { StockExclusionOverlay } from './StockExclusions';

/** Geometry has already crossed the staged/saved validation boundary before display. */
export default function PlanningLayout({
  parts,
  stock,
  nest,
  leftovers,
  leftoverError,
  instanceMap,
  label,
}: {
  parts: Part[];
  stock: Stock;
  nest: Nest;
  leftovers?: LeftoverAnalysis;
  leftoverError?: string;
  instanceMap?: InstanceMap;
  label: string;
}) {
  const [sheet, setSheet] = useState(0),
    [highlighted, setHighlighted] = useState<string | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const byId = new Map(parts.map(part => [part.id, part]));
  const originals = new Map(instanceMap?.map(item => [item.part_id, item.originals]));
  const placements = nest.placements.filter(item => item.sheet === sheet);
  const remaining = leftovers?.sheets.find(item => item.sheet === sheet);
  const loops = stock.domain ? [stock.domain.outer, ...stock.domain.holes] : [];
  const vertices =
    loops.reduce((n, loop) => n + (loop.type === 'circle' ? 2 : loop.points.length), 0) +
    placements.reduce(
      (n, placement) =>
        n +
        byId
          .get(placement.partId)!
          .loops.reduce((m, loop) => m + (loop.type === 'circle' ? 2 : loop.points.length), 0) +
        (byId.get(placement.partId)!.referencePaths?.reduce((m, path) => m + path.length, 0) ?? 0),
      0
    ) +
    (stock.exclusions?.reduce(
      (n, region) => n + (region.outline.type === 'circle' ? 2 : region.outline.points.length),
      0
    ) ?? 0) +
    (remaining?.regions.reduce(
      (n, region) => n + region.outer.length + region.holes.reduce((m, hole) => m + hole.length, 0),
      0
    ) ?? 0);
  function downloadSvg() {
    if (!svg.current) return;
    const exported = svg.current.cloneNode(true) as SVGSVGElement;
    // XMLSerializer supplies the SVG namespace from the DOM node; a React HTML
    // xmlns attribute would otherwise be duplicated by some serializers.
    exported.removeAttribute('xmlns');
    exported.setAttribute('width', `${mmToIn(stock.width)}in`);
    exported.setAttribute('height', `${mmToIn(stock.height)}in`);
    const title = exported.querySelector('title');
    if (title)
      title.textContent = `${label}: ${formatIn(stock.height)} × ${formatIn(stock.width)} in ${stock.domain ? 'bounding extents; actual reported outline and holes' : 'full sheet'}; ${placements.length} parts; margin ${formatIn(stock.margin)} in; part gap ${formatIn(stock.gap)} in. Planning review only; no reservation, inventory consumption or manufacturing approval. Internal viewBox coordinates remain millimeters.`;
    const blob = new Blob([new XMLSerializer().serializeToString(exported)], { type: 'image/svg+xml' });
    const url = URL.createObjectURL(blob),
      anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `nest-planning-${stock.domain ? 'recorded-piece' : 'sheet'}-${sheet + 1}.svg`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  if (!nest.sheets)
    return <p>No {stock.domain ? 'recorded piece was used' : 'full sheet was required'} by this stage.</p>;
  return (
    <section className="saved-run-preview" aria-label={label}>
      <h4>{label}</h4>
      <p className="helper inset-free">
        {stock.domain ? 'Recorded-piece bounding extents' : 'Full sheet'}: {formatIn(stock.height)} ×{' '}
        {formatIn(stock.width)} in · {placements.length} parts · physical-edge margin {formatIn(stock.margin)} in · part
        gap {formatIn(stock.gap)} in
      </p>
      {stock.domain && (
        <p>
          Actual reported outline and physical holes. Availability and eligibility remain unverified; this does not
          reserve or consume the piece.
        </p>
      )}
      {!stock.domain && (
        <label className="saved-run-sheet-picker">
          Sheet
          <select
            value={sheet}
            onChange={event => {
              setSheet(Number(event.target.value));
              setHighlighted(null);
            }}
          >
            {Array.from({ length: nest.sheets }, (_, index) => (
              <option key={index} value={index}>
                {index + 1} of {nest.sheets}
              </option>
            ))}
          </select>
        </label>
      )}
      {vertices <= 100000 ? (
        <>
          <svg
            ref={svg}
            xmlns="http://www.w3.org/2000/svg"
            viewBox={`0 0 ${stock.width} ${stock.height}`}
            role="img"
            aria-label={`${label}: actual part and material shapes`}
          >
            <title>
              {label} — dimensions in millimeters; displayed measurements in inches. Planning only, no physical
              reservation or manufacturing approval.
            </title>
            <g transform={`translate(0 ${stock.height}) scale(1 -1)`}>
              {stock.domain ? (
                <path
                  d={svgPath(loops)}
                  fill="#0f172a"
                  fillRule="evenodd"
                  stroke="#94a3b8"
                  strokeWidth={stock.width / 700}
                />
              ) : (
                <rect
                  width={stock.width}
                  height={stock.height}
                  fill="#0f172a"
                  stroke="#94a3b8"
                  strokeWidth={stock.width / 700}
                />
              )}
              <StockExclusionOverlay exclusions={stock.exclusions} />
              {remaining && <LeftoverOverlay sheet={remaining} highlightedId={highlighted} />}
              {placements.map(placement => {
                const part = byId.get(placement.partId)!;
                const original = originals.get(part.id)?.[placement.instance] ?? placement.instance;
                return (
                  <g key={`${placement.partId}:${placement.instance}`}>
                    <path
                      d={svgPath(transformLoops(part, placement))}
                      fill={['#f28b39', '#60a5fa', '#a78bfa', '#34d399'][part.color % 4]}
                      fillOpacity={0.8}
                      fillRule="evenodd"
                      stroke="#f8fafc"
                      strokeWidth={stock.width / 1200}
                    >
                      <title>
                        {part.name} · original instance {original + 1} · revision {part.revision || 'unassigned'} ·{' '}
                        {placement.rotation}°
                      </title>
                    </path>
                    {transformReferencePaths(part, placement).map((path, index) => (
                      <polyline
                        key={index}
                        points={path.map(point => `${point.x},${point.y}`).join(' ')}
                        fill="none"
                        stroke="#fde68a"
                        strokeWidth={stock.width / 1500}
                      />
                    ))}
                  </g>
                );
              })}
            </g>
          </svg>
          <button className="secondary compact" onClick={downloadSvg}>
            Download layout SVG
          </button>
        </>
      ) : (
        <p>
          This layout exceeds the bounded preview detail limit. Its validated geometry remains in the review record.
        </p>
      )}
      <LeftoverReview
        sheet={remaining}
        error={leftoverError}
        highlightedId={highlighted}
        onHighlight={setHighlighted}
      />
    </section>
  );
}
