/**
 * The report builder's editable draft and its translation to and from the wire
 * definition (types/reportBuilder.ts).
 *
 * The draft holds what the user typed (strings, so an input can be half-filled);
 * `buildReportRequest` turns it into a TYPED request — numbers as numbers, dates as
 * `YYYY-MM-DD`, datetimes as UTC instants resolved from a Central wall clock — or
 * says which filter is not yet a value. Everything else (grouping, aggregate and
 * sort rules) is the server's to judge: the builder only offers what is allowed and
 * shows a 422 `detail` verbatim, so the two can never disagree in the client's favor.
 */
import type {
  CustomReportRequest,
  ReportAggregate,
  ReportDataSourceCatalog,
  ReportDefinition,
  ReportField,
  ReportFieldType,
  ReportFilter,
  ReportFilterOperator,
  ReportScalar,
  ReportSortDirection,
} from '../../../types/reportBuilder';
import { centralWallClockToUtcISO, getCentralNowDateTimeLocal } from '../../../utils/centralTime';

export const DEFAULT_LIMIT = 1000;
export const MAX_LIMIT = 10000;

/** The server's request caps (backend schemas/analytics.py + services/report_builder.py). */
export const MAX_COLUMNS = 40;
export const MAX_FILTERS = 20;
export const MAX_GROUP_BY = 10;
export const MAX_SORT = 10;
export const MAX_ALIAS_LENGTH = 100;
export const MAX_TEXT_VALUE_LENGTH = 500;
export const MAX_IN_VALUES = 500;

/**
 * A filter comparison as the builder offers it: a wire operator, or one of the two
 * null tests the server spells as `eq` / `ne` with a null value.
 */
export type DraftOperator = ReportFilterOperator | 'is_empty' | 'is_not_empty';

/** The wire operators each field type allows (the server's rule). */
export const OPERATORS_BY_TYPE: Record<ReportFieldType, ReportFilterOperator[]> = {
  string: ['eq', 'ne', 'in', 'like'],
  number: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'between'],
  date: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between'],
  datetime: ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between'],
  enum: ['eq', 'ne', 'in'],
};

/** What the comparison select offers for a field type: its operators, then the null tests. */
export function draftOperatorsFor(type: ReportFieldType): DraftOperator[] {
  return [...OPERATORS_BY_TYPE[type], 'is_empty', 'is_not_empty'];
}

export function isNullTest(operator: DraftOperator): operator is 'is_empty' | 'is_not_empty' {
  return operator === 'is_empty' || operator === 'is_not_empty';
}

const NUMBER_OPERATOR_LABELS: Record<ReportFilterOperator, string> = {
  eq: 'equals',
  ne: 'does not equal',
  gt: 'greater than',
  gte: 'at least',
  lt: 'less than',
  lte: 'at most',
  in: 'is one of',
  like: 'contains',
  between: 'between',
};

const TIME_OPERATOR_LABELS: Partial<Record<ReportFilterOperator, string>> = {
  eq: 'is on',
  ne: 'is not on',
  gt: 'after',
  gte: 'on or after',
  lt: 'before',
  lte: 'on or before',
};

export function operatorLabel(operator: DraftOperator, type: ReportFieldType): string {
  if (operator === 'is_empty') return 'is empty';
  if (operator === 'is_not_empty') return 'is not empty';
  if (type === 'date' || type === 'datetime') {
    if (type === 'datetime' && (operator === 'eq' || operator === 'ne')) {
      return operator === 'eq' ? 'is at' : 'is not at';
    }
    return TIME_OPERATOR_LABELS[operator] ?? NUMBER_OPERATOR_LABELS[operator];
  }
  if (type === 'string' && operator === 'like') return 'contains';
  return NUMBER_OPERATOR_LABELS[operator];
}

const AGGREGATES_BY_TYPE: Record<ReportFieldType, ReportAggregate[]> = {
  number: ['sum', 'avg', 'min', 'max', 'count'],
  date: ['min', 'max', 'count'],
  datetime: ['min', 'max', 'count'],
  string: ['min', 'max', 'count'],
  enum: ['count'],
};

export const AGGREGATE_LABELS: Record<ReportAggregate, string> = {
  sum: 'Sum',
  avg: 'Average',
  count: 'Count',
  min: 'Minimum',
  max: 'Maximum',
};

