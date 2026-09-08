import React from 'react';
import { render, screen } from '@testing-library/react';
import api from '../../services/api';
import SavedRunPreview from './SavedRunPreview';
import { savedRunFixture } from '../../test-utils/nestingRunFixtures';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getNestingRunCheckpoint: jest.fn(),
    getNestingDraftRevision: jest.fn(),
  },
}));

beforeEach(() => jest.clearAllMocks());

test('loads the exact saved source and renders true circle/cutout geometry with zero-credit review', async () => {
  const { source, detail, checkpoint } = savedRunFixture();
  jest.mocked(api.getNestingRunCheckpoint).mockResolvedValue(checkpoint);
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue(source);
  render(<SavedRunPreview run={detail} sequence={1} />);
  const drawing = await screen.findByRole('img', { name: 'Actual saved part shapes on sheet 1' });
  expect(api.getNestingDraftRevision).toHaveBeenCalledWith(41, 3, expect.any(AbortSignal));
  const shapes = Array.from(drawing.querySelectorAll('path[fill-rule="evenodd"]')).filter(shape =>
    shape.querySelector('title')?.textContent?.startsWith('Synthetic')
  );
  expect(shapes).toHaveLength(2);
  expect(shapes.some(shape => (shape.getAttribute('d')?.match(/a/gi) ?? []).length >= 4)).toBe(true);
  expect(screen.getByText('$0 credited')).toBeInTheDocument();
});

test('refuses a foreign source even if its geometry and supplied hashes otherwise match', async () => {
  const { source, detail, checkpoint } = savedRunFixture();
  jest.mocked(api.getNestingRunCheckpoint).mockResolvedValue(checkpoint);
  jest.mocked(api.getNestingDraftRevision).mockResolvedValue({ ...source, company_id: 99 });
  render(<SavedRunPreview run={detail} sequence={1} />);
  expect(await screen.findByRole('alert')).toHaveTextContent('identity does not match');
  expect(screen.queryByRole('img')).not.toBeInTheDocument();
});
