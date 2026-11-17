import React from 'react';
import { ArrowDownIcon, ArrowUpIcon, PlusIcon, TrashIcon } from '@heroicons/react/24/outline';
import { Button, FormField } from '../../ui';
import type { ReportAggregate, ReportField } from '../../../types/reportBuilder';
import { AGGREGATE_LABELS, MAX_ALIAS_LENGTH, MAX_COLUMNS, aggregatesFor, type DraftColumn } from './reportDefinition';
import { COMPACT_INPUT, ICON_BUTTON, LEGEND, SECTION } from './builderStyles';

interface ColumnsEditorProps {
  fields: ReportField[];
  columns: DraftColumn[];
  onChange: (columns: DraftColumn[]) => void;
  onAdd: () => void;
  disabled?: boolean;
}

/** A field that is no longer in the catalog stays selectable so a stale template still shows what it held. */
export function fieldOptions(fields: ReportField[], current: string) {
  const options = fields.map(field => ({ value: field.name, label: field.label }));
  if (current && !fields.some(field => field.name === current)) {
    options.unshift({ value: current, label: `${current} (no longer available)` });
  }
  return options;
}

/**
 * The selected columns, in output order: a field, an optional summary (only the
 * aggregates its type allows) and an optional output label.
 */
export function ColumnsEditor({ fields, columns, onChange, onAdd, disabled }: ColumnsEditorProps) {
  const update = (uid: string, changes: Partial<DraftColumn>) =>
    onChange(columns.map(column => (column.uid === uid ? { ...column, ...changes } : column)));

  const move = (index: number, delta: number) => {
    const next = columns.slice();
    const [moved] = next.splice(index, 1);
    next.splice(index + delta, 0, moved);
    onChange(next);
  };

  return (
    <fieldset className={SECTION}>
      <legend className={LEGEND}>Columns</legend>
      {columns.length === 0 ? (
        <p className="mt-2 text-sm text-fd-mute">No columns yet. Add the fields the report should show.</p>
      ) : (
        <ol className="mt-2 space-y-2">
          {columns.map((column, index) => {
            const field = fields.find(candidate => candidate.name === column.field);
            const allowed = aggregatesFor(field?.type);
            const n = index + 1;
            return (
              <li key={column.uid} className="grid grid-cols-1 gap-2 sm:grid-cols-12 sm:items-end">
                <FormField label={`Column ${n} field`} className="sm:col-span-4" labelClassName="text-xs">
                  {props => (
                    <select
                      {...props}
                      className={COMPACT_INPUT}
                      value={column.field}
                      disabled={disabled}
                      onChange={event => {
                        const nextField = fields.find(candidate => candidate.name === event.target.value);
                        const keep =
                          column.aggregate && aggregatesFor(nextField?.type).includes(column.aggregate)
                            ? column.aggregate
                            : '';
                        update(column.uid, { field: event.target.value, aggregate: keep });
                      }}
                    >
                      {fieldOptions(fields, column.field).map(option => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label={`Column ${n} summary`} className="sm:col-span-3" labelClassName="text-xs">
                  {props => (
                    <select
                      {...props}
                      className={COMPACT_INPUT}
                      value={column.aggregate}
                      disabled={disabled}
                      onChange={event => update(column.uid, { aggregate: event.target.value as ReportAggregate | '' })}
                    >
                      <option value="">None (show values)</option>
                      {allowed.map(aggregate => (
                        <option key={aggregate} value={aggregate}>
                          {AGGREGATE_LABELS[aggregate]}
                        </option>
                      ))}
                      {column.aggregate && !allowed.includes(column.aggregate) && (
                        <option value={column.aggregate}>{AGGREGATE_LABELS[column.aggregate]} (not allowed)</option>
                      )}
                    </select>
                  )}
                </FormField>
                <FormField label={`Column ${n} label`} className="sm:col-span-3" labelClassName="text-xs">
                  {props => (
                    <input
                      {...props}
                      type="text"
                      className={COMPACT_INPUT}
                      value={column.alias}
                      placeholder="Optional"
                      maxLength={MAX_ALIAS_LENGTH}
                      disabled={disabled}
                      onChange={event => update(column.uid, { alias: event.target.value })}
                    />
                  )}
                </FormField>
                <div className="flex gap-1 sm:col-span-2 sm:justify-end">
                  <button
                    type="button"
                    className={ICON_BUTTON}
                    aria-label={`Move column ${n} up`}
                    title="Move up"
                    disabled={disabled || index === 0}
                    onClick={() => move(index, -1)}
                  >
                    <ArrowUpIcon className="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className={ICON_BUTTON}
                    aria-label={`Move column ${n} down`}
                    title="Move down"
                    disabled={disabled || index === columns.length - 1}
                    onClick={() => move(index, 1)}
                  >
                    <ArrowDownIcon className="h-4 w-4" aria-hidden="true" />
                  </button>
                  <button
                    type="button"
                    className={ICON_BUTTON}
                    aria-label={`Remove column ${n}`}
                    title="Remove"
                    disabled={disabled}
                    onClick={() => onChange(columns.filter(candidate => candidate.uid !== column.uid))}
                  >
                    <TrashIcon className="h-4 w-4" aria-hidden="true" />
                  </button>
                </div>
              </li>
            );
          })}
        </ol>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="mt-2 inline-flex items-center gap-1"
        onClick={onAdd}
        disabled={disabled || fields.length === 0 || columns.length >= MAX_COLUMNS}
        title={columns.length >= MAX_COLUMNS ? `At most ${MAX_COLUMNS} columns` : undefined}
      >
        <PlusIcon className="h-4 w-4" aria-hidden="true" />
        Add column
      </Button>
    </fieldset>
  );
}

export default ColumnsEditor;
