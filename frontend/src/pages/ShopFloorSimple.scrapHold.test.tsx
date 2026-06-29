/**
 * Batch 2 shop-floor compliance/traceability — /shop-floor/operations
 *
 * Scrap and hold actions on the desktop shop-floor page now capture the same
 * structured fields the kiosk does, so an AS9100D/CMMC audit trail is preserved
 * regardless of which surface the operator used:
 *
 *  1. Check-out scrap reason — entering quantity_scrapped > 0 reveals a REQUIRED
 *     SCRAP_REASONS picker and disables "End time and save" until one is chosen;
 *     api.clockOut then carries scrap_reason. Scrap 0 hides the field and omits
 *     scrap_reason entirely.
 *  2. In-shift production scrap reason — quantity_scrapped_delta > 0 requires a
 *     SCRAP_REASONS choice and disables "Add to Completed"; the quick +1 Complete
 *     button and a zero-scrap report both omit scrap_reason.
 *  3. Hold category + note — the Hold button opens "Place on Hold" with a
 *     REQUIRED HOLD_REASONS category and optional note; api.holdOperation carries
 *     the enum value. "Other" with an empty note sends a stub note so a blocker
 *     is always filed; a typed note on any category is passed through verbatim.
 *
 * The structured payloads are the compliance-critical part, so every assertion
 * below pins the exact object handed to the mocked api method.
 *
 * Note: ShopFloorSimple emits pre-existing act() warnings from its async polling
 * effects. Those are noise — assertions here are on concrete behavior (field
 * visibility, disabled state, exact API payloads), so a real regression fails
 * rather than hiding behind a warning.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import ShopFloorSimple from './ShopFloorSimple';
import api from '../services/api';
import { SCRAP_REASONS, HOLD_REASONS } from '../components/kiosk/kioskConstants';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getShopFloorOperations: jest.fn(),
    getWorkCenterQueue: jest.fn(),
    getWorkCenters: jest.fn(),
    getDashboard: jest.fn(),
    getMyActiveJob: jest.fn(),
    clockOut: jest.fn(),
    reportOperationProduction: jest.fn(),
    holdOperation: jest.fn(),
  },
}));

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => false }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

// An in-progress op with remaining quantity (complete < ordered) so the queue
// card surfaces the +1 Complete / More / Check Out / Hold action row.
const IN_PROGRESS_OP = {
  id: 101,
  work_order_id: 42,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_number: 'OP10',
  operation_name: 'Laser Cut',
  description: null,
  work_center_id: 1,
  work_center_name: 'Laser 1',
  status: 'in_progress',
  quantity_ordered: 25,
  quantity_complete: 5,
  quantity_scrapped: 0,
  priority: 3,
  due_date: null,
  customer_name: null,
  customer_po: null,
  actual_start: null,
  setup_instructions: null,
  run_instructions: null,
  requires_inspection: false,
};

// Matching active job (op.id === operation_id) so getActiveJobForOperation()
// resolves and the in-progress action row + Check Out / More open.
const ACTIVE_JOB = {
  time_entry_id: 501,
  clock_in: new Date(Date.now() - 60_000).toISOString(),
  entry_type: 'run',
  work_order_id: 42,
  operation_id: 101,
  work_center_id: 1,
  work_order_number: 'WO-2026-0042',
  part_number: 'PN-0099',
  part_name: 'Mount Plate',
  operation_name: 'Laser Cut',
  operation_number: 'OP10',
  quantity_ordered: 25,
  quantity_complete: 5,
};

function renderShopFloor() {
  return render(
    <MemoryRouter initialEntries={['/shop-floor/operations']}>
      <Routes>
        <Route path="/shop-floor/operations" element={<ShopFloorSimple />} />
      </Routes>
    </MemoryRouter>
  );
}

// SelectField is a custom combobox: a button (aria-label) that opens a portal
// listbox of role="option" buttons selected via onMouseDown. Open by clicking
// the labelled button, then mouseDown the option carrying `label`.
function pickReason(ariaLabel: string, label: string) {
  fireEvent.click(screen.getByRole('button', { name: ariaLabel }));
  const option = screen.getByRole('option', { name: new RegExp(label, 'i') });
  fireEvent.mouseDown(option);
}

// These modal inputs use a styled <label> that is NOT associated with its
// control (no htmlFor/id), so getByLabelText can't reach them. Each field is a
// <div><label>{text}</label><input/></div>, so resolve the number input by its
// label text via the wrapping div.
function numberInputByLabel(labelText: RegExp | string) {
  const label = screen.getByText(labelText);
  const wrapper = label.closest('div');
  if (!wrapper) throw new Error(`No wrapper div for label ${labelText}`);
  return within(wrapper as HTMLElement).getByRole('spinbutton');
}

beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = jest.fn();
});

beforeEach(() => {
  jest.clearAllMocks();
  localStorage.clear();
  mockedApi.getWorkCenters.mockResolvedValue([{ id: 1, name: 'Laser 1', code: 'LASER1' }]);
  mockedApi.getDashboard.mockResolvedValue({ work_centers: [] });
  mockedApi.getMyActiveJob.mockResolvedValue({ active_jobs: [ACTIVE_JOB], active_job: ACTIVE_JOB });
  mockedApi.getShopFloorOperations.mockResolvedValue({ operations: [IN_PROGRESS_OP] });
  mockedApi.getWorkCenterQueue.mockResolvedValue({ queue: [] });
  mockedApi.clockOut.mockResolvedValue({});
  mockedApi.reportOperationProduction.mockResolvedValue({});
  mockedApi.holdOperation.mockResolvedValue({});
});

// ---------------------------------------------------------------------------
// 1. Check-out scrap reason
// ---------------------------------------------------------------------------
describe('ShopFloorSimple check-out scrap reason', () => {
  async function openCheckOut() {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');
    // Row "Check Out" (there is also one in the green banner; scope to the row).
    const row = screen.getByTestId('shop-floor-op-101');
    fireEvent.click(within(row).getByRole('button', { name: /check out/i }));
    return screen.getByRole('heading', { name: /^check out$/i });
  }

  it('reveals a required scrap-reason picker and disables save until one is chosen', async () => {
    await openCheckOut();

    // No scrap yet: no reason picker, save enabled.
    expect(screen.queryByRole('button', { name: 'Scrap reason' })).not.toBeInTheDocument();
    const save = screen.getByRole('button', { name: /end time and save/i });
    expect(save).toBeEnabled();

    // Enter scrap > 0 -> reason picker appears and save is blocked.
    const scrapInput = numberInputByLabel('Scrap');
    fireEvent.change(scrapInput, { target: { value: '2' } });

    expect(screen.getByRole('button', { name: 'Scrap reason' })).toBeInTheDocument();
    expect(screen.getByText(/required when scrap is greater than zero/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /end time and save/i })).toBeDisabled();
    expect(mockedApi.clockOut).not.toHaveBeenCalled();
  });

  it('sends scrap_reason on clockOut once a reason is chosen', async () => {
    await openCheckOut();

    fireEvent.change(numberInputByLabel('Scrap'), { target: { value: '2' } });
    pickReason('Scrap reason', 'Out of tolerance');

    const save = screen.getByRole('button', { name: /end time and save/i });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(mockedApi.clockOut).toHaveBeenCalledTimes(1));
    expect(mockedApi.clockOut).toHaveBeenCalledWith(501, {
      quantity_produced: 0,
      quantity_scrapped: 2,
      notes: undefined,
      scrap_reason: 'Out of tolerance',
    });
    // The chosen value is a real SCRAP_REASONS enum value, not free text.
    expect(SCRAP_REASONS.map((r) => r.value)).toContain('Out of tolerance');
  });

  it('omits scrap_reason entirely when nothing was scrapped', async () => {
    await openCheckOut();

    // Leave scrap at 0; add a good part so there is something to save.
    fireEvent.change(numberInputByLabel(/additional good parts at checkout/i), {
      target: { value: '3' },
    });

    const save = screen.getByRole('button', { name: /end time and save/i });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(mockedApi.clockOut).toHaveBeenCalledTimes(1));
    expect(mockedApi.clockOut).toHaveBeenCalledWith(501, {
      quantity_produced: 3,
      quantity_scrapped: 0,
      notes: undefined,
      scrap_reason: undefined,
    });
    expect(mockedApi.clockOut.mock.calls[0][1].scrap_reason).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 2. Production-report scrap reason
// ---------------------------------------------------------------------------
describe('ShopFloorSimple production-report scrap reason', () => {
  async function openProductionModal() {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');
    const row = screen.getByTestId('shop-floor-op-101');
    fireEvent.click(within(row).getByRole('button', { name: /^more$/i }));
    return screen.getByRole('heading', { name: /add completed quantity/i });
  }

  it('requires a scrap reason and disables submit when scrap delta > 0', async () => {
    await openProductionModal();

    // Default state: 1 good part, 0 scrap -> submit enabled, no reason picker.
    expect(screen.queryByRole('button', { name: 'Scrap reason' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add to completed/i })).toBeEnabled();

    fireEvent.change(numberInputByLabel(/scrap to add/i), { target: { value: '4' } });

    expect(screen.getByRole('button', { name: 'Scrap reason' })).toBeInTheDocument();
    expect(screen.getByText(/required when scrap is greater than zero/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /add to completed/i })).toBeDisabled();
    expect(mockedApi.reportOperationProduction).not.toHaveBeenCalled();
  });

  it('sends scrap_reason on reportOperationProduction once chosen', async () => {
    await openProductionModal();

    fireEvent.change(numberInputByLabel(/scrap to add/i), { target: { value: '4' } });
    pickReason('Scrap reason', 'Material defect');

    const submit = screen.getByRole('button', { name: /add to completed/i });
    expect(submit).toBeEnabled();
    fireEvent.click(submit);

    await waitFor(() => expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1));
    expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(101, {
      quantity_complete_delta: 1,
      quantity_scrapped_delta: 4,
      notes: undefined,
      scrap_reason: 'Material defect',
    });
  });

  it('quick +1 Complete reports a good part with no scrap reason', async () => {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');
    const row = screen.getByTestId('shop-floor-op-101');

    fireEvent.click(within(row).getByRole('button', { name: /\+1 complete/i }));

    await waitFor(() => expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1));
    expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(101, {
      quantity_complete_delta: 1,
      quantity_scrapped_delta: 0,
      notes: undefined,
      scrap_reason: undefined,
    });
  });

  it('omits scrap_reason when reporting production with zero scrap delta', async () => {
    await openProductionModal();

    // Default 1 good / 0 scrap; just submit.
    fireEvent.click(screen.getByRole('button', { name: /add to completed/i }));

    await waitFor(() => expect(mockedApi.reportOperationProduction).toHaveBeenCalledTimes(1));
    expect(mockedApi.reportOperationProduction).toHaveBeenCalledWith(101, {
      quantity_complete_delta: 1,
      quantity_scrapped_delta: 0,
      notes: undefined,
      scrap_reason: undefined,
    });
  });
});

// ---------------------------------------------------------------------------
// 3. Hold category + note
// ---------------------------------------------------------------------------
describe('ShopFloorSimple place-on-hold', () => {
  async function openHoldModal() {
    renderShopFloor();
    await screen.findByTestId('shop-floor-op-101');
    const row = screen.getByTestId('shop-floor-op-101');
    fireEvent.click(within(row).getByRole('button', { name: /^hold$/i }));
    return screen.getByRole('heading', { name: /place on hold/i });
  }

  it('requires a category and disables confirm until one is chosen', async () => {
    await openHoldModal();

    expect(screen.getByText(/a reason is required to place a hold/i)).toBeInTheDocument();
    // Footer confirm button (distinct from the modal title).
    const confirm = screen.getByRole('button', { name: /^place on hold$/i });
    expect(confirm).toBeDisabled();
    expect(mockedApi.holdOperation).not.toHaveBeenCalled();
  });

  it('files a blocker with the selected enum category and a typed note', async () => {
    await openHoldModal();

    pickReason('Hold reason', 'Machine down');
    fireEvent.change(screen.getByPlaceholderText(/add context for the blocker/i), {
      target: { value: 'Spindle bearing failed' },
    });

    const confirm = screen.getByRole('button', { name: /^place on hold$/i });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    await waitFor(() => expect(mockedApi.holdOperation).toHaveBeenCalledTimes(1));
    expect(mockedApi.holdOperation).toHaveBeenCalledWith(101, {
      category: 'machine_down',
      severity: 'medium',
      note: 'Spindle bearing failed',
    });
    expect(HOLD_REASONS.map((r) => r.value)).toContain('machine_down');
  });

  it('passes a non-other typed note through verbatim (no stub substitution)', async () => {
    await openHoldModal();

    pickReason('Hold reason', 'Quality hold');
    fireEvent.change(screen.getByPlaceholderText(/add context for the blocker/i), {
      target: { value: 'Awaiting CMM result' },
    });
    fireEvent.click(screen.getByRole('button', { name: /^place on hold$/i }));

    await waitFor(() => expect(mockedApi.holdOperation).toHaveBeenCalledTimes(1));
    expect(mockedApi.holdOperation).toHaveBeenCalledWith(101, {
      category: 'quality_hold',
      severity: 'medium',
      note: 'Awaiting CMM result',
    });
  });

  it('substitutes a stub note for an "other" category with an empty note so a blocker is always filed', async () => {
    await openHoldModal();

    pickReason('Hold reason', 'Other');
    // Leave the note blank.
    fireEvent.click(screen.getByRole('button', { name: /^place on hold$/i }));

    await waitFor(() => expect(mockedApi.holdOperation).toHaveBeenCalledTimes(1));
    expect(mockedApi.holdOperation).toHaveBeenCalledWith(101, {
      category: 'other',
      severity: 'medium',
      note: 'Other (reported on shop floor)',
    });
  });
});
