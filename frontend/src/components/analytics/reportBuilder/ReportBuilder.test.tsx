/**
 * Custom Reports builder (/analytics/reports): the build → run flow, typed filter
 * inputs, the server's 422 shown verbatim, results notices and chart, and the saved
 * template list (open / run / export / save-as-new / edit / delete).
 *
 * The api module is mocked against the REAL typed client (jest.Mocked<typeof api>),
 * so a fixture whose shape contradicts the contract is a type-check failure.
 */
import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import api from '../../../services/api';
import { ToastProvider } from '../../ui/Toast';
import ReportBuilder from './ReportBuilder';
import type { CustomReportResult, ReportDataSourceCatalog, ReportTemplate } from '../../../types/reportBuilder';

// recharts ResponsiveContainer relies on ResizeObserver, absent in jsdom.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
(global as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver = ResizeObserverStub;

jest.mock('../../../services/api', () => ({
  __esModule: true,
  default: {
    getDataSources: jest.fn(),
    listReportTemplates: jest.fn(),
    getReportTemplate: jest.fn(),
    createReportTemplate: jest.fn(),
    updateReportTemplate: jest.fn(),
    deleteReportTemplate: jest.fn(),
    runCustomReport: jest.fn(),
    runReportTemplate: jest.fn(),
    exportReportTemplateCsv: jest.fn(),
  },
}));

let mockUser: { id: number; role: string; is_superuser: boolean } = { id: 7, role: 'manager', is_superuser: false };
jest.mock('../../../context/AuthContext', () => ({
  useAuth: () => ({ user: mockUser }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const catalog: ReportDataSourceCatalog = {
  work_orders: {
    label: 'Work Orders',
    fields: [
      { name: 'work_order_number', label: 'Work Order #', type: 'string' },
      { name: 'status', label: 'Status', type: 'enum', options: ['draft', 'released', 'complete'] },
      { name: 'quantity_ordered', label: 'Qty Ordered', type: 'number' },
      { name: 'due_date', label: 'Due Date', type: 'date' },
      { name: 'created_at', label: 'Created', type: 'datetime' },
    ],
  },
  parts: {
    label: 'Parts',
    fields: [{ name: 'part_number', label: 'Part #', type: 'string' }],
  },
};

const groupedTemplate: ReportTemplate = {
  id: 12,
  name: 'Open work by status',
  description: 'Ordered quantity per status',
  data_source: 'work_orders',
  columns: [{ field: 'status' }, { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' }],
  filters: [],
  group_by: [{ field: 'status' }],
  sort: [{ field: 'total_qty', direction: 'desc' }],
  is_shared: true,
  created_by: 7,
  created_at: '2026-11-01T15:00:00Z',
  updated_at: '2026-11-02T15:00:00Z',
  definition_errors: [],
};

const brokenTemplate: ReportTemplate = {
  id: 13,
  name: 'Legacy hours',
  description: null,
  data_source: 'work_orders',
  columns: [{ field: 'estimated_hours' }],
  filters: [],
  group_by: [],
  sort: [],
  is_shared: false,
  created_by: 99,
  created_at: '2026-10-01T15:00:00Z',
  updated_at: null,
  definition_errors: ['Unknown field estimated_hours for data source work_orders'],
};

const flatResult: CustomReportResult = {
  columns: [
    { key: 'work_order_number', label: 'Work Order #', type: 'string' },
    { key: 'status', label: 'Status', type: 'enum' },
  ],
  rows: [
    { work_order_number: 'WO-1001', status: 'released' },
    { work_order_number: 'WO-1002', status: 'draft' },
  ],
  row_count: 2,
  truncated: false,
  notes: [],
};

const groupedResult: CustomReportResult = {
  columns: [
    { key: 'status', label: 'Status', type: 'enum' },
    { key: 'total_qty', label: 'Total qty', type: 'number' },
  ],
  rows: [
    { status: 'released', total_qty: 140 },
    { status: 'draft', total_qty: 35 },
  ],
  row_count: 2,
  truncated: false,
  notes: [],
};

const httpError = (status: number, detail: string) =>
  Object.assign(new Error(detail), { response: { status, data: { detail } } });

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.search}</div>;
}

const renderBuilder = (path = '/analytics/reports') =>
  render(
    <MemoryRouter initialEntries={[path]}>
      <ToastProvider>
        <ReportBuilder />
        <LocationProbe />
      </ToastProvider>
    </MemoryRouter>
  );

const optionTexts = (select: HTMLElement) =>
  within(select)
    .getAllByRole('option')
    .map(option => option.textContent);

async function startWorkOrderReport() {
  renderBuilder();
  const source = await screen.findByLabelText(/Data source/);
  fireEvent.change(source, { target: { value: 'work_orders' } });
  fireEvent.click(screen.getByRole('button', { name: /Add column/ }));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockUser = { id: 7, role: 'manager', is_superuser: false };
  mockedApi.getDataSources.mockResolvedValue(catalog);
  mockedApi.listReportTemplates.mockResolvedValue([groupedTemplate, brokenTemplate]);
});

describe('building and running a report', () => {
  it('picks a source, columns and a between filter, runs it and renders the labeled table', async () => {
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: /Add column/ }));
    expect(screen.getByLabelText('Column 1 field')).toHaveValue('work_order_number');
    expect(screen.getByLabelText('Column 2 field')).toHaveValue('status');

    fireEvent.click(screen.getByRole('button', { name: /Add filter/ }));
    fireEvent.change(screen.getByLabelText('Filter 1 field'), { target: { value: 'quantity_ordered' } });
    fireEvent.change(screen.getByLabelText('Filter 1 comparison'), { target: { value: 'between' } });
    fireEvent.change(screen.getByLabelText('Filter 1 from'), { target: { value: '0' } });
    fireEvent.change(screen.getByLabelText('Filter 1 to'), { target: { value: '50' } });

    mockedApi.runCustomReport.mockResolvedValue(flatResult);
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));

    expect(await screen.findByRole('columnheader', { name: /Work Order #/ })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: /Status/ })).toBeInTheDocument();
    expect(screen.getByText('WO-1001')).toBeInTheDocument();
    expect(mockedApi.runCustomReport).toHaveBeenCalledWith({
      data_source: 'work_orders',
      columns: [{ field: 'work_order_number' }, { field: 'status' }],
      filters: [{ field: 'quantity_ordered', operator: 'between', value: 0, value2: 50 }],
      group_by: [],
      sort: [],
      limit: 1000,
    });
    // Not a grouped result: no chart.
    expect(screen.queryByRole('figure')).not.toBeInTheDocument();
  });

  it('changes the comparisons and the value input with the field type', async () => {
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: /Add filter/ }));

    expect(optionTexts(screen.getByLabelText('Filter 1 comparison'))).toEqual([
      'equals',
      'does not equal',
      'is one of',
      'contains',
      'is empty',
      'is not empty',
    ]);

    fireEvent.change(screen.getByLabelText('Filter 1 field'), { target: { value: 'due_date' } });
    expect(optionTexts(screen.getByLabelText('Filter 1 comparison'))).toEqual([
      'is on',
      'is not on',
      'after',
      'on or after',
      'before',
      'on or before',
      'between',
      'is empty',
      'is not empty',
    ]);
    expect(screen.getByLabelText('Filter 1 value')).toHaveAttribute('type', 'date');

    fireEvent.change(screen.getByLabelText('Filter 1 field'), { target: { value: 'status' } });
    expect(optionTexts(screen.getByLabelText('Filter 1 comparison'))).toEqual([
      'equals',
      'does not equal',
      'is one of',
      'is empty',
      'is not empty',
    ]);
    // An enum value is a select of its options, never free text.
    expect(optionTexts(screen.getByLabelText('Filter 1 value'))).toEqual(['Choose…', 'draft', 'released', 'complete']);

    fireEvent.change(screen.getByLabelText('Filter 1 field'), { target: { value: 'created_at' } });
    expect(screen.getByLabelText('Filter 1 value')).toHaveAttribute('type', 'datetime-local');
  });

  it('sends "is one of" as a typed list: ticked enum options and one string per line', async () => {
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: /Add filter/ }));
    fireEvent.change(screen.getByLabelText('Filter 1 comparison'), { target: { value: 'in' } });
    fireEvent.change(screen.getByLabelText(/Filter 1 values/), { target: { value: 'WO-1001\nWO-1002' } });

    fireEvent.click(screen.getByRole('button', { name: /Add filter/ }));
    fireEvent.change(screen.getByLabelText('Filter 2 field'), { target: { value: 'status' } });
    fireEvent.change(screen.getByLabelText('Filter 2 comparison'), { target: { value: 'in' } });
    const values = screen.getByRole('group', { name: 'Filter 2 values' });
    fireEvent.click(within(values).getByRole('checkbox', { name: 'draft' }));
    fireEvent.click(within(values).getByRole('checkbox', { name: 'complete' }));

    mockedApi.runCustomReport.mockResolvedValue(flatResult);
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));
    await waitFor(() => expect(mockedApi.runCustomReport).toHaveBeenCalledTimes(1));
    expect(mockedApi.runCustomReport.mock.calls[0][0].filters).toEqual([
      { field: 'work_order_number', operator: 'in', value: ['WO-1001', 'WO-1002'] },
      { field: 'status', operator: 'in', value: ['draft', 'complete'] },
    ]);
  });

  it('does not call the server until every filter is a value, and says which one is not', async () => {
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: /Add filter/ }));
    fireEvent.change(screen.getByLabelText('Filter 1 field'), { target: { value: 'quantity_ordered' } });
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));

    expect(await screen.findByText('Filter 1: Enter a number.')).toBeInTheDocument();
    expect(screen.getByText('Finish or remove the highlighted filters.')).toBeInTheDocument();
    expect(mockedApi.runCustomReport).not.toHaveBeenCalled();
  });

  it("shows the server's 422 detail verbatim, without a pointless Retry", async () => {
    await startWorkOrderReport();
    mockedApi.runCustomReport.mockRejectedValue(
      httpError(422, 'Column work_order_number must be in group_by when the report is grouped')
    );
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));

    expect(
      await screen.findByText('Column work_order_number must be in group_by when the report is grouped')
    ).toBeInTheDocument();
    expect(screen.getByText('The report did not run')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument();
  });

  it('offers Retry for a server failure and re-runs', async () => {
    await startWorkOrderReport();
    mockedApi.runCustomReport
      .mockRejectedValueOnce(httpError(503, 'Database unavailable'))
      .mockResolvedValueOnce(flatResult);
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));
    fireEvent.click(await screen.findByRole('button', { name: /Retry/ }));
    expect(await screen.findByText('WO-1001')).toBeInTheDocument();
    expect(mockedApi.runCustomReport).toHaveBeenCalledTimes(2);
  });

  it('shows the truncation notice and every server note', async () => {
    await startWorkOrderReport();
    mockedApi.runCustomReport.mockResolvedValue({
      ...flatResult,
      truncated: true,
      notes: [
        {
          code: 'labor_not_tracked',
          message: 'Actual hours and costs read 0 because labor cost tracking is off.',
          fields: ['actual_hours'],
        },
      ],
    });
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));

    expect(
      await screen.findByText('Showing the first 2 rows; narrow the filters or raise the limit.')
    ).toBeInTheDocument();
    expect(screen.getByText('Actual hours and costs read 0 because labor cost tracking is off.')).toBeInTheDocument();
  });

  it('renders the empty state when nothing matches', async () => {
    await startWorkOrderReport();
    mockedApi.runCustomReport.mockResolvedValue({ ...flatResult, rows: [], row_count: 0 });
    fireEvent.click(screen.getByRole('button', { name: /Run report/ }));
    expect(await screen.findByText('No rows match')).toBeInTheDocument();
  });

  it('offers to add ungrouped columns to Group by once a column is summarized', async () => {
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: /Add column/ }));
    fireEvent.change(screen.getByLabelText('Column 2 field'), { target: { value: 'quantity_ordered' } });
    // sum/avg are offered only on number fields.
    expect(optionTexts(screen.getByLabelText('Column 1 summary'))).toEqual([
      'None (show values)',
      'Minimum',
      'Maximum',
      'Count',
    ]);
    fireEvent.change(screen.getByLabelText('Column 2 summary'), { target: { value: 'sum' } });

    expect(
      screen.getByText(/A grouped report needs every column without a summary in Group by: Work Order #/)
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Group by it' }));
    expect(screen.getByRole('list', { name: 'Group-by fields' })).toHaveTextContent('Work Order #');
    expect(screen.queryByText(/A grouped report needs/)).not.toBeInTheDocument();
  });

  it('shows the catalog load failure with a working Retry', async () => {
    mockedApi.getDataSources.mockRejectedValueOnce(httpError(500, 'Catalog offline')).mockResolvedValueOnce(catalog);
    renderBuilder();
    expect(await screen.findByText('Catalog offline')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /Retry/ }));
    expect(await screen.findByLabelText(/Data source/)).toBeInTheDocument();
  });
});

