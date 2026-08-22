/**
 * The Renumber launcher on the Materials screen, and the gate around it.
 *
 * Materials is the PRIMARY launcher rather than the part detail page, and that is
 * a deliberate routing fact: the parts list defaults to engineering part types
 * (manufactured/assembly), Materials rows open a MODAL rather than a route, and
 * nothing in the app links `/parts/{id}` for a material. So for raw_material —
 * the sheet and plate stock this feature most needs to reach — a detail-page-only
 * control would sit on a page nobody arrives at.
 *
 * What this pins:
 *
 * 1. THE HIDDEN CONTROL AND THE REFUSED CALL AGREE. `parts:renumber` is
 *    deliberately narrower than `parts:edit`: a supervisor may edit a material's
 *    description but must not change its identity, because renumbering is an
 *    AS9100D 8.5.2 controlled change that sits with revision and delete. The
 *    server enforces `require_role([ADMIN, MANAGER])`; this is the client half,
 *    and if the two disagree a supervisor gets a button that 403s.
 * 2. THE ITEM NUMBER INPUT STAYS DISABLED, for everyone. Enabling it would put the
 *    number onto this modal's PartUpdate payload, which the backend applies with a
 *    blind setattr — the rename would then carry no reason, no audit identity, no
 *    collision check, and no drain of the operation links the number stands in for.
 */

import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Materials from './Materials';
import api from '../services/api';

let mockRole = 'admin';
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 1, role: mockRole, is_superuser: false },
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
    getPartRenumberImpact: jest.fn(),
    renumberPart: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const sheetStock = {
  id: 42,
  part_number: '0.250-60X120-A36',
  name: 'Hot rolled plate',
  part_type: 'raw_material',
  unit_of_measure: 'sheets',
  description: 'A36 plate',
  standard_cost: 84.5,
  requires_inspection: true,
  is_active: true,
  status: 'active',
  version: 0,
};

async function openEditModal() {
  render(
    <MemoryRouter initialEntries={['/materials']}>
      <Materials />
    </MemoryRouter>
  );
  const editButtons = await screen.findAllByRole('button', { name: /edit/i });
  fireEvent.click(editButtons[0]);
  await screen.findByLabelText(/Item Number/);
}

describe('Materials renumber gate', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockRole = 'admin';
    mockedApi.getMaterials.mockResolvedValue([sheetStock] as any);
  });

  it('offers Renumber to an admin editing a material', async () => {
    await openEditModal();
    expect(screen.getByRole('button', { name: /Renumber/i })).toBeInTheDocument();
  });

  it('offers Renumber to a manager', async () => {
    mockRole = 'manager';
    await openEditModal();
    expect(screen.getByRole('button', { name: /Renumber/i })).toBeInTheDocument();
  });

  it('hides Renumber from a supervisor, who the endpoint would refuse', async () => {
    mockRole = 'supervisor';
    await openEditModal();
    expect(screen.queryByRole('button', { name: /Renumber/i })).not.toBeInTheDocument();
  });

  it('hides Renumber from an operator', async () => {
    mockRole = 'operator';
    await openEditModal();
    expect(screen.queryByRole('button', { name: /Renumber/i })).not.toBeInTheDocument();
  });

  it('leaves the Item Number input disabled even for an admin', async () => {
    await openEditModal();
    expect(screen.getByLabelText(/Item Number/)).toBeDisabled();
  });

  it('does not offer Renumber when creating a new material', async () => {
    /** There is no identity to change yet — the number is simply typed. */
    render(
      <MemoryRouter initialEntries={['/materials']}>
        <Materials />
      </MemoryRouter>
    );
    fireEvent.click(await screen.findByRole('button', { name: /new item/i }));
    await screen.findByLabelText(/Item Number/);

    expect(screen.queryByRole('button', { name: /Renumber/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Item Number/)).toBeEnabled();
  });
});
