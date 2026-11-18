/**
 * QMSStandards — "Link Evidence to Clause" can link one of the company's uploaded documents,
 * and an evidence row that carries a `document_id` opens that document.
 *
 * Before this, the dialog had no way to name a document, and an evidence row stored with a
 * `document_id` (185 were loaded through the API that way, with no `module_reference`) had
 * no way to open it from its clause. What is pinned here:
 *
 *  - `currentEvidenceDocuments` offers the CURRENT revision of each document only (a row
 *    another row names as its `previous_revision_id` is superseded) and never shipping or
 *    receiving paperwork, sorted by title. A superseded revision stays `released`, so without
 *    this an obsolete revision would be an equal choice (AS9100D 7.5.3.2);
 *  - the optional "Linked Document" picker is fed by `api.getDocuments()`, fetched EVERY time
 *    the dialog opens; a list from an earlier open stays usable while the new one loads, and
 *    the picker is disabled only until the first list ever arrives;
 *  - choosing a document sends `document_id` as a NUMBER; with no document the payload has
 *    no `document_id` key at all;
 *  - the title follows the chosen document ("<title>, Rev <revision>") while it is empty or
 *    still exactly what the picker last wrote; clearing the document clears a title the
 *    picker wrote; a title someone typed or edited is never replaced or cleared;
 *  - Close, Cancel and Escape discard what was entered only after a confirm, close at once
 *    when nothing was entered, and do nothing while a link is saving; a backdrop click never
 *    closes; a successful link closes with no confirm;
 *  - a refused link (`404 Document not found`: an unknown id, or another company's) is shown
 *    inside the dialog as an alert, and the dialog stays open with everything typed;
 *  - a failed document list says so, offers Retry, and still lets evidence be linked
 *    without a document;
 *  - an evidence row with a `document_id` has an "Open document" link to
 *    `/documents?document=<id>`, which is the URL the Documents page opens a preview from.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QMSStandards, { currentEvidenceDocuments, linkedDocumentHref } from './QMSStandards';
import type { EvidenceDocumentChoice } from './QMSStandards';
import Documents from './Documents';
import { ToastProvider } from '../components/ui';
import api from '../services/api';
import { expectComboBoxOptions, openComboBox, selectComboBoxOption } from '../test-utils/comboBox';
import type {
  QMSAuditReadinessSummary,
  QMSEvidenceResponse,
  QMSStandardListResponse,
  QMSStandardResponse,
} from '../types/api';
import type { Part } from '../types';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQMSStandards: jest.fn(),
    getQMSAuditReadiness: jest.fn(),
    getQMSStandard: jest.fn(),
    getDocuments: jest.fn(),
    addQMSEvidence: jest.fn(),
    // Read by the Documents page in the deep-link round trip at the end.
    getParts: jest.fn(),
    getDocumentTypes: jest.fn(),
  },
}));
// Only the Documents page reads these; QMSStandards imports neither.
jest.mock('../hooks/usePermissions', () => ({
  usePermissions: () => ({ role: 'quality', isSuperuser: false }),
}));
jest.mock('../components/operations/DocumentDetail', () => ({
  __esModule: true,
  default: ({ id }: { id: number }) => <div role="region" aria-label={`Preview of document ${id}`} />,
}));

const mockedApi = api as jest.Mocked<typeof api>;

/**
 * A `GET /documents/` row: the fields the picker reads, plus two the backend DocumentResponse
 * also sends. `previous_revision_id` is always present on the wire (null for a first revision).
 */
interface DocumentRow extends EvidenceDocumentChoice {
  previous_revision_id: number | null;
  status: string;
  created_at: string;
}

function documentRow(
  fields: Pick<DocumentRow, 'id' | 'document_number' | 'revision' | 'title' | 'document_type'> &
    Partial<Pick<DocumentRow, 'previous_revision_id'>>
): DocumentRow {
  return { previous_revision_id: null, status: 'released', created_at: '2026-11-01T12:00:00Z', ...fields };
}

// Deliberately out of title order: the picker sorts by title.
const CALIBRATION_PROCEDURE = documentRow({
  id: 41,
  document_number: 'QP-100',
  revision: 'C',
  title: 'Sample Calibration Procedure',
  document_type: 'procedure',
});
const QUALITY_MANUAL = documentRow({
  id: 43,
  document_number: 'QM-001',
  revision: 'D',
  title: 'Quality Manual',
  document_type: 'procedure',
});
const AUDIT_CHECKLIST = documentRow({
  id: 42,
  document_number: 'QF-200',
  revision: 'A',
  title: 'Audit Checklist Form',
  document_type: 'quality_record',
});
const DOCUMENTS: DocumentRow[] = [CALIBRATION_PROCEDURE, QUALITY_MANUAL, AUDIT_CHECKLIST];

// Uploaded between two opens of the dialog.
const SUPPLIER_SURVEY = documentRow({
  id: 44,
  document_number: 'QF-300',
  revision: 'A',
  title: 'Supplier Survey Form',
  document_type: 'quality_record',
});

const DOCUMENT_OPTIONS = [
  'No document',
  'Audit Checklist Form QF-200 Rev A',
  'Quality Manual QM-001 Rev D',
  'Sample Calibration Procedure QP-100 Rev C',
];

const STANDARD_LIST: QMSStandardListResponse[] = [
  {
    id: 7,
    name: 'AS9100D',
    version: 'Rev D',
    is_active: true,
    total_clauses: 1,
    compliant_clauses: 0,
    partial_clauses: 1,
    non_compliant_clauses: 0,
    not_assessed_clauses: 0,
    created_at: '2026-11-01T12:00:00Z',
  },
];

