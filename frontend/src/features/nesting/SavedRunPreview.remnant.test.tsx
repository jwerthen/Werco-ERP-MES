import React from 'react';
import { render, screen } from '@testing-library/react';
import api from '../../services/api';
import SavedRunPreview from './SavedRunPreview';
import { remnantStageFixture } from '../../test-utils/remnantStageFixtures';
jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getNestingRunCheckpoint: jest.fn(), getNestingDraftRevision: jest.fn() },
}));
beforeEach(() => jest.clearAllMocks());
test('saved residual preview fetches its recorded predecessor and displays original instance numbers', async () => {
  const f = await remnantStageFixture();
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(f.source);
  jest.mocked(api.getNestingRunCheckpoint).mockImplementation(async (_id, sequence) => f.checkpoints[sequence - 1]);
  render(<SavedRunPreview run={f.detail} sequence={3} />);
  const drawing = await screen.findByRole('img', {
    name: 'Saved remaining full-sheet layout: actual part and material shapes',
  });
  expect(drawing).toHaveTextContent('original instance 2');
  expect(api.getNestingRunCheckpoint).toHaveBeenCalledWith(f.detail.id, 2, expect.any(AbortSignal));
});
test('saved recorded-piece view renders its polygon rather than its bounding rectangle', async () => {
  const f = await remnantStageFixture();
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(f.source);
  jest.mocked(api.getNestingRunCheckpoint).mockResolvedValue(f.checkpoints[1]);
  render(<SavedRunPreview run={f.detail} sequence={2} />);
  const drawing = await screen.findByRole('img', {
    name: 'Saved recorded-piece layout: actual part and material shapes',
  });
  expect(drawing.querySelector('path[fill-rule="evenodd"]')).not.toBeNull();
  expect(drawing.querySelector('rect')).toBeNull();
  expect(screen.getByText(/Availability and eligibility remain unverified/)).toBeInTheDocument();
});
test('a zero-residual checkpoint never fabricates a sheet preview', async () => {
  const f = await remnantStageFixture(1);
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(f.source);
  jest.mocked(api.getNestingRunCheckpoint).mockImplementation(async (_id, sequence) => f.checkpoints[sequence - 1]);
  render(<SavedRunPreview run={f.detail} sequence={3} />);
  expect(await screen.findByText(/requires zero full sheets/)).toBeInTheDocument();
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});
