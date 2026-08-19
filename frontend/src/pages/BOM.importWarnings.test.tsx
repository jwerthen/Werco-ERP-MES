/**
 * BOM — how a partially-completed import is announced.
 *
 * `POST /bom/import/commit` succeeds and hands back a `warnings` list, and
 * every sentence in that list has one shape: the import WORKED but did not do
 * everything asked. A part number was generated because none was found, a line
 * was created without one — or the parent part could NOT be reclassified as an
 * assembly, because unfinished work orders still tie it as material and the server's
 * conversion gate refuses to make a part something the shop produces while jobs
 * are standing to consume it. That last one leaves the catalog holding a class
 * the planner did not ask for and has to act on.
 *
 * That is exactly the `warning` toast variant, which renders `role="alert"` and
 * interrupts a screen reader. `info` renders `role="status"`, which waits for a
 * pause and may never be read at all — and `error` would be a lie, because the
 * BOM was created and saying otherwise sends someone hunting for a record that
 * exists.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import BOMPage from './BOM';
import { ToastProvider } from '../components/ui';
import api from '../services/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getBOMs: jest.fn(),
    getParts: jest.fn(),
    getBOM: jest.fn(),
    previewBOMImport: jest.fn(),
    commitBOMImport: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const PREVIEW = {
  document_type: 'bom' as const,
  assembly: { part_number: 'ASM-500', name: 'Gearbox', revision: 'A' },
  items: [{ line_number: 10, part_number: 'CMP-1', description: 'Shaft', quantity: 2 }],
  warnings: [],
};

const CREATED_BOM = {
  id: 88,
  part_id: 7,
  revision: 'A',
  bom_type: 'standard',
  status: 'draft',
  is_active: true,
  items: [],
};

/**
 * The refusal the conversion gate writes into `warnings` rather than aborting a
 * multi-record import over one step of catalog hygiene.
 *
 * PASTED, NOT PARAPHRASED. This is
 * `material_tie_part_gate.part_type_change_refusal` for a `purchased` part being
 * promoted to `assembly` while two unfinished work orders tie it, plus the
 * sentence `bom.py::_promote_existing_part_to_assembly` appends when it appends the
 * refusal to `warnings` instead of raising. A mock that invents a shorter
 * sentence proves the toast renders SOMETHING; it cannot prove the toast renders
 * what a planner will actually be shown, and this repo type-checks its test
 * files precisely so a mock cannot quietly contradict the contract it stands in
 * for.
 */
const NOT_PROMOTED =
  'Part ASM-500 cannot be reclassified as an assembly: 2 unfinished work orders still tie it as material. ' +
  'A tie depletes the tied part when that work completes, so reclassifying it now would leave standing ' +
  'demand that consumes finished goods. Untie those work orders first, then change the part type. ' +
  "The BOM was imported and the part's type was left unchanged.";

const renderBOM = () =>
  render(
    <ToastProvider>
      <MemoryRouter initialEntries={['/bom']}>
        <BOMPage />
      </MemoryRouter>
    </ToastProvider>
  );

/** Import → preview → Create, i.e. the whole path a planner actually walks. */
async function commitAnImport() {
  renderBOM();
  fireEvent.click(await screen.findByRole('button', { name: /import bom\/drawing/i }));

  const fileInput = document.body.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(fileInput, {
    target: { files: [new File(['x'], 'bom.xlsx', { type: 'application/vnd.ms-excel' })] },
  });
  fireEvent.submit(fileInput.closest('form')!);

  fireEvent.click(await screen.findByRole('button', { name: /^create$/i }));
  await waitFor(() => expect(mockApi.commitBOMImport).toHaveBeenCalledTimes(1));
}

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getBOMs.mockResolvedValue([]);
  mockApi.getParts.mockResolvedValue([]);
  mockApi.getBOM.mockResolvedValue(CREATED_BOM as never);
  mockApi.previewBOMImport.mockResolvedValue(PREVIEW as never);
});

describe('BOM import: a commit that left something undone', () => {
  it('announces the warnings as an interrupting alert, not a passive status', async () => {
    mockApi.commitBOMImport.mockResolvedValue({ bom_id: 88, warnings: [NOT_PROMOTED] } as never);

    await commitAnImport();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Import completed with warnings');
    // The sentence itself, not a count: "1 warning" says something was left
    // undone and never says what.
    expect(alert).toHaveTextContent(/cannot be reclassified as an assembly/i);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('quotes every warning, whatever its cause — they are all the same shape', async () => {
    mockApi.commitBOMImport.mockResolvedValue({
      bom_id: 88,
      warnings: ['Line 30: missing part number; generated automatically.', NOT_PROMOTED],
    } as never);

    await commitAnImport();

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/Line 30: missing part number/i);
    expect(alert).toHaveTextContent(/cannot be reclassified as an assembly/i);
  });

  it('stays silent when the import did everything asked', async () => {
    mockApi.commitBOMImport.mockResolvedValue({ bom_id: 88, warnings: [] } as never);

    await commitAnImport();

    await waitFor(() => expect(mockApi.getBOM).toHaveBeenCalledWith(88));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('still reports an outright failure as an error, not a warning', async () => {
    // `warning` is for a partial result. A refused commit created nothing, and
    // must not read as "done, with caveats".
    mockApi.commitBOMImport.mockRejectedValue({
      response: { status: 400, data: { detail: 'Assembly part number is required' } },
    });

    await commitAnImport();

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('Assembly part number is required')).toBeInTheDocument();
    expect(alert).not.toHaveTextContent(/Import completed/i);
  });
});
