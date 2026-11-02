import React from 'react';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import HankEngineering from './HankEngineering';
import api from '../services/api';
import { readDrawing } from '../features/hank-engineering/pdf';
import { matchEngineering, saveParts } from '../features/hank-engineering/entry';
import { reviewedPlan } from '../test-utils/hankEngineering';
import { getHankSessionScope, isHankReadOnlySession } from '../components/ai/hankSession';

jest.mock('../hooks/usePermissions', () => ({ usePermissions: () => ({ canAll: () => true, can: () => true }) }));
jest.mock('../services/api', () => ({ __esModule: true, default: { getWorkCenters: jest.fn() } }));
jest.mock('../features/hank-engineering/pdf', () => ({ readDrawing: jest.fn() }));
jest.mock('../features/hank-engineering/entry', () => ({
  matchEngineering: jest.fn(),
  saveParts: jest.fn(),
  saveBOM: jest.fn(),
  saveRouting: jest.fn(),
  createDraftWO: jest.fn(),
}));
jest.mock('../components/ui/PdfPreview', () => ({ __esModule: true, default: () => <p>Local source preview</p> }));
jest.mock('../components/ai/hankSession', () => ({
  ...jest.requireActual('../components/ai/hankSession'),
  getHankSessionScope: jest.fn(),
  isHankReadOnlySession: jest.fn(),
}));
const mockedRead = jest.mocked(readDrawing);
const mockedMatch = jest.mocked(matchEngineering);
const mockedSave = jest.mocked(saveParts);
beforeEach(() => {
  jest.clearAllMocks();
  jest.mocked(getHankSessionScope).mockReturnValue('user1-company1');
  jest.mocked(isHankReadOnlySession).mockReturnValue(false);
  URL.createObjectURL = jest.fn(() => 'blob:synthetic');
  URL.revokeObjectURL = jest.fn();
  mockedRead.mockResolvedValue(reviewedPlan());
  jest.mocked(api.getWorkCenters).mockResolvedValue([]);
  mockedMatch.mockResolvedValue({ parts: new Map() });
  mockedSave.mockResolvedValue(undefined);
});
async function load() {
  render(
    <MemoryRouter>
      <HankEngineering />
    </MemoryRouter>
  );
  fireEvent.change(screen.getByLabelText('Fabrication, assembly or detail PDF (up to 25 pages)'), {
    target: { files: [new File(['%PDF-'], 'drawing.pdf', { type: 'application/pdf' })] },
  });
  await screen.findByText('Local source preview');
  await waitFor(() => expect(screen.getByRole('button', { name: 'Match existing records' })).toBeEnabled());
}
test('review and match are inert; drawing edits invalidate approval and matches', async () => {
  await load();
  expect(mockedMatch).not.toHaveBeenCalled();
  expect(mockedSave).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Match existing records' }));
  await screen.findByRole('button', { name: 'Save missing Parts' });
  fireEvent.change(screen.getByLabelText('Drawing revision'), { target: { value: 'C' } });
  expect(screen.getByRole('button', { name: 'Match existing records' })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Save missing Parts' })).not.toBeInTheDocument();
});
test('double submission is locked; partial failure disables automatic retry and preserves receipts', async () => {
  await load();
  fireEvent.click(screen.getByRole('button', { name: 'Match existing records' }));
  const button = await screen.findByRole('button', { name: 'Save missing Parts' });
  let reject: (error: Error) => void = () => undefined;
  mockedSave.mockImplementation(async (_plan, guard, record) => {
    guard();
    record({ kind: 'part', id: 12, label: 'OBSERVED-PART', href: '/parts/12', action: 'created' });
    await new Promise<void>((_resolve, rejectPromise) => {
      reject = rejectPromise;
    });
  });
  fireEvent.click(button);
  fireEvent.click(button);
  expect(mockedSave).toHaveBeenCalledTimes(1);
  reject(new Error('Connection lost after one record'));
  await screen.findByText(/Automatic retry is disabled/);
  expect(button).toBeDisabled();
  expect(screen.getByRole('link', { name: 'OBSERVED-PART' })).toBeInTheDocument();
});
test('company switch clears drawings and late matching cannot continue entry', async () => {
  await load();
  let resolve: (value: { parts: Map<string, never> }) => void = () => undefined;
  mockedMatch.mockImplementation(async (_plan, guard) => {
    const value = await new Promise<{ parts: Map<string, never> }>(done => {
      resolve = done;
    });
    guard();
    return value;
  });
  fireEvent.click(screen.getByRole('button', { name: 'Match existing records' }));
  jest.mocked(getHankSessionScope).mockReturnValue('user1-company2');
  fireEvent(window, new Event('werco:auth-token-changed'));
  resolve({ parts: new Map() });
  await waitFor(() => expect(screen.queryByText('Local source preview')).not.toBeInTheDocument());
  expect(screen.queryByRole('button', { name: 'Save missing Parts' })).not.toBeInTheDocument();
  expect(mockedSave).not.toHaveBeenCalled();
});
test('read-only session cannot open a drawing for entry', () => {
  jest.mocked(isHankReadOnlySession).mockReturnValue(true);
  render(
    <MemoryRouter>
      <HankEngineering />
    </MemoryRouter>
  );
  expect(screen.getByLabelText('Fabrication, assembly or detail PDF (up to 25 pages)')).toBeDisabled();
});
