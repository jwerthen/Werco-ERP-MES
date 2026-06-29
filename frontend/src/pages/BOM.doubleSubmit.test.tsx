/**
 * BOM — Create BOM double-submit guard.
 *
 * Batch 1: the Create BOM submit uses LoadingButton + a `creatingBOM` flag and a
 * re-entrancy short-circuit (`if (creatingBOM) return`). A second submit while
 * the first create is in flight must NOT fire a second createBOM call.
 *
 * As with the Customers guard, we submit the form directly (fireEvent.submit)
 * to simulate rapid re-entry; passing this proves the handler-level guard.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BOMPage from './BOM';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getBOMs: jest.fn(),
    getParts: jest.fn(),
    getBOM: jest.fn(),
    createBOM: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const ASSEMBLY_PART = {
  id: 7,
  part_number: 'PN-ASM-1',
  name: 'Gearbox',
  part_type: 'assembly',
};

function renderBOM() {
  return render(
    <MemoryRouter initialEntries={['/bom']}>
      <BOMPage />
    </MemoryRouter>
  );
}

describe('BOM Create double-submit guard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getBOMs.mockResolvedValue([]);
    mockedApi.getParts.mockResolvedValue([ASSEMBLY_PART]);
  });

  it('disables the Create button and fires createBOM only once on a double submit', async () => {
    let resolveCreate: (value: unknown) => void = () => undefined;
    mockedApi.createBOM.mockImplementation(
      () => new Promise((resolve) => { resolveCreate = resolve; })
    );

    renderBOM();

    // Open the Create BOM modal.
    fireEvent.click(await screen.findByRole('button', { name: /create bom/i }));

    // Select the assembly part so the form is realistic. The modal portals to
    // document.body; the Part select is the first <select> in the create form.
    const partSelect = document.body.querySelector('form select') as HTMLSelectElement;
    expect(partSelect).not.toBeNull();
    fireEvent.change(partSelect, { target: { value: String(ASSEMBLY_PART.id) } });

    const form = partSelect.closest('form')!;

    // First submit kicks off the (hung) create; button flips to its loading state.
    fireEvent.submit(form);
    const submitBtn = await screen.findByRole('button', { name: /creating/i });
    expect(submitBtn).toBeDisabled();

    // Rapid re-entry while still creating must be a no-op.
    fireEvent.submit(form);

    await waitFor(() => expect(mockedApi.createBOM).toHaveBeenCalledTimes(1));

    // Settle the create cleanly; a successful create closes the modal.
    resolveCreate({
      id: 99,
      part_id: ASSEMBLY_PART.id,
      revision: 'A',
      bom_type: 'standard',
      description: '',
      items: [],
    });
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: /create new bom/i })).not.toBeInTheDocument()
    );
    expect(mockedApi.createBOM).toHaveBeenCalledTimes(1);
  });
});
