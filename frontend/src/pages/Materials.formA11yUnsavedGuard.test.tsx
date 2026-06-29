/**
 * Materials — Batch 6 form-level locks (FormField label association +
 * useUnsavedChanges discard guard).
 *
 * Second adopted form (Customers is the reference) proving the Batch 6 contract
 * generalizes across pages:
 *
 *   1. LABEL ASSOCIATION WORKS. Modal fields are reachable via getByLabelText —
 *      each <label> is wired to its control through FormField's htmlFor/id.
 *      Before Batch 6 these labels had no `for` and getByLabelText could not
 *      resolve them.
 *
 *   2. UNSAVED-CHANGES GUARD. Editing then trying to Cancel routes through
 *      useUnsavedChanges.confirmDiscard() (window.confirm):
 *        - clean form           -> closes, no prompt,
 *        - dirty + declined      -> stays open, edit preserved,
 *        - dirty + confirmed     -> closes, nothing saved,
 *        - successful SAVE       -> closes directly, NO prompt (the handler calls
 *          closeModal() and never confirmDiscard — see the code comment on
 *          requestCloseModal in Materials.tsx). This is the key lock.
 *
 * Materials uses the default no-op Toast context, so no ToastProvider is needed.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Materials from './Materials';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getMaterials: jest.fn(),
    createMaterial: jest.fn(),
    updateMaterial: jest.fn(),
    deleteMaterial: jest.fn(),
    importMaterialsCsv: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

function renderMaterials() {
  return render(
    <MemoryRouter initialEntries={['/materials']}>
      <Materials />
    </MemoryRouter>
  );
}

/** Open the New Item modal and return the Item Number input + its <form>. */
async function openCreateModal() {
  fireEvent.click(await screen.findByRole('button', { name: /new item/i }));
  const itemNumber = await screen.findByLabelText(/Item Number/);
  return { itemNumber, form: itemNumber.closest('form') as HTMLFormElement };
}

describe('Materials form — label association (FormField)', () => {
  let confirmSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getMaterials.mockResolvedValue([]);
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    confirmSpy.mockRestore();
  });

  it('exposes every modal field via its label (htmlFor/id wired by FormField)', async () => {
    renderMaterials();
    const { form } = await openCreateModal();
    const scoped = within(form);

    expect(scoped.getByLabelText(/Item Number/)).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Type')).toBeInstanceOf(HTMLSelectElement);
    expect(scoped.getByLabelText(/Name/)).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Unit of Measure')).toBeInstanceOf(HTMLSelectElement);
    expect(scoped.getByLabelText('Standard Cost ($)')).toBeInstanceOf(HTMLInputElement);
    expect(scoped.getByLabelText('Description')).toBeInstanceOf(HTMLTextAreaElement);
  });

  it('marks the required fields with aria-required and leaves optional ones unset', async () => {
    renderMaterials();
    const { itemNumber } = await openCreateModal();
    expect(itemNumber).toHaveAttribute('aria-required', 'true');
    expect(screen.getByLabelText(/^Name/)).toHaveAttribute('aria-required', 'true');
    // "Type" is not required.
    expect(screen.getByLabelText('Type')).not.toHaveAttribute('aria-required');
  });
});

describe('Materials form — unsaved-changes discard guard', () => {
  let confirmSpy: jest.SpyInstance;

  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getMaterials.mockResolvedValue([]);
  });

  afterEach(() => {
    confirmSpy?.mockRestore();
  });

  it('does NOT prompt when closing a clean (untouched) form', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    renderMaterials();
    await openCreateModal();

    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).not.toHaveBeenCalled();
    await waitFor(() =>
      expect(screen.queryByLabelText(/Item Number/)).not.toBeInTheDocument()
    );
  });

  it('prompts and keeps the modal open when the user cancels the discard confirm', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    renderMaterials();
    const { itemNumber } = await openCreateModal();

    fireEvent.change(itemNumber, { target: { value: 'RM-1001' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Item Number/)).toHaveValue('RM-1001');
  });

  it('prompts and closes when the user confirms the discard', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
    renderMaterials();
    const { itemNumber } = await openCreateModal();

    fireEvent.change(itemNumber, { target: { value: 'RM-1001' } });
    fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.queryByLabelText(/Item Number/)).not.toBeInTheDocument()
    );
    expect(mockedApi.createMaterial).not.toHaveBeenCalled();
  });

  it('does NOT prompt on a successful save even though the form is dirty', async () => {
    confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    mockedApi.createMaterial.mockResolvedValue({
      id: 1,
      part_number: 'RM-1001',
      name: 'Aluminum Plate',
    });
    renderMaterials();
    const { itemNumber, form } = await openCreateModal();

    // The required Name field must be filled for a realistic submit.
    fireEvent.change(itemNumber, { target: { value: 'RM-1001' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'Aluminum Plate' } });

    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createMaterial).toHaveBeenCalledTimes(1));
    await waitFor(() =>
      expect(screen.queryByLabelText(/Item Number/)).not.toBeInTheDocument()
    );
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});
