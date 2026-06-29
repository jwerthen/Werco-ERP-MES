/**
 * DataTable<T> tests
 *
 * Covers the behaviors downstream migrations depend on:
 *   - sort toggle (asc → desc → none) reorders rows without mutating the prop
 *   - client pagination slices rows + Prev/Next + "X–Y of N"
 *   - row click fires onRowClick, but an inner action button does not
 *   - selection: per-row add/remove + select-all
 *   - CSV export serializes the sorted, all-pages rows via csv()/accessor
 *   - loading → Skeleton, error → ErrorState(+retry), empty → EmptyState
 */

import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { DataTable, DataTableColumn, buildCsv } from './DataTable';

interface Row {
  id: number;
  name: string;
  qty: number;
}

const rows: Row[] = [
  { id: 1, name: 'Charlie', qty: 30 },
  { id: 2, name: 'Alpha', qty: 10 },
  { id: 3, name: 'Bravo', qty: 20 },
];

const columns: Array<DataTableColumn<Row>> = [
  { key: 'name', header: 'Name', accessor: (r) => r.name, sortable: true },
  { key: 'qty', header: 'Qty', accessor: (r) => r.qty, sortable: true, align: 'right' },
];

function getDataRowNames(): string[] {
  const table = screen.getByTestId('data-table');
  const bodyRows = within(table).getAllByRole('row').slice(1); // drop header row
  return bodyRows.map((r) => within(r).getAllByRole('cell')[0].textContent || '');
}

