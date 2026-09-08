import React from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import SavedRuns from './SavedRuns';
import { readyRuntime, runPage, savedRunFixture } from '../../test-utils/nestingRunFixtures';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    listNestingRuns: jest.fn(),
    getNestingRuntime: jest.fn(),
    getNestingRun: jest.fn(),
    startNestingRun: jest.fn(),
    cancelNestingRun: jest.fn(),
    getNestingRunReport: jest.fn(),
    getNestingRunCheckpoint: jest.fn(),
    getNestingDraftRevision: jest.fn(),
  },
}));
const list = jest.mocked(api.listNestingRuns);
const get = jest.mocked(api.getNestingRun);
const start = jest.mocked(api.startNestingRun);
beforeEach(() => {
  jest.clearAllMocks();
  list.mockResolvedValue(runPage());
  jest.mocked(api.getNestingRuntime).mockResolvedValue(readyRuntime);
  get.mockResolvedValue(savedRunFixture().detail);
  start.mockResolvedValue(savedRunFixture().detail);
});
afterEach(() => jest.useRealTimers());

test('reads history only until explicit start and submits exact saved revision/hash/company', async () => {
  const { source } = savedRunFixture();
  render(<SavedRuns target={source} canStart onBack={jest.fn()} />);
  await screen.findByText('No calculations saved for this revision.');
  expect(start).not.toHaveBeenCalled();
  expect(list).toHaveBeenCalledWith(41, 3, 1, expect.any(AbortSignal));
  const button = screen.getByRole('button', { name: 'Calculate saved revision' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
  expect(start.mock.calls[0][0]).toEqual({
    draft_id: 41,
    revision_number: 3,
    input_sha256: source.content_sha256,
    expected_company_id: 2,
    request_key: expect.any(String),
  });
});

test('an uncertain start retries the identical request key and prevents concurrent duplicate clicks', async () => {
  let reject!: (error: Error) => void;
  start.mockImplementationOnce(
    () =>
      new Promise((_resolve, fail) => {
        reject = fail;
      })
  );
  render(<SavedRuns target={savedRunFixture().source} canStart onBack={jest.fn()} />);
  const button = await screen.findByRole('button', { name: 'Calculate saved revision' });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  fireEvent.click(button);
  expect(start).toHaveBeenCalledTimes(1);
  const original = start.mock.calls[0][0];
  await act(async () => reject(new Error('Synthetic lost response')));
  fireEvent.click(await screen.findByRole('button', { name: 'Retry calculation request' }));
  await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
  expect(start.mock.calls[1][0]).toEqual(original);
});

test('unmount aborts active polling and prevents a scheduled follow-up', async () => {
  jest.useFakeTimers();
  const { source, detail } = savedRunFixture();
  const running = { ...detail, status: 'RUNNING' as const, finished_at: null };
  list.mockResolvedValue(runPage([running]));
  get.mockResolvedValue(running);
  const view = render(<SavedRuns target={source} canStart onBack={jest.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review run #11' }));
  await screen.findByRole('button', { name: 'Cancel calculation' });
  expect(get).toHaveBeenCalledTimes(1);
  const signal = get.mock.calls[0][1];
  view.unmount();
  expect(signal?.aborted).toBe(true);
  await act(async () => {
    jest.advanceTimersByTime(10000);
  });
  expect(get).toHaveBeenCalledTimes(1);
});

test('finished search still explains unplaced parts and read-only users cannot start or cancel', async () => {
  const { source, detail } = savedRunFixture();
  const incomplete = {
    ...detail,
    completed_count: 0,
    checkpoints: [{ ...detail.checkpoints[0], complete: false, placed: 1, unplaced: 1 }],
  };
  list.mockResolvedValue(runPage([incomplete]));
  get.mockResolvedValue(incomplete);
  render(<SavedRuns target={source} canStart={false} onBack={jest.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review run #11' }));
  await screen.findByText('Review incomplete layout', { exact: false });
  expect(screen.getByText(/0 options fit their complete material group/)).toBeInTheDocument();
  expect(screen.getByText(/Finished means the planned search ended/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Calculate saved revision' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Cancel calculation' })).not.toBeInTheDocument();
  expect(start).not.toHaveBeenCalled();
});

test.each([{ company_id: 99 }, { id: 12 }])('refuses mismatched selected detail %s', async mismatch => {
  const { source, detail } = savedRunFixture();
  list.mockResolvedValue(runPage([detail]));
  get.mockResolvedValue({ ...detail, ...mismatch });
  render(<SavedRuns target={source} canStart onBack={jest.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: 'Review run #11' }));
  expect(await screen.findByRole('alert')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Download saved report' })).not.toBeInTheDocument();
});