const AUDIT_SUMMARY: QMSAuditReadinessSummary = {
  total_standards: 1,
  total_clauses: 1,
  compliant: 0,
  partial: 1,
  non_compliant: 0,
  not_assessed: 0,
  not_applicable: 0,
  compliance_percentage: 0,
  total_evidence_links: 2,
  verified_evidence: 0,
  unverified_evidence: 2,
  clauses_needing_review: 0,
};

const CLAUSE_ID = 71;

function evidence(
  overrides: Partial<QMSEvidenceResponse> & Pick<QMSEvidenceResponse, 'id' | 'title'>
): QMSEvidenceResponse {
  return {
    clause_id: CLAUSE_ID,
    evidence_type: 'document',
    is_verified: false,
    is_auto_linked: false,
    created_at: '2026-11-01T12:00:00Z',
    ...overrides,
  };
}

// Loaded through the API the way the 185 production rows were: a document, no module link.
const DOCUMENT_EVIDENCE = evidence({ id: 1, title: 'Quality Manual, Rev D', document_id: 43 });
const MODULE_EVIDENCE = evidence({
  id: 2,
  evidence_type: 'module',
  title: 'Lot Traceability Module',
  module_reference: '/traceability',
});

const STANDARD_DETAIL: QMSStandardResponse = {
  id: 7,
  name: 'AS9100D',
  version: 'Rev D',
  is_active: true,
  created_at: '2026-11-01T12:00:00Z',
  clauses: [
    {
      id: CLAUSE_ID,
      standard_id: 7,
      clause_number: '7.5.3',
      title: 'Control of documented information',
      sort_order: 1,
      compliance_status: 'partial',
      evidence_links: [DOCUMENT_EVIDENCE, MODULE_EVIDENCE],
      sub_clauses: [],
      created_at: '2026-11-01T12:00:00Z',
    },
  ],
};

const OPTIONAL_HELP =
  'Optional. Pick the current revision of an uploaded procedure, form or record so it opens from this clause. ' +
  'Upload new documents on the Documents page.';
const FAILED_HELP =
  /^The document list could not be loaded\.\s+Retry\s+You can still link evidence without a document\.$/;
const DISCARD_MESSAGE = 'You have unsaved changes. Discard them?';

/** An axios-shaped rejection, as the API client's interceptor passes it on. */
function apiError(status: number, detail: unknown): Error {
  return Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data: { detail } },
  });
}

/** A promise the test settles by hand, to hold a request in flight. */
function deferred<T>() {
  let resolve: (value: T) => void = () => undefined;
  let reject: (reason: unknown) => void = () => undefined;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

let confirmSpy: jest.SpyInstance<boolean, [message?: string]>;

beforeEach(() => {
  // Reset, not clear: a once-queued value a failing test left unconsumed must not leak forward.
  jest.resetAllMocks();
  mockedApi.getQMSStandards.mockResolvedValue(STANDARD_LIST);
  mockedApi.getQMSAuditReadiness.mockResolvedValue(AUDIT_SUMMARY);
  mockedApi.getQMSStandard.mockResolvedValue(STANDARD_DETAIL);
  mockedApi.getDocuments.mockResolvedValue(DOCUMENTS);
  mockedApi.addQMSEvidence.mockResolvedValue({});
  // Discarding is refused unless a test says otherwise; a test that expects no prompt asserts it.
  confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
});

afterEach(() => {
  confirmSpy.mockRestore();
});

/** Render the page, open the standard and expand clause 7.5.3. */
async function openClause() {
  render(
    <MemoryRouter>
      <ToastProvider>
        <QMSStandards />
      </ToastProvider>
    </MemoryRouter>
  );
  fireEvent.click(await screen.findByRole('button', { name: /AS9100D/ }));
  fireEvent.click(await screen.findByRole('button', { name: /7\.5\.3/ }));
  await screen.findByText('Evidence Links');
}

/** Click "+ Link Evidence" on the expanded clause and return the dialog. */
async function openEvidenceDialog(): Promise<HTMLElement> {
  fireEvent.click(screen.getByRole('button', { name: '+ Link Evidence' }));
  return screen.findByRole('dialog', { name: 'Link Evidence to Clause' });
}

/** Open the dialog and wait until the document list has loaded into the picker. */
async function openDialogWithDocuments(): Promise<HTMLElement> {
  await openClause();
  const dialog = await openEvidenceDialog();
  await waitFor(() => expect(picker(dialog)).toBeEnabled());
  return dialog;
}

function queryDialog(): HTMLElement | null {
  return screen.queryByRole('dialog', { name: 'Link Evidence to Clause' });
}

function picker(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('combobox', { name: 'Linked Document' });
}

function evidenceTypeSelect(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('combobox', { name: /^Evidence Type/ });
}

function titleInput(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('textbox', { name: /^Title/ });
}

function descriptionInput(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('textbox', { name: 'Description' });
}

function moduleLinkInput(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('textbox', { name: 'System Module Link' });
}

function linkButton(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('button', { name: /^(Link Evidence|Linking\.\.\.)$/ });
}

function cancelButton(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('button', { name: 'Cancel' });
}

function closeButton(dialog: HTMLElement): HTMLElement {
  return within(dialog).getByRole('button', { name: 'Close' });
}

function clearDocument(dialog: HTMLElement) {
  fireEvent.click(within(dialog).getByRole('button', { name: 'Clear selection' }));
}

function setTitle(dialog: HTMLElement, value: string) {
  fireEvent.change(titleInput(dialog), { target: { value } });
}

/** The single `addQMSEvidence(clauseId, payload)` call. */
function linkedCall(): [number, Record<string, unknown>] {
  expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1);
  const [clauseId, payload] = mockedApi.addQMSEvidence.mock.calls[0];
  return [clauseId, payload];
}

