/**
 * PartBOMTab — ConfirmDialog `confirmPending` in-flight guard.
 *
 * The tab's confirm used to fire `confirmAction.action()` and close the dialog
 * immediately — a second click could double-fire and the user got no in-flight
 * feedback. Pins the fixed wiring: while the awaited action is in flight the
 * dialog stays open and pending (spinner, both buttons disabled), a re-click
 * posts nothing extra, and the dialog closes only after the promise settles
 * (either way — a confirm dialog loses no typed input, so close-with-error-
 * toast on refusal is the settled behavior, unlike InputDialog callers which
 * stay open on refusal).
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../../services/api';
import { PartBOMTab } from './PartBOMTab';
import { ToastProvider } from '../ui';
import { Part } from '../../types';
import { BOM } from '../../types/engineering';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    addBOMItem: jest.fn(),
    createBOM: jest.fn(),
    createMaterial: jest.fn(),
    createPart: jest.fn(),
    deleteBOM: jest.fn(),
    deleteBOMItem: jest.fn(),
    explodeBOM: jest.fn(),
    getParts: jest.fn(),
    releaseBOM: jest.fn(),
    unreleaseBOM: jest.fn(),
    updateBOMItem: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const part: Part = {
  id: 1,
  version: 1,
  part_number: 'ASM-001',
  revision: 'A',
  name: 'Weldment Assembly',
  part_type: 'assembly',
  unit_of_measure: 'EA',
  standard_cost: 100,
  is_critical: false,
  requires_inspection: true,
  backflush_components: false,
  is_active: true,
  status: 'active',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

const draftBom: BOM = {
  id: 10,
  part_id: 1,
  revision: 'A',
  bom_type: 'manufacturing',
  status: 'draft',
  is_active: true,
  items: [
    {
      id: 100,
      bom_id: 10,
      component_part_id: 2,
      item_number: 10,
      quantity: 4,
      item_type: 'buy',
      line_type: 'component',
      unit_of_measure: 'EA',
      scrap_factor: 0,
      is_optional: false,
      is_alternate: false,
      component_part: { id: 2, part_number: 'CMP-100', name: 'Bracket', revision: 'A', part_type: 'purchased' },
    },
  ],
};

function renderTab(onBOMChanged = jest.fn().mockResolvedValue(undefined)) {
  render(
    <ToastProvider>
      <MemoryRouter>
        <PartBOMTab part={part} bom={draftBom} onBOMChanged={onBOMChanged} />
      </MemoryRouter>
    </ToastProvider>
  );
  return onBOMChanged;
}

// The row's trash button is icon-only (no accessible name yet), so locate it
// as the last button in the component's row.
function getRowDeleteButton(): HTMLElement {
  const row = screen.getByText('CMP-100').closest('tr') as HTMLElement;
  const buttons = within(row).getAllByRole('button');
  return buttons[buttons.length - 1];
}

beforeEach(() => {
  jest.clearAllMocks();
});

describe('PartBOMTab delete-item confirm pending', () => {
  it('stays open and pending mid-flight, blocks a re-click, and closes after settle', async () => {
    let resolveDelete!: (value: unknown) => void;
    mockedApi.deleteBOMItem.mockImplementation(
      () => new Promise((resolve) => { resolveDelete = resolve; })
    );
    const onBOMChanged = renderTab();

    fireEvent.click(getRowDeleteButton());
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Delete BOM Item')).toBeInTheDocument();

    const confirmButton = within(dialog).getByRole('button', { name: 'Delete' });
    fireEvent.click(confirmButton);
    await waitFor(() => expect(mockedApi.deleteBOMItem).toHaveBeenCalledWith(100));

    // Mid-flight: dialog open + pending.
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByRole('status', { name: 'Loading' })).toBeInTheDocument();
    expect(confirmButton).toBeDisabled();
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled();

    // Re-click posts nothing extra.
    fireEvent.click(confirmButton);
    expect(mockedApi.deleteBOMItem).toHaveBeenCalledTimes(1);

    // Settle -> refresh -> close.
    resolveDelete(undefined);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(mockedApi.deleteBOMItem).toHaveBeenCalledTimes(1);
    expect(onBOMChanged).toHaveBeenCalledTimes(1);
  });

  it('a refused delete closes the dialog on settle with the verbatim error toast', async () => {
    const detail = 'BOM is released — items cannot be removed.';
    mockedApi.deleteBOMItem.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderTab();

    fireEvent.click(getRowDeleteButton());
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});


describe('BOM contents and line editing', () => {
  it('reads the API explosion envelope and keeps the component visible in Multi-Level', async () => {
    mockedApi.explodeBOM.mockResolvedValue({ bom_id: 10, part_id: 1, part_number: 'ASM-001', part_name: 'Assembly', revision: 'A', total_levels: 1, items: draftBom.items });
    renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Multi-Level' }));
    expect(await screen.findByText('CMP-100')).toBeInTheDocument();
    expect(screen.queryByText('No items to display')).not.toBeInTheDocument();
  });
  it('shows a recoverable explosion failure instead of an empty BOM', async () => {
    mockedApi.explodeBOM.mockRejectedValueOnce(new Error('Offline'));
    renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Multi-Level' }));
    expect(await screen.findByText('Could not load the multi-level BOM.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Retry multi-level BOM' })).toBeInTheDocument();
    expect(screen.queryByText('No items to display')).not.toBeInTheDocument();
  });
  it('edits a draft line in place and retains values after a refused save', async () => {
    mockedApi.updateBOMItem.mockRejectedValueOnce({ response: { data: { detail: 'BOM has just been released' } } });
    const refreshed = renderTab();
    fireEvent.click(screen.getByRole('button', { name: /Edit/ }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByRole('spinbutton', { name: /Quantity/ }), { target: { value: '7' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save component' }));
    expect(await screen.findByText('BOM has just been released')).toBeInTheDocument();
    expect(within(dialog).getByRole('spinbutton', { name: /Quantity/ })).toHaveValue(7);
    expect(mockedApi.updateBOMItem).toHaveBeenCalledWith(100, expect.objectContaining({ quantity: 7 }));
    expect(mockedApi.updateBOMItem.mock.calls[0][1]).not.toHaveProperty('component_part_id');
    expect(refreshed).not.toHaveBeenCalled();
  });
});
