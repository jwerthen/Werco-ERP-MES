/**
 * PartEdit — the Customer-supplied flag (owner decision 2026-11-22: a part the
 * customer furnishes creates no MRP demand).
 *
 * Same contract as the Materials edit modal:
 *   - the checkbox shows the LOADED value, never a default;
 *   - it reaches `PUT /parts/{id}` only when the planner CHANGED it, and only as a
 *     real boolean (`null` is a 422), so a save that never touched it cannot
 *     overwrite a flag set elsewhere since the page loaded;
 *   - a part read without the field (an API that predates the column) is UNKNOWN:
 *     no checkbox, and nothing sent.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import PartEdit from './PartEdit';
import api from '../services/api';
import { ToastProvider } from '../components/ui';
import { Part } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getPart: jest.fn(),
    updatePart: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const part = (overrides: Partial<Part> = {}): Part => ({
  id: 501,
  version: 3,
  part_number: 'CMP-9001',
  revision: 'B',
  name: 'Bracket',
  description: 'Formed bracket',
  part_type: 'manufactured',
  unit_of_measure: 'EA',
  standard_cost: 12.5,
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

const renderEdit = () =>
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/parts/501/edit']}>
        <Routes>
          <Route path="/parts/:id/edit" element={<PartEdit />} />
          <Route path="/parts/:id" element={<div>part detail</div>} />
        </Routes>
      </MemoryRouter>
    </ToastProvider>
  );

const customerSuppliedBox = () => screen.queryByRole('checkbox', { name: /customer-supplied/i });

const save = () => fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.updatePart.mockImplementation(() => Promise.resolve(part()));
});

describe('PartEdit: Customer-supplied checkbox', () => {
  it('shows the loaded value — checked for a flagged part', async () => {
    mockApi.getPart.mockResolvedValue(part({ customer_supplied: true }));
    renderEdit();
    await screen.findByDisplayValue('Bracket');

    const box = customerSuppliedBox();
    expect(box).toBeChecked();
    expect(box).toHaveAccessibleDescription(
      'The customer furnishes this part, so MRP will not plan purchases or work orders for it.'
    );
  });

  it('shows the loaded value — unchecked for an unflagged part', async () => {
    mockApi.getPart.mockResolvedValue(part({ customer_supplied: false }));
    renderEdit();
    await screen.findByDisplayValue('Bracket');

    expect(customerSuppliedBox()).toBeInTheDocument();
    expect(customerSuppliedBox()).not.toBeChecked();
  });

  it('sends customer_supplied: true when the planner ticks it', async () => {
    mockApi.getPart.mockResolvedValue(part({ customer_supplied: false }));
    renderEdit();
    await screen.findByDisplayValue('Bracket');

    fireEvent.click(customerSuppliedBox() as HTMLElement);
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    const [id, payload] = mockApi.updatePart.mock.calls[0];
    expect(id).toBe(501);
    expect(payload).toMatchObject({ customer_supplied: true, version: 3 });
  });

  it('sends customer_supplied: false when the planner clears it', async () => {
    mockApi.getPart.mockResolvedValue(part({ customer_supplied: true }));
    renderEdit();
    await screen.findByDisplayValue('Bracket');

    fireEvent.click(customerSuppliedBox() as HTMLElement);
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    // Strictly false — never null, which the server refuses 422.
    expect(mockApi.updatePart.mock.calls[0][1].customer_supplied).toBe(false);
  });

  it('omits the key on a save that never touched it', async () => {
    mockApi.getPart.mockResolvedValue(part({ customer_supplied: true }));
    renderEdit();
    const name = await screen.findByDisplayValue('Bracket');

    fireEvent.change(name, { target: { value: 'Bracket, LH' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    const payload = mockApi.updatePart.mock.calls[0][1];
    expect(payload).not.toHaveProperty('customer_supplied');
    // The rest of the form still goes.
    expect(payload).toMatchObject({ name: 'Bracket, LH', revision: 'B', is_critical: false, version: 3 });
  });

  it('treats a part read without the field as unknown: no checkbox, nothing sent', async () => {
    const legacy = part();
    delete legacy.customer_supplied;
    mockApi.getPart.mockResolvedValue(legacy);
    renderEdit();
    const name = await screen.findByDisplayValue('Bracket');

    expect(customerSuppliedBox()).not.toBeInTheDocument();
    // The neighbouring quality flags are untouched by this.
    expect(screen.getByRole('checkbox', { name: 'Critical Characteristic' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Requires Inspection' })).toBeInTheDocument();

    fireEvent.change(name, { target: { value: 'Bracket, LH' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    expect(mockApi.updatePart.mock.calls[0][1]).not.toHaveProperty('customer_supplied');
  });
});