function evidenceRow(title: string): HTMLElement {
  const row = screen.getByText(title).closest('[data-testid="linked-evidence-row"]');
  if (!(row instanceof HTMLElement)) throw new Error(`no linked-evidence row for ${title}`);
  return row;
}

/** The three ways out of the dialog that go through the unsaved-changes guard. */
const CLOSERS: Array<[string, (dialog: HTMLElement) => void]> = [
  ['Close (X)', dialog => fireEvent.click(closeButton(dialog))],
  ['Cancel', dialog => fireEvent.click(cancelButton(dialog))],
  ['Escape', dialog => fireEvent.keyDown(dialog, { key: 'Escape' })],
];

describe('currentEvidenceDocuments', () => {
  it('drops a revision that another row supersedes and keeps the newer one', () => {
    const revC = documentRow({
      id: 40,
      document_number: 'QM-001',
      revision: 'C',
      title: 'Quality Manual',
      document_type: 'procedure',
    });
    const revD = { ...QUALITY_MANUAL, previous_revision_id: 40 };

    expect(currentEvidenceDocuments([revC, revD, AUDIT_CHECKLIST])).toEqual([AUDIT_CHECKLIST, revD]);
  });

  it('keeps only the head of a chain of three revisions, whatever order the rows arrive in', () => {
    const revA = documentRow({
      id: 1,
      document_number: 'WI-500',
      revision: 'A',
      title: 'Deburr Work Instruction',
      document_type: 'work_instruction',
    });
    const revB = documentRow({ ...revA, id: 2, revision: 'B', previous_revision_id: 1 });
    const revC = documentRow({ ...revA, id: 3, revision: 'C', previous_revision_id: 2 });

    expect(currentEvidenceDocuments([revA, revB, revC])).toEqual([revC]);
    expect(currentEvidenceDocuments([revC, revA, revB])).toEqual([revC]);
    expect(currentEvidenceDocuments([revB, revC, revA])).toEqual([revC]);
  });

  it('keeps a revision whose predecessor is not in the list', () => {
    const revE = { ...QUALITY_MANUAL, id: 45, revision: 'E', previous_revision_id: 999 };

    expect(currentEvidenceDocuments([revE])).toEqual([revE]);
  });

  it('drops receiving labels, shipping labels and bills of lading', () => {
    const receivingLabel = documentRow({
      id: 50,
      document_number: 'RL-001',
      revision: 'A',
      title: 'Sample Receiving Label',
      document_type: 'receiving_label',
    });
    const shippingLabel = documentRow({
      id: 51,
      document_number: 'SL-001',
      revision: 'A',
      title: 'Sample Shipping Label',
      document_type: 'shipping_label',
    });
    const billOfLading = documentRow({
      id: 52,
      document_number: 'BOL-001',
      revision: 'A',
      title: 'Sample Bill of Lading',
      document_type: 'bill_of_lading',
    });

    expect(currentEvidenceDocuments([receivingLabel, QUALITY_MANUAL, shippingLabel, billOfLading])).toEqual([
      QUALITY_MANUAL,
    ]);
  });

  it('keeps every other document type', () => {
    // Every other value of the backend `DocumentType` enum.
    const kept = [
      'drawing',
      'specification',
      'work_instruction',
      'inspection_plan',
      'certificate',
      'material_cert',
      'procedure',
      'quality_record',
      'ncr',
      'car',
      'fai',
      'other',
    ].map((documentType, index) =>
      documentRow({
        id: 100 + index,
        document_number: `DOC-${100 + index}`,
        revision: 'A',
        title: `Sample ${documentType}`,
        document_type: documentType,
      })
    );

    expect(
      currentEvidenceDocuments(kept)
        .map(doc => doc.document_type)
        .sort()
    ).toEqual(kept.map(doc => doc.document_type).sort());
  });

  it('sorts by title', () => {
    expect(currentEvidenceDocuments(DOCUMENTS).map(doc => doc.title)).toEqual([
      'Audit Checklist Form',
      'Quality Manual',
      'Sample Calibration Procedure',
    ]);
  });

  it('returns a new list and leaves the one it was given as it was', () => {
    const given = [...DOCUMENTS];

    const current = currentEvidenceDocuments(given);

    expect(current).not.toBe(given);
    expect(given).toEqual(DOCUMENTS);
  });
});