describe('DataTable', () => {
  it('renders rows in source order with no sort applied', () => {
    render(<DataTable columns={columns} data={rows} rowKey={(r) => r.id} />);
    expect(getDataRowNames()).toEqual(['Charlie', 'Alpha', 'Bravo']);
  });

  it('sort toggles asc → desc → none and reorders rows', () => {
    render(<DataTable columns={columns} data={rows} rowKey={(r) => r.id} />);
    const nameHeader = screen.getByRole('button', { name: /Name/i });

    // asc
    fireEvent.click(nameHeader);
    expect(getDataRowNames()).toEqual(['Alpha', 'Bravo', 'Charlie']);
    expect(nameHeader.closest('th')).toHaveAttribute('aria-sort', 'ascending');

    // desc
    fireEvent.click(nameHeader);
    expect(getDataRowNames()).toEqual(['Charlie', 'Bravo', 'Alpha']);
    expect(nameHeader.closest('th')).toHaveAttribute('aria-sort', 'descending');

    // none → back to source order
    fireEvent.click(nameHeader);
    expect(getDataRowNames()).toEqual(['Charlie', 'Alpha', 'Bravo']);
    expect(nameHeader.closest('th')).toHaveAttribute('aria-sort', 'none');
  });

  it('does not mutate the data prop while sorting', () => {
    const snapshot = rows.map((r) => r.name);
    render(<DataTable columns={columns} data={rows} rowKey={(r) => r.id} />);
    fireEvent.click(screen.getByRole('button', { name: /Name/i }));
    expect(rows.map((r) => r.name)).toEqual(snapshot);
  });

  it('client paginates with slice + Prev/Next + "X–Y of N"', () => {
    const many: Row[] = Array.from({ length: 5 }, (_, i) => ({
      id: i + 1,
      name: `Row ${i + 1}`,
      qty: i,
    }));
    render(<DataTable columns={columns} data={many} rowKey={(r) => r.id} pageSize={2} />);

    // Page 1: rows 1–2
    expect(getDataRowNames()).toEqual(['Row 1', 'Row 2']);
    expect(screen.getByText(/1.*2.*of.*5/)).toBeInTheDocument();

    const prev = screen.getByRole('button', { name: 'Previous page' });
    const next = screen.getByRole('button', { name: 'Next page' });
    expect(prev).toBeDisabled();

    fireEvent.click(next);
    expect(getDataRowNames()).toEqual(['Row 3', 'Row 4']);

    fireEvent.click(next);
    expect(getDataRowNames()).toEqual(['Row 5']);
    expect(next).toBeDisabled();

    fireEvent.click(prev);
    expect(getDataRowNames()).toEqual(['Row 3', 'Row 4']);
  });

  it('fires onRowClick on a row click but not when an inner action button is clicked', () => {
    const onRowClick = jest.fn();
    const onAction = jest.fn();
    const cols: Array<DataTableColumn<Row>> = [
      { key: 'name', header: 'Name', accessor: (r) => r.name },
      {
        key: 'actions',
        header: 'Actions',
        render: (r) => (
          <button
            onClick={(e) => {
              e.stopPropagation();
              onAction(r.id);
            }}
          >
            Act {r.id}
          </button>
        ),
      },
    ];
    render(
      <DataTable columns={cols} data={rows} rowKey={(r) => r.id} onRowClick={onRowClick} />
    );

    // Click the inner action button → onAction fires, row click does NOT.
    fireEvent.click(screen.getByRole('button', { name: 'Act 1' }));
    expect(onAction).toHaveBeenCalledWith(1);
    expect(onRowClick).not.toHaveBeenCalled();

    // Click a plain cell → row click fires.
    fireEvent.click(screen.getByText('Charlie'));
    expect(onRowClick).toHaveBeenCalledTimes(1);
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);
  });

  it('supports per-row selection add/remove and select-all', () => {
    function Harness() {
      const [keys, setKeys] = React.useState<Set<string | number>>(new Set());
      return (
        <DataTable
          columns={columns}
          data={rows}
          rowKey={(r) => r.id}
          selection={{ selectedKeys: keys, onChange: setKeys }}
        />
      );
    }
    render(<Harness />);

    const selectAll = screen.getByLabelText('Select all rows') as HTMLInputElement;
    const row1 = screen.getByLabelText('Select row 1') as HTMLInputElement;
    const row2 = screen.getByLabelText('Select row 2') as HTMLInputElement;

    // Add one
    fireEvent.click(row1);
    expect(row1.checked).toBe(true);
    expect(row2.checked).toBe(false);

    // Remove it
    fireEvent.click(row1);
    expect(row1.checked).toBe(false);

    // Select all
    fireEvent.click(selectAll);
    expect(row1.checked).toBe(true);
    expect(row2.checked).toBe(true);
    expect((screen.getByLabelText('Select row 3') as HTMLInputElement).checked).toBe(true);

    // Deselect all
    fireEvent.click(selectAll);
    expect(row1.checked).toBe(false);
    expect(row2.checked).toBe(false);
  });

  it('shows the bulk-actions bar only when a selection exists', () => {
    function Harness({ initial }: { initial: Set<string | number> }) {
      const [keys, setKeys] = React.useState<Set<string | number>>(initial);
      return (
        <DataTable
          columns={columns}
          data={rows}
          rowKey={(r) => r.id}
          selection={{ selectedKeys: keys, onChange: setKeys }}
          bulkActions={<button>Bulk delete</button>}
        />
      );
    }
    const { unmount } = render(<Harness initial={new Set()} />);
    expect(screen.queryByRole('button', { name: 'Bulk delete' })).not.toBeInTheDocument();
    unmount();

    render(<Harness initial={new Set([1])} />);
    expect(screen.getByRole('button', { name: 'Bulk delete' })).toBeInTheDocument();
    expect(screen.getByText('1 selected')).toBeInTheDocument();
  });

  it('buildCsv serializes header + rows using csv()/accessor and escapes special chars', () => {
    const cols: Array<DataTableColumn<Row>> = [
      { key: 'name', header: 'Name', accessor: (r) => r.name },
      { key: 'qty', header: 'Qty', csv: (r) => r.qty },
    ];
    const csv = buildCsv(cols, [
      { id: 1, name: 'Alpha', qty: 10 },
      { id: 2, name: 'Bravo, Inc', qty: 20 },
    ]);
    expect(csv).toBe('Name,Qty\nAlpha,10\n"Bravo, Inc",20');
  });

  it('CSV export builds a blob and triggers a download click', () => {
    const createSpy = jest
      .spyOn(URL, 'createObjectURL')
      .mockReturnValue('blob:mock');
    const revokeSpy = jest.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const clickSpy = jest
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {});

    render(
      <DataTable
        columns={columns}
        data={rows}
        rowKey={(r) => r.id}
        csvExport={{ filename: 'rows' }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /Export CSV/i }));

    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(revokeSpy).toHaveBeenCalledTimes(1);

    createSpy.mockRestore();
    revokeSpy.mockRestore();
    clickSpy.mockRestore();
  });

  it('renders Skeleton rows when loading', () => {
    render(<DataTable columns={columns} data={[]} rowKey={(r) => r.id} loading />);
    expect(screen.getAllByTestId('skeleton').length).toBeGreaterThan(0);
    // No empty state while loading.
    expect(screen.queryByTestId('empty-state')).not.toBeInTheDocument();
  });

  it('renders ErrorState with retry when error is set', () => {
    const onRetry = jest.fn();
    render(
      <DataTable
        columns={columns}
        data={[]}
        rowKey={(r) => r.id}
        error="Boom"
        onRetry={onRetry}
      />
    );
    const alert = screen.getByRole('alert');
    expect(within(alert).getByText('Boom')).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    // The table is replaced by the error block.
    expect(screen.queryByTestId('data-table')).not.toBeInTheDocument();
  });

  it('renders EmptyState when not loading/error and data is empty', () => {
    render(
      <DataTable
        columns={columns}
        data={[]}
        rowKey={(r) => r.id}
        empty={{ title: 'Nothing here', description: 'Add some rows.' }}
      />
    );
    const empty = screen.getByTestId('empty-state');
    expect(within(empty).getByText('Nothing here')).toBeInTheDocument();
    expect(within(empty).getByText('Add some rows.')).toBeInTheDocument();
  });
});

