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

test('records a measurement result for a characteristic', async () => {
  mocked.updateFAICharacteristic.mockResolvedValue(char({ actual_value: '0.5002', is_conforming: true }));
  const { onChanged } = renderPanel();

  fireEvent.click(screen.getByRole('button', { name: 'Record measurement for characteristic 1' }));
  const form = screen.getByRole('form', { name: 'Measurement for characteristic 1' });
  fireEvent.change(within(form).getByLabelText('Actual value'), { target: { value: '0.5002' } });
  fireEvent.change(within(form).getByLabelText('Measuring device'), { target: { value: 'Bore gauge' } });
  fireEvent.change(within(form).getByLabelText('Result'), { target: { value: 'pass' } });
  fireEvent.click(within(form).getByRole('button', { name: 'Save measurement' }));

  await waitFor(() =>
    expect(mocked.updateFAICharacteristic).toHaveBeenCalledWith(7, 71, {
      actual_value: '0.5002',
      measuring_device: 'Bore gauge',
      is_conforming: true,
      notes: null,
    })
  );
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test('removing a characteristic is confirmed first', async () => {
  mocked.deleteFAICharacteristic.mockResolvedValue({ message: 'Characteristic deleted' });
  renderPanel();

  fireEvent.click(screen.getByRole('button', { name: 'Remove characteristic 1' }));
  expect(mocked.deleteFAICharacteristic).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button', { name: 'Remove characteristic' }));

  await waitFor(() => expect(mocked.deleteFAICharacteristic).toHaveBeenCalledWith(7, 71));
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
