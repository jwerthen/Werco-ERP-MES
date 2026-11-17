import React, { useState } from 'react';
import { PlusIcon, TrashIcon, XMarkIcon } from '@heroicons/react/24/outline';
import { Button, FormField } from '../../ui';
import type { ReportField, ReportSortDirection } from '../../../types/reportBuilder';
import { MAX_GROUP_BY, MAX_SORT, type DraftSort } from './reportDefinition';
import { COMPACT_INPUT, ICON_BUTTON, LEGEND, SECTION } from './builderStyles';

interface GroupByEditorProps {
  fields: ReportField[];
  groupBy: string[];
  onChange: (groupBy: string[]) => void;
  disabled?: boolean;
}

/** Ordered group-by fields. The first one is the chart's category axis. */
export function GroupByEditor({ fields, groupBy, onChange, disabled }: GroupByEditorProps) {
  const [pending, setPending] = useState('');
  const atCap = groupBy.length >= MAX_GROUP_BY;
  const available = atCap ? [] : fields.filter(field => !groupBy.includes(field.name));
  const labelOf = (name: string) => fields.find(field => field.name === name)?.label ?? `${name} (no longer available)`;
  const choice = available.some(field => field.name === pending) ? pending : '';

  return (
    <fieldset className={SECTION}>
      <legend className={LEGEND}>Group by</legend>
      {groupBy.length === 0 ? (
        <p className="mt-2 text-sm text-fd-mute">Not grouped: one row per record.</p>
      ) : (
        <ol className="mt-2 flex flex-wrap gap-2" aria-label="Group-by fields">
          {groupBy.map((name, index) => (
            <li
              key={name}
              className="inline-flex items-center gap-1 rounded-sm border border-fd-line bg-fd-raised px-2 py-1 text-sm text-fd-ink"
            >
              <span className="text-fd-mute tabular-nums">{index + 1}.</span>
              {labelOf(name)}
              <button
                type="button"
                className="ml-1 text-fd-mute hover:text-fd-ink disabled:opacity-40"
                aria-label={`Stop grouping by ${labelOf(name)}`}
                disabled={disabled}
                onClick={() => onChange(groupBy.filter(candidate => candidate !== name))}
              >
                <XMarkIcon className="h-4 w-4" aria-hidden="true" />
              </button>
            </li>
          ))}
        </ol>
      )}
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <FormField label="Group-by field to add" labelClassName="text-xs" className="w-full sm:w-64">
          {props => (
            <select
              {...props}
              className={COMPACT_INPUT}
              value={choice}
              disabled={disabled || available.length === 0}
              onChange={event => setPending(event.target.value)}
            >
              <option value="">{atCap ? `At most ${MAX_GROUP_BY} group-by fields` : 'Choose a field…'}</option>
              {available.map(field => (
                <option key={field.name} value={field.name}>
                  {field.label}
                </option>
              ))}
            </select>
          )}
        </FormField>
        <Button
          variant="ghost"
          size="sm"
          className="inline-flex items-center gap-1"
          disabled={disabled || !choice}
          onClick={() => {
            onChange([...groupBy, choice]);
            setPending('');
          }}
        >
          <PlusIcon className="h-4 w-4" aria-hidden="true" />
          Add group
        </Button>
      </div>
    </fieldset>
  );
}

interface SortEditorProps {
  options: Array<{ value: string; label: string }>;
  sort: DraftSort[];
  onChange: (sort: DraftSort[]) => void;
  onAdd: () => void;
  disabled?: boolean;
}

/** Sort keys, highest priority first. A grouped report sorts by group-by fields or selected columns only. */
export function SortEditor({ options, sort, onChange, onAdd, disabled }: SortEditorProps) {
  const update = (uid: string, changes: Partial<DraftSort>) =>
    onChange(sort.map(entry => (entry.uid === uid ? { ...entry, ...changes } : entry)));

  return (
    <fieldset className={SECTION}>
      <legend className={LEGEND}>Sort</legend>
      {sort.length === 0 ? (
        <p className="mt-2 text-sm text-fd-mute">Unsorted: the server&apos;s default order.</p>
      ) : (
        <ol className="mt-2 space-y-2">
          {sort.map((entry, index) => {
            const n = index + 1;
            const known = options.some(option => option.value === entry.field);
            return (
              <li key={entry.uid} className="grid grid-cols-1 gap-2 sm:grid-cols-12 sm:items-end">
                <FormField label={`Sort ${n} field`} className="sm:col-span-6" labelClassName="text-xs">
                  {props => (
                    <select
                      {...props}
                      className={COMPACT_INPUT}
                      value={entry.field}
                      disabled={disabled}
                      onChange={event => update(entry.uid, { field: event.target.value })}
                    >
                      {!entry.field && <option value="">Choose…</option>}
                      {!known && entry.field && <option value={entry.field}>{`${entry.field} (not available)`}</option>}
                      {options.map(option => (
                        <option key={option.value} value={option.value}>
                          {option.label}
                        </option>
                      ))}
                    </select>
                  )}
                </FormField>
                <FormField label={`Sort ${n} direction`} className="sm:col-span-4" labelClassName="text-xs">
                  {props => (
                    <select
                      {...props}
                      className={COMPACT_INPUT}
                      value={entry.direction}
                      disabled={disabled}
                      onChange={event => update(entry.uid, { direction: event.target.value as ReportSortDirection })}
                    >
                      <option value="asc">Ascending</option>
                      <option value="desc">Descending</option>
                    </select>
                  )}
                </FormField>
                <div className="flex sm:col-span-2 sm:justify-end">
                  <button
                    type="button"
                    className={ICON_BUTTON}
                    aria-label={`Remove sort ${n}`}
                    title="Remove"
                    disabled={disabled}
                    onClick={() => onChange(sort.filter(candidate => candidate.uid !== entry.uid))}
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
        disabled={disabled || options.length === 0 || sort.length >= MAX_SORT}
        title={sort.length >= MAX_SORT ? `At most ${MAX_SORT} sort keys` : undefined}
      >
        <PlusIcon className="h-4 w-4" aria-hidden="true" />
        Add sort
      </Button>
    </fieldset>
  );
}