describe('DataTable — server pagination', () => {
  // In server-pagination mode the parent owns the page window: `data` is the
  // already-paged slice, the client must not re-sort or re-slice it, Prev/Next
  // are pure callbacks into onPageChange, and Next is gated on hasNext.
  const serverRows: Row[] = [
    { id: 1, name: 'Charlie', qty: 30 },
    { id: 2, name: 'Alpha', qty: 10 },
  ];

  it('renders the data prop verbatim (no client sort) when serverPagination is set', () => {
    render(
      <DataTable
        columns={columns}
        data={serverRows}
        rowKey={(r) => r.id}
        serverPagination={{ page: 1, pageSize: 50, hasNext: true, onPageChange: jest.fn() }}
      />
    );
    // Source order preserved.
    expect(getDataRowNames()).toEqual(['Charlie', 'Alpha']);

    // Clicking a sortable header must NOT reorder the server-owned slice.
    fireEvent.click(screen.getByRole('button', { name: /Name/i }));
    expect(getDataRowNames()).toEqual(['Charlie', 'Alpha']);
  });

  it('Next calls onPageChange(page + 1); Prev calls onPageChange(page - 1)', () => {
    const onPageChange = jest.fn();
    render(
      <DataTable
        columns={columns}
        data={serverRows}
        rowKey={(r) => r.id}
        serverPagination={{ page: 3, pageSize: 50, hasNext: true, onPageChange }}
      />
    );

    const prev = screen.getByRole('button', { name: 'Previous page' });
    const next = screen.getByRole('button', { name: 'Next page' });

    fireEvent.click(next);
    expect(onPageChange).toHaveBeenLastCalledWith(4);

    fireEvent.click(prev);
    expect(onPageChange).toHaveBeenLastCalledWith(2);

    expect(onPageChange).toHaveBeenCalledTimes(2);
  });

  it('disables Prev on page 1 and disables Next when hasNext is false', () => {
    const onPageChange = jest.fn();
    const { rerender } = render(
      <DataTable
        columns={columns}
        data={serverRows}
        rowKey={(r) => r.id}
        serverPagination={{ page: 1, pageSize: 50, hasNext: true, onPageChange }}
      />
    );

    // Page 1 → Prev disabled, Next enabled (hasNext).
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Next page' })).not.toBeDisabled();

    // Last page → Next disabled (no hasNext), Prev enabled.
    rerender(
      <DataTable
        columns={columns}
        data={serverRows}
        rowKey={(r) => r.id}
        serverPagination={{ page: 2, pageSize: 50, hasNext: false, onPageChange }}
      />
    );
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Previous page' })).not.toBeDisabled();

    // Clicking a disabled Next is a no-op.
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }));
    expect(onPageChange).not.toHaveBeenCalled();
  });

  it('renders the server-derived "start–end" range from page/pageSize, not "of N"', () => {
    render(
      <DataTable
        columns={columns}
        data={serverRows}
        rowKey={(r) => r.id}
        serverPagination={{ page: 2, pageSize: 50, hasNext: true, onPageChange: jest.fn() }}
      />
    );
    // page 2, pageSize 50 → first row index is 51; two rows → 51–52.
    expect(screen.getByText('51–52')).toBeInTheDocument();
    // Server mode has no client total, so no "of N".
    expect(screen.queryByText(/of\s*\d/)).not.toBeInTheDocument();
  });
});