export function aggregatesFor(type: ReportFieldType | undefined): ReportAggregate[] {
  return type ? AGGREGATES_BY_TYPE[type] : ['count'];
}

export interface DraftColumn {
  uid: string;
  field: string;
  aggregate: ReportAggregate | '';
  alias: string;
}

export interface DraftFilter {
  uid: string;
  field: string;
  operator: DraftOperator;
  /** The value as typed. For `in` on a string/number field: one value per line. */
  value: string;
  /** The upper bound of `between`. */
  value2: string;
  /** The ticked options of `in` on an enum field. */
  values: string[];
}

export interface DraftSort {
  uid: string;
  field: string;
  direction: ReportSortDirection;
}

export interface BuilderDraft {
  data_source: string;
  columns: DraftColumn[];
  filters: DraftFilter[];
  group_by: string[];
  sort: DraftSort[];
  /** Not part of a saved template: a run-time cap only. */
  limit: string;
}

let uidSeq = 0;
export function nextUid(prefix = 'r'): string {
  uidSeq += 1;
  return `${prefix}${uidSeq}`;
}

export function emptyDraft(): BuilderDraft {
  return { data_source: '', columns: [], filters: [], group_by: [], sort: [], limit: String(DEFAULT_LIMIT) };
}

export function fieldsOf(catalog: ReportDataSourceCatalog, dataSource: string): ReportField[] {
  return catalog[dataSource]?.fields ?? [];
}

export function findField(catalog: ReportDataSourceCatalog, dataSource: string, name: string): ReportField | undefined {
  return fieldsOf(catalog, dataSource).find(field => field.name === name);
}

/** The output key of a column: its alias, else its field. */
export function columnKey(column: Pick<DraftColumn, 'field' | 'alias'>): string {
  return column.alias.trim() || column.field;
}

export function newColumn(field: string): DraftColumn {
  return { uid: nextUid('c'), field, aggregate: '', alias: '' };
}

export function newFilter(field: ReportField | undefined): DraftFilter {
  const operator = field ? OPERATORS_BY_TYPE[field.type][0] : 'eq';
  return { uid: nextUid('f'), field: field?.name ?? '', operator, value: '', value2: '', values: [] };
}

export function newSort(field: string): DraftSort {
  return { uid: nextUid('s'), field, direction: 'asc' };
}

/**
 * The sort fields to offer. Ungrouped: every catalog field plus every selected
 * column's output key (an aggregate is sorted by its key). Grouped: only the
 * group-by fields and the selected columns' keys, as the server requires.
 */
export function sortFieldOptions(
  draft: BuilderDraft,
  catalog: ReportDataSourceCatalog
): Array<{ value: string; label: string }> {
  const fields = fieldsOf(catalog, draft.data_source);
  const labelOf = (name: string) => fields.find(field => field.name === name)?.label ?? name;
  const grouped = isGrouped(draft);
  const options: Array<{ value: string; label: string }> = [];
  const seen = new Set<string>();
  const add = (value: string, label: string) => {
    if (!value || seen.has(value)) return;
    seen.add(value);
    options.push({ value, label });
  };
  draft.columns.forEach(column => {
    const key = columnKey(column);
    const base = labelOf(column.field);
    const label = column.aggregate
      ? `${column.alias.trim() || `${AGGREGATE_LABELS[column.aggregate]} of ${base}`} (column)`
      : column.alias.trim()
        ? `${column.alias.trim()} (column)`
        : base;
    add(key, label);
  });
  if (grouped) {
    draft.group_by.forEach(name => add(name, labelOf(name)));
  } else {
    fields.forEach(field => add(field.name, field.label));
  }
  return options;
}

export function isGrouped(draft: Pick<BuilderDraft, 'columns' | 'group_by'>): boolean {
  return draft.group_by.length > 0 || draft.columns.some(column => column.aggregate !== '');
}

/**
 * Non-aggregated columns missing from group-by while the report is grouped — the
 * server will refuse them. Advisory only (the server is the authority); the builder
 * offers a one-click fix.
 */
export function ungroupedColumns(draft: BuilderDraft): string[] {
  if (!isGrouped(draft)) return [];
  const missing: string[] = [];
  draft.columns.forEach(column => {
    if (column.aggregate || !column.field) return;
    if (!draft.group_by.includes(column.field) && !missing.includes(column.field)) missing.push(column.field);
  });
  return missing;
}

