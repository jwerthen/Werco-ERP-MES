/**
 * Materials — the edit payload must not carry CREATE defaults.
 *
 * The bug this pins: `handleSubmit` built ONE payload for both create and update —
 * `{ ...form, revision: 'A', is_critical: false }` — and `PartUpdate` is a blind
 * setattr over `exclude_unset`. So saving a material's NAME or DESCRIPTION also reset
 * its revision to 'A' and cleared its critical-characteristic flag. Neither field
 * renders anywhere on this screen, so the loss was invisible before and after, and
 * both are AS9100D traceability data (CLAUDE.md invariant 5). Non-'A' revisions and
 * set critical flags reach material rows legitimately via the CSV importer,
 * POST /parts/{id}/revision, and PartEdit.
 *
 * What is locked here:
 *   1. UPDATE omits `revision` and `is_critical` entirely — absent, not false/'A'.
 *      `exclude_unset` is what makes absence mean "leave it alone", so asserting the
 *      VALUE is not enough; the keys must not be present.
 *   2. UPDATE omits `part_number`. Inert today (the backend drops it), but the Item
 *      Number input is disabled while editing, so sending it is meaningless — and its
 *      absence is what stops this payload becoming a rename channel the day
 *      `part_number` becomes settable on PartUpdate.
 *   3. UPDATE still carries the fields the modal actually edits, and `version`.
 *   4. CREATE still sends the defaults — they are correct there, and dropping them
 *      from both paths would be the wrong fix.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Materials from './Materials';
import api from '../services/api';

// Materials reads the current user to decide whether to offer Renumber (a
// controlled identity change gated on parts:renumber, ADMIN/MANAGER only). This
// suite renders the page outside an AuthProvider, so the context is stubbed; the
// role is irrelevant to what it asserts.
//
// Needed only once the renumber work merged: this file and the useAuth() call
// arrived in different PRs, so neither PR's own CI could see the combination.
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: 'admin', is_superuser: false },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

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

// A material carrying exactly the state the old payload destroyed: a non-'A'
// revision and a set critical flag. Neither is rendered by this screen.
const existingMaterial = {
  id: 42,
  part_number: 'RM-1018-050',
  name: 'CRS Sheet .050',
  part_type: 'raw_material',
  unit_of_measure: 'sheets',
  description: 'Cold rolled steel sheet',
  standard_cost: 84.5,
  requires_inspection: true,
  revision: 'C',
  is_critical: true,
  is_active: true,
  status: 'active',
  version: 3,
};

function renderMaterials() {
  return render(
    <MemoryRouter initialEntries={['/materials']}>
      <Materials />
    </MemoryRouter>
  );
}

async function openEditModal() {
  const editButtons = await screen.findAllByRole('button', { name: /edit/i });
  fireEvent.click(editButtons[0]);
  await screen.findByLabelText(/Item Number/);
}

describe('Materials edit payload', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.getMaterials.mockResolvedValue([existingMaterial] as any);
    mockedApi.updateMaterial.mockResolvedValue({ ...existingMaterial } as any);
    mockedApi.createMaterial.mockResolvedValue({ ...existingMaterial, id: 43 } as any);
  });

  it('does not send revision or is_critical when updating', async () => {
    renderMaterials();
    await openEditModal();

    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'CRS Sheet .050 REV C' } });
    fireEvent.click(screen.getByRole('button', { name: /save|update/i }));

    await waitFor(() => expect(mockedApi.updateMaterial).toHaveBeenCalledTimes(1));
    const [id, payload] = mockedApi.updateMaterial.mock.calls[0];
    expect(id).toBe(42);

    // ABSENT, not false/'A' — exclude_unset is what makes absence protective.
    expect(payload).not.toHaveProperty('revision');
    expect(payload).not.toHaveProperty('is_critical');
    // Not a rename channel.
    expect(payload).not.toHaveProperty('part_number');
    // Still sends what the modal edits.
    expect(payload).toMatchObject({ name: 'CRS Sheet .050 REV C', version: 3 });
  });

  it('still sends the create defaults when creating', async () => {
    renderMaterials();
    fireEvent.click(await screen.findByRole('button', { name: /new item/i }));
    await screen.findByLabelText(/Item Number/);

    fireEvent.change(screen.getByLabelText(/Item Number/), { target: { value: 'RM-NEW-001' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'New Stock' } });
    fireEvent.click(screen.getByRole('button', { name: /save|create/i }));

    await waitFor(() => expect(mockedApi.createMaterial).toHaveBeenCalledTimes(1));
    const [payload] = mockedApi.createMaterial.mock.calls[0];
    expect(payload).toMatchObject({
      part_number: 'RM-NEW-001',
      name: 'New Stock',
      revision: 'A',
      is_critical: false,
    });
  });
});
