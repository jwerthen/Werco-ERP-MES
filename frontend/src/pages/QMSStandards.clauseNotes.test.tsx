/**
 * QMSStandards — a clause's compliance notes are editable from the page.
 *
 * Before this, the page could change only a clause's compliance STATUS; its notes were
 * display-only and changeable only through the API (`PUT /qms-standards/clauses/{id}`).
 *
 * Contract held here, through the real page and the real dialog:
 *  - An expanded clause shows a Notes block: the stored notes with their line breaks kept and
 *    an "Edit notes" button, or "No notes yet." and an "Add notes" button.
 *  - Saving sends `{ compliance_notes }` ONLY. The server stamps `last_assessed_date` /
 *    `last_assessed_by` whenever `compliance_status` is present in the body
 *    (`"compliance_status" in update_data`), so a notes edit must never carry the status, not
 *    even unchanged.
 *  - Non-optimistic: the dialog closes after the server accepts, then the standard is reloaded.
 *    A refusal stays in the dialog with the server's reason and nothing is reloaded.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QMSStandards from './QMSStandards';
import { ToastProvider } from '../components/ui';
import api from '../services/api';
import type {
  QMSAuditReadinessSummary,
  QMSClauseResponse,
  QMSStandardListResponse,
  QMSStandardResponse,
} from '../types/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQMSStandards: jest.fn(),
    getQMSAuditReadiness: jest.fn(),
    getQMSStandard: jest.fn(),
    updateQMSClause: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const STANDARD_ID = 9;
const NOTED_CLAUSE_ID = 91;
const BARE_CLAUSE_ID = 92;
const STORED_NOTES =
  'Router travels with every job.\nFAI on each new revision.\n\nReviewed at the last internal audit.';

const STANDARD_LIST: QMSStandardListResponse[] = [
  {
    id: STANDARD_ID,
    name: 'AS9100D',
    version: 'Rev D',
    is_active: true,
    total_clauses: 2,
    compliant_clauses: 0,
    partial_clauses: 1,
    non_compliant_clauses: 0,
    not_assessed_clauses: 1,
    created_at: '2026-11-01T12:00:00Z',
  },
];

const AUDIT_SUMMARY: QMSAuditReadinessSummary = {
  total_standards: 1,
  total_clauses: 2,
  compliant: 0,
  partial: 1,
  non_compliant: 0,
  not_assessed: 1,
  not_applicable: 0,
  compliance_percentage: 0,
  total_evidence_links: 0,
  verified_evidence: 0,
  unverified_evidence: 0,
  clauses_needing_review: 0,
};

function clause(overrides: Partial<QMSClauseResponse> & Pick<QMSClauseResponse, 'id' | 'clause_number' | 'title'>) {
  const base: QMSClauseResponse = {
    id: overrides.id,
    standard_id: STANDARD_ID,
    clause_number: overrides.clause_number,
    title: overrides.title,
    sort_order: 1,
    compliance_status: 'not_assessed',
    evidence_links: [],
    sub_clauses: [],
    created_at: '2026-11-01T12:00:00Z',
  };
  return { ...base, ...overrides };
}

function standardDetail(notedClauseNotes: string | undefined): QMSStandardResponse {
  return {
    id: STANDARD_ID,
    name: 'AS9100D',
    version: 'Rev D',
    is_active: true,
    created_at: '2026-11-01T12:00:00Z',
    clauses: [
      clause({
        id: NOTED_CLAUSE_ID,
        clause_number: '8.5.1',
        title: 'Control of production and service provision',
        sort_order: 1,
        compliance_status: 'partial',
        compliance_notes: notedClauseNotes,
        last_assessed_date: '2026-11-10T15:00:00Z',
        last_assessed_by: 4,
      }),
      clause({
        id: BARE_CLAUSE_ID,
        clause_number: '7.1.5',
        title: 'Monitoring and measuring resources',
        sort_order: 2,
      }),
    ],
  };
}

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <QMSStandards />
      </ToastProvider>
    </MemoryRouter>
  );
}

/** Opens the standard and expands one clause; returns that clause's Notes block. */
async function openClause(clauseNumber: '8.5.1' | '7.1.5') {
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: /AS9100D/ }));
  await screen.findByRole('button', { name: /Auto-Link Evidence/ });
  const header = screen
    .getAllByRole('button')
    .find(el => el.getAttribute('tabindex') === '0' && el.textContent?.startsWith(clauseNumber));
  if (!header) throw new Error(`no clause header for ${clauseNumber}`);
  fireEvent.click(header);
  const block = await screen.findByTestId('clause-notes');
  return block;
}

