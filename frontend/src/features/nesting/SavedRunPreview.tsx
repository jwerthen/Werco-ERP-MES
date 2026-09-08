import React, { useEffect, useState } from 'react';
import api from '../../services/api';
import type { NestingRunDetail } from '../../types/nestingRun';
import { projectFromFile } from './lib/quote-project';
import { stockFor, type Quote } from './lib/quoting';
import { svgPath, transformLoops, validateNest } from './lib/nesting';
import type { ServerOptionMessage } from './lib/server-run';
import { leftoversToFile } from './lib/leftovers';
import { formatIn } from './lib/units';
import { canonicalJSON } from './lib/provenance';
import { nestingApiMessage } from './useNestingCatalog';
import LeftoverReview, { LeftoverOverlay } from './LeftoverReview';
import { StockExclusionOverlay } from './StockExclusions';

export default function SavedRunPreview({ run, sequence }: { run: NestingRunDetail; sequence: number }) {
  const [data, setData] = useState<{ quote: Quote; output: ServerOptionMessage } | null>(null);
  const [error, setError] = useState('');
  const [sheet, setSheet] = useState(0);
  const [highlighted, setHighlighted] = useState<string | null>(null);
  // Bind the fetch to immutable identity, not each heartbeat's mutable status.
  const { id, company_id: companyId, draft_id: draftId, revision_number: revision, input_sha256: inputSha } = run;
  const expectedHash = run.checkpoints.find(item => item.sequence === sequence)?.content_sha256;
  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError('');
    setSheet(0);
    setHighlighted(null);
    void Promise.all([
      api.getNestingRunCheckpoint(id, sequence, controller.signal),
      api.getNestingDraftRevision(draftId, revision, controller.signal),
    ])
      .then(([checkpoint, source]) => {
        if (controller.signal.aborted) return;
        const output = checkpoint.result;
        if (
          source.company_id !== companyId ||
          source.draft_id !== draftId ||
          source.revision_number !== revision ||
          source.content_sha256 !== inputSha ||
          checkpoint.content_sha256 !== expectedHash ||
          output.type !== 'option' ||
          output.protocol !== 1 ||
          output.input_sha256 !== inputSha ||
          output.sequence !== sequence ||
          output.units !== 'mm'
        )
          throw new Error('Saved geometry identity does not match this run.');
        const project = projectFromFile(source.estimate);
        const quote = project.groups.find(group => group.id === output.group_id)?.quote;
        const option = quote?.options.find(item => item.id === output.option_id && item.enabled);
        if (
          !quote ||
          !option ||
          canonicalJSON(option) !== canonicalJSON(output.result.option) ||
          canonicalJSON(stockFor(quote, option)) !== canonicalJSON(output.stock)
        )
          throw new Error('Saved stock geometry differs from its input revision.');
        if (output.result.nest) validateNest(quote.parts, output.stock, output.result.nest);
        if (output.result.leftovers && output.result.nest)
          leftoversToFile(output.result.leftovers, {
            parts: quote.parts,
            stock: output.stock,
            nest: output.result.nest,
          });
        setData({ quote, output });
      })
      .catch(cause => {
        if (!controller.signal.aborted) setError(nestingApiMessage(cause));
      });
    return () => controller.abort();
  }, [id, companyId, draftId, revision, inputSha, sequence, expectedHash]);
  if (error)
    return (
      <p className="team-draft-error" role="alert">
        {error}
      </p>
    );
  if (!data) return <p role="status">Loading saved part geometry…</p>;
  const { quote, output } = data;
  const { result, stock } = output;
  const nest = result.nest;
  if (!nest || !nest.sheets)
    return <p>{result.error || 'This option has no placed parts. Review its unplaced quantities and constraints.'}</p>;
  const placed = nest.placements.filter(item => item.sheet === sheet);
  const byId = new Map(quote.parts.map(part => [part.id, part]));
  const vertices = placed.reduce(
    (total, item) =>
      total +
      byId.get(item.partId)!.loops.reduce((sum, loop) => sum + (loop.type === 'circle' ? 2 : loop.points.length), 0),
    0
  );
  const leftover = result.leftovers?.sheets.find(item => item.sheet === sheet);
  return (
    <section className="saved-run-preview" aria-label="Saved true-shape layout">
      <h4>
        {quote.material} · {formatIn(quote.thickness)} in · {formatIn(stock.height)} × {formatIn(stock.width)} in sheet
      </h4>
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
      <p className="helper inset-free">
        Saved quote layout · {placed.length} parts on this sheet · margin {formatIn(stock.margin)} in · part gap{' '}
        {formatIn(stock.gap)} in
      </p>
      {vertices <= 100000 ? (
        <svg
          viewBox={`0 0 ${stock.width} ${stock.height}`}
          role="img"
          aria-label={`Actual saved part shapes on sheet ${sheet + 1}`}
        >
          <rect
            width={stock.width}
            height={stock.height}
            fill="#0f172a"
            stroke="#94a3b8"
            strokeWidth={stock.width / 700}
          />
          <g transform={`translate(0 ${stock.height}) scale(1 -1)`}>
            <StockExclusionOverlay exclusions={stock.exclusions} />
            <rect
              x={stock.margin}
              y={stock.margin}
              width={Math.max(0, stock.width - 2 * stock.margin)}
              height={Math.max(0, stock.height - 2 * stock.margin)}
              fill="none"
              stroke="#64748b"
              strokeDasharray="5 3"
              strokeWidth={stock.width / 900}
            />
            {leftover && <LeftoverOverlay sheet={leftover} highlightedId={highlighted} />}
            {placed.map(placement => {
              const part = byId.get(placement.partId)!;
              return (
                <path
                  key={`${placement.partId}:${placement.instance}`}
                  d={svgPath(transformLoops(part, placement))}
                  fill={['#f28b39', '#60a5fa', '#a78bfa', '#34d399'][part.color % 4]}
                  fillOpacity={0.8}
                  fillRule="evenodd"
                  stroke="#f8fafc"
                  strokeWidth={stock.width / 1200}
                >
                  <title>
                    {part.name} · revision {part.revision || 'unassigned'} · instance {placement.instance + 1} ·{' '}
                    {placement.rotation}°
                  </title>
                </path>
              );
            })}
          </g>
        </svg>
      ) : (
        <p>
          This sheet exceeds the preview detail limit. Its validated saved result remains in the downloadable report.
        </p>
      )}
      <LeftoverReview
        sheet={leftover}
        error={result.leftoverError}
        highlightedId={highlighted}
        onHighlight={setHighlighted}
      />
    </section>
  );
}