describe('Link Evidence dialog: the document picker', () => {
  it('loads the company documents when the dialog opens, listed by title with "<number> Rev <revision>"', async () => {
    const dialog = await openDialogWithDocuments();

    expect(mockedApi.getDocuments).toHaveBeenCalledTimes(1);
    const documents = picker(dialog);
    expect(documents).not.toBeRequired();
    expect(documents).toHaveValue('');
    expect(documents).toHaveAccessibleDescription(OPTIONAL_HELP);
    openComboBox(documents);
    expectComboBoxOptions(documents, DOCUMENT_OPTIONS);
  });

  it('offers only the current revision of each document, and no shipping or receiving paperwork', async () => {
    const supersededManual = documentRow({
      id: 40,
      document_number: 'QM-001',
      revision: 'C',
      title: 'Quality Manual',
      document_type: 'procedure',
    });
    mockedApi.getDocuments.mockResolvedValue([
      supersededManual,
      { ...QUALITY_MANUAL, previous_revision_id: 40 },
      CALIBRATION_PROCEDURE,
      documentRow({
        id: 50,
        document_number: 'RL-001',
        revision: 'A',
        title: 'Sample Receiving Label',
        document_type: 'receiving_label',
      }),
      documentRow({
        id: 51,
        document_number: 'SL-001',
        revision: 'A',
        title: 'Sample Shipping Label',
        document_type: 'shipping_label',
      }),
      documentRow({
        id: 52,
        document_number: 'BOL-001',
        revision: 'A',
        title: 'Sample Bill of Lading',
        document_type: 'bill_of_lading',
      }),
      AUDIT_CHECKLIST,
    ]);
    const dialog = await openDialogWithDocuments();

    openComboBox(picker(dialog));
    expectComboBoxOptions(picker(dialog), DOCUMENT_OPTIONS);
  });

  it('fetches the list again every time the dialog opens, so a document uploaded since is offered', async () => {
    const dialog = await openDialogWithDocuments();
    fireEvent.click(cancelButton(dialog));
    expect(queryDialog()).not.toBeInTheDocument();

    mockedApi.getDocuments.mockResolvedValue([...DOCUMENTS, SUPPLIER_SURVEY]);
    const reopened = await openEvidenceDialog();

    expect(mockedApi.getDocuments).toHaveBeenCalledTimes(2);
    await waitFor(() => {
      openComboBox(picker(reopened));
      expectComboBoxOptions(picker(reopened), [...DOCUMENT_OPTIONS, 'Supplier Survey Form QF-300 Rev A']);
    });
  });

  it('keeps the earlier list usable while the new one loads', async () => {
    const dialog = await openDialogWithDocuments();
    fireEvent.click(cancelButton(dialog));

    const reload = deferred<DocumentRow[]>();
    mockedApi.getDocuments.mockReturnValue(reload.promise);
    const reopened = await openEvidenceDialog();
    expect(mockedApi.getDocuments).toHaveBeenCalledTimes(2);

    // Still loading, yet the list from the first open is offered and can be chosen from.
    expect(picker(reopened)).toBeEnabled();
    expect(picker(reopened)).toHaveAttribute('placeholder', 'Search by title or document number…');
    expect(picker(reopened)).toHaveAccessibleDescription(OPTIONAL_HELP);
    openComboBox(picker(reopened));
    expectComboBoxOptions(picker(reopened), DOCUMENT_OPTIONS);
    selectComboBoxOption(picker(reopened), /^Quality Manual/);
    expect(titleInput(reopened)).toHaveValue('Quality Manual, Rev D');

    await act(async () => reload.resolve([...DOCUMENTS, SUPPLIER_SURVEY]));

    // The new list replaces it, and the choice made meanwhile is kept.
    expect(picker(reopened)).toHaveValue('Quality Manual');
    expect(titleInput(reopened)).toHaveValue('Quality Manual, Rev D');
    openComboBox(picker(reopened));
    expectComboBoxOptions(picker(reopened), [...DOCUMENT_OPTIONS, 'Supplier Survey Form QF-300 Rev A']);

    fireEvent.click(linkButton(reopened));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    expect(linkedCall()[1].document_id).toBe(43);
  });

  it('drops a choice the reloaded list supersedes, says why, and never sends the obsolete revision', async () => {
    const dialog = await openDialogWithDocuments();
    fireEvent.click(cancelButton(dialog));

    const reload = deferred<DocumentRow[]>();
    mockedApi.getDocuments.mockReturnValue(reload.promise);
    const reopened = await openEvidenceDialog();
    // Chosen from the earlier list while the reload is in flight: Rev D, id 43.
    selectComboBoxOption(picker(reopened), /^Quality Manual/);
    expect(titleInput(reopened)).toHaveValue('Quality Manual, Rev D');

    // Meanwhile Rev E was uploaded as a revision of Rev D.
    const manualRevE = documentRow({
      id: 45,
      document_number: 'QM-002',
      revision: 'E',
      title: 'Quality Manual',
      document_type: 'procedure',
      previous_revision_id: 43,
    });
    await act(async () => reload.resolve([...DOCUMENTS, manualRevE]));

    // The superseded choice is dropped, with its picker-written title, and the dialog says so.
    expect(picker(reopened)).toHaveValue('');
    expect(titleInput(reopened)).toHaveValue('');
    expect(within(reopened).getByRole('status')).toHaveTextContent(/newer revision or was removed/);

    // Choosing again offers only Rev E, and that is what is sent.
    selectComboBoxOption(picker(reopened), /^Quality Manual/);
    expect(titleInput(reopened)).toHaveValue('Quality Manual, Rev E');
    expect(within(reopened).queryByText(/newer revision or was removed/)).not.toBeInTheDocument();
    fireEvent.click(linkButton(reopened));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    expect(linkedCall()[1].document_id).toBe(45);
  });

  it('keeps a typed title when a reload drops the chosen document', async () => {
    const dialog = await openDialogWithDocuments();
    fireEvent.click(cancelButton(dialog));

    const reload = deferred<DocumentRow[]>();
    mockedApi.getDocuments.mockReturnValue(reload.promise);
    const reopened = await openEvidenceDialog();
    setTitle(reopened, 'Manual section 4 evidence');
    selectComboBoxOption(picker(reopened), /^Quality Manual/);

    // Removed between opens: the new list no longer has id 43 at all.
    await act(async () => reload.resolve([CALIBRATION_PROCEDURE, AUDIT_CHECKLIST]));

    expect(picker(reopened)).toHaveValue('');
    expect(titleInput(reopened)).toHaveValue('Manual section 4 evidence');
    fireEvent.click(linkButton(reopened));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    expect(linkedCall()[1]).not.toHaveProperty('document_id');
  });

  it('stops the document read when the dialog closes', async () => {
    const pending = deferred<DocumentRow[]>();
    mockedApi.getDocuments.mockReturnValue(pending.promise);
    await openClause();
    const dialog = await openEvidenceDialog();
    const signal = mockedApi.getDocuments.mock.calls[0][1];
    expect(signal).toBeInstanceOf(AbortSignal);
    expect(signal?.aborted).toBe(false);

    fireEvent.click(cancelButton(dialog));
    expect(signal?.aborted).toBe(true);
  });

  it('is disabled while the very first list loads, so nothing can be chosen from a partial list', async () => {
    const firstLoad = deferred<DocumentRow[]>();
    mockedApi.getDocuments.mockReturnValue(firstLoad.promise);
    await openClause();
    const dialog = await openEvidenceDialog();

    expect(picker(dialog)).toBeDisabled();
    expect(picker(dialog)).toHaveAttribute('placeholder', 'Loading documents…');

    await act(async () => firstLoad.resolve(DOCUMENTS));
    expect(picker(dialog)).toBeEnabled();
    expect(picker(dialog)).toHaveAttribute('placeholder', 'Search by title or document number…');
  });
});

