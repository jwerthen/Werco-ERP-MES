import React, { useMemo, useState } from 'react';
import type { QuoteProject } from './lib/quote-project';
import { type RemnantStageMessage } from './lib/remnant-planning';
import { formatIn } from './lib/units';
import { quoteFromFile } from './lib/quoting';
import PlanningLayout from './PlanningLayout';

export default function RemnantPlanningResults({
  project,
  rawEstimate,
  stages,
  stale,
  status,
}: {
  project: QuoteProject;
  rawEstimate: { groups: { id: string; quote: unknown }[] };
  stages: RemnantStageMessage[];
  stale: boolean;
  status: string;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const recorded = stages.find(stage => stage.stage_kind === 'recorded_piece');
  const residuals = stages.filter(stage => stage.stage_kind === 'residual');

  const stage = stages.find(item => item.stage_id === selected) ?? recorded;
  const group = stage && project.groups.find(item => item.id === stage.group_id);
  const rawQuote = stage && rawEstimate.groups.find(item => item.id === stage.group_id)?.quote;
  // Display original indices from the worker's explicit map without rerunning
  // geometry during React rendering.
  const originalQuote = useMemo(() => (rawQuote && !stale ? quoteFromFile(rawQuote) : undefined), [rawQuote, stale]);
  const instanceMap = stage?.stage_kind === 'residual' ? stage.instance_map : undefined;
  const quote =
    originalQuote && instanceMap
      ? {
          ...originalQuote,
          parts: originalQuote.parts.flatMap(part => {
            const map = instanceMap.find(item => item.part_id === part.id);
            return map ? [{ ...part, quantity: map.originals.length }] : [];
          }),
        }
      : (originalQuote ?? group?.quote);
  if (!stages.length) return null;
  return (
    <section className="cad-source-panel remnant-planning-panel" aria-label="Conditional recorded-piece alternatives">
      <h3>Conditional piece + full-sheet alternatives</h3>
      <p>
        The full-sheet comparison remains the baseline. Each option below uses this one reported piece at most once,
        then full sheets for its remaining original instances. Alternatives are separate; do not add their counts
        together. Other material groups keep their full-sheet baselines; these cards show only the assigned group
        subtotal.
      </p>
      <p role="status">
        {stale
          ? 'Inputs changed. These prior conditional results are stale; refresh the piece assignment and compare again.'
          : status}
      </p>
      {recorded?.stage_kind === 'recorded_piece' && (
        <p>
          Recorded piece: {recorded.result.nest?.sheets ?? 0} used · {recorded.result.nest?.placements.length ?? 0}{' '}
          original instances placed. {recorded.result.error || ''} This is planning only; no reservation, consumption or
          credit.
        </p>
      )}
      <div className="saved-run-options">
        {recorded && (
          <button className="secondary compact" disabled={stale} onClick={() => setSelected(recorded.stage_id)}>
            View recorded piece
          </button>
        )}
        {residuals.map(result => {
          const assigned = project.groups.find(item => item.id === result.group_id)?.quote;
          const option = assigned?.options.find(item => item.id === result.option_id);
          return (
            <article className="team-draft-item" key={result.stage_id}>
              <div>
                <strong>
                  Piece +{' '}
                  {option ? `${formatIn(option.height)} × ${formatIn(option.width)} in full sheets` : result.option_id}
                </strong>
                <p>
                  {assigned?.material} · {assigned ? formatIn(assigned.thickness) : '—'} in · assigned group
                </p>
                <p>
                  {recorded?.result.nest?.sheets ?? 0} reported piece + {result.result?.nest?.sheets ?? 0} full sheets ·{' '}
                  {result.requested === 0 || result.result?.complete
                    ? 'Assigned group fits'
                    : 'Incomplete conditional layout'}{' '}
                  · {result.requested} instances assigned to full sheets.
                </p>
                {result.result?.error && <p role="alert">{result.result.error}</p>}
              </div>
              <button className="secondary" disabled={stale} onClick={() => setSelected(result.stage_id)}>
                View remaining sheets
              </button>
            </article>
          );
        })}
      </div>
      {!stale &&
        stage &&
        (stage.stage_kind === 'residual' && stage.requested === 0 ? (
          <p>
            No full sheet is needed for this conditional option: every original group instance was placed on the
            recorded piece.
          </p>
        ) : stage.result?.nest && stage.stock && quote ? (
          <PlanningLayout
            key={stage.stage_id}
            label={stage.stage_kind === 'recorded_piece' ? 'Recorded-piece layout' : 'Remaining full-sheet layout'}
            parts={quote.parts}
            stock={stage.stock}
            nest={stage.result.nest}
            leftovers={stage.result.leftovers}
            leftoverError={stage.result.leftoverError}
            instanceMap={instanceMap}
          />
        ) : (
          <p>{stage.result?.error || 'This stage has no placed geometry.'}</p>
        ))}
    </section>
  );
}