/** Output keys used by more than one column (they must be unique). */
export function duplicateColumnKeys(draft: BuilderDraft): string[] {
  const counts = new Map<string, number>();
  draft.columns.forEach(column => {
    const key = columnKey(column);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  });
  const duplicates: string[] = [];
  counts.forEach((count, key) => {
    if (count > 1 && key) duplicates.push(key);
  });
  return duplicates;
}

const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

type Conversion = { ok: true; value: ReportScalar } | { ok: false; message: string };

function convertScalar(text: string, field: ReportField): Conversion {
  switch (field.type) {
    case 'number': {
      const trimmed = text.trim();
      const value = Number(trimmed);
      if (!trimmed || !Number.isFinite(value)) return { ok: false, message: 'Enter a number.' };
      return { ok: true, value };
    }
    case 'date':
      return DATE_PATTERN.test(text) ? { ok: true, value: text } : { ok: false, message: 'Pick a date.' };
    case 'datetime': {
      const instant = centralWallClockToUtcISO(text);
      return instant ? { ok: true, value: instant } : { ok: false, message: 'Pick a date and time.' };
    }
    case 'enum':
      if (!text) return { ok: false, message: 'Pick a value.' };
      if (field.options && !field.options.includes(text)) {
        return { ok: false, message: `"${text}" is not one of this field's values.` };
      }
      return { ok: true, value: text };
    default:
      if (!text) return { ok: false, message: 'Enter a value.' };
      if (text.length > MAX_TEXT_VALUE_LENGTH) {
        return { ok: false, message: `A text value may be at most ${MAX_TEXT_VALUE_LENGTH} characters.` };
      }
      return { ok: true, value: text };
  }
}

function convertFilter(filter: DraftFilter, field: ReportField | undefined): ReportFilter | string {
  if (!field) return 'This field is no longer available; remove the filter.';
  if (isNullTest(filter.operator)) {
    return { field: field.name, operator: filter.operator === 'is_empty' ? 'eq' : 'ne', value: null };
  }
  if (!OPERATORS_BY_TYPE[field.type].includes(filter.operator)) return 'Pick a comparison this field allows.';
  if (filter.operator === 'in') {
    const raw =
      field.type === 'enum'
        ? filter.values
        : filter.value
            .split('\n')
            .map(line => (field.type === 'number' ? line.trim() : line))
            .filter(line => line.trim() !== '');
    if (raw.length === 0) return 'Enter at least one value.';
    if (raw.length > MAX_IN_VALUES) return `"Is one of" takes at most ${MAX_IN_VALUES} values.`;
    const values: ReportScalar[] = [];
    for (const text of raw) {
      const converted = convertScalar(text, field);
      if (!converted.ok) return `"${text}": ${converted.message}`;
      values.push(converted.value);
    }
    return { field: field.name, operator: 'in', value: values };
  }
  const operator: ReportFilterOperator = filter.operator;
  const first = convertScalar(filter.value, field);
  if (!first.ok) return operator === 'between' ? `From: ${first.message}` : first.message;
  if (operator === 'between') {
    const second = convertScalar(filter.value2, field);
    if (!second.ok) return `To: ${second.message}`;
    return { field: field.name, operator: 'between', value: first.value, value2: second.value };
  }
  return { field: field.name, operator, value: first.value };
}

export type BuildResult =
  | { ok: true; request: CustomReportRequest; definition: ReportDefinition }
  | { ok: false; filterErrors: Record<string, string>; errors: string[] };