describe('Link Evidence dialog: what is sent', () => {
  it('sends the chosen document as a numeric document_id and names the evidence after it', async () => {
    const dialog = await openDialogWithDocuments();

    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    expect(picker(dialog)).toHaveValue('Quality Manual');
    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D');

    fireEvent.click(linkButton(dialog));

    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [clauseId, payload] = linkedCall();
    expect(clauseId).toBe(CLAUSE_ID);
    expect(payload).toStrictEqual({
      evidence_type: 'document',
      title: 'Quality Manual, Rev D',
      description: '',
      module_reference: '',
      document_id: 43,
    });
    expect(typeof payload.document_id).toBe('number');
    await waitFor(() => expect(queryDialog()).not.toBeInTheDocument());
    // The standard is re-read so the new row shows.
    expect(mockedApi.getQMSStandard).toHaveBeenCalledTimes(2);
  });

  it('sends no document_id key at all when no document is chosen', async () => {
    const dialog = await openDialogWithDocuments();
    setTitle(dialog, 'Lot Traceability Module');
    fireEvent.change(moduleLinkInput(dialog), { target: { value: '/traceability' } });

    fireEvent.click(linkButton(dialog));

    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [clauseId, payload] = linkedCall();
    expect(clauseId).toBe(CLAUSE_ID);
    expect(payload).not.toHaveProperty('document_id');
    expect(payload).toStrictEqual({
      evidence_type: 'document',
      title: 'Lot Traceability Module',
      description: '',
      module_reference: '/traceability',
    });
  });

  it('drops document_id again when the chosen document is cleared', async () => {
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Audit Checklist Form/);
    clearDocument(dialog);
    expect(picker(dialog)).toHaveValue('');
    // The title the picker wrote went with the document, so there is nothing to link yet.
    expect(titleInput(dialog)).toHaveValue('');
    expect(linkButton(dialog)).toBeDisabled();

    setTitle(dialog, 'Internal audit checklist');
    fireEvent.click(linkButton(dialog));

    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [, payload] = linkedCall();
    expect(payload).not.toHaveProperty('document_id');
    expect(payload.title).toBe('Internal audit checklist');
  });
});

describe('Link Evidence dialog: the title the picker fills in', () => {
  it('replaces the title it wrote when another document is chosen', async () => {
    const dialog = await openDialogWithDocuments();

    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D');
    selectComboBoxOption(picker(dialog), /^Audit Checklist Form/);
    expect(picker(dialog)).toHaveValue('Audit Checklist Form');
    expect(titleInput(dialog)).toHaveValue('Audit Checklist Form, Rev A');
    selectComboBoxOption(picker(dialog), /^Sample Calibration Procedure/);
    expect(titleInput(dialog)).toHaveValue('Sample Calibration Procedure, Rev C');

    fireEvent.click(linkButton(dialog));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [, payload] = linkedCall();
    expect(payload.title).toBe('Sample Calibration Procedure, Rev C');
    expect(payload.document_id).toBe(41);
  });

  it('clears the title it wrote when the document is cleared, and fills it again on the next choice', async () => {
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Quality Manual/);

    clearDocument(dialog);
    expect(titleInput(dialog)).toHaveValue('');

    selectComboBoxOption(picker(dialog), /^Audit Checklist Form/);
    expect(titleInput(dialog)).toHaveValue('Audit Checklist Form, Rev A');
  });

  it('never replaces or clears a title typed before a document was chosen', async () => {
    const dialog = await openDialogWithDocuments();
    setTitle(dialog, 'Calibration records evidence');

    selectComboBoxOption(picker(dialog), /^Sample Calibration Procedure/);
    expect(titleInput(dialog)).toHaveValue('Calibration records evidence');
    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    expect(titleInput(dialog)).toHaveValue('Calibration records evidence');

    fireEvent.click(linkButton(dialog));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [, payload] = linkedCall();
    expect(payload.title).toBe('Calibration records evidence');
    expect(payload.document_id).toBe(43);
  });

  it('never clears a typed title when the document is cleared', async () => {
    const dialog = await openDialogWithDocuments();
    setTitle(dialog, 'Calibration records evidence');
    selectComboBoxOption(picker(dialog), /^Sample Calibration Procedure/);

    clearDocument(dialog);

    expect(picker(dialog)).toHaveValue('');
    expect(titleInput(dialog)).toHaveValue('Calibration records evidence');
  });

  it('never replaces or clears a title it wrote once someone has edited it', async () => {
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    setTitle(dialog, 'Quality Manual, Rev D: section 7.5');

    selectComboBoxOption(picker(dialog), /^Audit Checklist Form/);
    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D: section 7.5');

    clearDocument(dialog);
    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D: section 7.5');

    fireEvent.click(linkButton(dialog));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [, payload] = linkedCall();
    expect(payload.title).toBe('Quality Manual, Rev D: section 7.5');
    expect(payload).not.toHaveProperty('document_id');
  });

  it('follows the document again once someone empties the title', async () => {
    const dialog = await openDialogWithDocuments();
    setTitle(dialog, 'Calibration records evidence');
    selectComboBoxOption(picker(dialog), /^Sample Calibration Procedure/);

    setTitle(dialog, '');
    selectComboBoxOption(picker(dialog), /^Quality Manual/);

    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D');
  });
});

