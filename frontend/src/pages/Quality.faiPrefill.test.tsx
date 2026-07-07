/**
 * Process Sheets PR 4 — FAI prefill-from-steps on the Quality page.
 *
 * Clicking an FAI row opens the detail modal (characteristics table +
 * "Prefill from process steps"). The prefill is NON-optimistic: nothing
 * changes until the server answers; then the summary renders N filled and M
 * unmatched WITH the server's reasons, and the characteristics re-fetch.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QualityPage from './Quality';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    getFAI: jest.fn(),
    prefillFAIFromSteps: jest.fn(),
    createNCR: jest.fn(),
    createCAR: jest.fn(),
    createFAI: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const FAI_ROW = {
  id: 7,
  fai_number: 'FAI-000007',
  part_id: 1,
  part: { id: 1, part_number: 'PN-7731', name: 'Bracket, hinge' },
  part_revision: 'B',
  fai_type: 'full',
  status: 'in_progress',
  total_characteristics: 3,
  characteristics_passed: 0,
  characteristics_failed: 0,
  due_date: undefined,
  created_at: '2026-07-01T12:00:00Z',
};

const CHAR_EMPTY = {
  id: 71,
  char_number: 1,
  characteristic: 'Bore diameter',
  nominal: '0.5000',
  tolerance_plus: '0.002',
  tolerance_minus: '0.002',
  specification: null,
  actual_value: null,
  measuring_device: null,
  is_conforming: null,
  is_critical: true,
  is_major: false,
  notes: null,
};

const FAI_DETAIL = {
  ...FAI_ROW,
  work_order_id: 9,
  characteristics: [
    CHAR_EMPTY,
    { ...CHAR_EMPTY, id: 72, char_number: 2, characteristic: 'Overall length', is_critical: false },
    { ...CHAR_EMPTY, id: 73, char_number: 3, characteristic: 'Surface finish', is_critical: false },
  ],
};

const FAI_DETAIL_AFTER = {
  ...FAI_DETAIL,
  characteristics: [
    { ...FAI_DETAIL.characteristics[0], actual_value: '0.5001', measuring_device: '0-1 in micrometer' },
    { ...FAI_DETAIL.characteristics[1], actual_value: '4.998', measuring_device: 'Caliper 6 in' },
    FAI_DETAIL.characteristics[2],
  ],
};

const PREFILL_RESULT = {
  fai_id: 7,
  fai_number: 'FAI-000007',
  work_order_id: 9,
  prefilled: [
    {
      char_number: 1,
      characteristic: 'Bore diameter',
      actual_value: '0.5001',
      measuring_device: '0-1 in micrometer',
      wo_operation_step_id: 101,
      record_id: 900,
      serial_number: 'SN-001',
    },
    {
      char_number: 2,
      characteristic: 'Overall length',
      actual_value: '4.998',
      measuring_device: 'Caliper 6 in',
      wo_operation_step_id: 102,
      record_id: 901,
      serial_number: null,
    },
  ],
  unmatched: [
    {
      char_number: 3,
      characteristic: 'Surface finish',
      reason: 'no conforming measurement step record matched this characteristic',
    },
  ],
  prefilled_count: 2,
  unmatched_count: 1,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getNCRs.mockResolvedValue([]);
  mockedApi.getCARs.mockResolvedValue([]);
  mockedApi.getFAIs.mockResolvedValue([FAI_ROW]);
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0, open_cars: 0, pending_fais: 1 });
  mockedApi.getParts.mockResolvedValue([]);
  mockedApi.getFAI.mockResolvedValue(FAI_DETAIL);
  mockedApi.prefillFAIFromSteps.mockResolvedValue(PREFILL_RESULT);
});

async function openFaiDetail() {
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/quality']}>
        <QualityPage />
      </MemoryRouter>
    </ToastProvider>
  );
  // Switch to the FAI tab and open the row (DataTable renders the desktop
  // table AND the mobile cards — click the table row instance).
  fireEvent.click(await screen.findByRole('button', { name: /^fai$/i }));
  fireEvent.click((await screen.findAllByText('FAI-000007'))[0]);
  const dialog = await screen.findByRole('dialog');
  await within(dialog).findByTestId('fai-prefill-button');
  return dialog;
}

describe('Quality FAI prefill from process steps', () => {
  it('prefill shows a fill/unmatched summary with reasons and refreshes the characteristics (non-optimistic)', async () => {
    const dialog = await openFaiDetail();
    expect(mockedApi.getFAI).toHaveBeenCalledWith(7);
    // Before: empty actuals, no summary.
    expect(within(dialog).queryByTestId('fai-prefill-summary')).not.toBeInTheDocument();

    mockedApi.getFAI.mockResolvedValue(FAI_DETAIL_AFTER);
    fireEvent.click(within(dialog).getByTestId('fai-prefill-button'));

    await waitFor(() => expect(mockedApi.prefillFAIFromSteps).toHaveBeenCalledWith(7));

    const summary = await within(dialog).findByTestId('fai-prefill-summary');
    expect(summary).toHaveTextContent('2 characteristics filled · 1 unmatched');
    expect(summary).toHaveTextContent('#1 Bore diameter → 0.5001 (0-1 in micrometer) SN SN-001');
    expect(summary).toHaveTextContent(
      '#3 Surface finish — no conforming measurement step record matched this characteristic'
    );

    // Characteristics re-fetched: the filled actuals render in the table too
    // (once in the summary, once in the characteristics table).
    await waitFor(() => expect(mockedApi.getFAI).toHaveBeenCalledTimes(2));
    expect(within(dialog).getAllByText('0.5001').length).toBeGreaterThanOrEqual(2);
    expect(within(dialog).getByText('Caliper 6 in')).toBeInTheDocument();
  });

  it('surfaces a prefill refusal verbatim and leaves the modal usable', async () => {
    mockedApi.prefillFAIFromSteps.mockRejectedValue({
      response: { status: 409, data: { detail: 'FAI is not linked to a work order' } },
    });
    const dialog = await openFaiDetail();

    fireEvent.click(within(dialog).getByTestId('fai-prefill-button'));

    expect(await screen.findByText('FAI is not linked to a work order')).toBeInTheDocument();
    expect(within(dialog).queryByTestId('fai-prefill-summary')).not.toBeInTheDocument();
    expect(mockedApi.getFAI).toHaveBeenCalledTimes(1);
  });
});
