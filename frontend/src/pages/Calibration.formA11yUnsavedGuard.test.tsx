/**
 * Calibration — useUnsavedChanges discard guard on the equipment and
 * record-calibration modals.
 *
 * Clones the Customers/Materials formA11yUnsavedGuard template:
 *   - clean (untouched) form -> Cancel closes with NO confirm prompt (for the
 *     record modal the pre-filled date/provider snapshot counts as clean),
 *   - dirty + declined       -> modal stays open, the entry is preserved,
 *   - dirty + confirmed      -> modal closes, nothing saved,
 *   - successful SAVE        -> closes directly, NO prompt,
 *   - a beforeunload listener is registered only while the form is dirty.
 *
 * The guard is wired into each Modal's onClose (Escape included), not just the
 * Cancel button.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Calibration from './Calibration';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getEquipment: jest.fn(),
    createEquipment: jest.fn(),
    updateEquipment: jest.fn(),
    recordCalibration: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const equipment = [
  {
    id: 1,
    equipment_id: 'CAL-001',
    name: 'Digital Caliper',
    calibration_interval_days: 365,
    calibration_provider: 'Acme Labs',
    status: 'active',
    is_active: true,
  },
];

const renderPage = () =>
  render(
    <MemoryRouter initialEntries={['/calibration']}>
      <Calibration />
    </MemoryRouter>
  );

/** Load the page and open the Add Equipment modal; returns the ID input + form. */
async function openAddEquipmentModal() {
  renderPage();
  fireEvent.click((await screen.findAllByRole('button', { name: /add equipment/i }))[0]);
  await screen.findByRole('heading', { name: 'Add Equipment' });
  const idInput = screen.getByLabelText(/Equipment ID/);
  return { idInput, form: idInput.closest('form') as HTMLFormElement };
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getEquipment.mockResolvedValue(equipment as any);
});

describe('Calibration — equipment form unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing a clean (untouched) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    await openAddEquipmentModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Add Equipment' })).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user declines the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    const { idInput } = await openAddEquipmentModal();

    fireEvent.change(idInput, { target: { value: 'CAL-042' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Equipment ID/)).toHaveValue('CAL-042');
  });

  it('prompts and closes (discarding the entry) when the user confirms', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const { idInput } = await openAddEquipmentModal();

    fireEvent.change(idInput, { target: { value: 'CAL-042' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Add Equipment' })).not.toBeInTheDocument()
    );
    expect(mockedApi.createEquipment).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful save even though the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockedApi.createEquipment.mockResolvedValue({ id: 2 } as any);
    const { idInput, form } = await openAddEquipmentModal();

    fireEvent.change(idInput, { target: { value: 'CAL-042' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Micrometer' } });
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createEquipment).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Add Equipment' })).not.toBeInTheDocument()
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('registers a beforeunload guard only while the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    const addSpy = jest.spyOn(window, 'addEventListener');
    const { idInput } = await openAddEquipmentModal();

    const beforeUnloadCalls = () =>
      addSpy.mock.calls.filter(([type]) => type === 'beforeunload');

    expect(beforeUnloadCalls()).toHaveLength(0);

    fireEvent.change(idInput, { target: { value: 'CAL-042' } });
    await waitFor(() => expect(beforeUnloadCalls().length).toBeGreaterThan(0));
    addSpy.mockRestore();
  });
});

describe('Calibration — record-calibration form unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  /** "CAL-001" renders in both the desktop table and the mobile card list —
   *  pick the table row occurrence. */
  async function findEquipmentRow() {
    const matches = await screen.findAllByText('CAL-001');
    return matches.map((el) => el.closest('tr')).find(Boolean) as HTMLElement;
  }

  async function openRecordModal() {
    renderPage();
    const row = await findEquipmentRow();
    fireEvent.click(within(row).getByRole('button', { name: 'Record Calibration' }));
    await screen.findByRole('heading', { name: 'Record Calibration' });
  }

  it('treats the pre-filled form as clean and only prompts after an edit', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    await openRecordModal();

    // Pre-filled (today's date + provider) but untouched: closes silently.
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Record Calibration' })).not.toBeInTheDocument()
    );

    // Reopen and edit — now Cancel prompts and a decline preserves the entry.
    const row = await findEquipmentRow();
    fireEvent.click(within(row).getByRole('button', { name: 'Record Calibration' }));
    await screen.findByRole('heading', { name: 'Record Calibration' });
    fireEvent.change(screen.getByLabelText(/Certificate/), { target: { value: 'CERT-9' } });

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Certificate/)).toHaveValue('CERT-9');
    expect(mockedApi.recordCalibration).not.toHaveBeenCalled();
  });
});

describe('Calibration — Edit Equipment unsaved-changes guard', () => {
  let confirmSpy: jest.SpyInstance;

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  /** "CAL-001" renders in both the desktop table and the mobile card list —
   *  pick the table row occurrence. */
  async function findEquipmentRow() {
    const matches = await screen.findAllByText('CAL-001');
    return matches.map((el) => el.closest('tr')).find(Boolean) as HTMLElement;
  }

  it('treats the row-prefilled edit form as clean and only prompts after an edit', async () => {
    // Edit mode snapshots the row's values in handleEdit, so opening and
    // immediately closing must NOT prompt (a blank-shape comparison would
    // false-flag every edit open as dirty).
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    renderPage();

    let row = await findEquipmentRow();
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    await screen.findByRole('heading', { name: 'Edit Equipment' });
    expect(screen.getByLabelText(/Equipment ID/)).toHaveValue('CAL-001');

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'Edit Equipment' })).not.toBeInTheDocument()
    );

    // Reopen, edit the name — now Cancel prompts and a decline preserves it.
    row = await findEquipmentRow();
    fireEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    await screen.findByRole('heading', { name: 'Edit Equipment' });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Digital Caliper 2' } });

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^Name/)).toHaveValue('Digital Caliper 2');
    expect(mockedApi.updateEquipment).not.toHaveBeenCalled();
  });
});
