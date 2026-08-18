import React from 'react';
import { fireEvent, renderWithRouter, screen, waitFor } from '../../test-utils';
import api from '../../services/api';
import { ToastProvider } from '../ui';
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

  describe('shared Modal adoption', () => {
    // The wizard renders through the shared <Modal> primitive (no hand-rolled
    // overlay): a role=dialog panel, portaled into document.body via Modal's
    // own portal, which also keeps it above the fixed z-50 sidebar at z-[60].
    const renderUpload = () =>
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={jest.fn()} />);

    const getOverlay = () =>
      document.body.querySelector('.fixed.inset-0') as HTMLElement | null;

    it('renders the upload step when opened', () => {
      renderUpload();
      expect(screen.getByText('Import BOM / Drawing')).toBeInTheDocument();
      expect(document.getElementById('upload-form')).toBeInTheDocument();
      expect(document.querySelector('input[type="file"]')).toBeInTheDocument();
    });

    it('renders through the shared Modal as a role=dialog panel', () => {
      renderUpload();
      const dialog = screen.getByRole('dialog');
      expect(dialog).toHaveAttribute('aria-modal', 'true');
      // The wizard content lives inside the Modal panel.
      expect(dialog).toContainElement(screen.getByText('Import BOM / Drawing'));
    });

    it('renders the overlay into document.body, outside the component container', () => {
      const { container } = renderUpload();

      const overlay = getOverlay();
      expect(overlay).not.toBeNull();
      // Portaled: the overlay is NOT nested inside the RTL render container.
      expect(container).not.toContainElement(overlay);
      // Portal target is document.body itself (overlay is a direct child).
      expect(overlay!.parentElement).toBe(document.body);
    });

    it('layers the overlay above the sidebar via z-[60] and fixed inset-0', () => {
      renderUpload();
      const overlay = getOverlay();
      expect(overlay).toHaveClass('fixed', 'inset-0', 'z-[60]');
      expect(overlay).not.toHaveClass('z-50');
    });

    it('calls onClose when the backdrop is clicked', () => {
      const onClose = jest.fn();
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={onClose} />);

      fireEvent.click(getOverlay() as HTMLElement);
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('does not call onClose when the modal panel itself is clicked', () => {
      const onClose = jest.fn();
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={onClose} />);

      // Clicking inside the panel (e.g. the heading) should not bubble to the backdrop.
      fireEvent.click(screen.getByText('Import BOM / Drawing'));
      expect(onClose).not.toHaveBeenCalled();
    });

    it('calls onClose when the header close button is clicked', () => {
      const onClose = jest.fn();
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={onClose} />);

      // The header X and the footer Cancel both close; click the first close affordance.
      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(onClose).toHaveBeenCalledTimes(1);
    });
  });

  describe('unsaved-progress close guard', () => {
    // A chosen file or a parsed/edited preview is real work: every close path
    // (Cancel, header X, Escape, backdrop) routes through confirmDiscard(). A
    // pristine just-opened wizard still closes silently (covered above by the
    // backdrop / Cancel tests, which never see a confirm prompt).
    let confirmSpy: jest.SpyInstance;

    afterEach(() => {
      confirmSpy?.mockRestore();
    });

    it('closes a pristine wizard without prompting', () => {
      confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
      const onClose = jest.fn();
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={onClose} />);

      fireEvent.click(screen.getByRole('button', { name: 'Close' }));
      expect(confirmSpy).not.toHaveBeenCalled();
      expect(onClose).toHaveBeenCalledTimes(1);
    });

    it('prompts once a file is chosen and stays open when the user declines', () => {
      confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
      const onClose = jest.fn();
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={onClose} />);

      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      fireEvent.change(fileInput, {
        target: { files: [new File(['bom'], 'bom.pdf', { type: 'application/pdf' })] },
      });

      fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(onClose).not.toHaveBeenCalled();
    });

    it('guards Escape on the preview step and closes when the user confirms', async () => {
      confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
      const onClose = jest.fn();
      mockedApi.previewBOMImport.mockResolvedValue(previewResponse);
      renderWithRouter(<BOMImportWizard onComplete={jest.fn()} onClose={onClose} />);

      const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
      fireEvent.change(fileInput, {
        target: { files: [new File(['bom'], 'bom.pdf', { type: 'application/pdf' })] },
      });
      fireEvent.submit(document.getElementById('upload-form') as HTMLFormElement);
      await screen.findByText('Review Import');

      fireEvent.keyDown(window, { key: 'Escape' });
      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(onClose).toHaveBeenCalledTimes(1);
    });
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

/**
 * The SECOND surface for `POST /bom/import/commit` (Parts → BOM tab).
 *
 * Its `warnings` list has one shape: the import WORKED but did not do
 * everything asked — a generated part number, a line created without one, or a
 * parent part the server's conversion gate would NOT reclassify as an assembly
 * because unfinished work orders still tie it as material. That is the `warning`
 * toast variant (role="alert", which interrupts a screen reader), not `info`
 * (role="status", which waits for a pause and may never be read) — and it has
 * to QUOTE the sentences: a bare count says something was left undone and never
 * says what, which on the conversion refusal is the whole of the message.
 */
describe('BOMImportWizard: a commit that left something undone', () => {
  // PASTED FROM THE SERVER, not paraphrased: `part_type_change_refusal` for a
  // material part being promoted to `assembly` while two unfinished work orders
  // tie it, plus the sentence `bom.py::_promote_existing_part_to_assembly` appends when
  // it routes the refusal into `warnings` rather than raising. The point of this
  // suite is that the toast QUOTES the server, so the quote has to be real.
  const NOT_PROMOTED =
    'Part 818-3928-638 cannot be reclassified as an assembly: 2 unfinished work orders still tie it as ' +
    'material. A tie depletes the tied part when that work completes, so reclassifying it now would leave ' +
    'standing demand that consumes finished goods. Untie those work orders first, then change the part type. ' +
    "The BOM was imported and the part's type was left unchanged.";

  const renderWithToasts = async (warnings: string[]) => {
    mockedApi.previewBOMImport.mockResolvedValue(previewResponse);
    mockedApi.commitBOMImport.mockResolvedValue({
      document_type: 'bom',
      assembly_part_id: 1,
      assembly_part_number: '818-3928-638',
      bom_id: null,
      created_parts: 0,
      created_bom_items: 0,
      extraction_confidence: 'high',
      warnings,
    });

    renderWithRouter(
      <ToastProvider>
        <BOMImportWizard onComplete={jest.fn().mockResolvedValue(undefined)} onClose={jest.fn()} />
      </ToastProvider>
    );

    const fileInput = document.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, {
      target: { files: [new File(['bom'], 'bom.pdf', { type: 'application/pdf' })] },
    });
    fireEvent.submit(document.getElementById('upload-form') as HTMLFormElement);
    await screen.findByDisplayValue('MS20426AD4');

    fireEvent.click(screen.getByRole('button', { name: 'Create' }));
    await waitFor(() => expect(mockedApi.commitBOMImport).toHaveBeenCalledTimes(1));
  };

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('interrupts with the sentences themselves, not a count or a passive status', async () => {
    await renderWithToasts([NOT_PROMOTED]);

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Import completed with warnings');
    expect(alert).toHaveTextContent(/cannot be reclassified as an assembly/i);
    expect(alert).not.toHaveTextContent(/1 warning/i);
  });

  it('still says plain success when nothing was left undone', async () => {
    await renderWithToasts([]);

    expect(await screen.findByText('Import completed')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});
