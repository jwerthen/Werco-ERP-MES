/**
 * The FAI detail body: record, characteristics CRUD, the final disposition and the
 * customer approval. Every write is server-gated, so the panel is non-optimistic:
 * nothing changes until the server answers, a success re-reads the FAI (onChanged), and
 * a refusal is shown in the server's own words with nothing re-read.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import FaiInspectionPanel, { type FAIDetailRecord } from './FaiInspectionPanel';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import { getCentralTodayISODate } from '../../utils/centralTime';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getAssignmentPeople: jest.fn(),
    updateFAI: jest.fn(),
    addFAICharacteristic: jest.fn(),
    updateFAICharacteristic: jest.fn(),
    deleteFAICharacteristic: jest.fn(),
    recordFAICustomerApproval: jest.fn(),
  },
}));

const mocked = api as jest.Mocked<typeof api>;

const char = (overrides: Partial<FAIDetailRecord['characteristics'][number]> = {}) => ({
  id: 71,
  char_number: 1,
  characteristic: 'Bore diameter',
  nominal: '0.5000',
  tolerance_plus: '0.0005',
  tolerance_minus: '0.0005',
  specification: null,
  actual_value: null,
  measuring_device: null,
  is_conforming: null,
  is_critical: true,
  is_major: false,
  notes: null,
  ...overrides,
});

const baseFai: FAIDetailRecord = {
  id: 7,
  fai_number: 'FAI-20261113-001',
  part_id: 1,
  part: { id: 1, part_number: 'PN-7731', name: 'Bracket, hinge' },
  part_revision: 'B',
  part_current_revision: 'C',
  work_order_id: 55,
  work_order_number: 'WO-1055',
  customer_name: 'Acme Aerospace',
  customer_po: 'PO-889',
  serial_number: 'SN-001',
  fai_type: 'full',
  reason: 'new_part',
  status: 'in_progress',
  total_characteristics: 1,
  characteristics_passed: 0,
  characteristics_failed: 0,
  inspection_date: null,
  inspector_id: null,
  inspector_name: null,
  completed_date: null,
  customer_approval_required: true,
  customer_approved: false,
  customer_approval_date: null,
  created_at: '2026-11-13T15:00:00Z',
  characteristics: [char()],
};

function renderPanel(
  fai: FAIDetailRecord = baseFai,
  { canEdit = true, canApprove = true }: { canEdit?: boolean; canApprove?: boolean } = {}
) {
  const onChanged = jest.fn().mockResolvedValue(undefined);
  const onDirtyChange = jest.fn();
  render(
    <ToastProvider>
      <MemoryRouter>
        <FaiInspectionPanel
          fai={fai}
          canEdit={canEdit}
          canApprove={canApprove}
          onChanged={onChanged}
          onDirtyChange={onDirtyChange}
        />
      </MemoryRouter>
    </ToastProvider>
  );
  return { onChanged, onDirtyChange };
}

beforeEach(() => {
  jest.clearAllMocks();
  mocked.getAssignmentPeople.mockResolvedValue([{ id: 4, full_name: 'Ivy Inspector' }]);
});

test('the summary names the job, customer, PO and flags a revision the part has since moved past', () => {
  renderPanel();
  const summary = screen.getByTestId('fai-summary');
  expect(within(summary).getByRole('link', { name: 'WO-1055' })).toHaveAttribute('href', '/work-orders/55');
  expect(summary).toHaveTextContent('Acme Aerospace');
  expect(summary).toHaveTextContent('PO-889');
  expect(summary).toHaveTextContent('Part is now Rev C');
  expect(summary).toHaveTextContent('Required — not yet recorded');
});

test('adds a characteristic with the next number, then re-reads the FAI', async () => {
  mocked.addFAICharacteristic.mockResolvedValue(char({ id: 72, char_number: 2 }));
  const { onChanged } = renderPanel();

  fireEvent.click(screen.getByRole('button', { name: 'Add characteristic' }));
  const form = screen.getByRole('form', { name: 'New characteristic' });
  expect(within(form).getByLabelText(/^Char #/)).toHaveValue(2);
  fireEvent.change(within(form).getByLabelText(/^Characteristic/), { target: { value: 'Flange thickness' } });
  fireEvent.change(within(form).getByLabelText('Nominal'), { target: { value: '0.125' } });
  fireEvent.click(within(form).getByLabelText('Major'));
  fireEvent.click(within(form).getByRole('button', { name: 'Add' }));

  await waitFor(() =>
    expect(mocked.addFAICharacteristic).toHaveBeenCalledWith(7, {
      char_number: 2,
      characteristic: 'Flange thickness',
      nominal: '0.125',
      tolerance_plus: null,
      tolerance_minus: null,
      specification: null,
      is_critical: false,
      is_major: true,
    })
  );
  await waitFor(() => expect(onChanged).toHaveBeenCalledTimes(1));
  expect(screen.queryByRole('form', { name: 'New characteristic' })).not.toBeInTheDocument();
});

test('records a measurement result for a characteristic, sending only what was entered', async () => {
  mocked.updateFAICharacteristic.mockResolvedValue(char({ actual_value: '0.5002', is_conforming: true }));
  const { onChanged } = renderPanel();

  fireEvent.click(screen.getByRole('button', { name: 'Record measurement for characteristic 1' }));
  const form = screen.getByRole('form', { name: 'Measurement for characteristic 1' });
  // Nothing entered yet: nothing to save.
  expect(within(form).getByRole('button', { name: 'Save measurement' })).toBeDisabled();
  fireEvent.change(within(form).getByLabelText('Actual value'), { target: { value: '0.5002' } });
  fireEvent.change(within(form).getByLabelText('Measuring device'), { target: { value: 'Bore gauge' } });
  fireEvent.change(within(form).getByLabelText('Result'), { target: { value: 'pass' } });
  fireEvent.click(within(form).getByRole('button', { name: 'Save measurement' }));

  // The untouched notes are not sent: the server writes every key it receives.
  await waitFor(() =>
    expect(mocked.updateFAICharacteristic).toHaveBeenCalledWith(7, 71, {
      actual_value: '0.5002',
      measuring_device: 'Bore gauge',
      is_conforming: true,
    })
  );
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

const twoChars: FAIDetailRecord = {
  ...baseFai,
  total_characteristics: 2,
  characteristics: [char(), char({ id: 72, char_number: 2, characteristic: 'Flange thickness' })],
};

function renderRerenderable(fai: FAIDetailRecord) {
  const onChanged = jest.fn().mockResolvedValue(undefined);
  const onDirtyChange = jest.fn();
  const tree = (record: FAIDetailRecord) => (
    <ToastProvider>
      <MemoryRouter>
        <FaiInspectionPanel fai={record} canEdit canApprove onChanged={onChanged} onDirtyChange={onDirtyChange} />
      </MemoryRouter>
    </ToastProvider>
  );
  const { rerender } = render(tree(fai));
  return { rerender: (record: FAIDetailRecord) => rerender(tree(record)), onChanged, onDirtyChange };
}

test('another row cannot be opened over a measurement with unsaved entries', () => {
  renderPanel(twoChars);
  const recordTwo = () => screen.getByRole('button', { name: 'Record measurement for characteristic 2' });

  fireEvent.click(screen.getByRole('button', { name: 'Record measurement for characteristic 1' }));
  // An untouched editor may be swapped for another row: nothing would be lost.
  expect(recordTwo()).toBeEnabled();
  const form = screen.getByRole('form', { name: 'Measurement for characteristic 1' });
  fireEvent.change(within(form).getByLabelText('Actual value'), { target: { value: '0.5003' } });

  // Regression: Record on row 2 replaced row 1's typed entries with no prompt.
  expect(recordTwo()).toBeDisabled();
  expect(recordTwo()).toHaveAttribute('title', 'Save or cancel the measurement for characteristic 1 first');
  fireEvent.click(recordTwo());
  expect(screen.getByRole('form', { name: 'Measurement for characteristic 1' })).toBeInTheDocument();
  expect(screen.getByLabelText('Actual value')).toHaveValue('0.5003');
  expect(screen.queryByRole('form', { name: 'Measurement for characteristic 2' })).not.toBeInTheDocument();

  fireEvent.click(within(form).getByRole('button', { name: 'Cancel' }));
  expect(recordTwo()).toBeEnabled();
});

test('an open editor follows a re-read of its row and never writes stale blanks over saved values', async () => {
  mocked.updateFAICharacteristic.mockResolvedValue(char({ is_conforming: true }));
  const { rerender } = renderRerenderable(baseFai);

  fireEvent.click(screen.getByRole('button', { name: 'Record measurement for characteristic 1' }));
  fireEvent.change(screen.getByLabelText('Measuring device'), { target: { value: 'Mic #12' } });
  // Prefill from process steps fills the empty actual value and the page re-reads the FAI.
  rerender({
    ...baseFai,
    characteristics: [char({ actual_value: '1.0041', measuring_device: 'Mitutoyo caliper 7' })],
  });

  // Regression: the editor kept the blank it opened with and its save erased the prefill.
  await waitFor(() => expect(screen.getByLabelText('Actual value')).toHaveValue('1.0041'));
  expect(screen.getByLabelText('Measuring device')).toHaveValue('Mic #12');
  expect(screen.getByRole('status')).toHaveTextContent('The saved measurement changed while this was open');
  fireEvent.change(screen.getByLabelText('Result'), { target: { value: 'pass' } });
  fireEvent.click(screen.getByRole('button', { name: 'Save measurement' }));

  await waitFor(() =>
    expect(mocked.updateFAICharacteristic).toHaveBeenCalledWith(7, 71, {
      measuring_device: 'Mic #12',
      is_conforming: true,
    })
  );
});

test("a re-read that only echoes the inspector's own entries back is not reported as a change", () => {
  const { rerender } = renderRerenderable(baseFai);

  fireEvent.click(screen.getByRole('button', { name: 'Record measurement for characteristic 1' }));
  fireEvent.change(screen.getByLabelText('Actual value'), { target: { value: '0.5003' } });
  rerender({ ...baseFai, characteristics: [char({ actual_value: '0.5003' })] });

  expect(screen.getByLabelText('Actual value')).toHaveValue('0.5003');
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

test('an editor whose row was removed by another writer closes and says its entries were discarded', async () => {
  const { rerender } = renderRerenderable(twoChars);

  fireEvent.click(screen.getByRole('button', { name: 'Record measurement for characteristic 1' }));
  fireEvent.change(screen.getByLabelText('Actual value'), { target: { value: '0.5003' } });
  rerender({ ...twoChars, total_characteristics: 1, characteristics: [twoChars.characteristics[1]] });

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Characteristic 1 is no longer on this FAI; its unsaved measurement was discarded.'
  );
  expect(screen.queryByRole('form', { name: /^Measurement for/ })).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Record measurement for characteristic 2' })).toBeEnabled();
});

test('removing a characteristic is confirmed first', async () => {
  mocked.deleteFAICharacteristic.mockResolvedValue({ message: 'Characteristic deleted' });
  renderPanel();

  fireEvent.click(screen.getByRole('button', { name: 'Remove characteristic 1' }));
  expect(mocked.deleteFAICharacteristic).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button', { name: 'Remove characteristic' }));

  await waitFor(() => expect(mocked.deleteFAICharacteristic).toHaveBeenCalledWith(7, 71));
});

test('the inspection date cannot be set in the future (the server refuses it too)', () => {
  renderPanel();
  expect(screen.getByLabelText('Inspection date')).toHaveAttribute('max', getCentralTodayISODate());
});

test('saves the inspection record (date, inspector) and reports unsaved entries meanwhile', async () => {
  mocked.updateFAI.mockResolvedValue(baseFai);
  const { onDirtyChange } = renderPanel();

  fireEvent.change(screen.getByLabelText('Inspection date'), { target: { value: '2026-11-12' } });
  await waitFor(() => expect(onDirtyChange).toHaveBeenLastCalledWith(true));
  fireEvent.click(screen.getByRole('button', { name: 'Save inspection record' }));

  await waitFor(() =>
    expect(mocked.updateFAI).toHaveBeenCalledWith(7, {
      version: 0,
      status: 'in_progress',
      inspection_date: '2026-11-12',
      inspector_id: null,
      notes: null,
      deviations: null,
    })
  );
});

test('the disposition is confirmed, and a refusal is shown verbatim with nothing re-read', async () => {
  const detail = 'FAI approval requires Admin, Manager or Quality';
  mocked.updateFAI.mockRejectedValue({ response: { status: 403, data: { detail } } });
  const { onChanged } = renderPanel();

  fireEvent.change(screen.getByLabelText('Disposition'), { target: { value: 'passed' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record disposition' }));
  // The confirmation names what the characteristics still lack.
  expect(await screen.findByText(/1 characteristic has no result recorded/)).toBeInTheDocument();
  expect(mocked.updateFAI).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByRole('button', { name: 'Record disposition' }).at(-1)!);

  await waitFor(() => expect(mocked.updateFAI).toHaveBeenCalledWith(7, { version: 0, status: 'passed' }));
  expect(await screen.findByText(detail)).toBeInTheDocument();
  expect(onChanged).not.toHaveBeenCalled();
});

test('only the approve roles see the disposition; authoring roles still record evidence', () => {
  renderPanel(baseFai, { canEdit: true, canApprove: false });
  expect(screen.queryByLabelText('Disposition')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Add characteristic' })).toBeInTheDocument();
});

test('a final FAI is read-only evidence: no authoring, no disposition', () => {
  renderPanel({ ...baseFai, status: 'passed', completed_date: '2026-11-13', customer_approval_required: false });
  expect(screen.getByText('Bore diameter')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Add characteristic' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Record measurement/ })).not.toBeInTheDocument();
  expect(screen.queryByRole('form', { name: 'Inspection record' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Disposition')).not.toBeInTheDocument();
});

test('the customer approval is recorded after a passing disposition, once, with its date', async () => {
  const passed = { ...baseFai, status: 'passed', completed_date: '2026-11-13' };
  mocked.recordFAICustomerApproval.mockResolvedValue({ ...passed, customer_approved: true });
  const { onChanged } = renderPanel(passed);

  fireEvent.change(screen.getByLabelText(/^Customer approval date/), { target: { value: '2026-11-10' } });
  fireEvent.click(screen.getByRole('button', { name: 'Record customer approval' }));
  expect(mocked.recordFAICustomerApproval).not.toHaveBeenCalled();
  fireEvent.click(screen.getAllByRole('button', { name: 'Record customer approval' }).at(-1)!);

  await waitFor(() =>
    expect(mocked.recordFAICustomerApproval).toHaveBeenCalledWith(7, { customer_approval_date: '2026-11-10' })
  );
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test.each([
  ['a failed FAI', { status: 'failed', completed_date: '2026-11-13' }],
  ['an undispositioned FAI', {}],
  ['an already-approved FAI', { status: 'passed', customer_approved: true, customer_approval_date: '2026-11-10' }],
])('%s offers no customer approval', (_label, overrides) => {
  renderPanel({ ...baseFai, ...overrides });
  expect(screen.queryByRole('button', { name: 'Record customer approval' })).not.toBeInTheDocument();
});
