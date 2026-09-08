import React, { useEffect, useState } from 'react';
import type { LeftoverRegion, SheetLeftoverAnalysis } from './lib/leftovers';
import { svgPath } from './lib/nesting';
import { formatIn } from './lib/units';

const areaIn2 = (area: number) => (area / 25.4 ** 2).toLocaleString('en-US', { maximumFractionDigits: 3 });
const areaFt2 = (area: number) => (area / 304.8 ** 2).toLocaleString('en-US', { maximumFractionDigits: 2 });

export const leftoverPath = (region: LeftoverRegion) =>
  svgPath([{ type: 'poly', points: region.outer }, ...region.holes.map(points => ({ type: 'poly' as const, points }))]);

export function LeftoverOverlay({
  sheet,
  highlightedId,
}: {
  sheet: SheetLeftoverAnalysis;
  highlightedId: string | null;
}) {
  return (
    <g className="leftover-overlay" pointerEvents="none">
      {sheet.regions.map((region, index) => (
        <path
          key={region.id}
          d={leftoverPath(region)}
          fill="#fbbf24"
          fillOpacity={highlightedId === region.id ? 0.23 : 0.075}
          fillRule="evenodd"
          stroke="#fbbf24"
          strokeWidth={highlightedId === region.id ? 3 : 1}
          strokeDasharray={highlightedId === region.id ? undefined : '8 5'}
        >
          <title>{`Potential leftover ${index + 1}: ${areaIn2(region.area)} in². Requires review; no value credited.`}</title>
        </path>
      ))}
    </g>
  );
}

export default function LeftoverReview({
  sheet,
  error,
  highlightedId,
  onHighlight,
}: {
  sheet?: SheetLeftoverAnalysis;
  error?: string;
  highlightedId: string | null;
  onHighlight: (id: string) => void;
}) {
  const [page, setPage] = useState(0);
  useEffect(() => setPage(0), [sheet]);
  const pageSize = 8;
  const pages = Math.ceil((sheet?.regions.length ?? 0) / pageSize);
  const currentPage = Math.min(page, Math.max(0, pages - 1));
  if (!sheet && !error) return null;
  return (
    <section className="leftover-review" aria-label="Leftover material review">
      <div className="leftover-heading">
        <div>
          <div className="eyebrow">LEFTOVER MATERIAL</div>
          <h2>{sheet ? `Review potential leftovers · sheet ${sheet.sheet + 1}` : 'Leftover analysis unavailable'}</h2>
        </div>
        <span className="source-review-badge">$0 credited</span>
      </div>
      {error && <p className="orientation-warning">{error} No remnant area or value is claimed.</p>}
      {sheet && (
        <>
          <p>
            {areaFt2(sheet.remainingArea)} ft² remains in {sheet.regions.length} connected{' '}
            {sheet.regions.length === 1 ? 'region' : 'regions'} after margins and reserved part envelopes. Amber areas
            need physical review for handling, storage and material traceability. A connected region can still be an
            unusable skeleton.
          </p>
          {sheet.regions.length ? (
            <div className="leftover-regions">
              {sheet.regions.slice(currentPage * pageSize, (currentPage + 1) * pageSize).map((region, pageIndex) => {
                const index = currentPage * pageSize + pageIndex;
                return (
                  <button
                    key={region.id}
                    className={`leftover-region${highlightedId === region.id ? ' selected' : ''}`}
                    aria-label={`Highlight leftover region ${index + 1}`}
                    aria-pressed={highlightedId === region.id}
                    onClick={() => onHighlight(region.id)}
                  >
                    <svg
                      viewBox={`${region.bounds.x - 2} ${region.bounds.y - 2} ${region.bounds.width + 4} ${region.bounds.height + 4}`}
                      aria-hidden="true"
                    >
                      <g transform={`translate(0 ${2 * region.bounds.y + region.bounds.height}) scale(1 -1)`}>
                        <path d={leftoverPath(region)} fill="currentColor" fillRule="evenodd" />
                      </g>
                    </svg>
                    <span>
                      <strong>Region {index + 1} · review</strong>
                      <span>{areaIn2(region.area)} in²</span>
                      <small>
                        Overall extents: {formatIn(region.bounds.width)} × {formatIn(region.bounds.height)} in
                      </small>
                      <small>Extents do not guarantee a usable rectangle.</small>
                    </span>
                  </button>
                );
              })}
            </div>
          ) : (
            <p>No leftover region remains within the analysis precision.</p>
          )}
          {pages > 1 && (
            <div className="leftover-pagination">
              <span>
                Regions {currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, sheet.regions.length)} of{' '}
                {sheet.regions.length}
              </span>
              <button className="subtle" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>
                Previous regions
              </button>
              <button className="subtle" disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>
                Next regions
              </button>
            </div>
          )}
          <details className="leftover-ledger">
            <summary>Area breakdown and assumptions</summary>
            <dl>
              {(
                [
                  ['Gross sheet', sheet.grossArea],
                  ['Outside edge margins', sheet.edgeMarginArea],
                  ['Nominal finished parts', sheet.nominalPartArea],
                  ['Reserved internal cutouts', sheet.reservedCutoutArea],
                  ['Clearance and numerical protection', sheet.clearanceAndProtectionArea],
                  ['Potential leftover regions', sheet.remainingArea],
                ] as const
              ).map(([label, area]) => (
                <React.Fragment key={label}>
                  <dt>{label}</dt>
                  <dd>{areaIn2(area)} in²</dd>
                </React.Fragment>
              ))}
            </dl>
            <p>
              Internal cutouts stay reserved. Remaining regions exclude half the selected part gap around each part plus
              conservative curve and numerical protection. This is a quoting allowance, not an exact kerf or
              recoverable-scrap calculation.
            </p>
            <p>
              These are predicted shapes. No remnant is added to inventory or credited against the sheet cost. Reuse
              eligibility and value require approved shop rules and physical verification.
            </p>
          </details>
        </>
      )}
    </section>
  );
}
