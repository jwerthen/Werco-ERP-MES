/**
 * Quality — useUnsavedChanges discard guard on the NCR / CAR / FAI create modals.
 *
 * Clones the Customers/Materials formA11yUnsavedGuard template:
 *   - clean (untouched) form -> Cancel closes with NO confirm prompt,
 *   - dirty + declined       -> modal stays open, the entry is preserved,
 *   - dirty + confirmed      -> modal closes, nothing created,
 *   - successful SAVE        -> closes directly, NO prompt,
 *   - a beforeunload listener is registered only while a form is dirty.
 *
 * The guard is wired into each Modal's onClose (header X / Escape included),
 * not just the Cancel button. NCR gets the full matrix; CAR and FAI get the
 * dirty-prompt lock to prove the wiring generalizes.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Quality from './Quality';
import api from '../services/api';

jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ can: () => true, canAny: () => true }),
}));

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getNCRs: jest.fn(),
    getCARs: jest.fn(),
    getFAIs: jest.fn(),
    getQualitySummary: jest.fn(),
    getParts: jest.fn(),
    createNCR: jest.fn(),
    createCAR: jest.fn(),
    createFAI: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const renderQuality = () =>
  render(
    <MemoryRouter initialEntries={['/quality']}>
      <Quality />
    </MemoryRouter>
  );

/** Load the page and open the New NCR modal; returns the Title input + form. */
async function openNCRModal() {
  renderQuality();
  fireEvent.click(await screen.findByRole('button', { name: /new ncr/i }));
  await screen.findByRole('heading', { name: 'New Non-Conformance Report' });
  const titleInput = screen.getByLabelText(/^Title/);
  return { titleInput, form: titleInput.closest('form') as HTMLFormElement };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getNCRs.mockResolvedValue([] as any);
  mockedApi.getCARs.mockResolvedValue([] as any);
  mockedApi.getFAIs.mockResolvedValue([] as any);
  mockedApi.getQualitySummary.mockResolvedValue({ open_ncrs: 0, open_cars: 0, pending_fais: 0 } as any);
  mockedApi.getParts.mockResolvedValue([{ id: 5, part_number: 'PN-5', name: 'Bracket' }] as any);
});

describe('Quality — New NCR unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing a clean (untouched) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openNCRModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New Non-Conformance Report' })).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user declines the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { titleInput } = await openNCRModal();

    fireEvent.change(titleInput, { target: { value: 'Surface scratch' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^Title/)).toHaveValue('Surface scratch');
  });

  it('prompts and closes (discarding the entry) when the user confirms', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const { titleInput } = await openNCRModal();

    fireEvent.change(titleInput, { target: { value: 'Surface scratch' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New Non-Conformance Report' })).not.toBeInTheDocument()
    );
    expect(mockedApi.createNCR).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful save even though the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockedApi.createNCR.mockResolvedValue({ id: 1 } as any);
    const { titleInput, form } = await openNCRModal();

    fireEvent.change(titleInput, { target: { value: 'Surface scratch' } });
    fireEvent.change(screen.getByLabelText(/^Description/), { target: { value: 'Deep scratch on face' } });
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createNCR).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New Non-Conformance Report' })).not.toBeInTheDocument()
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('registers a beforeunload guard only while the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const addSpy = jest.spyOn(window, 'addEventListener');
    const { titleInput } = await openNCRModal();

    const beforeUnloadCalls = () =>
      addSpy.mock.calls.filter(([type]) => type === 'beforeunload');

    expect(beforeUnloadCalls()).toHaveLength(0);

    fireEvent.change(titleInput, { target: { value: 'Surface scratch' } });
    await waitFor(() => expect(beforeUnloadCalls().length).toBeGreaterThan(0));
    addSpy.mockRestore();
  });
});

describe('Quality — CAR and FAI modals share the guard wiring', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('New CAR: dirty Cancel prompts and a decline preserves the entry', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    renderQuality();
    fireEvent.click(await screen.findByRole('button', { name: 'CAR' }));
    // The empty CAR list also renders an EmptyState "New CAR" action — either opens the modal.
    fireEvent.click((await screen.findAllByRole('button', { name: /new car/i }))[0]);
    await screen.findByRole('heading', { name: 'New Corrective Action Request' });

    fireEvent.change(screen.getByLabelText(/^Title/), { target: { value: 'Recurring burr' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^Title/)).toHaveValue('Recurring burr');
  });

  it('New FAI: dirty Cancel prompts; confirming closes without creating', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    renderQuality();
    fireEvent.click(await screen.findByRole('button', { name: 'FAI' }));
    // The empty FAI list also renders an EmptyState "New FAI" action — either opens the modal.
    fireEvent.click((await screen.findAllByRole('button', { name: /new fai/i }))[0]);
    const heading = await screen.findByRole('heading', { name: /New First Article Inspection/i });
    expect(heading).toBeInTheDocument();

    // Dirty the form via the Part select (the FAI form is select/checkbox only).
    fireEvent.change(screen.getByLabelText(/^Part/), { target: { value: '5' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /New First Article Inspection/i })).not.toBeInTheDocument()
    );
    expect(mockedApi.createFAI).not.toHaveBeenCalled();
  });
});
