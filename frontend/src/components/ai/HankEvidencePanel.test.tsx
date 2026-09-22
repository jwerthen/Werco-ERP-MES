import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import type { HankEvidence } from '../../types/hankWork';
import type EntityPicker from '../operations/EntityPicker';
import type { HankPurchaseOrderPicker } from './HankPurchaseOrderPicker';
import { HankEvidencePanel } from './HankEvidencePanel';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getHankReadiness: jest.fn(),
    getHankKnowledge: jest.fn(),
    getHankShippingPacket: jest.fn(),
    getHankPurchasingImpact: jest.fn(),
    getHankTrace: jest.fn(),
  },
}));
jest.mock('../operations/EntityPicker', () => ({
  __esModule: true,
  default: ({ id, value, disabled, onChange }: React.ComponentProps<typeof EntityPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">Choose</option>
      <option value="7">WO-7</option>
      <option value="8">WO-8</option>
    </select>
  ),
}));
jest.mock('./HankPurchaseOrderPicker', () => ({
  HankPurchaseOrderPicker: ({
    id,
    value,
    disabled,
    onChange,
  }: React.ComponentProps<typeof HankPurchaseOrderPicker>) => (
    <select id={id} value={value} disabled={disabled} onChange={e => onChange(e.target.value)}>
      <option value="">Choose</option>
      <option value="11">PO-11</option>
    </select>
  ),
}));
jest.mock('./HankJobScan', () => ({ HankJobScan: () => null }));
const mocked = jest.mocked(api);
const evidence: HankEvidence = {
  company_id: 4,
  checked_at: '2026-09-22T15:00:00Z',
  title: 'Evidence for WO-7',
  summary: 'Review these gaps before starting.',
  checks: [
    {
      key: 'material',
      title: 'Material coverage',
      status: 'unknown',
      detail: 'Unreserved stock is not allocated to this job.',
      references: [{ type: 'work_order', id: 7, label: 'WO-7 source', url: '/work-orders/7' }],
    },
  ],
  coverage_notes: ['This evidence does not authorize production.'],
  draft_text: null,
};
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, type: 'access' }))}.sig`);
}
function show(props: React.ComponentProps<typeof HankEvidencePanel>) {
  return render(
    <MemoryRouter>
      <HankEvidencePanel {...props} />
    </MemoryRouter>
  );
}
function check() {
  fireEvent.click(screen.getByRole('button', { name: 'Check records' }));
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  mocked.getHankReadiness.mockResolvedValue(evidence);
  mocked.getHankKnowledge.mockResolvedValue(evidence);
  mocked.getHankShippingPacket.mockResolvedValue(evidence);
  mocked.getHankTrace.mockResolvedValue(evidence);
  mocked.getHankPurchasingImpact.mockResolvedValue({ ...evidence, draft_text: 'Please confirm PO-11 delivery dates.' });
});

it('presents coverage limits with inspectable source links and invalidates a changed target', async () => {
  const onNavigate = jest.fn();
  show({ kind: 'readiness', workOrderId: 7, onNavigate });
  expect(mocked.getHankReadiness).not.toHaveBeenCalled();
  check();
  await screen.findByText(evidence.summary);
  expect(screen.getByText(evidence.coverage_notes[0])).toBeInTheDocument();
  expect(screen.getByText(/Material coverage/)).toHaveTextContent('unknown');
  expect(mocked.getHankReadiness).toHaveBeenCalledWith(7, expect.any(AbortSignal));
  fireEvent.click(screen.getByRole('link', { name: 'WO-7 source' }));
  expect(onNavigate).toHaveBeenCalledTimes(1);
  fireEvent.change(screen.getByLabelText(/Work order/), { target: { value: '8' } });
  expect(screen.queryByText(evidence.summary)).not.toBeInTheDocument();
});

it.each(['knowledge', 'shipping'] as const)('loads the selected %s report only on explicit check', async kind => {
  show({ kind, workOrderId: 7, onNavigate: jest.fn() });
  check();
  await screen.findByText(evidence.summary);
  expect(kind === 'knowledge' ? mocked.getHankKnowledge : mocked.getHankShippingPacket).toHaveBeenCalledWith(
    7,
    expect.any(AbortSignal)
  );
  expect(mocked.getHankReadiness).not.toHaveBeenCalled();
});

it('copies a supplier draft for employee review without sending it', async () => {
  const writeText = jest.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  show({ kind: 'purchasing', purchaseOrderId: 11, onNavigate: jest.fn() });
  check();
  const draft = await screen.findByLabelText('Draft for your review');
  expect(draft).toHaveValue('Please confirm PO-11 delivery dates.');
  expect(draft).toHaveAttribute('readonly');
  fireEvent.click(screen.getByRole('button', { name: 'Copy draft' }));
  await screen.findByText('Draft copied.');
  expect(writeText).toHaveBeenCalledWith('Please confirm PO-11 delivery dates.');
  expect(screen.queryByRole('button', { name: /send/i })).not.toBeInTheDocument();
});

it('uses exact trimmed trace identifiers and shows permission refusals without stale evidence', async () => {
  mocked.getHankTrace.mockRejectedValueOnce({
    isAxiosError: true,
    response: { status: 403, data: { detail: 'Trace access is required.' } },
  });
  show({ kind: 'trace', onNavigate: jest.fn() });
  expect(screen.getByRole('button', { name: 'Check records' })).toBeDisabled();
  fireEvent.change(screen.getByLabelText('Trace type'), { target: { value: 'serial' } });
  fireEvent.change(screen.getByLabelText('Exact lot or serial'), { target: { value: ' SN-22 ' } });
  check();
  await screen.findByRole('alert');
  expect(screen.getByRole('alert')).toHaveTextContent('Trace access is required.');
  expect(mocked.getHankTrace).toHaveBeenCalledWith('serial', 'SN-22', expect.any(AbortSignal));
  expect(screen.queryByText(evidence.summary)).not.toBeInTheDocument();
});

it('aborts an old-company report and ignores its late data', async () => {
  let resolve!: (value: HankEvidence) => void;
  mocked.getHankReadiness.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  show({ kind: 'readiness', workOrderId: 7, onNavigate: jest.fn() });
  check();
  await waitFor(() => expect(mocked.getHankReadiness).toHaveBeenCalledTimes(1));
  const signal = mocked.getHankReadiness.mock.calls[0][1];
  session(5);
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(evidence));
  expect(screen.queryByText(evidence.summary)).not.toBeInTheDocument();
  expect(screen.getByRole('alert')).toHaveTextContent('session changed');
});
