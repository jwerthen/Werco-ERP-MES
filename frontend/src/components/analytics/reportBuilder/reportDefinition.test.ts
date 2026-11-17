import type { ReportDataSourceCatalog, ReportDefinition } from '../../../types/reportBuilder';
import {
  OPERATORS_BY_TYPE,
  aggregatesFor,
  buildReportRequest,
  draftFromDefinition,
  draftOperatorsFor,
  draftSignature,
  duplicateColumnKeys,
  emptyDraft,
  newColumn,
  newFilter,
  retargetFilter,
  sortFieldOptions,
  staleEnumValues,
  ungroupedColumns,
  type BuilderDraft,
  type DraftFilter,
} from './reportDefinition';
import { chartSpec } from './ReportChart';
import { formatReportValue } from './formatReportValue';

const catalog: ReportDataSourceCatalog = {
  work_orders: {
    label: 'Work Orders',
    fields: [
      { name: 'work_order_number', label: 'Work Order #', type: 'string' },
      { name: 'status', label: 'Status', type: 'enum', options: ['draft', 'released', 'complete'] },
      { name: 'quantity_ordered', label: 'Qty Ordered', type: 'number' },
      { name: 'due_date', label: 'Due Date', type: 'date' },
      { name: 'created_at', label: 'Created', type: 'datetime' },
      { name: 'stage', label: 'Stage', type: 'enum', options: ['queued', 'released', 'shipped'] },
    ],
  },
};

const field = (name: string) => catalog.work_orders.fields.find(candidate => candidate.name === name);

const draftWith = (filters: DraftFilter[], extra: Partial<BuilderDraft> = {}): BuilderDraft => ({
  ...emptyDraft(),
  data_source: 'work_orders',
  columns: [newColumn('work_order_number')],
  filters,
  ...extra,
});

const filterOn = (name: string, changes: Partial<DraftFilter>): DraftFilter => ({
  ...newFilter(field(name)),
  ...changes,
});