/** The typed request for a draft, or what still has to be filled in. */
export function buildReportRequest(draft: BuilderDraft, catalog: ReportDataSourceCatalog): BuildResult {
  const errors: string[] = [];
  const filterErrors: Record<string, string> = {};
  if (!draft.data_source) errors.push('Choose a data source.');
  if (draft.columns.length === 0) errors.push('Add at least one column.');
  // Only a stored template can exceed these (the Add buttons stop at each cap).
  const caps: Array<[number, number, string]> = [
    [draft.columns.length, MAX_COLUMNS, 'columns'],
    [draft.filters.length, MAX_FILTERS, 'filters'],
    [draft.group_by.length, MAX_GROUP_BY, 'group-by fields'],
    [draft.sort.length, MAX_SORT, 'sort keys'],
  ];
  caps.forEach(([count, max, noun]) => {
    if (count > max) errors.push(`A report may have at most ${max} ${noun}; remove ${count - max}.`);
  });
  draft.columns.forEach((column, index) => {
    if (column.alias.trim().length > MAX_ALIAS_LENGTH) {
      errors.push(`Column ${index + 1}: a label may be at most ${MAX_ALIAS_LENGTH} characters.`);
    }
  });
  const filters: ReportFilter[] = [];
  draft.filters.forEach(filter => {
    const converted = convertFilter(filter, findField(catalog, draft.data_source, filter.field));
    if (typeof converted === 'string') filterErrors[filter.uid] = converted;
    else filters.push(converted);
  });
  const limitText = draft.limit.trim();
  const limit = limitText === '' ? DEFAULT_LIMIT : Number(limitText);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_LIMIT) {
    errors.push(`Row limit must be a whole number from 1 to ${MAX_LIMIT.toLocaleString()}.`);
  }
  if (Object.keys(filterErrors).length > 0) errors.push('Finish or remove the highlighted filters.');
  if (errors.length > 0) return { ok: false, filterErrors, errors };
  const definition: ReportDefinition = {
    data_source: draft.data_source,
    columns: draft.columns.map(column => {
      const alias = column.alias.trim();
      return {
        field: column.field,
        ...(alias ? { alias } : {}),
        ...(column.aggregate ? { aggregate: column.aggregate } : {}),
      };
    }),
    filters,
    group_by: draft.group_by.map(field => ({ field })),
    sort: draft.sort.filter(sort => sort.field).map(sort => ({ field: sort.field, direction: sort.direction })),
  };
  return { ok: true, definition, request: { ...definition, limit } };
}

function scalarToText(value: unknown, field: ReportField | undefined): string {
  if (value === null || value === undefined) return '';
  if (field?.type === 'datetime' && typeof value === 'string') return getCentralNowDateTimeLocal(value);
  return String(value);
}

/** A stored definition (a saved template) as an editable draft. */
export function draftFromDefinition(definition: ReportDefinition, catalog: ReportDataSourceCatalog): BuilderDraft {
  const fieldOf = (name: string) => findField(catalog, definition.data_source, name);
  return {
    data_source: definition.data_source,
    columns: (definition.columns ?? []).map(column => ({
      uid: nextUid('c'),
      field: column.field,
      aggregate: column.aggregate ?? '',
      alias: column.alias ?? '',
    })),
    filters: (definition.filters ?? []).map(filter => {
      const field = fieldOf(filter.field);
      const list = Array.isArray(filter.value) ? filter.value : null;
      const isEnumIn = filter.operator === 'in' && field?.type === 'enum';
      const nullTest = filter.value === null && (filter.operator === 'eq' || filter.operator === 'ne');
      const operator: DraftOperator = nullTest
        ? filter.operator === 'eq'
          ? 'is_empty'
          : 'is_not_empty'
        : filter.operator;
      return {
        uid: nextUid('f'),
        field: filter.field,
        operator,
        value: list
          ? isEnumIn
            ? ''
            : list.map(item => scalarToText(item, field)).join('\n')
          : scalarToText(filter.value, field),
        value2: scalarToText(filter.value2, field),
        values: list && isEnumIn ? list.map(String) : [],
      };
    }),
    group_by: (definition.group_by ?? []).map(group => group.field),
    sort: (definition.sort ?? []).map(sort => ({ uid: nextUid('s'), field: sort.field, direction: sort.direction })),
    limit: String(DEFAULT_LIMIT),
  };
}

/** The draft with row ids and the run-time limit stripped: what "unsaved changes" compares. */
export function draftSignature(draft: BuilderDraft): string {
  return JSON.stringify({
    data_source: draft.data_source,
    columns: draft.columns.map(({ field, aggregate, alias }) => [field, aggregate, alias.trim()]),
    filters: draft.filters.map(({ field, operator, value, value2, values }) => [
      field,
      operator,
      value,
      value2,
      values,
    ]),
    group_by: draft.group_by,
    sort: draft.sort.map(({ field, direction }) => [field, direction]),
  });
}