describe('Link Evidence dialog: closing', () => {
  it.each(CLOSERS)('%s closes at once when nothing was entered', async (_name, close) => {
    const dialog = await openDialogWithDocuments();

    close(dialog);

    expect(queryDialog()).not.toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it.each(CLOSERS)(
    '%s asks before discarding; refused, it keeps the dialog and every value; accepted, it closes and resets',
    async (_name, close) => {
      const dialog = await openDialogWithDocuments();
      fireEvent.change(evidenceTypeSelect(dialog), { target: { value: 'procedure' } });
      selectComboBoxOption(picker(dialog), /^Quality Manual/);
      fireEvent.change(descriptionInput(dialog), { target: { value: 'Section 7.5 controls documented information.' } });
      fireEvent.change(moduleLinkInput(dialog), { target: { value: '/documents' } });

      close(dialog);

      expect(confirmSpy).toHaveBeenCalledTimes(1);
      expect(confirmSpy).toHaveBeenCalledWith(DISCARD_MESSAGE);
      expect(queryDialog()).toBe(dialog);
      expect(evidenceTypeSelect(dialog)).toHaveValue('procedure');
      expect(picker(dialog)).toHaveValue('Quality Manual');
      expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D');
      expect(descriptionInput(dialog)).toHaveValue('Section 7.5 controls documented information.');
      expect(moduleLinkInput(dialog)).toHaveValue('/documents');

      confirmSpy.mockReturnValueOnce(true);
      close(dialog);

      expect(confirmSpy).toHaveBeenCalledTimes(2);
      expect(queryDialog()).not.toBeInTheDocument();
      expect(mockedApi.addQMSEvidence).not.toHaveBeenCalled();

      const reopened = await openEvidenceDialog();
      expect(evidenceTypeSelect(reopened)).toHaveValue('document');
      expect(picker(reopened)).toHaveValue('');
      expect(titleInput(reopened)).toHaveValue('');
      expect(descriptionInput(reopened)).toHaveValue('');
      expect(moduleLinkInput(reopened)).toHaveValue('');
    }
  );

  const ENTRIES: Array<[string, (dialog: HTMLElement) => void]> = [
    ['a chosen document', dialog => selectComboBoxOption(picker(dialog), /^Audit Checklist Form/)],
    [
      'an evidence type other than Document',
      dialog => fireEvent.change(evidenceTypeSelect(dialog), { target: { value: 'training' } }),
    ],
    ['a title', dialog => setTitle(dialog, 'Training Sign-off Sheet')],
    [
      'a description',
      dialog => fireEvent.change(descriptionInput(dialog), { target: { value: 'Signed by each operator.' } }),
    ],
    ['a module link', dialog => fireEvent.change(moduleLinkInput(dialog), { target: { value: '/training' } })],
  ];

  it.each(ENTRIES)('counts %s as something to confirm before discarding', async (_name, enter) => {
    const dialog = await openDialogWithDocuments();
    enter(dialog);

    fireEvent.click(cancelButton(dialog));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(queryDialog()).toBe(dialog);
  });

  it('never closes on a backdrop click, with or without anything entered', async () => {
    const dialog = await openDialogWithDocuments();
    const backdrop = dialog.parentElement;
    if (!backdrop) throw new Error('the dialog has no backdrop');

    fireEvent.click(backdrop);
    expect(queryDialog()).toBe(dialog);

    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    fireEvent.click(backdrop);
    expect(queryDialog()).toBe(dialog);
    expect(picker(dialog)).toHaveValue('Quality Manual');
    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D');
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('Escape with the document list open closes only the list', async () => {
    const dialog = await openDialogWithDocuments();
    openComboBox(picker(dialog));

    fireEvent.keyDown(picker(dialog), { key: 'Escape' });

    expect(picker(dialog)).toHaveAttribute('aria-expanded', 'false');
    expect(queryDialog()).toBe(dialog);
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('closes without asking after a successful link, and asks again for the next unsaved entry', async () => {
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Quality Manual/);

    fireEvent.click(linkButton(dialog));

    await waitFor(() => expect(queryDialog()).not.toBeInTheDocument());
    expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1);
    expect(confirmSpy).not.toHaveBeenCalled();

    // The save does not wave through what is typed on the next open.
    const reopened = await openEvidenceDialog();
    expect(picker(reopened)).toHaveValue('');
    setTitle(reopened, 'Management Review Minutes');
    fireEvent.click(cancelButton(reopened));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(queryDialog()).toBe(reopened);
    expect(titleInput(reopened)).toHaveValue('Management Review Minutes');
  });

  it('ignores Close, Cancel and Escape while the link is saving', async () => {
    const save = deferred<unknown>();
    mockedApi.addQMSEvidence.mockReturnValue(save.promise);
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Quality Manual/);

    fireEvent.click(linkButton(dialog));

    expect(linkButton(dialog)).toHaveTextContent('Linking...');
    expect(closeButton(dialog)).toBeDisabled();
    expect(cancelButton(dialog)).toBeDisabled();
    for (const [, close] of CLOSERS) close(dialog);
    expect(queryDialog()).toBe(dialog);
    expect(confirmSpy).not.toHaveBeenCalled();

    await act(async () => save.resolve({}));
    expect(queryDialog()).not.toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
  });
});

describe('Link Evidence dialog: a refused link', () => {
  it('shows 404 "Document not found" inside the dialog and keeps the dialog and its values', async () => {
    mockedApi.addQMSEvidence.mockRejectedValueOnce(apiError(404, 'Document not found'));
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    fireEvent.change(descriptionInput(dialog), { target: { value: 'Section 7.5 controls documented information.' } });

    fireEvent.click(linkButton(dialog));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Document not found');
    // Shown once, in the dialog; not in the page's error banner behind it.
    expect(screen.getAllByText('Document not found')).toHaveLength(1);
    expect(queryDialog()).toBe(dialog);
    expect(picker(dialog)).toHaveValue('Quality Manual');
    expect(titleInput(dialog)).toHaveValue('Quality Manual, Rev D');
    expect(descriptionInput(dialog)).toHaveValue('Section 7.5 controls documented information.');
    expect(linkButton(dialog)).toBeEnabled();
    expect(cancelButton(dialog)).toBeEnabled();
    // Nothing was linked, so the standard is not re-read.
    expect(mockedApi.getQMSStandard).toHaveBeenCalledTimes(1);

    // Choosing another document (which renames the picker-written title) and retrying links
    // it, and the alert goes with the dialog.
    selectComboBoxOption(picker(dialog), /^Audit Checklist Form/);
    fireEvent.click(linkButton(dialog));
    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(2));
    expect(mockedApi.addQMSEvidence.mock.calls[1][1]).toStrictEqual({
      evidence_type: 'document',
      title: 'Audit Checklist Form, Rev A',
      description: 'Section 7.5 controls documented information.',
      module_reference: '',
      document_id: 42,
    });
    await waitFor(() => expect(queryDialog()).not.toBeInTheDocument());
    expect(screen.queryByText('Document not found')).not.toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('falls back to a generic message when the refusal carries no text', async () => {
    mockedApi.addQMSEvidence.mockRejectedValueOnce(apiError(500, undefined));
    const dialog = await openDialogWithDocuments();
    setTitle(dialog, 'Management Review Minutes');

    fireEvent.click(linkButton(dialog));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Failed to link evidence. Try again.');
    expect(titleInput(dialog)).toHaveValue('Management Review Minutes');
  });

  it('clears the alert when the dialog is discarded and reopened', async () => {
    mockedApi.addQMSEvidence.mockRejectedValueOnce(apiError(404, 'Document not found'));
    const dialog = await openDialogWithDocuments();
    selectComboBoxOption(picker(dialog), /^Quality Manual/);
    fireEvent.click(linkButton(dialog));
    await within(dialog).findByRole('alert');

    // What was entered is still unsaved, so closing asks first.
    confirmSpy.mockReturnValueOnce(true);
    fireEvent.click(closeButton(dialog));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(queryDialog()).not.toBeInTheDocument();

    const reopened = await openEvidenceDialog();
    expect(within(reopened).queryByRole('alert')).not.toBeInTheDocument();
    expect(picker(reopened)).toHaveValue('');
    expect(titleInput(reopened)).toHaveValue('');
  });
});

describe('Link Evidence dialog: the document list fails to load', () => {
  it('says so in the help text, offers Retry, and still links evidence without a document', async () => {
    mockedApi.getDocuments.mockRejectedValueOnce(apiError(500, 'Internal Server Error'));
    await openClause();
    const dialog = await openEvidenceDialog();

    await waitFor(() => expect(picker(dialog)).toHaveAccessibleDescription(FAILED_HELP));
    expect(within(dialog).getByRole('button', { name: 'Retry' })).toBeEnabled();
    expect(picker(dialog)).toBeDisabled();

    setTitle(dialog, 'Training Sign-off Sheet');
    expect(linkButton(dialog)).toBeEnabled();
    fireEvent.click(linkButton(dialog));

    await waitFor(() => expect(mockedApi.addQMSEvidence).toHaveBeenCalledTimes(1));
    const [clauseId, payload] = linkedCall();
    expect(clauseId).toBe(CLAUSE_ID);
    expect(payload).not.toHaveProperty('document_id');
    expect(payload.title).toBe('Training Sign-off Sheet');
    await waitFor(() => expect(queryDialog()).not.toBeInTheDocument());
  });

  it('Retry fetches the list again: the options appear and the failure text goes', async () => {
    mockedApi.getDocuments.mockRejectedValueOnce(apiError(500, 'Internal Server Error'));
    await openClause();
    const dialog = await openEvidenceDialog();
    await waitFor(() => expect(picker(dialog)).toHaveAccessibleDescription(FAILED_HELP));
    setTitle(dialog, 'Calibration records evidence');

    const retry = deferred<DocumentRow[]>();
    mockedApi.getDocuments.mockReturnValueOnce(retry.promise);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));

    expect(mockedApi.getDocuments).toHaveBeenCalledTimes(2);
    // Loading again: the failure text is gone, and no list has ever arrived to choose from.
    expect(within(dialog).queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument();
    expect(picker(dialog)).toHaveAccessibleDescription(OPTIONAL_HELP);
    expect(picker(dialog)).toBeDisabled();
    expect(picker(dialog)).toHaveAttribute('placeholder', 'Loading documents…');

    await act(async () => retry.resolve(DOCUMENTS));

    expect(picker(dialog)).toBeEnabled();
    expect(picker(dialog)).toHaveAccessibleDescription(OPTIONAL_HELP);
    expect(within(dialog).queryByText(/could not be loaded/)).not.toBeInTheDocument();
    openComboBox(picker(dialog));
    expectComboBoxOptions(picker(dialog), DOCUMENT_OPTIONS);
    // Retrying lost nothing that was typed.
    expect(titleInput(dialog)).toHaveValue('Calibration records evidence');
  });

  it('announces the failure, and after Retry puts focus on the picker (or back on Retry if it fails again)', async () => {
    mockedApi.getDocuments.mockRejectedValueOnce(apiError(500, 'Internal Server Error'));
    await openClause();
    const dialog = await openEvidenceDialog();
    await waitFor(() => expect(picker(dialog)).toHaveAccessibleDescription(FAILED_HELP));
    expect(within(dialog).getByRole('status')).toHaveTextContent('The document list could not be loaded.');

    // A second failure: focus returns to the new Retry button rather than falling to <body>.
    mockedApi.getDocuments.mockRejectedValueOnce(apiError(500, 'Internal Server Error'));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(within(dialog).getByRole('button', { name: 'Retry' })).toHaveFocus());

    // A success: focus lands on the now-enabled picker.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(picker(dialog)).toHaveFocus());
  });

  it('tries the list again when the dialog is reopened', async () => {
    mockedApi.getDocuments.mockRejectedValueOnce(apiError(500, 'Internal Server Error'));
    await openClause();
    const dialog = await openEvidenceDialog();
    await waitFor(() => expect(picker(dialog)).toHaveAccessibleDescription(FAILED_HELP));
    fireEvent.click(cancelButton(dialog));
    expect(confirmSpy).not.toHaveBeenCalled();

    const reopened = await openEvidenceDialog();
    await waitFor(() => expect(picker(reopened)).toBeEnabled());
    expect(mockedApi.getDocuments).toHaveBeenCalledTimes(2);
    expect(picker(reopened)).toHaveAccessibleDescription(OPTIONAL_HELP);
    selectComboBoxOption(picker(reopened), /^Quality Manual/);
    expect(titleInput(reopened)).toHaveValue('Quality Manual, Rev D');
  });

  it('a failed reload keeps the earlier list usable, and offers Retry', async () => {
    const dialog = await openDialogWithDocuments();
    fireEvent.click(cancelButton(dialog));

    mockedApi.getDocuments.mockRejectedValueOnce(apiError(500, 'Internal Server Error'));
    const reopened = await openEvidenceDialog();

    await waitFor(() => expect(picker(reopened)).toHaveAccessibleDescription(FAILED_HELP));
    expect(picker(reopened)).toBeEnabled();
    openComboBox(picker(reopened));
    expectComboBoxOptions(picker(reopened), DOCUMENT_OPTIONS);
    selectComboBoxOption(picker(reopened), /^Audit Checklist Form/);
    expect(titleInput(reopened)).toHaveValue('Audit Checklist Form, Rev A');
  });
});

describe('Evidence rows: opening a linked document', () => {
  it('builds the Documents deep link from the document id', () => {
    expect(linkedDocumentHref(43)).toBe('/documents?document=43');
  });

  it('gives a row with a document_id an "Open document" link, and a row without one none', async () => {
    await openClause();

    const link = within(evidenceRow(DOCUMENT_EVIDENCE.title)).getByRole('link', { name: /^Open document/ });
    // Label in Name: the accessible name starts with the visible text and names the evidence.
    expect(link).toHaveTextContent('Open document');
    expect(link).toHaveAccessibleName('Open document: Quality Manual, Rev D');
    expect(link).toHaveAttribute('href', '/documents?document=43');

    const moduleRow = evidenceRow(MODULE_EVIDENCE.title);
    expect(within(moduleRow).queryByRole('link', { name: /^Open document/ })).not.toBeInTheDocument();
    // Its module link is unaffected.
    expect(within(moduleRow).getByRole('link', { name: '/traceability' })).toHaveAttribute('href', '/traceability');

    expect(screen.getAllByRole('link', { name: /^Open document/ })).toHaveLength(1);
  });

  it('the link lands on the Documents page with that document open', async () => {
    const parts: Part[] = [];
    mockedApi.getParts.mockResolvedValue(parts);
    mockedApi.getDocumentTypes.mockResolvedValue([{ value: 'procedure', label: 'Procedure' }]);

    render(
      <MemoryRouter initialEntries={[linkedDocumentHref(43)]}>
        <Documents />
      </MemoryRouter>
    );

    expect(await screen.findByRole('region', { name: 'Preview of document 43' })).toBeInTheDocument();
  });
});
