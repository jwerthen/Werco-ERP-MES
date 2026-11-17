import React from 'react';
import { PlusIcon, TrashIcon } from '@heroicons/react/24/outline';
import { Button, FormField } from '../../ui';
import type { ReportField } from '../../../types/reportBuilder';
import {
  MAX_FILTERS,
  MAX_TEXT_VALUE_LENGTH,
  draftOperatorsFor,
  isNullTest,
  operatorLabel,
  type DraftFilter,
  type DraftOperator,
} from './reportDefinition';
import { fieldOptions } from './ColumnsEditor';
import { COMPACT_INPUT, ICON_BUTTON, LEGEND, SECTION } from './builderStyles';

interface FiltersEditorProps {
  fields: ReportField[];
  filters: DraftFilter[];
  /** Per-filter problems from the last Run attempt, keyed by row uid. */
  errors: Record<string, string>;
  onChange: (filters: DraftFilter[]) => void;
  onAdd: () => void;
  disabled?: boolean;
}

function inputType(field: ReportField | undefined): string {
  switch (field?.type) {
    case 'number':
      return 'number';
    case 'date':
      return 'date';
    case 'datetime':
      return 'datetime-local';
    default:
      return 'text';
  }
}

interface ScalarInputProps {
  label: string;
  field: ReportField | undefined;
  value: string;
  disabled?: boolean;
  invalid: boolean;
  onChange: (value: string) => void;
}

