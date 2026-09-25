import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import KioskDocViewer from '../components/kiosk/KioskDocViewer';
import ShopFloorCameraScanner from '../components/shopfloor/ShopFloorCameraScanner';
import ShopFloorSimple from './ShopFloorSimple';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    getScrapReasonCodes: jest.fn(),
    getOperationDetails: jest.fn(),
    getOperationDocuments: jest.fn(),
    fetchShopFloorDocumentBlob: jest.fn(),
    clockOut: jest.fn(),
    startOperation: jest.fn(),
    clockIn: jest.fn(),
    completeOperation: jest.fn(),
    reportOperationProduction: jest.fn(),
    reduceOperationProduction: jest.fn(),
    resolveScanAction: jest.fn(),
    scannerLookup: jest.fn(),
  },
}));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));
jest.mock('../hooks/usePermissions', () => ({ usePermissions: () => ({ can: () => false }) }));
jest.mock('../components/kiosk/KioskDocViewer', () => ({
  __esModule: true,
  default: jest.fn(({ operationId, initialTab, onBack }: React.ComponentProps<typeof KioskDocViewer>) => (
    <div data-testid="document-viewer">
      <p>{`Operation ${operationId}: ${initialTab}`}</p>
      <button onClick={onBack}>Back to work</button>
    </div>
  )),
}));
jest.mock('../components/shopfloor/ShopFloorCameraScanner', () => ({
  __esModule: true,
  default: jest.fn(({ open, onClose }: React.ComponentProps<typeof ShopFloorCameraScanner>) =>
    open ? (
      <div role="dialog" aria-label="Traveler camera scanner">
        <button onClick={onClose}>Close camera</button>
      </div>
    ) : null
  ),
}));

jest.mock('../context/CompanyContext', () => ({
  useCompany: () => ({ currentCompany: mockCompany }),
}));

const mockedApi = api as jest.Mocked<typeof api>;
let mockUser = { id: 1, company_id: 1, role: 'operator' };
let mockCompany = { id: 1 };
const workspaceKey = (userId: number) => `shop_floor_workspace:v1:company:1:user:${userId}`;
const originalMatchMedia = window.matchMedia;

function operation(id: number, overrides: Record<string, unknown> = {}) {
  return {
    id,
    work_order_id: id + 1000,
    work_order_number: `WO-${id}`,
    part_number: `PART-${id}`,
    part_name: 'Mount plate',
    operation_number: '20',
    operation_name: id === 102 ? 'Brake Form' : 'Laser Cut',
    description: null,
    work_center_id: 1,
    work_center_name: 'Fabrication',
    status: 'in_progress',
    quantity_ordered: 20,
    quantity_complete: 4,
    quantity_scrapped: 0,
    priority: 3,
    due_date: null,
    customer_name: null,
    customer_po: null,
    actual_start: null,
    setup_instructions: null,
    run_instructions: null,
    requires_inspection: false,
    can_check_in: true,
    ...overrides,
  };
}

function job(id: number, overrides: Record<string, unknown> = {}) {
  return {
    ...operation(id),
    operation_id: id,
    time_entry_id: id + 5000,
    clock_in: new Date(Date.now() - 60_000).toISOString(),
    entry_type: 'run' as const,
    ...overrides,
  };
}

const jobs = [job(101), job(102)];
function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/shop-floor/operations']}>
      <ShopFloorSimple />
    </MemoryRouter>
  );
}

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  sessionStorage.clear();
  mockUser = { id: 1, company_id: 1, role: 'operator' };
  mockCompany = { id: 1 };
  window.matchMedia = jest.fn(query => ({
    matches: query === '(max-width: 767px)',
    media: query,
    onchange: null,
    addListener: jest.fn(),
    removeListener: jest.fn(),
    addEventListener: jest.fn(),
    removeEventListener: jest.fn(),
    dispatchEvent: jest.fn(),
  }));
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.getWorkCenters.mockResolvedValue([]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: jobs });
  mockedApi.getScrapReasonCodes.mockResolvedValue([]);
  mockedApi.getOperationDocuments.mockResolvedValue({} as never);
  mockedApi.fetchShopFloorDocumentBlob.mockResolvedValue('blob:operation-drawing');
  mockedApi.clockOut.mockResolvedValue({});
  mockedApi.completeOperation.mockResolvedValue({});
  mockedApi.reportOperationProduction.mockResolvedValue({});
  mockedApi.reduceOperationProduction.mockResolvedValue({});
});

