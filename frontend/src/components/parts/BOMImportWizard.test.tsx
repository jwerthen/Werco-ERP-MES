import React from 'react';
import { fireEvent, renderWithRouter, screen, waitFor } from '../../test-utils';
import api from '../../services/api';
import { BOMImportWizard } from './BOMImportWizard';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    previewBOMImport: jest.fn(),
    commitBOMImport: jest.fn(),
    getBOM: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const previewResponse = {
  document_type: 'bom',
  assembly: {
    part_number: '818-3928-638',
    revision: 'A',
    name: 'RETAINER, CAPACITOR',
    part_type: 'assembly',
  },
  extraction_confidence: 'high',
  warnings: [],
  raw_columns: [],
  raw_rows: [],
  items: [
    {
      line_number: 10,
      part_number: '820-5052-010',
      description: 'ALUMINUM, 5052-H32',
      quantity: 1,
      unit_of_measure: 'each',
      item_type: 'buy',
      line_type: 'component',
    },
    {
      line_number: 20,
      part_number: 'MS20426AD4',
      description: 'RIVET',
      quantity: 4,
      unit_of_measure: 'each',
      item_type: 'buy',
      line_type: 'hardware',
    },
    {
      line_number: 30,
      part_number: 'AA56032-IBLK',
      description: 'INK, MARKING',
      quantity: 1,
      unit_of_measure: 'ar',
      item_type: 'buy',
      line_type: 'consumable',
    },
  ],
};

async function renderPreview(onComplete = jest.fn().mockResolvedValue(undefined)) {
  mockedApi.previewBOMImport.mockResolvedValue(previewResponse);
  mockedApi.commitBOMImport.mockResolvedValue({
    document_type: 'bom',
    assembly_part_id: 1,
    assembly_part_number: '818-3928-638',
    bom_id: null,
    created_parts: 0,
    created_bom_items: 0,
    extraction_confidence: 'high',
    warnings: [],
  });

  renderWithRouter(
    <BOMImportWizard
      onComplete={onComplete}
      onClose={jest.fn()}
    />
  );

  const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(fileInput, {
    target: {
      files: [new File(['bom'], 'bom.pdf', { type: 'application/pdf' })],
    },
  });
  fireEvent.submit(document.getElementById('upload-form') as HTMLFormElement);

  await screen.findByText('Review Import');
  await screen.findByDisplayValue('MS20426AD4');
  return onComplete;
}

describe('BOMImportWizard', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('removes and restores import review line items', async () => {
    await renderPreview();

    expect(screen.getByText('3 lines ready')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Remove line 20' }));

    expect(screen.queryByDisplayValue('MS20426AD4')).not.toBeInTheDocument();
    expect(screen.getByText('2 lines ready · 1 removed')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Undo Remove' }));

    expect(screen.getByDisplayValue('MS20426AD4')).toBeInTheDocument();
    expect(screen.getByText('3 lines ready')).toBeInTheDocument();
  });

  it('commits only the remaining import review line items', async () => {
    const onComplete = await renderPreview();

    fireEvent.click(screen.getByRole('button', { name: 'Remove line 20' }));
    fireEvent.click(screen.getByRole('button', { name: 'Create' }));

    await waitFor(() => expect(mockedApi.commitBOMImport).toHaveBeenCalledTimes(1));
    expect(mockedApi.commitBOMImport).toHaveBeenCalledWith(
      expect.objectContaining({
        items: expect.arrayContaining([
          expect.objectContaining({ part_number: '820-5052-010' }),
          expect.objectContaining({ part_number: 'AA56032-IBLK' }),
        ]),
      })
    );
    expect(mockedApi.commitBOMImport).toHaveBeenCalledWith(
      expect.objectContaining({
        items: expect.not.arrayContaining([
          expect.objectContaining({ part_number: 'MS20426AD4' }),
        ]),
      })
    );
    await waitFor(() => expect(onComplete).toHaveBeenCalledTimes(1));
  });

  it('prevents creating an empty BOM after all line items are removed', async () => {
    await renderPreview();

    fireEvent.click(screen.getByRole('button', { name: 'Remove line 10' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove line 20' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove line 30' }));

    expect(screen.getByText('No BOM items selected.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create' })).toBeDisabled();
  });
});