describe('saved templates', () => {
  it('lists templates with Shared and a "needs editing" flag carrying the reasons', async () => {
    renderBuilder();
    expect(await screen.findByText('Open work by status')).toBeInTheDocument();
    expect(screen.getByText('Shared')).toBeInTheDocument();
    expect(screen.getByText('Needs editing')).toBeInTheDocument();
    const reasons = screen.getByRole('list', { name: 'Why Legacy hours needs editing' });
    expect(reasons).toHaveTextContent('Unknown field estimated_hours for data source work_orders');
    expect(screen.getByRole('button', { name: 'Run Legacy hours' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Export Legacy hours as CSV' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Run Open work by status' })).toBeEnabled();
  });

  it('shows Edit / Delete only to the author or an Admin', async () => {
    renderBuilder();
    await screen.findByText('Open work by status');
    expect(screen.getByRole('button', { name: 'Edit Open work by status' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Edit Legacy hours' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete Legacy hours' })).not.toBeInTheDocument();
  });

  it('shows Edit / Delete on every template to an Admin', async () => {
    mockUser = { id: 1, role: 'admin', is_superuser: false };
    renderBuilder();
    await screen.findByText('Legacy hours');
    expect(screen.getByRole('button', { name: 'Edit Legacy hours' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete Legacy hours' })).toBeInTheDocument();
  });

  it('runs a grouped template and charts it, with the table as the data of record', async () => {
    mockedApi.runReportTemplate.mockResolvedValue(groupedResult);
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Run Open work by status' }));

    const chart = await screen.findByRole('figure', { name: /Bar chart of Total qty by Status/ });
    expect(chart).toHaveTextContent('The table below is the data of record.');
    expect(mockedApi.runReportTemplate).toHaveBeenCalledWith(12);
    expect(screen.getByRole('columnheader', { name: /Total qty/ })).toBeInTheDocument();
    expect(screen.getByText('140')).toBeInTheDocument();
  });

  it('shows a 409 from a template run verbatim and refreshes the list', async () => {
    mockedApi.runReportTemplate.mockRejectedValue(
      httpError(409, 'Report template "Open work by status" no longer runs: unknown field status')
    );
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Run Open work by status' }));
    expect(
      await screen.findByText('Report template "Open work by status" no longer runs: unknown field status')
    ).toBeInTheDocument();
    await waitFor(() => expect(mockedApi.listReportTemplates).toHaveBeenCalledTimes(2));
  });

  it('exports a template as CSV under the server-named file', async () => {
    const downloads: string[] = [];
    const click = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement
    ) {
      downloads.push(this.download);
    });
    mockedApi.exportReportTemplateCsv.mockResolvedValue({
      blob: new Blob(['status,total_qty\n']),
      filename: 'Rückstand "by" status.csv',
    });
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Export Open work by status as CSV' }));

    expect(await screen.findByText('Exported Rückstand "by" status.csv.')).toBeInTheDocument();
    expect(mockedApi.exportReportTemplateCsv).toHaveBeenCalledWith(12);
    expect(downloads).toEqual(['Rückstand "by" status.csv']);
    click.mockRestore();
  });

  it('toasts an export refusal verbatim', async () => {
    mockedApi.exportReportTemplateCsv.mockRejectedValue(httpError(400, 'No data to export'));
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Export Open work by status as CSV' }));
    expect(await screen.findByText('No data to export')).toBeInTheDocument();
  });

  it('opens a template into the builder through ?template=', async () => {
    mockedApi.getReportTemplate.mockResolvedValue(groupedTemplate);
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Open Open work by status' }));

    expect(await screen.findByText('Editing template')).toBeInTheDocument();
    expect(screen.getByTestId('location')).toHaveTextContent('?template=12');
    expect(mockedApi.getReportTemplate).toHaveBeenCalledWith(12);
    expect(screen.getByLabelText(/Data source/)).toHaveValue('work_orders');
    expect(screen.getByLabelText('Column 2 summary')).toHaveValue('sum');
    expect(screen.getByLabelText('Column 2 label')).toHaveValue('total_qty');
    expect(screen.getByRole('list', { name: 'Group-by fields' })).toHaveTextContent('Status');
    // Freshly loaded: nothing unsaved, so Save changes is not yet available.
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  });

  it('reports a template it cannot open (deep link) and lets the user start over', async () => {
    mockedApi.getReportTemplate.mockRejectedValue(httpError(403, 'Not authorized to view this report template'));
    renderBuilder('/analytics/reports?template=55');
    expect(await screen.findByText('Not authorized to view this report template')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Start a new report instead' }));
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe(''));
    expect(await screen.findByLabelText(/Data source/)).toBeInTheDocument();
  });

  it('saves the builder as a new template and opens it without re-fetching', async () => {
    const created: ReportTemplate = {
      ...groupedTemplate,
      id: 77,
      name: 'Released work',
      description: null,
      columns: [{ field: 'work_order_number' }],
      group_by: [],
      sort: [],
      is_shared: true,
    };
    mockedApi.createReportTemplate.mockResolvedValue(created);
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: 'Save as new' }));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: '  Released work ' } });
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /Shared/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save template' }));

    await waitFor(() => expect(mockedApi.createReportTemplate).toHaveBeenCalledTimes(1));
    expect(mockedApi.createReportTemplate).toHaveBeenCalledWith({
      name: 'Released work',
      description: null,
      is_shared: true,
      data_source: 'work_orders',
      columns: [{ field: 'work_order_number' }],
      filters: [],
      group_by: [],
      sort: [],
    });
    expect(await screen.findByText('Saved "Released work".')).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('?template=77'));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument();
    expect(mockedApi.getReportTemplate).not.toHaveBeenCalled();
  });

  it('keeps the save dialog up and shows the refusal when the server rejects the name', async () => {
    mockedApi.createReportTemplate.mockRejectedValue(httpError(422, 'name: String should have at most 255 characters'));
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: 'Save as new' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'Mine' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save template' }));
    expect(await screen.findByText('name: String should have at most 255 characters')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('requires a name before saving', async () => {
    await startWorkOrderReport();
    fireEvent.click(screen.getByRole('button', { name: 'Save as new' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save template' }));
    expect(await within(dialog).findByText('A name is required.')).toBeInTheDocument();
    expect(mockedApi.createReportTemplate).not.toHaveBeenCalled();
  });

  it('edits name / description / sharing with a PATCH that leaves the definition alone', async () => {
    mockedApi.updateReportTemplate.mockResolvedValue({ ...groupedTemplate, name: 'WIP by status', is_shared: false });
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit Open work by status' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText(/^Name/)).toHaveValue('Open work by status');
    // Not open in the builder: there is no definition to offer.
    expect(within(dialog).queryByRole('checkbox', { name: /Also save/ })).not.toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText(/^Name/), { target: { value: 'WIP by status' } });
    fireEvent.change(within(dialog).getByLabelText(/^Description/), { target: { value: '   ' } });
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /Shared/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(mockedApi.updateReportTemplate).toHaveBeenCalledWith(12, {
        name: 'WIP by status',
        description: null,
        is_shared: false,
      })
    );
    expect(await screen.findByText('Saved "WIP by status".')).toBeInTheDocument();
    expect(screen.getByText('WIP by status')).toBeInTheDocument();
  });

  it('edits an open, changed template with its new definition when asked', async () => {
    mockedApi.getReportTemplate.mockResolvedValue(groupedTemplate);
    mockedApi.updateReportTemplate.mockResolvedValue(groupedTemplate);
    renderBuilder('/analytics/reports?template=12');
    await screen.findByText('Editing template');
    fireEvent.click(screen.getByRole('button', { name: 'Remove sort 1' }));
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Edit Open work by status' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByRole('checkbox', { name: /Also save/ })).toBeChecked();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() => expect(mockedApi.updateReportTemplate).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateReportTemplate).toHaveBeenCalledWith(12, {
      name: 'Open work by status',
      description: 'Ordered quantity per status',
      is_shared: true,
      definition: {
        data_source: 'work_orders',
        columns: [{ field: 'status' }, { field: 'quantity_ordered', aggregate: 'sum', alias: 'total_qty' }],
        filters: [],
        group_by: [{ field: 'status' }],
        sort: [],
      },
    });
    await waitFor(() => expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument());
  });

  it('saves builder changes onto the open template, and toasts a 403 verbatim', async () => {
    mockedApi.getReportTemplate.mockResolvedValue(groupedTemplate);
    mockedApi.updateReportTemplate
      .mockRejectedValueOnce(httpError(403, 'Only the author or an Admin can change this report template'))
      .mockResolvedValueOnce(groupedTemplate);
    renderBuilder('/analytics/reports?template=12');
    await screen.findByText('Editing template');
    fireEvent.change(screen.getByLabelText('Sort 1 direction'), { target: { value: 'asc' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    expect(await screen.findByText('Only the author or an Admin can change this report template')).toBeInTheDocument();
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(mockedApi.updateReportTemplate).toHaveBeenCalledTimes(2));
    expect(mockedApi.updateReportTemplate).toHaveBeenLastCalledWith(12, {
      definition: expect.objectContaining({ sort: [{ field: 'total_qty', direction: 'asc' }] }),
    });
    await waitFor(() => expect(screen.queryByText('Unsaved changes')).not.toBeInTheDocument());
  });

  it('deletes through a ConfirmDialog that holds while the request is pending', async () => {
    let finishDelete: (value: { message: string }) => void = () => undefined;
    mockedApi.deleteReportTemplate.mockImplementation(
      () =>
        new Promise(resolve => {
          finishDelete = resolve;
        })
    );
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Open work by status' }));

    const dialog = await screen.findByRole('dialog');
    expect(dialog).toHaveTextContent('"Open work by status" will be deleted');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete template' }));

    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled());
    expect(mockedApi.deleteReportTemplate).toHaveBeenCalledWith(12);
    // Escape cannot dismiss a delete already on the wire.
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    await act(async () => {
      finishDelete({ message: 'Report template deleted' });
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Run Open work by status' })).not.toBeInTheDocument();
    expect(screen.getByText('Deleted "Open work by status".')).toBeInTheDocument();
  });

  it('keeps the delete dialog up and toasts a refusal', async () => {
    mockedApi.deleteReportTemplate.mockRejectedValue(httpError(403, 'Only the author or an Admin can delete it'));
    renderBuilder();
    fireEvent.click(await screen.findByRole('button', { name: 'Delete Open work by status' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete template' }));
    expect(await screen.findByText('Only the author or an Admin can delete it')).toBeInTheDocument();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });
});
