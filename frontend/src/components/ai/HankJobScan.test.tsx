import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { ScanResolveResult } from '../../types/scan';
import { HankJobScan } from './HankJobScan';

jest.mock('../../services/api', () => ({ __esModule: true, default: { resolveScanAction: jest.fn() } }));
const mocked = jest.mocked(api);
const operation: ScanResolveResult = {
  kind: 'operation',
  code: 'OP:71',
  legal_actions: ['report_production'],
  blockers: {},
  warning: null,
  routing_revision_check: null,
  operation: {
    id: 71,
    sequence: 10,
    operation_number: null,
    name: 'Mill',
    status: 'in_progress',
    work_order_id: 7,
    work_order_number: 'WO-7',
    work_order_status: 'in_progress',
    part_number: 'P1',
    part_name: 'Bracket',
    work_center_id: 1,
    work_center_name: 'Mill',
    work_center_match: null,
    quantity_complete: 0,
    target_quantity: 10,
  },
};
function session(cid = 4) {
  sessionStorage.setItem('token', `header.${btoa(JSON.stringify({ sub: '17', cid, type: 'access' }))}.sig`);
}
function scan() {
  fireEvent.change(screen.getByLabelText('Scan job or operation label'), { target: { value: ' OP:71 ' } });
  fireEvent.submit(screen.getByRole('form', { name: 'Select a job by scan' }));
}
beforeEach(() => {
  jest.resetAllMocks();
  sessionStorage.clear();
  session();
  mocked.resolveScanAction.mockResolvedValue(operation);
});

it('selects the exact operation and job without performing a production action', async () => {
  const onSelect = jest.fn();
  render(<HankJobScan onSelect={onSelect} />);
  scan();
  await screen.findByText('Selected WO-7 · Mill.');
  expect(mocked.resolveScanAction).toHaveBeenCalledWith('OP:71', undefined, expect.any(AbortSignal));
  expect(onSelect).toHaveBeenCalledWith({ workOrderId: 7, operationId: 71, label: 'WO-7 · Mill' });
  expect(mocked.resolveScanAction).toHaveBeenCalledTimes(1);
});

it('rejects employee badge scans as job selections', async () => {
  mocked.resolveScanAction.mockResolvedValue({
    kind: 'employee',
    code: 'EMP:4',
    employee_id: '4',
    first_name: 'Sam',
    last_initial: 'J',
  });
  const onSelect = jest.fn();
  render(<HankJobScan onSelect={onSelect} />);
  scan();
  await screen.findByText('Scan a work-order or operation label to select a job.');
  expect(onSelect).not.toHaveBeenCalled();
});

it('aborts pending scans on company change and never selects their late result', async () => {
  let resolve!: (value: ScanResolveResult) => void;
  mocked.resolveScanAction.mockReturnValue(
    new Promise(done => {
      resolve = done;
    })
  );
  const onSelect = jest.fn();
  render(<HankJobScan onSelect={onSelect} />);
  scan();
  await waitFor(() => expect(mocked.resolveScanAction).toHaveBeenCalledTimes(1));
  const signal = mocked.resolveScanAction.mock.calls[0][2];
  session(5);
  act(() => window.dispatchEvent(new Event('werco:auth-token-changed')));
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve(operation));
  expect(onSelect).not.toHaveBeenCalled();
  expect(screen.getByRole('alert')).toHaveTextContent('session changed');
});
