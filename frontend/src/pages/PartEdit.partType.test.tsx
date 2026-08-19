/**
 * PartEdit — the Type field, and what a save actually PUTs.
 *
 * `PUT /parts/{id}` resolves ANY part in the company, engineering or
 * material/supply, and that is deliberate: the BOM tab drills through to a
 * component's part page (`/parts/{id}` → Edit) and BOM components are routinely
 * `purchased` / `raw_material`. What the endpoint still refuses is a
 * `part_type` it is SENT that sits outside the engineering pair — **400**
 * "Engineering parts must be manufactured or assembly".
 *
 * This form's Type select offers that pair only, so posting the whole form on
 * every save made the material-row door unusable: a `purchased` component
 * reached from the BOM tab 400'd on a save that touched nothing but the
 * description, and the select itself rendered blank because its value matched
 * no option — reading as "this part has no type" and inviting a planner to pick
 * one, silently reclassifying a part they only came to rename.
 *
 * Two rules close it, and both are pinned here:
 *   1. an UNCHANGED `part_type` is not in the PUT at all, and
 *   2. the loaded class is pinned into the option list so the field states the
 *      truth.
 *
 * A DELIBERATE reclassification still sends the key and still meets the server
 * gates (400 for a non-engineering target; 409 while unfinished work orders tie the
 * part as material).
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
  name: 'Hex bolt',
  description: 'Grade 8',
  part_type: 'purchased',
  unit_of_measure: 'EA',
  standard_cost: 1.25,
  is_critical: false,
  requires_inspection: true,
  backflush_components: false,
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

const typeSelect = () => screen.getByLabelText(/type/i) as HTMLSelectElement;

const save = () => fireEvent.click(screen.getByRole('button', { name: /save changes/i }));

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.updatePart.mockImplementation((_id, _data) => Promise.resolve(part()));
});

describe('PartEdit: a material row reached through the BOM drill-through', () => {
  beforeEach(() => {
    mockApi.getPart.mockResolvedValue(part({ part_type: 'purchased' }));
  });

  it('shows the part its real class instead of a blank select', async () => {
    renderEdit();
    await screen.findByDisplayValue('Hex bolt');

    // Blank here reads as "this part has no type" — on a row whose type is the
    // reason this endpoint had to stay open to it in the first place.
    expect(typeSelect()).toHaveValue('purchased');
    expect(
      Array.from(typeSelect().options).map((option) => option.textContent)
    ).toEqual(['Purchased COTS', 'Manufactured', 'Assembly']);
  });

  it('omits part_type from the PUT when the planner never touched it', async () => {
    renderEdit();
    const name = await screen.findByDisplayValue('Hex bolt');

    fireEvent.change(name, { target: { value: 'Hex bolt, plated' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    const [id, payload] = mockApi.updatePart.mock.calls[0];
    expect(id).toBe(501);
    expect(payload).not.toHaveProperty('part_type');
    // The rest of the form still goes, unchanged.
    expect(payload).toMatchObject({ name: 'Hex bolt, plated', revision: 'B', version: 3 });
  });

  it('omits it again once a changed type is put back — the save is not poisoned by a detour', async () => {
    renderEdit();
    await screen.findByDisplayValue('Hex bolt');

    fireEvent.change(typeSelect(), { target: { value: 'manufactured' } });
    fireEvent.change(typeSelect(), { target: { value: 'purchased' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    expect(mockApi.updatePart.mock.calls[0][1]).not.toHaveProperty('part_type');
  });

  it('DOES send it for a deliberate reclassification — the gate is the server’s to apply', async () => {
    renderEdit();
    await screen.findByDisplayValue('Hex bolt');

    fireEvent.change(typeSelect(), { target: { value: 'manufactured' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    expect(mockApi.updatePart.mock.calls[0][1]).toMatchObject({ part_type: 'manufactured' });
  });

  it('surfaces the server’s refusal verbatim rather than a generic failure', async () => {
    // The conversion gate answers 409 while unfinished work orders still tie this
    // part as material. The planner needs the sentence, not "Failed to update".
    //
    // PASTED FROM `material_tie_part_gate.part_type_change_refusal`, not
    // paraphrased — this fixture's whole claim is that the server's own words
    // reach the screen, and a shortened stand-in cannot make that claim. The
    // remedy clause at the end is the half a planner acts on.
    mockApi.updatePart.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail:
            'Part CMP-9001 cannot be reclassified as a manufactured part: 2 unfinished work orders still tie ' +
            'it as material. A tie depletes the tied part when that work completes, so reclassifying it now ' +
            'would leave standing demand that consumes finished goods. Untie those work orders first, then ' +
            'change the part type.',
        },
      },
    });

    renderEdit();
    await screen.findByDisplayValue('Hex bolt');

    fireEvent.change(typeSelect(), { target: { value: 'manufactured' } });
    save();

    expect(
      await screen.findByText(/2 unfinished work orders still tie it as material/i)
    ).toBeInTheDocument();
    // The remedy, verbatim — a refusal that does not say what to do next is the
    // "Failed to update" this test exists to rule out.
    expect(screen.getByText(/Untie those work orders first, then change the part type\./i)).toBeInTheDocument();
  });
});

describe('PartEdit: an ordinary engineering part is unchanged', () => {
  it('offers only the engineering pair and still omits an untouched type', async () => {
    mockApi.getPart.mockResolvedValue(part({ part_type: 'manufactured', name: 'Bracket' }));

    renderEdit();
    const name = await screen.findByDisplayValue('Bracket');

    expect(
      Array.from(typeSelect().options).map((option) => option.value)
    ).toEqual(['manufactured', 'assembly']);

    fireEvent.change(name, { target: { value: 'Bracket, LH' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    expect(mockApi.updatePart.mock.calls[0][1]).not.toHaveProperty('part_type');
  });

  it('sends the switch when the planner really changes it', async () => {
    mockApi.getPart.mockResolvedValue(part({ part_type: 'manufactured', name: 'Bracket' }));

    renderEdit();
    await screen.findByDisplayValue('Bracket');

    fireEvent.change(typeSelect(), { target: { value: 'assembly' } });
    save();

    await waitFor(() => expect(mockApi.updatePart).toHaveBeenCalledTimes(1));
    expect(mockApi.updatePart.mock.calls[0][1]).toMatchObject({ part_type: 'assembly' });
  });
});
