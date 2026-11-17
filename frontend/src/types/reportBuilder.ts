/**
 * Custom report builder: the wire contract for `/api/v1/analytics` data sources,
 * ad-hoc runs and saved report templates.
 *
 * Every report endpoint is ADMIN / MANAGER except `GET /custom-report/templates`
 * (any role). The UI gates the screen on `analytics:export`, which is exactly that
 * role set. The server is the authority on every rule below; the builder only
 * offers what the rules allow and shows a 422 `detail` verbatim.
 */

/** A catalog field's type. Decides the operators, aggregates and value input offered. */
export type ReportFieldType = 'string' | 'number' | 'date' | 'datetime' | 'enum';

export interface ReportField {
  name: string;
  label: string;
  type: ReportFieldType;
  /** The allowed values of an `enum` field. */
  options?: string[];
}

export interface ReportDataSource {
  label: string;
  fields: ReportField[];
}

/** `GET /analytics/data-sources`, keyed by data-source id (work_orders, parts, ...). */
export type ReportDataSourceCatalog = Record<string, ReportDataSource>;

/** sum / avg: number fields only. min / max: number, date, datetime, string. count: any. */
export type ReportAggregate = 'sum' | 'avg' | 'count' | 'min' | 'max';

export type ReportFilterOperator = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | 'like' | 'between';

export type ReportSortDirection = 'asc' | 'desc';

/**
 * A typed filter value: a JS number for a number field, `YYYY-MM-DD` for a date, a
 * full ISO instant for a datetime, one of `options` for an enum.
 */
export type ReportScalar = string | number;

export interface ReportColumn {
  field: string;
  /** Renames the output key. A stored template may carry null. */
  alias?: string | null;
  aggregate?: ReportAggregate | null;
}

export interface ReportFilter {
  field: string;
  operator: ReportFilterOperator;
  /** A non-empty array for `in`; null only with eq / ne (is empty / is not empty); a scalar otherwise. */
  value: ReportScalar | ReportScalar[] | null;
  /** The upper bound of `between` (0 is a valid value). */
  value2?: ReportScalar | null;
}

export interface ReportGroupBy {
  field: string;
  /** Reserved by the server; must be null (never sent by the builder). */
  granularity?: string | null;
}

export interface ReportSort {
  /** A catalog field, or the output key of a selected column (how an aggregate is sorted). */
  field: string;
  direction: ReportSortDirection;
}

/** What a template stores, and what a run is made of (less the row limit). */
export interface ReportDefinition {
  data_source: string;
  columns: ReportColumn[];
  filters: ReportFilter[];
  group_by: ReportGroupBy[];
  sort: ReportSort[];
}

/** `POST /analytics/custom-report`. `limit` is 1-10,000, default 1,000. */
export interface CustomReportRequest extends ReportDefinition {
  limit?: number;
}

export interface ReportResultColumn {
  /** The output key: the column's alias, else its field. */
  key: string;
  label: string;
  type: ReportFieldType;
}

/** One result row, keyed by `ReportResultColumn.key`. Datetimes arrive as UTC `Z` strings. */
export type ReportRow = Record<string, unknown>;

export interface ReportResultNote {
  code: string;
  message: string;
  fields: string[];
}

export interface CustomReportResult {
  columns: ReportResultColumn[];
  rows: ReportRow[];
  row_count: number;
  /** True when the row limit cut the result short. */
  truncated: boolean;
  notes: ReportResultNote[];
}

export interface ReportTemplate extends ReportDefinition {
  id: number;
  name: string;
  description: string | null;
  is_shared: boolean;
  created_by: number;
  created_at: string;
  updated_at: string | null;
  /** Why the stored definition no longer runs (a field or rule changed). Empty when it runs. */
  definition_errors: string[];
}

/** `POST /analytics/custom-report/templates`. `name` is 1-255 characters, `description` at most 2,000. */
export interface ReportTemplateCreate extends ReportDefinition {
  name: string;
  description?: string | null;
  is_shared: boolean;
}

/** `PATCH /analytics/custom-report/templates/{id}` (the author or an Admin). */
export interface ReportTemplateUpdate {
  name?: string;
  description?: string | null;
  is_shared?: boolean;
  definition?: ReportDefinition;
}
