/**
 * ProcessSheets — engineering library UI (PR 2 of docs/PROCESS_SHEETS_SCOPE.md).
 *
 * Guards:
 *   - the list renders from GET /process-sheets and the status filter re-queries;
 *   - `?sheet=<id>` URL-param selection deep-links into the detail view;
 *   - a RELEASED sheet is read-only (no step authoring) with a New Revision path;
 *   - the step editor blocks invalid measurement limits CLIENT-side (no API call)
 *     and forces INSTRUCTION steps to non-required;
 *   - the release dialog implements the settled obsolete-prior-by-default UX:
 *     a released sibling revision shows a PRE-CHECKED "obsolete Rev X" option and
 *     confirm sequences release -> obsolete (non-optimistic, server-ordered);
 *   - server refusals (409) surface their verbatim detail via toast, including
 *     the partial release-succeeded/obsolete-failed outcome.
 */

import React from 'react';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import api from '../services/api';
import ProcessSheetsPage from './ProcessSheets';
import { ToastProvider } from '../components/ui';
import { ProcessSheet, ProcessSheetListItem, ProcessSheetStep } from '../types/processSheet';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getProcessSheets: jest.fn(),
    getProcessSheet: jest.fn(),
    createProcessSheet: jest.fn(),
    updateProcessSheet: jest.fn(),
    deleteProcessSheet: jest.fn(),
    releaseProcessSheet: jest.fn(),
    obsoleteProcessSheet: jest.fn(),
    newProcessSheetRevision: jest.fn(),
    addProcessSheetStep: jest.fn(),
    updateProcessSheetStep: jest.fn(),
    deleteProcessSheetStep: jest.fn(),
    getSPCCharacteristics: jest.fn(),
  },
}));

// Mutable mock user so each test can pick a role before rendering.
let mockUser: { id: number; role: string; is_superuser?: boolean } = {
  id: 1,
  role: 'manager',
  is_superuser: false,
};
jest.mock('../context/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const mockedApi = api as jest.Mocked<typeof api>;

const measurementStep: ProcessSheetStep = {
  id: 10,
  process_sheet_id: 2,
  sequence: 10,
  label: 'Bore diameter',
  instruction_text: null,
  step_type: 'measurement',
  is_required: true,
  config: { nominal: 10, lsl: 9.9, usl: 10.1, unit: 'mm' },
  requires_gauge: true,
  spc_characteristic_id: null,
  created_at: '2026-07-01T12:00:00Z',
  updated_at: '2026-07-01T12:00:00Z',
};

const releasedSheet: ProcessSheet = {
  id: 1,
  sheet_number: 'PS-000001',
  title: 'Final Inspection',
  description: null,
  revision: 'A',
  status: 'released',
  effective_date: '2026-06-01T12:00:00Z',
  obsolete_date: null,
  is_active: true,
  version: 2,
  created_by: 1,
  updated_by: 1,
  created_at: '2026-05-01T12:00:00Z',
  updated_at: '2026-06-01T12:00:00Z',
  steps: [{ ...measurementStep, id: 5, process_sheet_id: 1 }],
};

const draftSheet: ProcessSheet = {
  id: 2,
  sheet_number: 'PS-000001',
  title: 'Final Inspection',
  description: 'Rev B tightens the bore tolerance.',
  revision: 'B',
  status: 'draft',
  effective_date: null,
  obsolete_date: null,
  is_active: true,
  version: 1,
  created_by: 1,
  updated_by: null,
  created_at: '2026-07-01T12:00:00Z',
  updated_at: '2026-07-01T12:00:00Z',
  steps: [measurementStep],
};

function toListItem(sheet: ProcessSheet): ProcessSheetListItem {
  return {
    id: sheet.id,
    sheet_number: sheet.sheet_number,
    title: sheet.title,
    revision: sheet.revision,
    status: sheet.status,
    is_active: sheet.is_active,
    effective_date: sheet.effective_date,
    step_count: sheet.steps.length,
    created_at: sheet.created_at,
    updated_at: sheet.updated_at,
  };
}

const listItems = [toListItem(draftSheet), toListItem(releasedSheet)];

function renderPage(initialEntry = '/process-sheets') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <ToastProvider>
        <ProcessSheetsPage />
      </ToastProvider>
    </MemoryRouter>
  );
}

