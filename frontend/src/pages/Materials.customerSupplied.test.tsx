/**
 * Materials — the Customer-supplied flag (owner decision 2026-11-22: a part the
 * customer furnishes creates no MRP demand).
 *
 * `customer_supplied` is on `PartResponse` and `PartUpdate` and deliberately NOT on
 * `PartCreate`, which silently drops unknown keys. So:
 *
 *   1. The checkbox is EDIT-ONLY. A create-mode box would silently do nothing.
 *   2. On edit it shows the LOADED value, never a default.
 *   3. It reaches the PUT only when the operator CHANGED it, and only as a real
 *      boolean (`null` is a 422). A save that never touched it carries no key, so a
 *      stale list row cannot overwrite a flag set elsewhere (the API, PartEdit).
 *   4. A row that carries no boolean at all (an API that predates the column) is
 *      UNKNOWN, not false: no checkbox, and nothing sent.
 *   5. The list shows a read-only chip for a flagged row, taken from the server's
 *      record (the row is replaced from the PUT response, never toggled locally).
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Materials from './Materials';
import api from '../services/api';
import { Part } from '../types';

// Materials reads the current user to gate Renumber / Combine / Activate. The page
// renders outside an AuthProvider here, so the context is stubbed; the role is
// irrelevant to what this suite asserts.
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

const material = (overrides: Partial<Part> = {}): Part => ({
  id: 42,
  version: 3,
  part_number: 'RM-1018-050',
  revision: 'A',
  name: 'CRS Sheet .050',
  description: 'Cold rolled steel sheet',
  part_type: 'raw_material',
  unit_of_measure: 'sheets',
  standard_cost: 84.5,
  is_critical: false,
  requires_inspection: true,
  backflush_components: false,
  customer_supplied: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
  ...overrides,
});

function renderMaterials() {
  return render(
    <MemoryRouter initialEntries={['/materials']}>
      <Materials />
    </MemoryRouter>
  );
}

async function openEditModal() {
  const editButtons = await screen.findAllByRole('button', { name: /^edit /i });
  fireEvent.click(editButtons[0]);
  await screen.findByLabelText(/Item Number/);
}

const customerSuppliedBox = () => screen.queryByRole('checkbox', { name: /customer-supplied/i });

const save = () => fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

describe('Materials: Customer-supplied checkbox', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockedApi.updateMaterial.mockImplementation((id, data) =>
      Promise.resolve(material({ id, ...(data as Partial<Part>) }))
    );
    mockedApi.createMaterial.mockResolvedValue(material({ id: 43, part_number: 'RM-NEW-001' }));
  });

  it('shows the loaded value — checked for a flagged material', async () => {
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: true })]);
    renderMaterials();
    await openEditModal();

    const box = customerSuppliedBox();
    expect(box).toBeInTheDocument();
    expect(box).toBeChecked();
    // The helper text is the box's accessible description, not loose text beside it.
    expect(box).toHaveAccessibleDescription(
      'The customer furnishes this item, so MRP will not plan purchases or work orders for it.'
    );
  });

  it('shows the loaded value — unchecked for an unflagged material', async () => {
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: false })]);
    renderMaterials();
    await openEditModal();

    expect(customerSuppliedBox()).not.toBeChecked();
  });

  it('sends customer_supplied: true when the operator ticks it', async () => {
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: false })]);
    renderMaterials();
    await openEditModal();

    fireEvent.click(customerSuppliedBox() as HTMLElement);
    save();

    await waitFor(() => expect(mockedApi.updateMaterial).toHaveBeenCalledTimes(1));
    const [id, payload] = mockedApi.updateMaterial.mock.calls[0];
    expect(id).toBe(42);
    expect(payload).toMatchObject({ customer_supplied: true, version: 3 });
  });

  it('sends customer_supplied: false when the operator clears it', async () => {
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: true })]);
    renderMaterials();
    await openEditModal();

    fireEvent.click(customerSuppliedBox() as HTMLElement);
    save();

    await waitFor(() => expect(mockedApi.updateMaterial).toHaveBeenCalledTimes(1));
    // Strictly false — never null, which the server refuses 422.
    expect(mockedApi.updateMaterial.mock.calls[0][1].customer_supplied).toBe(false);
  });

  it('omits the key on a save that never touched it, so a stale row cannot clear the flag', async () => {
    // The row says false; the flag may have been set elsewhere since the list loaded.
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: false })]);
    renderMaterials();
    await openEditModal();

    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'CRS Sheet .050 P&O' } });
    save();

    await waitFor(() => expect(mockedApi.updateMaterial).toHaveBeenCalledTimes(1));
    const payload = mockedApi.updateMaterial.mock.calls[0][1];
    expect(payload).not.toHaveProperty('customer_supplied');
    expect(payload).toMatchObject({ name: 'CRS Sheet .050 P&O' });
  });

  it('omits it again when a tick is undone before saving', async () => {
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: true })]);
    renderMaterials();
    await openEditModal();

    const box = customerSuppliedBox() as HTMLElement;
    fireEvent.click(box);
    fireEvent.click(box);
    save();

    await waitFor(() => expect(mockedApi.updateMaterial).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateMaterial.mock.calls[0][1]).not.toHaveProperty('customer_supplied');
  });

  it('treats a row without the field as unknown: no checkbox, nothing sent', async () => {
    const legacyRow = material();
    delete legacyRow.customer_supplied;
    mockedApi.getMaterials.mockResolvedValue([legacyRow]);
    renderMaterials();
    await openEditModal();

    expect(customerSuppliedBox()).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'CRS Sheet .050 P&O' } });
    save();

    await waitFor(() => expect(mockedApi.updateMaterial).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateMaterial.mock.calls[0][1]).not.toHaveProperty('customer_supplied');
  });

  it('marks the form dirty, so Cancel asks before discarding the change', async () => {
    const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
    try {
      mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: false })]);
      renderMaterials();
      await openEditModal();

      fireEvent.click(customerSuppliedBox() as HTMLElement);
      fireEvent.click(screen.getByRole('button', { name: /^cancel$/i }));

      expect(confirmSpy).toHaveBeenCalledTimes(1);
      // Declined: the modal and the tick both survive.
      expect(customerSuppliedBox()).toBeChecked();
    } finally {
      confirmSpy.mockRestore();
    }
  });

  it('is not offered on the create form, and the create payload never carries it', async () => {
    mockedApi.getMaterials.mockResolvedValue([]);
    renderMaterials();
    fireEvent.click(await screen.findByRole('button', { name: /new item/i }));
    await screen.findByLabelText(/Item Number/);

    expect(customerSuppliedBox()).not.toBeInTheDocument();
    expect(screen.queryByText(/MRP will not plan/i)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Item Number/), { target: { value: 'RM-NEW-001' } });
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'New Stock' } });
    fireEvent.click(screen.getByRole('button', { name: /create item/i }));

    await waitFor(() => expect(mockedApi.createMaterial).toHaveBeenCalledTimes(1));
    expect(mockedApi.createMaterial.mock.calls[0][0]).not.toHaveProperty('customer_supplied');
  });
});

describe('Materials: Customer-supplied chip on the list', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('marks a flagged row and leaves an unflagged one alone', async () => {
    mockedApi.getMaterials.mockResolvedValue([
      material({ id: 1, part_number: 'RM-FLAG-001', name: 'Furnished bar', customer_supplied: true }),
      material({ id: 2, part_number: 'RM-PLAIN-002', name: 'Bought bar', customer_supplied: false }),
    ]);
    renderMaterials();

    const table = await screen.findByRole('table');
    const flaggedRow = (await within(table).findByText('RM-FLAG-001')).closest('tr') as HTMLElement;
    const plainRow = within(table).getByText('RM-PLAIN-002').closest('tr') as HTMLElement;

    expect(within(flaggedRow).getByText('Customer-supplied')).toBeInTheDocument();
    expect(within(plainRow).queryByText('Customer-supplied')).not.toBeInTheDocument();
  });

  it('shows the chip from the server record once a tick is saved', async () => {
    mockedApi.getMaterials.mockResolvedValue([material({ customer_supplied: false })]);
    mockedApi.updateMaterial.mockResolvedValue(material({ customer_supplied: true, version: 4 }));
    renderMaterials();

    const table = await screen.findByRole('table');
    expect(within(table).queryByText('Customer-supplied')).not.toBeInTheDocument();

    await openEditModal();
    fireEvent.click(customerSuppliedBox() as HTMLElement);
    save();

    await waitFor(() => expect(customerSuppliedBox()).not.toBeInTheDocument());
    expect(within(table).getByText('Customer-supplied')).toBeInTheDocument();
  });
});
