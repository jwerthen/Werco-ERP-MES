import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import SpacingPolicyControls from './SpacingPolicyControls';
import api from '../../services/api';
import { createBlankQuote, type Quote } from './lib/quoting';
import { inToMm } from './lib/units';
import { policySnapshot } from '../../test-utils/nestingPolicyFixtures';
import type { NestingPolicyResolution } from '../../types/nestingPolicy';

jest.mock('../../services/api', () => ({ __esModule: true, default: { resolveNestingSpacingPolicy: jest.fn() } }));
jest.mock('./SpacingPolicyManager', () => ({
  __esModule: true,
  default: ({ onChanged }: { onChanged: () => void }) => (
    <button onClick={onChanged}>Synthetic completed policy decision</button>
  ),
}));
const resolve = jest.mocked(api.resolveNestingSpacingPolicy);
const changed = jest.fn();
const resolution: NestingPolicyResolution = {
  schema_version: 1,
  status: 'resolved',
  policy: policySnapshot,
  explanation: 'Synthetic family-level approval.',
};
const blank = () => createBlankQuote();
function mount(quote: Quote = blank()) {
  return render(<SpacingPolicyControls companyId={2} quote={quote} canManage={false} onChange={changed} />);
}
beforeEach(() => {
  jest.clearAllMocks();
  resolve.mockResolvedValue(resolution);
});

test('resolving is a read; applying the exact reviewed allowances is a separate explicit action', async () => {
  mount();
  expect(resolve).not.toHaveBeenCalled();
  expect(screen.getByText('Unreviewed starting allowances')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
  await screen.findByRole('button', { name: 'Apply these allowances' });
  expect(resolve).toHaveBeenCalledWith({ material: 'Carbon steel', thickness_in: '0.125' }, expect.any(AbortSignal));
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Apply these allowances' }));
  expect(changed).toHaveBeenCalledWith({
    spacingMode: 'policy',
    spacingPolicy: policySnapshot,
    spacingOverride: undefined,
    gap: inToMm(0.125),
    margin: inToMm(0.375),
  });
});

test('changing material or company aborts a pending resolution and refuses its late result', async () => {
  let finish!: (value: NestingPolicyResolution) => void;
  resolve.mockImplementationOnce(
    () =>
      new Promise(done => {
        finish = done;
      })
  );
  const mounted = mount();
  fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
  const signal = resolve.mock.calls[0][1];
  mounted.rerender(
    <SpacingPolicyControls
      companyId={3}
      quote={{ ...blank(), material: 'Aluminum' }}
      canManage={false}
      onChange={changed}
    />
  );
  expect(signal?.aborted).toBe(true);
  await act(async () => finish(resolution));
  expect(screen.queryByRole('button', { name: 'Apply these allowances' })).not.toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
});

test.each(['company', 'formula'])('refuses a resolved response with altered %s before applying', async kind => {
  resolve.mockResolvedValue({
    ...resolution,
    policy: { ...policySnapshot, ...(kind === 'company' ? { company_id: 3 } : { gap_in: '0.124' }) },
  });
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: 'Apply these allowances' })).not.toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
});

test.each(['company', 'band'])(
  'does not mark an altered saved %s current just because its IDs and hash match',
  async kind => {
    mount({
      ...blank(),
      spacingMode: 'policy',
      spacingPolicy: {
        ...policySnapshot,
        ...(kind === 'company' ? { company_id: 3 } : { band: { ...policySnapshot.band, minimum_margin_in: '0.5' } }),
      },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
    await screen.findByRole('button', { name: 'Apply these allowances' });
    expect(screen.queryByText(/Current publication checked/)).not.toBeInTheDocument();
    expect(screen.getByText(/Historical snapshot/)).toBeInTheDocument();
    expect(changed).not.toHaveBeenCalled();
  }
);

test('a fresh check of the identical applied snapshot can update its read timestamp without changing allowances', async () => {
  mount({
    ...blank(),
    spacingMode: 'policy',
    spacingPolicy: { ...policySnapshot, resolved_at: '2026-09-08T12:30:00Z' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
  await screen.findByText(/Current publication checked/);
  expect(changed).not.toHaveBeenCalled();
});

test('a completed governance decision aborts a pending check and prevents applying its late publication', async () => {
  let finish!: (value: NestingPolicyResolution) => void;
  resolve.mockImplementationOnce(
    () =>
      new Promise(done => {
        finish = done;
      })
  );
  mount({ ...blank(), spacingMode: 'policy', spacingPolicy: policySnapshot });
  fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
  const signal = resolve.mock.calls[0][1];
  fireEvent.click(screen.getByRole('button', { name: 'Synthetic completed policy decision' }));
  expect(signal?.aborted).toBe(true);
  expect(screen.getByRole('button', { name: 'Check current spacing policy' })).toBeEnabled();
  await act(async () => finish(resolution));
  expect(screen.queryByRole('button', { name: 'Apply these allowances' })).not.toBeInTheDocument();
  expect(screen.queryByText(/Current publication checked/)).not.toBeInTheDocument();
  expect(changed).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Check current spacing policy' }));
  await screen.findByRole('button', { name: 'Apply these allowances' });
  expect(resolve).toHaveBeenCalledTimes(2);
});

test('custom spacing needs a reason and clears the policy claim while retaining the numerical allowances', async () => {
  mount({ ...blank(), spacingMode: 'policy', spacingPolicy: policySnapshot });
  fireEvent.click(screen.getByRole('button', { name: 'Use custom spacing' }));
  fireEvent.click(screen.getByRole('button', { name: 'Use custom allowances' }));
  await screen.findByRole('alert');
  expect(changed).not.toHaveBeenCalled();
  fireEvent.change(screen.getByLabelText('Estimator reason'), {
    target: { value: 'Reviewed customer stock constraint' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Use custom allowances' }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(changed.mock.calls[0][0]).toEqual({
    spacingPolicy: undefined,
    spacingMode: 'manual',
    spacingOverride: {
      schema_version: 1,
      reason: 'Reviewed customer stock constraint',
      changed_at: expect.any(String),
    },
  });
});