/** One typed value: a select of the options for an enum, else a number / date / datetime / text input. */
function ScalarInput({ label, field, value, disabled, invalid, onChange }: ScalarInputProps) {
  const help = field?.type === 'datetime' ? 'Central time' : undefined;
  return (
    <FormField label={label} labelClassName="text-xs" help={help} className="min-w-0 flex-1">
      {props =>
        field?.type === 'enum' ? (
          <select
            {...props}
            aria-invalid={invalid || undefined}
            className={COMPACT_INPUT}
            value={value}
            disabled={disabled}
            onChange={event => onChange(event.target.value)}
          >
            <option value="">Choose…</option>
            {(field.options ?? []).map(option => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        ) : (
          <input
            {...props}
            aria-invalid={invalid || undefined}
            type={inputType(field)}
            step={field?.type === 'number' ? 'any' : undefined}
            maxLength={field?.type === 'string' ? MAX_TEXT_VALUE_LENGTH : undefined}
            className={COMPACT_INPUT}
            value={value}
            disabled={disabled}
            onChange={event => onChange(event.target.value)}
          />
        )
      }
    </FormField>
  );
}

/**
 * Typed filters. The comparisons offered follow the field's type (a string gets
 * equals / contains / one of, a date gets before / after / between, ...), and the
 * value input follows it too: `between` shows two inputs, `in` a list.
 */
export function FiltersEditor({ fields, filters, errors, onChange, onAdd, disabled }: FiltersEditorProps) {
  const update = (uid: string, changes: Partial<DraftFilter>) =>
    onChange(filters.map(filter => (filter.uid === uid ? { ...filter, ...changes } : filter)));

  return (
    <fieldset className={SECTION}>
      <legend className={LEGEND}>Filters</legend>
      {filters.length === 0 ? (
        <p className="mt-2 text-sm text-fd-mute">No filters: every record in the data source is included.</p>
      ) : (
        <ol className="mt-2 space-y-3">
          {filters.map((filter, index) => {
            const field = fields.find(candidate => candidate.name === filter.field);
            const allowed = field ? draftOperatorsFor(field.type) : [];
            // A stored operator this field no longer allows stays visible (and is refused at Run).
            const operators = allowed.includes(filter.operator) ? allowed : [filter.operator, ...allowed];
            const n = index + 1;
            const error = errors[filter.uid];
            const errorId = `report-filter-${filter.uid}-error`;
            return (
              <li key={filter.uid}>
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-12 sm:items-start">
                  <FormField label={`Filter ${n} field`} className="sm:col-span-3" labelClassName="text-xs">
                    {props => (
                      <select
                        {...props}
                        className={COMPACT_INPUT}
                        value={filter.field}
                        disabled={disabled}
                        onChange={event => {
                          const next = fields.find(candidate => candidate.name === event.target.value);
                          const nextAllowed = next ? draftOperatorsFor(next.type) : [];
                          const sameType = next?.type === field?.type;
                          update(filter.uid, {
                            field: event.target.value,
                            operator: nextAllowed.includes(filter.operator)
                              ? filter.operator
                              : (nextAllowed[0] ?? 'eq'),
                            // A value typed for another type means nothing to this one.
                            ...(sameType ? {} : { value: '', value2: '', values: [] }),
                          });
                        }}
                      >
                        {fieldOptions(fields, filter.field).map(option => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <FormField label={`Filter ${n} comparison`} className="sm:col-span-3" labelClassName="text-xs">
                    {props => (
                      <select
                        {...props}
                        className={COMPACT_INPUT}
                        value={filter.operator}
                        disabled={disabled}
                        onChange={event => update(filter.uid, { operator: event.target.value as DraftOperator })}
                      >
                        {operators.map(operator => (
                          <option key={operator} value={operator}>
                            {allowed.includes(operator)
                              ? operatorLabel(operator, field?.type ?? 'string')
                              : `${operator} (not allowed)`}
                          </option>
                        ))}
                      </select>
                    )}
                  </FormField>
                  <div className="flex min-w-0 flex-col gap-2 sm:col-span-5 sm:flex-row">
                    {isNullTest(filter.operator) ? (
                      <p className="self-end pb-2 text-xs text-fd-mute">No value needed.</p>
                    ) : filter.operator === 'in' && field?.type === 'enum' ? (
                      <fieldset className="min-w-0 flex-1">
                        <legend className="label text-xs">{`Filter ${n} values`}</legend>
                        <div className="flex flex-wrap gap-x-4 gap-y-1">
                          {(field.options ?? []).map(option => (
                            <label key={option} className="inline-flex items-center gap-1.5 text-sm text-fd-body">
                              <input
                                type="checkbox"
                                className="checkbox h-4 w-4"
                                checked={filter.values.includes(option)}
                                disabled={disabled}
                                onChange={event =>
                                  update(filter.uid, {
                                    values: event.target.checked
                                      ? [...filter.values, option]
                                      : filter.values.filter(value => value !== option),
                                  })
                                }
                              />
                              {option}
                            </label>
                          ))}
                        </div>
                      </fieldset>
                    ) : filter.operator === 'in' ? (
                      <FormField
                        label={`Filter ${n} values`}
                        labelClassName="text-xs"
                        help="One value per line"
                        className="min-w-0 flex-1"
                      >
                        {props => (
                          <textarea
                            {...props}
                            aria-invalid={Boolean(error) || undefined}
                            className={`${COMPACT_INPUT} min-h-[72px]`}
                            rows={3}
                            value={filter.value}
                            disabled={disabled}
                            onChange={event => update(filter.uid, { value: event.target.value })}
                          />
                        )}
                      </FormField>
                    ) : filter.operator === 'between' ? (
                      <>
                        <ScalarInput
                          label={`Filter ${n} from`}
                          field={field}
                          value={filter.value}
                          disabled={disabled}
                          invalid={Boolean(error)}
                          onChange={value => update(filter.uid, { value })}
                        />
                        <ScalarInput
                          label={`Filter ${n} to`}
                          field={field}
                          value={filter.value2}
                          disabled={disabled}
                          invalid={Boolean(error)}
                          onChange={value2 => update(filter.uid, { value2 })}
                        />
                      </>
                    ) : (
                      <ScalarInput
                        label={`Filter ${n} value`}
                        field={field}
                        value={filter.value}
                        disabled={disabled}
                        invalid={Boolean(error)}
                        onChange={value => update(filter.uid, { value })}
                      />
                    )}
                  </div>
                  <div className="flex sm:col-span-1 sm:justify-end sm:pt-6">
                    <button
                      type="button"
                      className={ICON_BUTTON}
                      aria-label={`Remove filter ${n}`}
                      title="Remove"
                      disabled={disabled}
                      onClick={() => onChange(filters.filter(candidate => candidate.uid !== filter.uid))}
                    >
                      <TrashIcon className="h-4 w-4" aria-hidden="true" />
                    </button>
                  </div>
                </div>
                {error && (
                  <p id={errorId} role="alert" className="mt-1 text-xs text-fd-red">
                    {`Filter ${n}: ${error}`}
                  </p>
                )}
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
        disabled={disabled || fields.length === 0 || filters.length >= MAX_FILTERS}
        title={filters.length >= MAX_FILTERS ? `At most ${MAX_FILTERS} filters` : undefined}
      >
        <PlusIcon className="h-4 w-4" aria-hidden="true" />
        Add filter
      </Button>
    </fieldset>
  );
}

export default FiltersEditor;
