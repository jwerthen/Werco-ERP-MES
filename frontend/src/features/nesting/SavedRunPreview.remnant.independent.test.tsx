import React from 'react';
import { act, render, screen, waitFor } from '@testing-library/react';
import api from '../../services/api';
import type { NestingRunCheckpoint } from '../../types/nestingRun';
import { jsonCopy, remnantStageFixture } from '../../test-utils/remnantStageFixtures';
import SavedRunPreview from './SavedRunPreview';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getNestingRunCheckpoint: jest.fn(), getNestingDraftRevision: jest.fn() },
}));
beforeEach(() => jest.clearAllMocks());
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {
    resolve = done;
  });
  return { promise, resolve };
};

test('a predecessor from the previous run cannot replace a newly selected recorded-piece preview', async () => {
  const fixture = await remnantStageFixture();
  const late = deferred<NestingRunCheckpoint>();
  const signals: AbortSignal[] = [];
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(fixture.source);
  jest.mocked(api.getNestingRunCheckpoint).mockImplementation(async (id, sequence, signal) => {
    if (id === fixture.detail.id && sequence === 2) {
      if (signal) signals.push(signal);
      return late.promise;
    }
    return fixture.checkpoints[sequence - 1];
  });
  const mounted = render(<SavedRunPreview run={fixture.detail} sequence={3} />);
  await waitFor(() => expect(signals).toHaveLength(1));
  mounted.rerender(<SavedRunPreview run={{ ...fixture.detail, id: fixture.detail.id + 1 }} sequence={2} />);
  expect(
    await screen.findByRole('img', { name: 'Saved recorded-piece layout: actual part and material shapes' })
  ).toBeInTheDocument();
  expect(signals[0].aborted).toBe(true);
  await act(async () => late.resolve(fixture.checkpoints[1]));
  expect(
    screen.queryByRole('img', { name: 'Saved remaining full-sheet layout: actual part and material shapes' })
  ).not.toBeInTheDocument();
  expect(
    screen.getByRole('img', { name: 'Saved recorded-piece layout: actual part and material shapes' })
  ).toBeInTheDocument();
});

test('unmount aborts an outstanding predecessor read without processing its late evidence', async () => {
  const fixture = await remnantStageFixture();
  const late = deferred<NestingRunCheckpoint>();
  let signal: AbortSignal | undefined;
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(fixture.source);
  jest.mocked(api.getNestingRunCheckpoint).mockImplementation(async (_id, sequence, requestSignal) => {
    if (sequence === 2) {
      signal = requestSignal;
      return late.promise;
    }
    return fixture.checkpoints[sequence - 1];
  });
  const mounted = render(<SavedRunPreview run={fixture.detail} sequence={3} />);
  await waitFor(() => expect(signal).toBeDefined());
  mounted.unmount();
  expect(signal!.aborted).toBe(true);
  await act(async () => late.resolve(fixture.checkpoints[1]));
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});

test('wrong-company source evidence never becomes a saved stock preview', async () => {
  const fixture = await remnantStageFixture();
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue({ ...fixture.source, company_id: 999 });
  jest.mocked(api.getNestingRunCheckpoint).mockResolvedValue(fixture.checkpoints[1]);
  render(<SavedRunPreview run={fixture.detail} sequence={2} />);
  expect(await screen.findByRole('alert')).toHaveTextContent(/identity/i);
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});

test('matching source with a forged predecessor receipt cannot authorize a residual instance map', async () => {
  const fixture = await remnantStageFixture();
  const forged = jsonCopy(fixture.checkpoints[1]);
  forged.content_sha256 = 'f'.repeat(64);
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(fixture.source);
  jest
    .mocked(api.getNestingRunCheckpoint)
    .mockImplementation(async (_id, sequence) => (sequence === 2 ? forged : fixture.checkpoints[sequence - 1]));
  render(<SavedRunPreview run={fixture.detail} sequence={3} />);
  expect(await screen.findByRole('alert')).toHaveTextContent(/receipt/i);
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});