describe('ProcessSheets page', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockUser = { id: 1, role: 'manager', is_superuser: false };
    mockedApi.getProcessSheets.mockResolvedValue(listItems);
    mockedApi.getProcessSheet.mockImplementation(async (id: number) => {
      if (id === draftSheet.id) return draftSheet;
      if (id === releasedSheet.id) return releasedSheet;
      throw { response: { status: 404, data: { detail: 'Process sheet not found' } } };
    });
    mockedApi.getSPCCharacteristics.mockResolvedValue([]);
    jest.spyOn(window, 'confirm').mockReturnValue(true);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  // ---- list ---------------------------------------------------------------

  it('renders the sheet list with revision, status, and step count', async () => {
    renderPage();
    expect(await screen.findAllByText('PS-000001')).toHaveLength(2);
    expect(screen.getByText('draft')).toBeInTheDocument();
    expect(screen.getByText('released')).toBeInTheDocument();
  });

  it('re-queries with the status filter and reflects it in the URL params', async () => {
    renderPage();
    await screen.findAllByText('PS-000001');

    fireEvent.change(screen.getByLabelText('Status filter'), { target: { value: 'released' } });
    await waitFor(() => {
      expect(mockedApi.getProcessSheets).toHaveBeenCalledWith(
        expect.objectContaining({ status: 'released' })
      );
    });
  });

  it('sends the debounced search to the server', async () => {
    renderPage();
    await screen.findAllByText('PS-000001');

    fireEvent.change(screen.getByLabelText('Search process sheets'), { target: { value: 'bore' } });
    await waitFor(() => {
      expect(mockedApi.getProcessSheets).toHaveBeenCalledWith(
        expect.objectContaining({ search: 'bore' })
      );
    });
  });

  it('hides the New Process Sheet action from non-author roles', async () => {
    mockUser = { id: 9, role: 'viewer' };
    renderPage();
    await screen.findAllByText('PS-000001');
    expect(screen.queryByText('New Process Sheet')).not.toBeInTheDocument();
  });

  // ---- detail / deep link ---------------------------------------------------

  it('deep-links to a sheet via the ?sheet= URL param', async () => {
    renderPage('/process-sheets?sheet=2');
    await waitFor(() => expect(mockedApi.getProcessSheet).toHaveBeenCalledWith(2));
    expect(await screen.findByText('Rev B')).toBeInTheDocument();
    expect(screen.getByText('Bore diameter')).toBeInTheDocument();
  });

  it('renders a released sheet read-only with a New Revision action', async () => {
    renderPage('/process-sheets?sheet=1');
    expect(await screen.findByText('Rev A')).toBeInTheDocument();

    expect(screen.getByText(/Released — content is locked/)).toBeInTheDocument();
    expect(screen.queryByText('Add Step')).not.toBeInTheDocument();
    expect(screen.queryByText('Edit Details')).not.toBeInTheDocument();
    // The per-row step edit/delete actions are also withheld on a released sheet.
    expect(screen.queryByLabelText(/Edit step|Delete step/)).not.toBeInTheDocument();
    expect(screen.getByText('New Revision')).toBeInTheDocument();
    expect(screen.getByText('Obsolete')).toBeInTheDocument();
  });

  it('New Revision creates a draft and navigates to it', async () => {
    const newDraft = { ...draftSheet, id: 7, revision: 'C' };
    mockedApi.newProcessSheetRevision.mockResolvedValue(newDraft);
    mockedApi.getProcessSheet.mockImplementation(async (id: number) =>
      id === 7 ? newDraft : releasedSheet
    );

    renderPage('/process-sheets?sheet=1');
    fireEvent.click(await screen.findByText('New Revision'));

    await waitFor(() => expect(mockedApi.newProcessSheetRevision).toHaveBeenCalledWith(1));
    await waitFor(() => expect(mockedApi.getProcessSheet).toHaveBeenCalledWith(7));
  });

  // ---- step editor ----------------------------------------------------------

  it('blocks invalid measurement limits client-side (no API call)', async () => {
    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Add Step'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Step Type/), { target: { value: 'measurement' } });
    fireEvent.change(within(dialog).getByLabelText(/^Label/), { target: { value: 'OD check' } });
    fireEvent.change(within(dialog).getByLabelText(/^LSL/), { target: { value: '10' } });
    fireEvent.change(within(dialog).getByLabelText(/^Nominal/), { target: { value: '10' } });
    fireEvent.change(within(dialog).getByLabelText(/^USL/), { target: { value: '5' } });
    fireEvent.change(within(dialog).getByLabelText(/^Unit/), { target: { value: 'mm' } });

    fireEvent.click(within(dialog).getByText('Add Step', { selector: 'button' }));

    expect(await within(dialog).findByText('LSL must be less than USL')).toBeInTheDocument();
    expect(mockedApi.addProcessSheetStep).not.toHaveBeenCalled();
  });

  it('submits a valid measurement step with the typed config payload', async () => {
    mockedApi.addProcessSheetStep.mockResolvedValue({ ...measurementStep, id: 99 });
    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Add Step'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Step Type/), { target: { value: 'measurement' } });
    fireEvent.change(within(dialog).getByLabelText(/^Label/), { target: { value: 'OD check' } });
    fireEvent.change(within(dialog).getByLabelText(/^LSL/), { target: { value: '10' } });
    fireEvent.change(within(dialog).getByLabelText(/^Nominal/), { target: { value: '10' } });
    fireEvent.change(within(dialog).getByLabelText(/^USL/), { target: { value: '12' } });
    fireEvent.change(within(dialog).getByLabelText(/^Unit/), { target: { value: 'mm' } });

    fireEvent.click(within(dialog).getByText('Add Step', { selector: 'button' }));

    await waitFor(() => expect(mockedApi.addProcessSheetStep).toHaveBeenCalledTimes(1));
    const [sheetId, payload] = mockedApi.addProcessSheetStep.mock.calls[0];
    expect(sheetId).toBe(2);
    expect(payload).toMatchObject({
      label: 'OD check',
      step_type: 'measurement',
      is_required: true,
      config: { nominal: 10, lsl: 10, usl: 12, unit: 'mm' },
    });
  });

  it('forces INSTRUCTION steps to non-required (checkbox disabled, payload false)', async () => {
    mockedApi.addProcessSheetStep.mockResolvedValue({ ...measurementStep, id: 99 });
    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Add Step'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Step Type/), { target: { value: 'instruction' } });

    const requiredCheckbox = within(dialog).getByLabelText(/Required to complete/) as HTMLInputElement;
    await waitFor(() => expect(requiredCheckbox).toBeDisabled());
    expect(requiredCheckbox).not.toBeChecked();

    fireEvent.change(within(dialog).getByLabelText(/^Label/), { target: { value: 'Read the drawing notes' } });
    fireEvent.click(within(dialog).getByText('Add Step', { selector: 'button' }));

    await waitFor(() => expect(mockedApi.addProcessSheetStep).toHaveBeenCalledTimes(1));
    const [, payload] = mockedApi.addProcessSheetStep.mock.calls[0];
    expect(payload).toMatchObject({
      step_type: 'instruction',
      is_required: false,
      requires_gauge: false,
      spc_characteristic_id: null,
    });
  });

  it('surfaces the server 409 detail via toast when a step write is refused', async () => {
    const detail = 'Cannot add a step to a released process sheet — only drafts are editable.';
    mockedApi.addProcessSheetStep.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Add Step'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/^Label/), { target: { value: 'Deburr' } });
    fireEvent.click(within(dialog).getByText('Add Step', { selector: 'button' }));

    expect(await screen.findByText(detail)).toBeInTheDocument();
  });

  // ---- release dialog (settled obsolete-prior-by-default UX) ----------------

  it('release dialog shows a pre-checked obsolete-prior option and sequences both calls', async () => {
    mockedApi.releaseProcessSheet.mockResolvedValue({ ...draftSheet, status: 'released' });
    mockedApi.obsoleteProcessSheet.mockResolvedValue({ ...releasedSheet, status: 'obsolete' });

    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Release'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Rev A is currently released.')).toBeInTheDocument();
    const checkbox = within(dialog).getByLabelText(/Obsolete Rev A after releasing/) as HTMLInputElement;
    expect(checkbox).toBeChecked();

    fireEvent.click(within(dialog).getByText('Release & Obsolete Rev A'));

    await waitFor(() => expect(mockedApi.obsoleteProcessSheet).toHaveBeenCalledWith(1));
    expect(mockedApi.releaseProcessSheet).toHaveBeenCalledWith(2);
    // Sequenced awaits: release strictly BEFORE obsolete.
    expect(mockedApi.releaseProcessSheet.mock.invocationCallOrder[0]).toBeLessThan(
      mockedApi.obsoleteProcessSheet.mock.invocationCallOrder[0]
    );
  });

  it('unchecking the obsolete-prior option releases without obsoleting', async () => {
    mockedApi.releaseProcessSheet.mockResolvedValue({ ...draftSheet, status: 'released' });

    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Release'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByLabelText(/Obsolete Rev A after releasing/));
    fireEvent.click(within(dialog).getByText('Release', { selector: 'button' }));

    await waitFor(() => expect(mockedApi.releaseProcessSheet).toHaveBeenCalledWith(2));
    expect(mockedApi.obsoleteProcessSheet).not.toHaveBeenCalled();
  });

  it('surfaces a release 409 verbatim and never calls obsolete', async () => {
    const detail = 'Only a draft sheet can be released (this one is released)';
    mockedApi.releaseProcessSheet.mockRejectedValue({ response: { status: 409, data: { detail } } });

    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Release'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByText('Release & Obsolete Rev A'));

    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(mockedApi.obsoleteProcessSheet).not.toHaveBeenCalled();
  });

  it('release succeeds but obsolete fails: toasts the exact failure and refreshes', async () => {
    mockedApi.releaseProcessSheet.mockResolvedValue({ ...draftSheet, status: 'released' });
    mockedApi.obsoleteProcessSheet.mockRejectedValue({
      response: { status: 409, data: { detail: 'Only a released sheet can be obsoleted (this one is obsolete)' } },
    });

    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Release'));

    const dialog = await screen.findByRole('dialog');
    const detailCalls = mockedApi.getProcessSheet.mock.calls.length;
    const listCalls = mockedApi.getProcessSheets.mock.calls.length;
    fireEvent.click(within(dialog).getByText('Release & Obsolete Rev A'));

    expect(
      await screen.findByText(
        /Released Rev B, but failed to obsolete Rev A: Only a released sheet can be obsoleted/
      )
    ).toBeInTheDocument();
    // Non-optimistic: BOTH the detail and the list re-fetch to reflect only
    // what the server did.
    await waitFor(() => expect(mockedApi.getProcessSheet.mock.calls.length).toBeGreaterThan(detailCalls));
    await waitFor(() => expect(mockedApi.getProcessSheets.mock.calls.length).toBeGreaterThan(listCalls));
    // The header badge still shows the status the (mocked) server returned —
    // never an optimistic 'released'.
    expect(await screen.findByText('draft')).toBeInTheDocument();
    expect(screen.queryByText('released')).not.toBeInTheDocument();
  });

  it('hides Release from roles without the release permission', async () => {
    mockUser = { id: 3, role: 'supervisor' }; // author but NOT release
    renderPage('/process-sheets?sheet=2');
    expect(await screen.findByText('Rev B')).toBeInTheDocument();
    expect(screen.getByText('Add Step')).toBeInTheDocument();
    expect(screen.queryByText('Release')).not.toBeInTheDocument();
  });

  // ---- deep-link edge cases (invalid ?sheet ids) -----------------------------

  it('falls back to the list for a non-numeric ?sheet param (no detail fetch)', async () => {
    renderPage('/process-sheets?sheet=abc');
    expect(await screen.findAllByText('PS-000001')).toHaveLength(2);
    expect(mockedApi.getProcessSheet).not.toHaveBeenCalled();
  });

  it('shows a recoverable error state for an unknown ?sheet id', async () => {
    renderPage('/process-sheets?sheet=999');
    expect(await screen.findByText('Could not load this process sheet.')).toBeInTheDocument();
    expect(mockedApi.getProcessSheet).toHaveBeenCalledWith(999);

    // Retry re-runs the SAME fetch (still targeting the deep-linked id).
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(mockedApi.getProcessSheet).toHaveBeenCalledTimes(2));
    expect(mockedApi.getProcessSheet).toHaveBeenLastCalledWith(999);
  });

  // ---- permissions on the detail view ---------------------------------------

  it('operator sees the sheet detail read-only (steps visible, no author/release controls)', async () => {
    mockUser = { id: 4, role: 'operator' };
    renderPage('/process-sheets?sheet=2');
    expect(await screen.findByText('Rev B')).toBeInTheDocument();

    // Steps render for reading...
    expect(screen.getByText('Bore diameter')).toBeInTheDocument();
    // ...but every mutating control is withheld, header and per-row alike.
    expect(screen.queryByText('Add Step')).not.toBeInTheDocument();
    expect(screen.queryByText('Edit Details')).not.toBeInTheDocument();
    expect(screen.queryByText('Delete')).not.toBeInTheDocument();
    expect(screen.queryByText('Release')).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Edit step|Delete step/)).not.toBeInTheDocument();
  });

  it('quality gets both author and release controls on a draft sheet', async () => {
    mockUser = { id: 5, role: 'quality' };
    renderPage('/process-sheets?sheet=2');
    expect(await screen.findByText('Rev B')).toBeInTheDocument();
    expect(screen.getByText('Add Step')).toBeInTheDocument();
    expect(screen.getByText('Release')).toBeInTheDocument();
  });

  // ---- CSV export ------------------------------------------------------------

  it('exports the list to CSV without crashing on a zero-step sheet', async () => {
    const zeroStepItem: ProcessSheetListItem = {
      ...toListItem(draftSheet),
      id: 42,
      sheet_number: 'PS-000042',
      title: 'Empty sheet',
      step_count: 0,
    };
    mockedApi.getProcessSheets.mockResolvedValue([...listItems, zeroStepItem]);

    let exported: Blob | null = null;
    const createSpy = jest.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
      exported = blob as Blob;
      return 'blob:mock';
    });
    const revokeSpy = jest.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
    const clickSpy = jest.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});

    renderPage();
    await screen.findByText('PS-000042');
    fireEvent.click(screen.getByRole('button', { name: /Export CSV/i }));

    expect(createSpy).toHaveBeenCalledTimes(1);
    expect(clickSpy).toHaveBeenCalledTimes(1);
    const text = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(reader.error);
      reader.readAsText(exported as unknown as Blob);
    });
    // The zero-step row exports as a literal 0 — not a blank, not a crash.
    expect(text).toContain('PS-000042,B,Empty sheet,draft,0,');

    createSpy.mockRestore();
    revokeSpy.mockRestore();
    clickSpy.mockRestore();
  });

  // ---- step modal: type switching + the edit (PATCH) path --------------------

  it('switching the type to INSTRUCTION hides measurement fields and strips stale gauge/SPC from the payload', async () => {
    mockedApi.addProcessSheetStep.mockResolvedValue({ ...measurementStep, id: 99 });
    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Add Step'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.change(within(dialog).getByLabelText(/Step Type/), { target: { value: 'measurement' } });
    // Set a measurement-only flag, then switch away.
    fireEvent.click(within(dialog).getByLabelText(/Requires a calibrated gauge/));
    expect(within(dialog).getByText('Measurement tolerance')).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText(/Step Type/), { target: { value: 'instruction' } });

    // Measurement-only fields disappear...
    expect(within(dialog).queryByText('Measurement tolerance')).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/Requires a calibrated gauge/)).not.toBeInTheDocument();
    expect(within(dialog).queryByLabelText(/SPC Characteristic/)).not.toBeInTheDocument();
    // ...and the required toggle is forced off + disabled.
    const requiredCheckbox = within(dialog).getByLabelText(/Required to complete/) as HTMLInputElement;
    await waitFor(() => expect(requiredCheckbox).toBeDisabled());
    expect(requiredCheckbox).not.toBeChecked();

    fireEvent.change(within(dialog).getByLabelText(/^Label/), { target: { value: 'Read the spec note' } });
    fireEvent.click(within(dialog).getByText('Add Step', { selector: 'button' }));

    await waitFor(() => expect(mockedApi.addProcessSheetStep).toHaveBeenCalledTimes(1));
    const [, payload] = mockedApi.addProcessSheetStep.mock.calls[0];
    // The gauge flag checked while the type was MEASUREMENT must not leak.
    expect(payload).toMatchObject({
      step_type: 'instruction',
      is_required: false,
      requires_gauge: false,
      spc_characteristic_id: null,
      config: null,
    });
  });

  it('editing a step seeds the modal from the existing definition and PATCHes it', async () => {
    mockedApi.updateProcessSheetStep.mockResolvedValue(measurementStep);
    renderPage('/process-sheets?sheet=2');
    await screen.findByText('Bore diameter');
    fireEvent.click(screen.getByLabelText('Edit step 10'));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByLabelText(/^Label/)).toHaveValue('Bore diameter');
    expect(within(dialog).getByLabelText(/^LSL/)).toHaveValue('9.9');
    expect(within(dialog).getByLabelText(/^USL/)).toHaveValue('10.1');
    expect(within(dialog).getByLabelText(/Requires a calibrated gauge/)).toBeChecked();

    fireEvent.change(within(dialog).getByLabelText(/^Label/), { target: { value: 'Bore diameter (final)' } });
    fireEvent.click(within(dialog).getByText('Save Step'));

    await waitFor(() => expect(mockedApi.updateProcessSheetStep).toHaveBeenCalledTimes(1));
    const [sheetId, stepId, payload] = mockedApi.updateProcessSheetStep.mock.calls[0];
    expect(sheetId).toBe(2);
    expect(stepId).toBe(10);
    expect(payload).toMatchObject({
      label: 'Bore diameter (final)',
      step_type: 'measurement',
      requires_gauge: true,
      config: { nominal: 10, lsl: 9.9, usl: 10.1, unit: 'mm' },
    });
    expect(mockedApi.addProcessSheetStep).not.toHaveBeenCalled();
  });

  it('a stale draft step edit surfaces the server 409 detail verbatim', async () => {
    const detail = 'Process sheet PS-000001 Rev B is no longer a draft — steps cannot be edited.';
    mockedApi.updateProcessSheetStep.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderPage('/process-sheets?sheet=2');
    await screen.findByText('Bore diameter');
    fireEvent.click(screen.getByLabelText('Edit step 10'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByText('Save Step'));

    expect(await screen.findByText(detail)).toBeInTheDocument();
  });

  it('a stale draft header edit (Edit Details) surfaces the server 409 detail verbatim', async () => {
    const detail = 'Only a draft process sheet can be edited (this one is released)';
    mockedApi.updateProcessSheet.mockRejectedValue({ response: { status: 409, data: { detail } } });
    renderPage('/process-sheets?sheet=2');
    fireEvent.click(await screen.findByText('Edit Details'));

    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByText('Save Changes'));

    expect(await screen.findByText(detail)).toBeInTheDocument();
  });
});
