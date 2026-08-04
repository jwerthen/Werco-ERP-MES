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
import { Part } from '../types';

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

// A full Part, exactly as `api.getParts()` resolves it — a short-shaped fixture
// is a mock of a payload the real API never sends.
const ASSEMBLY_PART: Part = {
  id: 7,
  version: 1,
  part_number: 'PN-ASM-1',
  revision: 'A',
  name: 'Gearbox',
  part_type: 'assembly',
  unit_of_measure: 'EA',
  standard_cost: 0,
  is_critical: false,
  requires_inspection: false,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
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