describe('operators and aggregates per field type', () => {
  it('offers exactly the server-allowed operators, plus the two null tests', () => {
    expect(OPERATORS_BY_TYPE.string).toEqual(['eq', 'ne', 'in', 'like']);
    expect(OPERATORS_BY_TYPE.number).toEqual(['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'between']);
    expect(OPERATORS_BY_TYPE.date).toEqual(['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'between']);
    expect(OPERATORS_BY_TYPE.datetime).toEqual(OPERATORS_BY_TYPE.date);
    expect(OPERATORS_BY_TYPE.enum).toEqual(['eq', 'ne', 'in']);
    expect(draftOperatorsFor('enum')).toEqual(['eq', 'ne', 'in', 'is_empty', 'is_not_empty']);
  });

  it('offers sum/avg on numbers only and count everywhere', () => {
    expect(aggregatesFor('number')).toEqual(['sum', 'avg', 'min', 'max', 'count']);
    expect(aggregatesFor('date')).toEqual(['min', 'max', 'count']);
    expect(aggregatesFor('string')).toEqual(['min', 'max', 'count']);
    expect(aggregatesFor('enum')).toEqual(['count']);
  });
});

describe('buildReportRequest', () => {
  it('types each value: number, date, enum, datetime from a Central wall clock', () => {
    const built = buildReportRequest(
      draftWith([
        filterOn('quantity_ordered', { operator: 'gte', value: ' 12.5 ' }),
        filterOn('due_date', { operator: 'lt', value: '2026-12-01' }),
        filterOn('status', { operator: 'eq', value: 'released' }),
        // 08:00 CDT (DST began 2026-03-08) is 13:00 UTC.
        filterOn('created_at', { operator: 'gte', value: '2026-03-10T08:00' }),
      ]),
      catalog
    );
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.request.filters).toEqual([
      { field: 'quantity_ordered', operator: 'gte', value: 12.5 },
      { field: 'due_date', operator: 'lt', value: '2026-12-01' },
      { field: 'status', operator: 'eq', value: 'released' },
      { field: 'created_at', operator: 'gte', value: '2026-03-10T13:00:00.000Z' },
    ]);
    expect(built.request.limit).toBe(1000);
  });

  it('between sends value and value2, and 0 is a valid bound', () => {
    const built = buildReportRequest(
      draftWith([filterOn('quantity_ordered', { operator: 'between', value: '0', value2: '0' })]),
      catalog
    );
    expect(built.ok && built.request.filters).toEqual([
      { field: 'quantity_ordered', operator: 'between', value: 0, value2: 0 },
    ]);
  });

  it('in sends a non-empty typed list: lines for string/number, ticked options for enum', () => {
    const built = buildReportRequest(
      draftWith([
        filterOn('work_order_number', { operator: 'in', value: 'WO-1001\n\nWO-1002\n' }),
        filterOn('quantity_ordered', { operator: 'in', value: '5\n 10 ' }),
        filterOn('status', { operator: 'in', values: ['draft', 'released'] }),
      ]),
      catalog
    );
    expect(built.ok && built.request.filters).toEqual([
      { field: 'work_order_number', operator: 'in', value: ['WO-1001', 'WO-1002'] },
      { field: 'quantity_ordered', operator: 'in', value: [5, 10] },
      { field: 'status', operator: 'in', value: ['draft', 'released'] },
    ]);
  });

  it('is empty / is not empty send eq / ne with a null value', () => {
    const built = buildReportRequest(
      draftWith([filterOn('due_date', { operator: 'is_empty' }), filterOn('status', { operator: 'is_not_empty' })]),
      catalog
    );
    expect(built.ok && built.request.filters).toEqual([
      { field: 'due_date', operator: 'eq', value: null },
      { field: 'status', operator: 'ne', value: null },
    ]);
  });

  it('reports what is not yet a value, per filter, and does not build', () => {
    const empty = filterOn('quantity_ordered', { operator: 'eq', value: '' });
    const words = filterOn('quantity_ordered', { operator: 'in', value: '4\nabc' });
    const openRange = filterOn('due_date', { operator: 'between', value: '2026-01-01', value2: '' });
    const noList = filterOn('status', { operator: 'in', values: [] });
    const built = buildReportRequest(draftWith([empty, words, openRange, noList], { limit: '20000' }), catalog);
    expect(built.ok).toBe(false);
    if (built.ok) return;
    expect(built.filterErrors).toEqual({
      [empty.uid]: 'Enter a number.',
      [words.uid]: '"abc": Enter a number.',
      [openRange.uid]: 'To: Pick a date.',
      [noList.uid]: 'Enter at least one value.',
    });
    expect(built.errors).toEqual([
      'Row limit must be a whole number from 1 to 10,000.',
      'Finish or remove the highlighted filters.',
    ]);
  });

  it("holds the server's caps: text length, list size, row counts and label length", () => {
    const longText = filterOn('work_order_number', { operator: 'eq', value: 'x'.repeat(501) });
    const bigList = filterOn('quantity_ordered', {
      operator: 'in',
      value: Array.from({ length: 501 }, (_, index) => String(index)).join('\n'),
    });
    const tooMany = Array.from({ length: 21 }, () => filterOn('due_date', { operator: 'is_empty' }));
    const built = buildReportRequest(
      draftWith([longText, bigList, ...tooMany], {
        columns: [{ uid: 'a', field: 'status', aggregate: '', alias: 'y'.repeat(101) }],
      }),
      catalog
    );
    expect(built.ok).toBe(false);
    if (built.ok) return;
    expect(built.filterErrors[longText.uid]).toBe('A text value may be at most 500 characters.');
    expect(built.filterErrors[bigList.uid]).toBe('"Is one of" takes at most 500 values.');
    expect(built.errors).toEqual(
      expect.arrayContaining([
        'A report may have at most 20 filters; remove 3.',
        'Column 1: a label may be at most 100 characters.',
      ])
    );
  });

  it('sends value2 only for between and never a UI-only key', () => {
    const typed = filterOn('quantity_ordered', { operator: 'eq', value: '3', value2: '9' });
    const built = buildReportRequest(
      draftWith([typed], { group_by: ['work_order_number'], sort: [{ uid: 's1', field: 'status', direction: 'asc' }] }),
      catalog
    );
    expect(built.ok).toBe(true);
    if (!built.ok) return;
    expect(built.request.filters).toEqual([{ field: 'quantity_ordered', operator: 'eq', value: 3 }]);
    const items = [
      ...built.request.columns,
      ...built.request.filters,
      ...built.request.group_by,
      ...built.request.sort,
    ];
    items.forEach(item => expect(Object.keys(item)).not.toEqual(expect.arrayContaining(['uid'])));
    expect(built.request.columns).toEqual([{ field: 'work_order_number' }]);
    expect(built.request.group_by).toEqual([{ field: 'work_order_number' }]);
    expect(built.request.sort).toEqual([{ field: 'status', direction: 'asc' }]);
  });

  it('needs a data source and a column', () => {
    const built = buildReportRequest(emptyDraft(), catalog);
    expect(!built.ok && built.errors).toEqual(['Choose a data source.', 'Add at least one column.']);
  });

  it('sends alias and aggregate only when set, and group_by as objects', () => {
    const built = buildReportRequest(
      {
        ...emptyDraft(),
        data_source: 'work_orders',
        columns: [
          { uid: 'a', field: 'status', aggregate: '', alias: '  ' },
          { uid: 'b', field: 'quantity_ordered', aggregate: 'sum', alias: ' total_qty ' },
        ],
        group_by: ['status'],
        sort: [{ uid: 's', field: 'total_qty', direction: 'desc' }],
        limit: '250',
      },
      catalog
    );
    expect(built.ok && built.request).toEqual({
      data_source: 'work_orders',
      columns: [{ field: 'status' }, { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' }],
      filters: [],
      group_by: [{ field: 'status' }],
      sort: [{ field: 'total_qty', direction: 'desc' }],
      limit: 250,
    });
  });
});

describe('draftFromDefinition', () => {
  const stored: ReportDefinition = {
    data_source: 'work_orders',
    columns: [
      { field: 'status', alias: null, aggregate: null },
      { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' },
    ],
    filters: [
      { field: 'created_at', operator: 'gte', value: '2026-03-10T13:00:00Z' },
      { field: 'status', operator: 'in', value: ['draft', 'released'] },
      { field: 'work_order_number', operator: 'in', value: ['WO-1', 'WO-2'] },
      { field: 'quantity_ordered', operator: 'between', value: 0, value2: 50 },
      { field: 'due_date', operator: 'eq', value: null },
    ],
    group_by: [{ field: 'status', granularity: null }],
    sort: [{ field: 'total_qty', direction: 'desc' }],
  };

  it('round-trips a stored definition through the draft unchanged', () => {
    const draft = draftFromDefinition(stored, catalog);
    expect(draft.filters.map(filter => [filter.operator, filter.value, filter.value2, filter.values])).toEqual([
      ['gte', '2026-03-10T08:00', '', []],
      ['in', '', '', ['draft', 'released']],
      ['in', 'WO-1\nWO-2', '', []],
      ['between', '0', '50', []],
      ['is_empty', '', '', []],
    ]);
    const built = buildReportRequest(draft, catalog);
    expect(built.ok && built.definition).toEqual({
      ...stored,
      columns: [{ field: 'status' }, { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' }],
      filters: [
        { field: 'created_at', operator: 'gte', value: '2026-03-10T13:00:00.000Z' },
        { field: 'status', operator: 'in', value: ['draft', 'released'] },
        { field: 'work_order_number', operator: 'in', value: ['WO-1', 'WO-2'] },
        { field: 'quantity_ordered', operator: 'between', value: 0, value2: 50 },
        { field: 'due_date', operator: 'eq', value: null },
      ],
      group_by: [{ field: 'status' }],
    });
  });

  it('gives a loaded template the same signature twice (no phantom unsaved changes)', () => {
    expect(draftSignature(draftFromDefinition(stored, catalog))).toBe(
      draftSignature(draftFromDefinition(stored, catalog))
    );
    const edited = draftFromDefinition(stored, catalog);
    edited.group_by = [];
    expect(draftSignature(edited)).not.toBe(draftSignature(draftFromDefinition(stored, catalog)));
  });
});

describe('retargetFilter', () => {
  const fields = catalog.work_orders.fields;

  it('keeps an enum value only when it is an option of the new enum field', () => {
    const moved = retargetFilter(
      filterOn('status', { operator: 'in', values: ['draft', 'released'] }),
      fields,
      'stage'
    );
    expect(moved).toMatchObject({ field: 'stage', operator: 'in', values: ['released'] });
    expect(retargetFilter(filterOn('status', { operator: 'eq', value: 'complete' }), fields, 'stage')).toMatchObject({
      field: 'stage',
      operator: 'eq',
      value: '',
    });
    expect(retargetFilter(filterOn('status', { operator: 'ne', value: 'released' }), fields, 'stage')).toMatchObject({
      operator: 'ne',
      value: 'released',
    });
  });

  it('keeps a value between fields of one non-enum type, and clears it across types', () => {
    const typed = filterOn('due_date', { operator: 'between', value: '2026-01-01', value2: '2026-02-01' });
    const withShipDate = [...fields, { name: 'ship_date', label: 'Ship Date', type: 'date' as const }];
    expect(retargetFilter(typed, withShipDate, 'ship_date')).toMatchObject({
      field: 'ship_date',
      operator: 'between',
      value: '2026-01-01',
      value2: '2026-02-01',
    });
    // date -> number: the comparison is allowed there, the typed dates are not numbers.
    expect(retargetFilter(typed, fields, 'quantity_ordered')).toMatchObject({
      field: 'quantity_ordered',
      operator: 'between',
      value: '',
      value2: '',
      values: [],
    });
    // number -> enum: `between` is not an enum comparison; fall back to the first one.
    expect(retargetFilter(filterOn('quantity_ordered', { operator: 'between' }), fields, 'status')).toMatchObject({
      operator: 'eq',
    });
  });
});

describe('staleEnumValues', () => {
  it('names the values that are not current options, once each and never the empty choice', () => {
    expect(staleEnumValues(field('status'), ['IN_PROGRESS', 'released', 'IN_PROGRESS', '', 'ON_HOLD'])).toEqual([
      'IN_PROGRESS',
      'ON_HOLD',
    ]);
    expect(staleEnumValues(field('status'), ['draft'])).toEqual([]);
  });
});

describe('grouping helpers', () => {
  const grouped: BuilderDraft = {
    ...emptyDraft(),
    data_source: 'work_orders',
    columns: [
      { uid: 'a', field: 'status', aggregate: '', alias: '' },
      { uid: 'b', field: 'due_date', aggregate: '', alias: '' },
      { uid: 'c', field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' },
    ],
    group_by: ['status'],
  };

  it('names non-summarized columns missing from group-by', () => {
    expect(ungroupedColumns(grouped)).toEqual(['due_date']);
    expect(ungroupedColumns({ ...grouped, columns: grouped.columns.slice(0, 2), group_by: [] })).toEqual([]);
  });

  it('flags duplicate output keys', () => {
    expect(duplicateColumnKeys(grouped)).toEqual([]);
    // An alias collides with another column's field name just as much as two fields do.
    expect(duplicateColumnKeys({ ...grouped, columns: [...grouped.columns, newColumn('total_qty')] })).toEqual([
      'total_qty',
    ]);
    expect(
      duplicateColumnKeys({
        ...grouped,
        columns: [...grouped.columns, { uid: 'd', field: 'status', aggregate: '', alias: '' }],
      })
    ).toEqual(['status']);
  });

  it('offers only group-by fields and column keys as sort fields when grouped', () => {
    expect(sortFieldOptions(grouped, catalog).map(option => option.value)).toEqual(['status', 'due_date', 'total_qty']);
    const ungrouped = { ...grouped, columns: grouped.columns.slice(0, 1), group_by: [] };
    expect(sortFieldOptions(ungrouped, catalog).map(option => option.value)).toEqual([
      'status',
      'work_order_number',
      'quantity_ordered',
      'due_date',
      'created_at',
      'stage',
    ]);
  });
});

describe('chartSpec', () => {
  const definition: ReportDefinition = {
    data_source: 'work_orders',
    columns: [
      { field: 'due_date', alias: 'due' },
      { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' },
    ],
    filters: [],
    group_by: [{ field: 'due_date' }],
    sort: [],
  };
  const result = {
    columns: [
      { key: 'due', label: 'Due', type: 'date' as const },
      { key: 'total_qty', label: 'Total qty', type: 'number' as const },
    ],
    rows: [{ due: '2026-11-02', total_qty: 4 }],
    row_count: 1,
    truncated: false,
    notes: [],
  };

  it('charts a date-grouped result as a line over the aliased category column', () => {
    const spec = chartSpec(result, definition, catalog);
    expect(spec?.kind).toBe('line');
    expect(spec?.category.key).toBe('due');
    expect(spec?.series.map(column => column.key)).toEqual(['total_qty']);
  });

  it('does not chart an ungrouped result or one without a number column', () => {
    expect(chartSpec(result, { ...definition, group_by: [] }, catalog)).toBeNull();
    expect(chartSpec({ ...result, columns: result.columns.slice(0, 1) }, definition, catalog)).toBeNull();
  });

  it('never puts a summarized column on the category axis', () => {
    // Grouped by due date, but due date is shown only as a count — keyed `due_date`, like the field.
    const summarizedOnly: ReportDefinition = {
      ...definition,
      columns: [
        { field: 'due_date', aggregate: 'count' },
        { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' },
      ],
    };
    const summarizedResult = {
      ...result,
      columns: [
        { key: 'due_date', label: 'Count of Due Date', type: 'number' as const },
        { key: 'total_qty', label: 'Total qty', type: 'number' as const },
      ],
      rows: [{ due_date: 3, total_qty: 4 }],
    };
    expect(chartSpec(summarizedResult, summarizedOnly, catalog)).toBeNull();
    // Grouped by a field the report does not show at all: no axis either.
    expect(
      chartSpec(
        { ...result, columns: result.columns.slice(1) },
        { ...definition, columns: definition.columns.slice(1) },
        catalog
      )
    ).toBeNull();
  });
});

describe('formatReportValue', () => {
  it('renders dates as the calendar day, datetimes in Central, numbers grouped', () => {
    expect(formatReportValue('2026-11-02', 'date')).toBe('Nov 2, 2026');
    // 03:30 UTC on Nov 2 is still Nov 1 in Central (CST after DST ended that morning).
    expect(formatReportValue('2026-11-02T03:30:00Z', 'datetime')).toMatch(/^Nov 1, 2026, 9:30\sPM$/);
    expect(formatReportValue(12345.5, 'number')).toBe('12,345.5');
    expect(formatReportValue(null, 'string')).toBe('—');
    expect(formatReportValue('released', 'enum')).toBe('released');
  });
});
