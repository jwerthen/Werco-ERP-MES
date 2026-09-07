import React from 'react';
import { Button } from './Button';

interface PageHeaderProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  metadata?: React.ReactNode;
  icon?: React.ReactNode;
  level?: 1 | 2;
}

/** Keep page identity, context, and actions together in every load state. */
export function PageHeader({ title, description, actions, metadata, icon, level = 1 }: PageHeaderProps) {
  const Heading = level === 1 ? 'h1' : 'h2';
  return (
    <header className="flex min-w-0 flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-start gap-3">
          {icon && (
            <span aria-hidden="true" className="shrink-0">
              {icon}
            </span>
          )}
          <Heading
            className={`${level === 1 ? 'text-2xl' : 'text-xl'} font-bold tracking-tight text-white [overflow-wrap:anywhere]`}
          >
            {title}
          </Heading>
        </div>
        {description && <p className="mt-1 text-sm text-slate-400 [overflow-wrap:anywhere]">{description}</p>}
        {metadata && (
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">{metadata}</div>
        )}
      </div>
      {actions && <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2 sm:justify-end">{actions}</div>}
    </header>
  );
}

interface RecordHeaderProps {
  title: React.ReactNode;
  description?: React.ReactNode;
  fields?: { label: string; value: React.ReactNode }[];
  onClose: () => void;
  closeLabel: string;
  closeDisabled?: boolean;
}

/** Closing a detail is separate from any business cancellation or deletion. */
export function RecordHeader({ title, description, fields, onClose, closeLabel, closeDisabled }: RecordHeaderProps) {
  return (
    <header className="mb-4 space-y-3 border-b border-fd-line pb-4">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-white [overflow-wrap:anywhere]">{title}</h2>
          {description && <p className="mt-1 text-sm text-slate-400 [overflow-wrap:anywhere]">{description}</p>}
        </div>
        <Button variant="ghost" className="shrink-0" aria-label={closeLabel} disabled={closeDisabled} onClick={onClose}>
          Close
        </Button>
      </div>
      {fields && fields.length > 0 && (
        <dl className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-2 sm:grid-cols-2">
          {fields.map(field => (
            <div key={field.label} className="min-w-0">
              <dt className="text-xs text-slate-400">{field.label}</dt>
              <dd className="text-sm text-slate-100 [overflow-wrap:anywhere]">{field.value ?? '—'}</dd>
            </div>
          ))}
        </dl>
      )}
    </header>
  );
}
