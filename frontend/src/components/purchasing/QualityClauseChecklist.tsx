/**
 * The quality-clause checklist on a purchase order (AS9100D 8.4.3 flow-down).
 *
 * Shared by the Create PO modal and the PO detail/edit modal. It is a controlled list: the
 * caller owns `selected` and decides what to send. The catalog it offers is the company's
 * ACTIVE clauses; a selected code that is no longer active (retired since, or restored from a
 * saved draft) is still shown — checked and flagged, and the buyer can uncheck it — so the
 * list on screen is exactly the list a save would send, and the buyer can drop it rather than
 * meet the server's 422 with no way to act on it.
 */
import React, { useId } from 'react';
import type { POQualityClause } from '../../types/poFlowdown';
import { sortQualityClauses } from '../../utils/qualityFlowdown';

interface QualityClauseChecklistProps {
  /** Active catalog clauses; `null` while loading. */
  clauses: POQualityClause[] | null;
  /** The catalog could not be read. */
  loadError?: boolean;
  /** What to say when it could not be read (what a save will do instead). */
  loadErrorText?: string;
  /** The codes currently selected, as displayed. */
  selected: readonly string[];
  /** Called with the new selection, catalog order first, then any no-longer-active codes. */
  onChange?: (codes: string[]) => void;
  /** Show the list without letting it change. */
  readOnly?: boolean;
  /** Extra explanation under the legend (e.g. the frozen-at-issue note). */
  note?: React.ReactNode;
}

export function QualityClauseChecklist({
  clauses,
  loadError = false,
  loadErrorText = 'Quality clauses could not be loaded.',
  selected,
  onChange,
  readOnly = false,
  note,
}: QualityClauseChecklistProps) {
  const baseId = useId();
  const noteId = `${baseId}-note`;

  const legend = (
    <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-fd-mute">
      Quality flow-down clauses
    </legend>
  );

  if (loadError) {
    return (
      <fieldset className="rounded-sm border border-fd-line p-3" data-testid="po-quality-clauses">
        {legend}
        <p className="text-sm text-fd-mute">{loadErrorText}</p>
      </fieldset>
    );
  }

  if (clauses === null) {
    return (
      <fieldset className="rounded-sm border border-fd-line p-3" data-testid="po-quality-clauses">
        {legend}
        <p className="text-sm text-fd-mute" role="status">
          Loading quality clauses…
        </p>
      </fieldset>
    );
  }

  const catalog = sortQualityClauses(clauses.filter(clause => clause.is_active));
  const catalogCodes = new Set(catalog.map(clause => clause.code));
  const selectedSet = new Set(selected);
  // Selected codes the active catalog does not offer. Kept visible on purpose (see header).
  const stale = Array.from(selectedSet).filter(code => !catalogCodes.has(code));

  if (catalog.length === 0 && stale.length === 0) {
    return (
      <fieldset className="rounded-sm border border-fd-line p-3" data-testid="po-quality-clauses">
        {legend}
        {note && <div className="mb-2 text-xs text-fd-mute">{note}</div>}
        <p className="text-sm text-fd-mute">
          No quality clauses are set up. Quality can add them under Quality → PO Flow-down.
        </p>
      </fieldset>
    );
  }

  const toggle = (code: string, checked: boolean) => {
    if (readOnly || !onChange) return;
    const next = new Set(selectedSet);
    if (checked) next.add(code);
    else next.delete(code);
    onChange([...catalog.map(clause => clause.code), ...stale].filter(item => next.has(item)));
  };

  return (
    <fieldset
      className="rounded-sm border border-fd-line p-3"
      data-testid="po-quality-clauses"
      aria-describedby={note ? noteId : undefined}
    >
      {legend}
      {note && (
        <div id={noteId} className="mb-2 text-xs text-fd-mute">
          {note}
        </div>
      )}
      <ul className="space-y-2">
        {catalog.map(clause => {
          const inputId = `${baseId}-${clause.code}`;
          return (
            <li key={clause.code} className="flex items-start gap-2">
              <input
                id={inputId}
                type="checkbox"
                className="mt-1 rounded border-slate-600 disabled:opacity-60"
                checked={selectedSet.has(clause.code)}
                disabled={readOnly}
                onChange={e => toggle(clause.code, e.target.checked)}
              />
              <label htmlFor={inputId} className="text-sm">
                <span className="font-mono font-medium text-fd-ink">{clause.code}</span>
                <span className="text-fd-body"> — {clause.title}</span>
                {clause.default_selected && <span className="ml-2 text-xs text-fd-mute">(default)</span>}
                {clause.body && <span className="mt-0.5 block text-xs text-fd-mute line-clamp-2">{clause.body}</span>}
              </label>
            </li>
          );
        })}
        {stale.map(code => {
          const inputId = `${baseId}-stale-${code}`;
          return (
            <li key={`stale-${code}`} className="flex items-start gap-2">
              <input
                id={inputId}
                type="checkbox"
                className="mt-1 rounded border-slate-600 disabled:opacity-60"
                checked
                disabled={readOnly}
                onChange={e => toggle(code, e.target.checked)}
              />
              <label htmlFor={inputId} className="text-sm">
                <span className="font-mono font-medium text-fd-ink">{code}</span>
                <span className="text-fd-amber">
                  {readOnly
                    ? ' — no longer in the active clause catalog'
                    : ' — no longer in the active clause catalog; uncheck it to save a clause selection'}
                </span>
              </label>
            </li>
          );
        })}
      </ul>
    </fieldset>
  );
}

export default QualityClauseChecklist;