const notesDialog = (clauseNumber: string) => screen.getByRole('dialog', { name: `Notes for clause ${clauseNumber}` });
const notesBox = () => screen.getByLabelText('Compliance notes') as HTMLTextAreaElement;

let confirmSpy: jest.SpyInstance<boolean, [message?: string]>;

beforeEach(() => {
  // reset, not clear: some tests queue mockResolvedValueOnce reloads, and an unconsumed one
  // must not leak into the next test's first load.
  jest.resetAllMocks();
  confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(true);
  mockedApi.getQMSStandards.mockResolvedValue(STANDARD_LIST);
  mockedApi.getQMSAuditReadiness.mockResolvedValue(AUDIT_SUMMARY);
  mockedApi.getQMSStandard.mockResolvedValue(standardDetail(STORED_NOTES));
});

afterEach(() => {
  confirmSpy.mockRestore();
});

describe('the Notes block on an expanded clause', () => {
  it('shows stored notes with their line breaks preserved, and an "Edit notes" button', async () => {
    const block = await openClause('8.5.1');

    const text = within(block).getByText((_content, el) => el?.tagName === 'P' && el.textContent === STORED_NOTES);
    // jsdom applies no Tailwind CSS, so the class is the only observable: pre-line keeps the
    // newlines that the old inline "<strong>Notes:</strong> {notes}" rendering collapsed.
    expect(text).toHaveClass('whitespace-pre-line');
    expect(text.textContent).toContain('\n\n');

    const edit = within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' });
    expect(edit).toHaveTextContent('Edit notes');
    expect(within(block).queryByText('No notes yet.')).not.toBeInTheDocument();
    expect(within(block).queryByRole('button', { name: /Add notes/ })).not.toBeInTheDocument();
  });

  it('says "No notes yet." with an "Add notes" button when the clause has none', async () => {
    const block = await openClause('7.1.5');

    expect(within(block).getByText('No notes yet.')).toBeInTheDocument();
    const add = within(block).getByRole('button', { name: 'Add notes for clause 7.1.5' });
    expect(add).toHaveTextContent('Add notes');
    expect(within(block).queryByRole('button', { name: /Edit notes/ })).not.toBeInTheDocument();

    fireEvent.click(add);
    expect(notesDialog('7.1.5')).toBeInTheDocument();
    expect(notesBox().value).toBe('');
  });

  it('opening and cancelling the editor leaves the clause expanded and writes nothing', async () => {
    const block = await openClause('8.5.1');
    fireEvent.click(within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' }));
    fireEvent.click(within(notesDialog('8.5.1')).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByTestId('clause-notes')).toBeInTheDocument();
    expect(mockedApi.updateQMSClause).not.toHaveBeenCalled();
  });
});

describe('editing notes', () => {
  it('"Edit notes" opens the dialog holding the stored notes', async () => {
    const block = await openClause('8.5.1');
    fireEvent.click(within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' }));

    const dialog = notesDialog('8.5.1');
    expect(within(dialog).getByRole('heading')).toHaveTextContent(
      'Notes for 8.5.1 Control of production and service provision'
    );
    expect(notesBox().value).toBe(STORED_NOTES);
    expect(within(dialog).getByRole('button', { name: 'Save notes' })).toBeDisabled();
  });

  it('saves the notes alone (no compliance_status key), then closes and reloads the standard', async () => {
    const edited = 'Router travels with every job.\nTraveler scanned at each operation.';
    mockedApi.updateQMSClause.mockResolvedValue({});
    mockedApi.getQMSStandard
      .mockResolvedValueOnce(standardDetail(STORED_NOTES))
      .mockResolvedValueOnce(standardDetail(edited));

    const block = await openClause('8.5.1');
    expect(mockedApi.getQMSStandard).toHaveBeenCalledTimes(1);
    fireEvent.click(within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' }));
    fireEvent.change(notesBox(), { target: { value: edited } });
    fireEvent.click(within(notesDialog('8.5.1')).getByRole('button', { name: 'Save notes' }));

    await waitFor(() => expect(mockedApi.getQMSStandard).toHaveBeenCalledTimes(2));
    expect(mockedApi.updateQMSClause).toHaveBeenCalledTimes(1);
    const [clauseId, body] = mockedApi.updateQMSClause.mock.calls[0];
    expect(clauseId).toBe(NOTED_CLAUSE_ID);
    // Strict: a `compliance_status` key, even undefined or unchanged, is a status edit.
    expect(body).toStrictEqual({ compliance_notes: edited });
    expect(mockedApi.getQMSStandard).toHaveBeenLastCalledWith(STANDARD_ID);
    // The reload follows the write; it never races ahead of it.
    expect(mockedApi.updateQMSClause.mock.invocationCallOrder[0]).toBeLessThan(
      mockedApi.getQMSStandard.mock.invocationCallOrder[1]
    );

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const reloaded = await within(screen.getByTestId('clause-notes')).findByText(
      (_content, el) => el?.tagName === 'P' && el.textContent === edited
    );
    expect(reloaded).toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it('clearing the notes sends compliance_notes: null (not undefined, which JSON would drop)', async () => {
    mockedApi.updateQMSClause.mockResolvedValue({});
    mockedApi.getQMSStandard
      .mockResolvedValueOnce(standardDetail(STORED_NOTES))
      .mockResolvedValueOnce(standardDetail(undefined));

    const block = await openClause('8.5.1');
    fireEvent.click(within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' }));
    fireEvent.change(notesBox(), { target: { value: '  \n ' } });
    fireEvent.click(within(notesDialog('8.5.1')).getByRole('button', { name: 'Save notes' }));

    await waitFor(() => expect(mockedApi.getQMSStandard).toHaveBeenCalledTimes(2));
    expect(mockedApi.updateQMSClause).toHaveBeenCalledWith(NOTED_CLAUSE_ID, expect.anything());
    expect(mockedApi.updateQMSClause.mock.calls[0][1]).toStrictEqual({ compliance_notes: null });
    expect(JSON.parse(JSON.stringify(mockedApi.updateQMSClause.mock.calls[0][1]))).toStrictEqual({
      compliance_notes: null,
    });

    expect(await within(screen.getByTestId('clause-notes')).findByText('No notes yet.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add notes for clause 8.5.1' })).toBeInTheDocument();
  });

  it('adds first notes to a clause that had none', async () => {
    mockedApi.updateQMSClause.mockResolvedValue({});
    const block = await openClause('7.1.5');
    fireEvent.click(within(block).getByRole('button', { name: 'Add notes for clause 7.1.5' }));
    fireEvent.change(notesBox(), { target: { value: 'Gauge list kept in calibration module.' } });
    fireEvent.click(within(notesDialog('7.1.5')).getByRole('button', { name: 'Save notes' }));

    await waitFor(() => expect(mockedApi.updateQMSClause).toHaveBeenCalledTimes(1));
    expect(mockedApi.updateQMSClause.mock.calls[0][0]).toBe(BARE_CLAUSE_ID);
    expect(mockedApi.updateQMSClause.mock.calls[0][1]).toStrictEqual({
      compliance_notes: 'Gauge list kept in calibration module.',
    });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it("a refusal stays in the dialog with the server's reason, keeps the text, and reloads nothing", async () => {
    const detail = 'Only Admin, Manager or Quality can edit clause notes.';
    mockedApi.updateQMSClause.mockRejectedValue({ response: { status: 403, data: { detail } } });

    const block = await openClause('8.5.1');
    fireEvent.click(within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' }));
    fireEvent.change(notesBox(), { target: { value: 'Refused edit' } });
    fireEvent.click(within(notesDialog('8.5.1')).getByRole('button', { name: 'Save notes' }));

    const dialog = notesDialog('8.5.1');
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(detail);
    expect(notesBox().value).toBe('Refused edit');
    expect(mockedApi.getQMSStandard).toHaveBeenCalledTimes(1);
    // The page's own error banner is not where this lands.
    expect(screen.getAllByText(detail)).toHaveLength(1);
    // The stored notes on the page are untouched (non-optimistic).
    expect(
      within(screen.getByTestId('clause-notes')).getByText(
        (_content, el) => el?.tagName === 'P' && el.textContent === STORED_NOTES
      )
    ).toBeInTheDocument();
  });

  it('discarding an edit and reopening shows the stored notes again, not the abandoned text', async () => {
    const block = await openClause('8.5.1');
    const edit = within(block).getByRole('button', { name: 'Edit notes for clause 8.5.1' });
    fireEvent.click(edit);
    fireEvent.change(notesBox(), { target: { value: 'Abandoned draft' } });
    fireEvent.click(within(notesDialog('8.5.1')).getByRole('button', { name: 'Cancel' }));

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(edit);
    expect(notesBox().value).toBe(STORED_NOTES);
    expect(mockedApi.updateQMSClause).not.toHaveBeenCalled();
  });
});
