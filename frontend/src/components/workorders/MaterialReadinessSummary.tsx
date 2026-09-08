import React from 'react';
import { Link } from 'react-router-dom';
import { MaterialReadiness } from '../../types/jobPlanning';
import { formatCentralDate } from '../../utils/centralTime';

export default function MaterialReadinessSummary({ materials }: { materials: MaterialReadiness }) {
  return (
    <div className="mt-3 text-sm space-y-2">
      <p className={materials.status === 'unknown' ? 'text-amber-200' : 'text-slate-300'}>
        Material-ready date:{' '}
        <strong>{materials.ready_date ? formatCentralDate(materials.ready_date) : 'Unknown'}</strong>
      </p>
      {materials.warnings.map((warning, index) => (
        <p className="text-amber-200" key={index}>
          {warning}
        </p>
      ))}
      {materials.lines.length > 0 && (
        <details className="rounded border border-slate-700 p-2">
          <summary className="cursor-pointer text-fd-link">
            Material coverage · {materials.lines.length} requirements
          </summary>
          <ul className="mt-2 space-y-3">
            {materials.lines.map((line, index) => (
              <li key={`${line.part_id}-${index}`} className="border-t border-slate-700 pt-2">
                <p className="font-medium">
                  {line.part_number} · {line.required_quantity.toLocaleString()} {line.unit_of_measure || ''} needed
                </p>
                <p>
                  {line.covered_quantity.toLocaleString()} covered · {line.shortage_quantity.toLocaleString()}{' '}
                  unresolved
                </p>
                {line.reason && <p className="text-amber-200">{line.reason}</p>}
                <ul className="mt-1 text-slate-300">
                  {line.sources.map((source, sourceIndex) => (
                    <li key={sourceIndex}>
                      {source.quantity.toLocaleString()} from{' '}
                      {source.kind === 'purchase_order' ? (
                        <Link className="text-fd-link underline" to={`/purchasing?po=${source.id}`}>
                          {source.label}
                        </Link>
                      ) : (
                        source.label
                      )}{' '}
                      {source.expires_on && <span> (expires {formatCentralDate(source.expires_on)})</span>} ·{' '}
                      {source.kind === 'stock'
                        ? 'usable stock'
                        : `supplier confirmed ${formatCentralDate(source.available_date)}`}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
        </details>
      )}
      <p className="text-xs text-slate-400">{materials.basis}</p>
    </div>
  );
}
