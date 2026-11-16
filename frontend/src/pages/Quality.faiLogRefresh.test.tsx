/**
 * The FAI Log view re-reads after every FAI write the Quality page makes.
 *
 * The log stays mounted under the (portaled) FAI detail modal, and the page's loadData()
 * re-reads only the Inspections list. Without a refresh signal a disposition, a prefill or
 * a new FAI left the log showing the old rows until a filter change or a reload, so the
 * user could think the write had not saved.
 */
import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QualityPage from './Quality';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { selectComboBoxOption } from '../test-utils/comboBox';
import type { FAILogRow } from '../types/quality';

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: 'quality', isSuperuser: false, can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    getCustomerNames: jest.fn(),
    getVendors: jest.fn(),
    getWorkOrders: jest.fn(),
    getFAILog: jest.fn(),
    exportFAILog: jest.fn(),
    getFAI: jest.fn(),
    updateFAI: jest.fn(),
    prefillFAIFromSteps: jest.fn(),
    getAssignmentPeople: jest.fn(),
    createNCR: jest.fn(),
    createCAR: jest.fn(),
    createFAI: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const logRow = (overrides: Partial<FAILogRow> = {}): FAILogRow => ({
  fai_id: 7,
  fai_number: 'FAI-20261113-007',
  log_date: '2026-11-13',
  part_id: 5,
  part_number: 'PN-5',
  part_name: 'Bracket',
  part_revision: 'C',
  part_current_revision: 'C',
  customer: 'Acme Aerospace',
  customer_po: 'PO-889',
  work_order_id: 55,
  job_number: 'WO-1055',
  serial_number: null,
  status: 'pending',
  is_complete: false,
  fai_type: 'full',
  reason: 'new_part',
  total_characteristics: 1,
  characteristics_passed: 0,
  characteristics_failed: 0,
  inspection_date: null,
  completed_date: null,
  customer_approval_required: true,
  customer_approved: false,
  customer_approval_date: null,
  created_at: '2026-11-13T15:00:00Z',
  ...overrides,
});

const FAI_DETAIL = {
  id: 7,
  fai_number: 'FAI-20261113-007',
  part_id: 5,
  part: { id: 5, part_number: 'PN-5', name: 'Bracket' },
  part_revision: 'C',
  part_current_revision: 'C',
  work_order_id: 55,
  work_order_number: 'WO-1055',
  customer_name: 'Acme Aerospace',
  customer_po: 'PO-889',
  serial_number: null,
  fai_type: 'full',
  reason: 'new_part',
  status: 'pending',
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
  characteristics: [
    {
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
    },
  ],
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getNCRs.mockResolvedValue([]);
  mockedApi.getCARs.mockResolvedValue([]);
  mockedApi.getFAIs.mockResolvedValue([]);
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0, open_cars: 0, pending_fais: 1 });
  mockedApi.getParts.mockResolvedValue([{ id: 5, part_number: 'PN-5', name: 'Bracket', revision: 'C' }] as any);
  mockedApi.getCustomerNames.mockResolvedValue([]);
  mockedApi.getVendors.mockResolvedValue([]);
  mockedApi.getWorkOrders.mockResolvedValue([]);
  mockedApi.getAssignmentPeople.mockResolvedValue([]);
  mockedApi.getFAILog.mockResolvedValue([logRow()]);
  mockedApi.getFAI.mockResolvedValue(FAI_DETAIL as any);
});

async function renderLog() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/quality?tab=fai&fai_view=log']}>
        <QualityPage />
      </MemoryRouter>
    </ToastProvider>
  );
  const log = await screen.findByTestId('fai-log-view');
  await within(log).findAllByText('FAI-20261113-007');
  expect(mockedApi.getFAILog).toHaveBeenCalledTimes(1);
  return log;
}

async function openDetailFromLog(log: HTMLElement) {
  fireEvent.click(within(log).getAllByText('FAI-20261113-007')[0]);
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByText('Bore diameter');
  return dialog;
}

test('a disposition recorded from the log shows in the log when the detail closes', async () => {
  const log = await renderLog();
  const dialog = await openDetailFromLog(log);
  // What the server answers once the disposition is saved.
  mockedApi.updateFAI.mockResolvedValue({ ...FAI_DETAIL, status: 'passed' } as any);
  mockedApi.getFAI.mockResolvedValue({ ...FAI_DETAIL, status: 'passed', completed_date: '2026-11-15' } as any);
  mockedApi.getFAILog.mockResolvedValue([
    logRow({ status: 'passed', is_complete: true, completed_date: '2026-11-15' }),
  ]);

  fireEvent.change(within(dialog).getByLabelText('Disposition'), { target: { value: 'passed' } });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Record disposition' }));
  fireEvent.click(screen.getAllByRole('button', { name: 'Record disposition' }).at(-1)!);
  await waitFor(() => expect(mockedApi.updateFAI).toHaveBeenCalledWith(7, { version: 0, status: 'passed' }));

  await waitFor(() => expect(mockedApi.getFAILog).toHaveBeenCalledTimes(2));
  // The same filters and page are re-read.
  expect(mockedApi.getFAILog.mock.calls[1][0]).toEqual(mockedApi.getFAILog.mock.calls[0][0]);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Close dialog' }));
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(within(log).getAllByText('X').length).toBeGreaterThan(0);
});

test('a prefill from process steps re-reads the log', async () => {
  const log = await renderLog();
  const dialog = await openDetailFromLog(log);
  mockedApi.prefillFAIFromSteps.mockResolvedValue({
    fai_id: 7,
    fai_number: 'FAI-20261113-007',
    work_order_id: 55,
    prefilled: [],
    unmatched: [],
    prefilled_count: 0,
    unmatched_count: 0,
  } as any);

  fireEvent.click(within(dialog).getByTestId('fai-prefill-button'));

  await waitFor(() => expect(mockedApi.prefillFAIFromSteps).toHaveBeenCalledWith(7));
  await waitFor(() => expect(mockedApi.getFAILog).toHaveBeenCalledTimes(2));
});

test('an FAI created while the log is showing appears in it', async () => {
  const log = await renderLog();
  mockedApi.createFAI.mockResolvedValue({ id: 8 } as any);
  mockedApi.getFAILog.mockResolvedValue([
    logRow({ fai_id: 8, fai_number: 'FAI-20261115-001', log_date: '2026-11-15' }),
    logRow(),
  ]);

  fireEvent.click(screen.getAllByRole('button', { name: /new fai/i })[0]);
  const dialog = await screen.findByRole('dialog');
  selectComboBoxOption(within(dialog).getByLabelText(/^Part/), /PN-5/);
  fireEvent.click(within(dialog).getByRole('button', { name: 'Create FAI' }));

  await waitFor(() => expect(mockedApi.createFAI).toHaveBeenCalled());
  await waitFor(() => expect(mockedApi.getFAILog).toHaveBeenCalledTimes(2));
  expect((await within(log).findAllByText('FAI-20261115-001')).length).toBeGreaterThan(0);
});
