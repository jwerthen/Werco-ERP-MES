/**
 * The quality flow-down an ISSUED purchase order carries (AS9100D 8.4.3), read from
 * `flowdown_snapshot` — the block frozen onto the PO when it left DRAFT, i.e. what the
 * supplier was actually sent.
 *
 * Deliberately NOT drawn from the PO's stored `quality_clause_codes`: the freeze drops a clause
 * retired before issue, and a disabled profile freezes no block at all, so the stored codes can
 * name a clause the supplier never saw. Read-only; rendered as React text.
 */
import React from 'react';
import type { POFlowdownFields } from '../../types/poFlowdown';

interface FrozenFlowdownSummaryProps {
  snapshot: POFlowdownFields['flowdown_snapshot'];
}

export function FrozenFlowdownSummary({ snapshot }: FrozenFlowdownSummaryProps) {
  let content: React.ReactNode;
  if (snapshot === undefined) {
    // An API that does not report the snapshot (separate SPA / API deploys): say where the
    // truth is rather than guess at it.
    content = (
      <p className="text-sm text-fd-mute">Open the printed purchase order to see the flow-down it was issued with.</p>
    );
  } else if (snapshot === null) {
    content = <p className="text-sm text-fd-mute">Issued before quality flow-down was recorded.</p>;
  } else if (snapshot.enabled) {
    const clauses = snapshot.clauses ?? [];
    content = (
      <div className="space-y-2 text-sm">
        <p>
          <span className="font-medium text-fd-ink">{snapshot.heading || 'Supplier Quality Requirements'}</span>
          {snapshot.document_reference && (
            <span className="text-fd-mute"> · Reference: {snapshot.document_reference}</span>
          )}
        </p>
        {clauses.length > 0 ? (
          <ul className="space-y-1" aria-label="Clauses printed on this purchase order">
            {clauses.map(clause => (
              <li key={clause.code}>
                <span className="font-mono font-medium text-fd-ink">{clause.code}</span>
                <span className="text-fd-body"> — {clause.title}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-fd-mute">No clauses were printed with it.</p>
        )}
      </div>
    );
  } else {
    content = <p className="text-sm text-fd-mute">No quality flow-down block was printed on this PO.</p>;
  }

  return (
    <fieldset className="rounded-sm border border-fd-line p-3" data-testid="po-frozen-flowdown">
      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-fd-mute">Quality flow-down</legend>
      <p className="mb-2 text-xs text-fd-mute">Frozen when the PO was issued.</p>
      {content}
    </fieldset>
  );
}

export default FrozenFlowdownSummary;
