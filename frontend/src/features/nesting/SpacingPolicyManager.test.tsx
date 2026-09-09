import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import SpacingPolicyManager from './SpacingPolicyManager';
import api from '../../services/api';
import { NestingPortalContext } from './PortalContext';
import { policyContent, policyReceipt, policyState } from '../../test-utils/nestingPolicyFixtures';
import type { NestingPolicyReceipt } from '../../types/nestingPolicy';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNestingSpacingPolicies: jest.fn(),
    getNestingSpacingRevision: jest.fn(),
    createNestingSpacingRevision: jest.fn(),
    publishNestingSpacingPolicy: jest.fn(),
    withdrawNestingSpacingPolicy: jest.fn(),
  },
}));
const history = jest.mocked(api.getNestingSpacingPolicies);
const read = jest.mocked(api.getNestingSpacingRevision);
const publish = jest.mocked(api.publishNestingSpacingPolicy);
const changed = jest.fn();
function mount(companyId = 2, canManage = true) {
  return render(<SpacingPolicyManager companyId={companyId} canManage={canManage} onChanged={changed} />, {
    wrapper: ({ children }) => (
      <NestingPortalContext.Provider value={document.body}>{children}</NestingPortalContext.Provider>
    ),
  });
}
async function openReview() {
  fireEvent.click(screen.getByRole('button', { name: 'Review spacing policies' }));
  await screen.findByRole('button', { name: 'Review revision 1' });
  fireEvent.click(screen.getByRole('button', { name: 'Review revision 1' }));
  await screen.findByRole('region', { name: 'Policy revision details' });
}
async function prepareApproval() {
  await openReview();
  fireEvent.click(screen.getByRole('button', { name: 'Review approval' }));
  fireEvent.change(screen.getByLabelText('Decision reason'), { target: { value: 'Reviewed exact synthetic bands' } });
}
beforeEach(() => {
  jest.clearAllMocks();
  history.mockResolvedValue(policyState);
  read.mockResolvedValue({ ...policyState.revisions[0], content: policyContent });
  publish.mockResolvedValue(policyReceipt);
});

test('policy history is explicit and read-only viewers cannot publish a reviewed revision', async () => {
  mount(2, false);
  expect(history).not.toHaveBeenCalled();
  await openReview();
  expect(screen.queryByRole('button', { name: 'Review approval' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'New policy draft' })).not.toBeInTheDocument();
  expect(publish).not.toHaveBeenCalled();
});

test('approval binds the inspected revision and exact uncertain retry is single-flight', async () => {
  publish.mockRejectedValueOnce(new Error('Connection interrupted'));
  mount();
  await prepareApproval();
  expect(read).toHaveBeenCalledWith(1, expect.any(AbortSignal));
  expect(publish).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Approve this policy revision' }));
  await screen.findByRole('button', { name: 'Retry previous command' });
  const request = publish.mock.calls[0][0];
  expect(request).toEqual({
    expected_company_id: 2,
    expected_version: 1,
    request_key: expect.any(String),
    reason: 'Reviewed exact synthetic bands',
    revision_number: 1,
    content_sha256: policyState.revisions[0].content_sha256,
    effective_at: null,
  });
  let finish!: (value: NestingPolicyReceipt) => void;
  publish.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const retry = screen.getByRole('button', { name: 'Retry previous command' });
  fireEvent.click(retry);
  fireEvent.click(retry);
  expect(publish).toHaveBeenCalledTimes(2);
  expect(publish.mock.calls[1][0]).toEqual(request);
  await act(async () => finish(policyReceipt));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
});

test('a same-company receipt for a different reviewed hash cannot confirm approval', async () => {
  publish.mockResolvedValue({
    ...policyReceipt,
    revision: { ...policyReceipt.revision, content_sha256: 'b'.repeat(64) },
  });
  mount();
  await prepareApproval();
  fireEvent.click(screen.getByRole('button', { name: 'Approve this policy revision' }));
  await waitFor(() => expect(publish).toHaveBeenCalledTimes(1));
  await screen.findByRole('alert');
  expect(changed).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: 'Retry previous command' })).toBeInTheDocument();
});

test('company changes abort pending commands and clear the prior approval target', async () => {
  let finish!: (value: NestingPolicyReceipt) => void;
  publish.mockImplementationOnce(
    () =>
      new Promise(resolve => {
        finish = resolve;
      })
  );
  const mounted = mount();
  await prepareApproval();
  fireEvent.click(screen.getByRole('button', { name: 'Approve this policy revision' }));
  await waitFor(() => expect(publish).toHaveBeenCalledTimes(1));
  const signal = publish.mock.calls[0][1];
  history.mockResolvedValue({ ...policyState, policy: null, revisions: [], total_revisions: 0 });
  mounted.rerender(<SpacingPolicyManager companyId={3} canManage onChanged={changed} />);
  await waitFor(() => expect(signal?.aborted).toBe(true));
  expect(screen.queryByRole('region', { name: 'Policy revision details' })).not.toBeInTheDocument();
  await act(async () => finish(policyReceipt));
  expect(changed).not.toHaveBeenCalled();
  expect(screen.queryByRole('button', { name: 'Retry previous command' })).not.toBeInTheDocument();
});

test('review refuses different displayed bands under the selected immutable hash', async () => {
  read.mockResolvedValue({
    ...policyState.revisions[0],
    content: { ...policyContent, bands: [{ ...policyContent.bands[0], minimum_gap_in: '0.5' }] },
  });
  mount();
  fireEvent.click(screen.getByRole('button', { name: 'Review spacing policies' }));
  await screen.findByRole('button', { name: 'Review revision 1' });
  fireEvent.click(screen.getByRole('button', { name: 'Review revision 1' }));
  await screen.findByRole('alert');
  expect(screen.queryByRole('button', { name: 'Review approval' })).not.toBeInTheDocument();
  expect(publish).not.toHaveBeenCalled();
});