afterEach(() => {
  window.matchMedia = originalMatchMedia;
});

describe('ShopFloorSimple phone workspace', () => {
  it('lands on current work with useful actions when the checked-in operation is outside the queue', async () => {
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [operation(201, { status: 'ready' })] });
    renderPage();
    const work = await screen.findByRole('region', { name: 'My current work' });
    expect(within(work).getByRole('heading', { name: 'WO-101' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'My work (2)' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByTestId('shop-floor-op-201')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Drawing' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Report quantity' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: /^more$/i })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Report quantity' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add Completed Quantity' });
    expect(within(dialog).getByText(/WO-101/)).toBeInTheDocument();
  });

  it('switches active jobs and restores the same operation and view for the same operator only', async () => {
    const first = renderPage();
    fireEvent.change(await screen.findByLabelText('Switch active job (2)'), { target: { value: '5102' } });
    expect(screen.getByRole('heading', { name: 'WO-102' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'All operations' }));
    expect(JSON.parse(sessionStorage.getItem(workspaceKey(1))!)).toMatchObject({
      activeTimeEntryId: 5102,
      selectedOperationId: 102,
      view: 'all',
    });
    first.unmount();

    mockUser = { id: 2, company_id: 1, role: 'operator' };
    const second = renderPage();
    expect(await screen.findByRole('heading', { name: 'WO-101' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'My work (2)' })).toHaveAttribute('aria-pressed', 'true');
    second.unmount();

    mockUser = { id: 1, company_id: 1, role: 'operator' };
    renderPage();
    expect(await screen.findByRole('button', { name: 'All operations' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: 'My work (2)' }));
    expect(screen.getByRole('heading', { name: 'WO-102' })).toBeInTheDocument();
    expect(screen.getByLabelText('Switch active job (2)')).toHaveValue('5102');
  });

  it('ignores a saved check-in that is no longer active', async () => {
    sessionStorage.setItem(
      workspaceKey(1),
      JSON.stringify({
        selectedOperationId: 999,
        activeTimeEntryId: 5999,
        view: 'my-work',
        workCenterId: null,
        savedAt: Date.now(),
      })
    );
    renderPage();
    expect(await screen.findByRole('heading', { name: 'WO-101' })).toBeInTheDocument();
    expect(screen.getByLabelText('Switch active job (2)')).toHaveValue('5101');
    expect(screen.queryByText('WO-999')).not.toBeInTheDocument();
  });

  it('keeps platform-user resume state under the effective company and resets work on a tenant switch', async () => {
    mockUser = { id: 1, company_id: 99, role: 'platform_admin' };
    const view = renderPage();
    fireEvent.change(await screen.findByLabelText('Switch active job (2)'), { target: { value: '5102' } });
    expect(JSON.parse(sessionStorage.getItem(workspaceKey(1))!)).toMatchObject({ selectedOperationId: 102 });
    expect(sessionStorage.getItem('shop_floor_workspace:v1:company:99:user:1')).toBeNull();
    mockCompany = { id: 2 };
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [job(201)] });
    view.rerender(
      <MemoryRouter initialEntries={['/shop-floor/operations']}>
        <ShopFloorSimple />
      </MemoryRouter>
    );
    expect(await screen.findByRole('heading', { name: 'WO-201' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'WO-102' })).not.toBeInTheDocument();
    mockCompany = { id: 1 };
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: jobs });
    view.rerender(
      <MemoryRouter initialEntries={['/shop-floor/operations']}>
        <ShopFloorSimple />
      </MemoryRouter>
    );
    expect(await screen.findByRole('heading', { name: 'WO-102' })).toBeInTheDocument();
  });

  it('opens a newly checked-in job in My work while preserving the existing check-in', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [jobs[0]] });
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [operation(102, { status: 'ready' })] });
    mockedApi.startOperation.mockImplementation(async () => {
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: jobs });
      return {};
    });
    renderPage();
    await screen.findByRole('heading', { name: 'WO-101' });
    fireEvent.click(screen.getByRole('button', { name: 'Ready here' }));
    const card = await screen.findByTestId('shop-floor-op-102');
    fireEvent.click(within(card).getByRole('button', { expanded: false }));
    fireEvent.click(within(card).getByRole('button', { name: 'Check In' }));
    expect(await screen.findByRole('heading', { name: 'WO-102' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'My work (2)' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('Switch active job (2)')).toHaveValue('5102');
    expect(mockedApi.startOperation).toHaveBeenCalledWith(102);
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
  });

  it('shows compact ready rows in dispatch order and exposes held, blocked, and active work in All operations', async () => {
    const queue = [
      operation(205, { status: 'ready', priority: 5, run_order: 1 }),
      operation(206, { status: 'on_hold', run_order: 2 }),
      operation(101, { run_order: 3 }),
      operation(203, { status: 'pending', can_check_in: false, blocked_by_previous_operations: true, run_order: 4 }),
      operation(201, { status: 'ready', priority: 1, due_date: '2020-01-01', run_order: 5 }),
    ];
    mockedApi.getShopFloorOperations.mockResolvedValue({ operations: queue });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Ready here' }));
    await screen.findByTestId('shop-floor-op-205');
    const visibleIds = () => screen.getAllByTestId(/^shop-floor-op-\d+$/).map(node => node.dataset.testid);
    expect(visibleIds()).toEqual(['shop-floor-op-205', 'shop-floor-op-201']);
    const firstCard = screen.getByTestId('shop-floor-op-205');
    const row = within(firstCard).getByRole('button', { expanded: false });
    expect(within(firstCard).queryByRole('button', { name: 'Check In' })).not.toBeInTheDocument();
    fireEvent.click(row);
    expect(within(firstCard).getByRole('button', { name: 'Check In' })).toBeEnabled();
    expect(row).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(row);
    expect(within(firstCard).queryByRole('button', { name: 'Check In' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'All operations' }));
    expect(visibleIds()).toEqual(queue.map(op => `shop-floor-op-${op.id}`));
  });

  it('opens the selected operation drawing with the authenticated shop-floor transport', async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText('Switch active job (2)'), { target: { value: '5102' } });
    fireEvent.click(screen.getByRole('button', { name: 'Drawing' }));
    expect(await screen.findByText('Operation 102: drawing')).toBeInTheDocument();
    const viewer = jest.mocked(KioskDocViewer).mock.calls.at(-1)![0];
    expect(viewer.operationId).toBe(102);
    await viewer.transport.fetchOperationDocuments(viewer.operationId);
    await viewer.transport.fetchDocumentBlob(909);
    expect(mockedApi.getOperationDocuments).toHaveBeenCalledWith(102);
    expect(mockedApi.fetchShopFloorDocumentBlob).toHaveBeenCalledWith(909);
    fireEvent.click(screen.getByRole('button', { name: 'Back to work' }));
    expect(screen.queryByTestId('document-viewer')).not.toBeInTheDocument();
  });

  it('loads the selected operation instructions directly from current work', async () => {
    mockedApi.getOperationDetails.mockResolvedValue({
      work_order: { id: 1102, work_order_number: 'WO-102', part: { part_number: 'PART-102', name: 'Mount plate' } },
      operation: {
        id: 102,
        operation_number: '20',
        name: 'Brake Form',
        status: 'in_progress',
        quantity_complete: 4,
        quantity_ordered: 20,
        setup_instructions: 'Install the 85-degree punch.',
        run_instructions: 'Check the first bend against the drawing.',
      },
      work_center: { name: 'Fabrication' },
      all_operations: [],
      history: [],
    });
    renderPage();
    fireEvent.change(await screen.findByLabelText('Switch active job (2)'), { target: { value: '5102' } });
    fireEvent.click(screen.getByRole('button', { name: 'Instructions' }));
    const dialog = await screen.findByRole('dialog', { name: 'Operation Details' });
    expect(mockedApi.getOperationDetails).toHaveBeenCalledWith(102);
    expect(within(dialog).getByText('Install the 85-degree punch.')).toBeInTheDocument();
    expect(within(dialog).getByText('Check the first bend against the drawing.')).toBeInTheDocument();
  });

  it('opens the camera scanner from Scan and closes it without changing the current job', async () => {
    renderPage();
    await screen.findByRole('heading', { name: 'WO-101' });
    fireEvent.click(screen.getByRole('button', { name: 'Scan traveler' }));
    expect(screen.getByRole('dialog', { name: 'Traveler camera scanner' })).toBeInTheDocument();
    expect(jest.mocked(ShopFloorCameraScanner).mock.calls.at(-1)![0]).toEqual(
      expect.objectContaining({
        open: true,
        onScan: expect.any(Function),
        onClose: expect.any(Function),
      })
    );
    expect(screen.queryByPlaceholderText('Scan or enter traveler code')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Close camera' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'WO-101' })).toBeInTheDocument();
  });

  it.each(['work_order', 'legacy'] as const)(
    'clears a conflicting work-center filter for a %s traveler scan',
    async kind => {
      sessionStorage.setItem(
        workspaceKey(1),
        JSON.stringify({
          selectedOperationId: null,
          activeTimeEntryId: null,
          view: 'ready',
          workCenterId: 1,
          savedAt: Date.now(),
        })
      );
      mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [] });
      mockedApi.getShopFloorOperations.mockImplementation(async params => ({
        operations: params?.work_center_id ? [] : [operation(202, { work_center_id: 2, status: 'ready' })],
      }));
      if (kind === 'work_order') {
        mockedApi.resolveScanAction.mockResolvedValue({
          kind: 'work_order',
          code: 'WO:WO-202',
          operations: [],
          work_order: {
            id: 1202,
            work_order_number: 'WO-202',
            status: 'released',
            quantity_ordered: 20,
            quantity_complete: 0,
            part_number: 'PART-202',
            part_name: 'Mount plate',
            current_operation_id: 202,
          },
        });
      } else {
        mockedApi.resolveScanAction.mockResolvedValue({ kind: 'unknown', code: 'WO-202', reason: 'Legacy barcode' });
        mockedApi.scannerLookup.mockResolvedValue({ work_order_number: 'WO-202' });
      }
      renderPage();
      fireEvent.click(await screen.findByRole('button', { name: 'Scan traveler' }));
      const scanner = jest.mocked(ShopFloorCameraScanner).mock.calls.at(-1)![0];
      await act(async () => {
        await scanner.onScan('WO-202');
        scanner.onClose();
      });
      expect(await screen.findByTestId('shop-floor-op-202')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'All operations' })).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByLabelText('Work center')).toHaveValue('');
      expect(screen.getByRole('searchbox', { name: 'Search work orders or parts' })).toHaveValue('WO-202');
      expect(mockedApi.getShopFloorOperations).toHaveBeenLastCalledWith({ search: 'WO-202' });
    }
  );

  it('checks out only the selected time entry without completing the operation', async () => {
    renderPage();
    fireEvent.change(await screen.findByLabelText('Switch active job (2)'), { target: { value: '5102' } });
    fireEvent.click(screen.getByRole('button', { name: 'Check Out' }));
    const dialog = await screen.findByRole('dialog', { name: 'Check Out' });
    expect(within(dialog).getByText(/WO-102/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'End time and save' }));
    await waitFor(() => expect(mockedApi.clockOut).toHaveBeenCalledTimes(1));
    expect(mockedApi.clockOut.mock.calls[0][0]).toBe(5102);
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
  });

  it('keeps an unconfirmed quantity visible and checks the original report without creating another addition', async () => {
    mockedApi.reportOperationProduction
      .mockRejectedValueOnce(new Error('Network response lost'))
      .mockResolvedValueOnce({});
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Report quantity' }));
    const dialog = await screen.findByRole('dialog', { name: 'Add Completed Quantity' });
    fireEvent.change(within(dialog).getByLabelText('Good parts to add'), { target: { value: '7' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add to Completed' }));
    expect(await within(dialog).findByText('Not confirmed')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Good parts to add')).toHaveValue(7);
    expect(within(dialog).getByRole('button', { name: 'Add to Completed' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+1 Complete' })).toBeDisabled();
    const original = mockedApi.reportOperationProduction.mock.calls[0];
    expect(original).toEqual([
      101,
      expect.objectContaining({
        quantity_complete_delta: 7,
        request_id: expect.any(String),
      }),
    ]);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Check original report' }));
    await waitFor(() => expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(2));
    expect(mockedApi.reportOperationProduction.mock.calls[1]).toEqual(original);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(screen.getByText('Saved')).toBeInTheDocument();
  });

  it('holds an uncertain correction across reload until history is reviewed without resending the removal', async () => {
    mockedApi.reduceOperationProduction.mockRejectedValueOnce(new Error('Connection ended before response'));
    mockedApi.getOperationDetails.mockResolvedValue({
      work_order: { work_order_number: 'WO-101' },
      operation: { id: 101, name: 'Laser Cut' },
      history: [
        { created_at: '2026-09-25T14:00:00Z', details: 'Removed 2 completed parts: counted the same tray twice.' },
      ],
    });
    const first = renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Report quantity' }));
    fireEvent.click(screen.getByRole('button', { name: 'Correct over-count' }));
    const form = screen.getByRole('dialog', { name: 'Correct Over-Count' });
    fireEvent.change(within(form).getByLabelText('Parts to remove'), { target: { value: '2' } });
    fireEvent.change(within(form).getByLabelText('Reason for correction'), {
      target: { value: 'counted the same tray twice' },
    });
    fireEvent.click(within(form).getByRole('button', { name: /remove from completed/i }));
    expect(await within(form).findByText('Not confirmed')).toBeInTheDocument();
    expect(within(form).getByLabelText('Parts to remove')).toHaveValue(2);
    expect(within(form).getByRole('button', { name: /remove from completed/i })).toBeDisabled();
    expect(screen.getByRole('button', { name: '+1 Complete' })).toBeDisabled();
    expect(mockedApi.reduceOperationProduction).toHaveBeenCalledWith(101, {
      quantity_delta: 2,
      reason: 'counted the same tray twice',
      notes: undefined,
      source: 'desktop',
    });
    const storageKey = 'werco:shop-floor-production:v1:1:1';
    expect(JSON.parse(sessionStorage.getItem(storageKey)!)).toMatchObject({
      pendingCorrection: { operationId: 101, body: { quantity_delta: 2 } },
    });
    first.unmount();

    renderPage();
    expect(await screen.findByRole('button', { name: '+1 Complete' })).toBeDisabled();
    expect(mockedApi.reduceOperationProduction).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Correction reviewed with supervisor' }));
    const review = await screen.findByRole('dialog', { name: 'Review quantity correction' });
    expect(within(review).getByRole('button', { name: 'Finish review' })).toBeDisabled();
    expect(
      await within(review).findByText('Removed 2 completed parts: counted the same tray twice.')
    ).toBeInTheDocument();
    expect(mockedApi.getOperationDetails).toHaveBeenCalledWith(101);
    fireEvent.click(
      within(review).getByRole('checkbox', {
        name: 'I reviewed this correction with my supervisor and confirmed whether it was recorded.',
      })
    );
    fireEvent.click(within(review).getByRole('button', { name: 'Finish review' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.reduceOperationProduction).toHaveBeenCalledTimes(1);
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '+1 Complete' })).toBeEnabled();
    const stored = JSON.parse(sessionStorage.getItem(storageKey)!);
    expect(stored.pendingCorrection).toBeNull();
    expect(stored.drafts['101']).toBeUndefined();
  });

  it('requires confirmation to complete full quantity and keeps Check Out a separate action', async () => {
    mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [job(101, { quantity_complete: 20 })] });
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Complete operation' }));
    const dialog = await screen.findByRole('dialog', { name: 'Complete operation at full quantity?' });
    expect(mockedApi.completeOperation).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByRole('button', { name: 'Check Out' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Complete operation' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Complete Operation' }));
    await waitFor(() => expect(mockedApi.completeOperation).toHaveBeenCalledTimes(1));
    expect(mockedApi.completeOperation.mock.calls[0][0]).toBe(101);
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
  });
});
