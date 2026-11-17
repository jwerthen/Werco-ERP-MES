/**
 * QMSStandards — a stored evidence link shows its `description` in the clause's
 * linked-evidence list.
 *
 * `POST /qms-standards/{id}/auto-link` stores one field of each live evidence row's
 * findings: `description`. The auto-evidence service puts its disclosures there (a
 * paused audit hash chain, missing audit triggers), APPENDED to the end of the text.
 * Until this list rendered `description`, those disclosures reached API and MCP
 * readers but never an auditor reading the page. Owner principle: a false claim in an
 * auditor-facing view is worse than "not assessed", so:
 *
 *  - an auto-linked description is shown in full, never clamped (a clamp could show
 *    the claim and hide the qualification after it);
 *  - a manual description is shown too. It is clamped to three lines only while its
 *    measured height needs more, and a clamped description always has its "Show more"
 *    control: no text is ever hidden without a way to reveal it. The whole text stays
 *    in the DOM either way;
 *  - a link with no description (or only whitespace) renders no empty line.
 *
 * jsdom does no layout: `scrollHeight` is 0 and the computed line height is empty, so
 * the component falls back to a 16px line (three lines = 48px). These tests stub
 * `scrollHeight` to simulate a layout in which the texts in `tallTexts` need more than
 * three lines and every other text fits.
 */

import React from 'react';
import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QMSStandards from './QMSStandards';
import { ToastProvider } from '../components/ui';
import api from '../services/api';
import type {
  QMSAuditReadinessSummary,
  QMSEvidenceResponse,
  QMSStandardListResponse,
  QMSStandardResponse,
} from '../types/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQMSStandards: jest.fn(),
    getQMSAuditReadiness: jest.fn(),
    getQMSStandard: jest.fn(),
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

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
  total_evidence_links: 5,
  verified_evidence: 0,
  unverified_evidence: 5,
  clauses_needing_review: 0,
};

// The shape backend app/services/auto_evidence_service.py (_query_audit_log) composes when
// the chain is back on but some entries were written while it was paused, and the
// UPDATE/DELETE triggers are missing. Both disclosures sit at the END of the text.
const AUDIT_DISCLOSURE_TAIL =
  'SHA-256 integrity chain enabled, but 12 entries were written while it was paused ' +
  '(3 in last 12 months); database UPDATE/DELETE triggers missing or disabled';
const AUDIT_DESCRIPTION = `1234 audit log entries (210 in last 12 months); ${AUDIT_DISCLOSURE_TAIL}`;

const MANUAL_SHORT_DESCRIPTION = 'Quality manual section 4 names the audit log as the record of change.';

const MANUAL_LONG_DESCRIPTION =
  'Internal audit procedure QP-9.2 defines the annual audit program, auditor independence, and ' +
  'how findings are raised as corrective actions. The procedure is reviewed at every management ' +
  'review and was last revised after the surveillance audit. Its final paragraph explains how the ' +
  'audit schedule is risk-weighted toward processes with open nonconformances.';

/** Texts that need more than three lines in the simulated layout. Reset before each test. */
let tallTexts = new Set<string>();

const TALL_HEIGHT = 96; // six 16px lines
const SHORT_HEIGHT = 32; // two 16px lines

const realScrollHeight = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollHeight');

beforeAll(() => {
  Object.defineProperty(Element.prototype, 'scrollHeight', {
    configurable: true,
    get(this: Element) {
      return tallTexts.has(this.textContent ?? '') ? TALL_HEIGHT : SHORT_HEIGHT;
    },
  });
});

afterAll(() => {
  if (realScrollHeight) {
    Object.defineProperty(Element.prototype, 'scrollHeight', realScrollHeight);
  } else {
    delete (Element.prototype as { scrollHeight?: number }).scrollHeight;
  }
});

function evidence(
  overrides: Partial<QMSEvidenceResponse> & Pick<QMSEvidenceResponse, 'id' | 'title'>
): QMSEvidenceResponse {
  return {
    clause_id: 71,
    evidence_type: 'module',
    is_verified: false,
    is_auto_linked: false,
    created_at: '2026-11-01T12:00:00Z',
    ...overrides,
  };
}

const AUTO_LINKED = evidence({
  id: 1,
  title: 'Audit Trail (Audit Log & Hash-Chain Status)',
  description: AUDIT_DESCRIPTION,
  module_reference: '/audit',
  is_auto_linked: true,
  live_count: 1234,
  last_refreshed: '2026-11-16T15:00:00Z',
});

const MANUAL_SHORT = evidence({
  id: 2,
  evidence_type: 'document',
  title: 'Quality Manual',
  description: MANUAL_SHORT_DESCRIPTION,
});

const MANUAL_LONG = evidence({
  id: 3,
  evidence_type: 'procedure',
  title: 'Internal Audit Procedure',
  description: MANUAL_LONG_DESCRIPTION,
});

const NO_DESCRIPTION = evidence({ id: 4, evidence_type: 'other', title: 'Training Sign-off Sheet' });

const BLANK_DESCRIPTION = evidence({
  id: 5,
  evidence_type: 'other',
  title: 'Management Review Minutes',
  description: '   \n  ',
});

const STANDARD_DETAIL: QMSStandardResponse = {
  id: 7,
  name: 'AS9100D',
  version: 'Rev D',
  is_active: true,
  created_at: '2026-11-01T12:00:00Z',
  clauses: [
    {
      id: 71,
      standard_id: 7,
      clause_number: '7.5.3',
      title: 'Control of documented information',
      sort_order: 1,
      compliance_status: 'partial',
      evidence_links: [AUTO_LINKED, MANUAL_SHORT, MANUAL_LONG, NO_DESCRIPTION, BLANK_DESCRIPTION],
      sub_clauses: [],
      created_at: '2026-11-01T12:00:00Z',
    },
  ],
};

function withLinks(links: QMSEvidenceResponse[]): QMSStandardResponse {
  return { ...STANDARD_DETAIL, clauses: [{ ...STANDARD_DETAIL.clauses[0], evidence_links: links }] };
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

async function openClause() {
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: /AS9100D/ }));
  fireEvent.click(await screen.findByRole('button', { name: /7\.5\.3/ }));
  await screen.findByText('Evidence Links');
}

function linkedRow(title: string): HTMLElement {
  const row = screen.getByText(title).closest('[data-testid="linked-evidence-row"]');
  if (!(row instanceof HTMLElement)) throw new Error(`no linked-evidence row for ${title}`);
  return row;
}

function descriptionIn(row: HTMLElement): HTMLElement {
  return within(row).getByTestId('linked-evidence-description');
}

function toggleIn(row: HTMLElement): HTMLElement | null {
  return within(row).queryByRole('button', { name: /Show (more|less)/ });
}

/** The component re-measures on window resize when ResizeObserver is unavailable (as in jsdom). */
function resizeWindow() {
  act(() => {
    window.dispatchEvent(new Event('resize'));
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  tallTexts = new Set([MANUAL_LONG_DESCRIPTION]);
  mockedApi.getQMSStandards.mockResolvedValue(STANDARD_LIST);
  mockedApi.getQMSAuditReadiness.mockResolvedValue(AUDIT_SUMMARY);
  mockedApi.getQMSStandard.mockResolvedValue(STANDARD_DETAIL);
});

describe('linked-evidence list: stored description', () => {
  it("shows an auto-linked row's description in full, disclosures included, never clamped", async () => {
    // Even when it would not fit in three lines.
    tallTexts.add(AUDIT_DESCRIPTION);
    await openClause();

    const row = linkedRow(AUTO_LINKED.title);
    const description = descriptionIn(row);
    expect(description).toHaveTextContent(AUDIT_DESCRIPTION);
    expect(description).toHaveTextContent(/triggers missing or disabled$/);
    expect(description.className).not.toContain('line-clamp');
    expect(toggleIn(row)).not.toBeInTheDocument();

    // The count and refresh date still sit beside the title; the description is its own line.
    expect(row).toHaveTextContent('(1234 records | Refreshed');
  });

  it('shows a short manual description in full with no toggle', async () => {
    await openClause();

    const row = linkedRow(MANUAL_SHORT.title);
    const description = descriptionIn(row);
    expect(description).toHaveTextContent(MANUAL_SHORT_DESCRIPTION);
    expect(description.className).not.toContain('line-clamp');
    expect(toggleIn(row)).not.toBeInTheDocument();
  });

  it('clamps a manual description too tall for three lines behind Show more, whole text still in the DOM', async () => {
    await openClause();

    const row = linkedRow(MANUAL_LONG.title);
    const description = descriptionIn(row);
    expect(description).toHaveTextContent(MANUAL_LONG_DESCRIPTION);
    expect(description.className).toContain('line-clamp-3');

    const toggle = within(row).getByRole('button', { name: 'Show more' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle).toHaveAttribute('aria-controls', description.id);

    fireEvent.click(toggle);
    expect(description.className).not.toContain('line-clamp');
    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(toggle).toHaveTextContent('Show less');

    fireEvent.click(toggle);
    expect(description.className).toContain('line-clamp-3');
    expect(toggle).toHaveTextContent('Show more');
  });

  it('leaves a long manual description unclamped, with no toggle, when it fits in three lines on screen', async () => {
    // Character count is not the test: on a wide screen 355 characters fit in three lines, and a
    // Show more that reveals nothing would only flip aria-expanded.
    tallTexts = new Set();
    await openClause();

    const row = linkedRow(MANUAL_LONG.title);
    expect(MANUAL_LONG_DESCRIPTION.length).toBeGreaterThan(240);
    expect(descriptionIn(row).className).not.toContain('line-clamp');
    expect(toggleIn(row)).not.toBeInTheDocument();
  });

  it('re-measures on resize: the control appears when the text stops fitting and goes when it fits again', async () => {
    tallTexts = new Set();
    await openClause();
    const row = linkedRow(MANUAL_LONG.title);
    expect(toggleIn(row)).not.toBeInTheDocument();

    // The window narrows: the same text now needs more than three lines.
    tallTexts.add(MANUAL_LONG_DESCRIPTION);
    resizeWindow();
    expect(descriptionIn(row).className).toContain('line-clamp-3');
    expect(within(row).getByRole('button', { name: 'Show more' })).toBeInTheDocument();

    // And widens again.
    tallTexts.delete(MANUAL_LONG_DESCRIPTION);
    resizeWindow();
    expect(descriptionIn(row).className).not.toContain('line-clamp');
    expect(toggleIn(row)).not.toBeInTheDocument();
  });

  it('never clamps an auto-linked description, however long or however many lines', async () => {
    const longAutoText = `${'x'.repeat(300)}\nsecond line\nthird line\nfourth line; disclosure at the end`;
    const longAuto = evidence({
      id: 7,
      title: 'Supplier Management & Evaluation',
      description: longAutoText,
      is_auto_linked: true,
      live_count: 3,
    });
    tallTexts.add(longAutoText);
    mockedApi.getQMSStandard.mockResolvedValue(withLinks([longAuto]));
    await openClause();

    const row = linkedRow(longAuto.title);
    const description = descriptionIn(row);
    expect(description).toHaveTextContent(/fourth line; disclosure at the end$/);
    expect(description.className).not.toContain('line-clamp');
    expect(toggleIn(row)).not.toBeInTheDocument();

    // A later resize does not clamp it either.
    resizeWindow();
    expect(description.className).not.toContain('line-clamp');
  });

  it('never clamps a description without its control, in any row', async () => {
    const tallManyLines = 'Line one\nLine two\nLine three\nLine four';
    const manyLines = evidence({
      id: 6,
      evidence_type: 'document',
      title: 'Record Retention Schedule',
      description: tallManyLines,
    });
    tallTexts.add(tallManyLines);
    tallTexts.add(AUDIT_DESCRIPTION);
    mockedApi.getQMSStandard.mockResolvedValue(withLinks([...STANDARD_DETAIL.clauses[0].evidence_links, manyLines]));
    await openClause();

    const rows = screen.getAllByTestId('linked-evidence-row');
    expect(rows).toHaveLength(6);
    for (const row of rows) {
      const description = within(row).queryByTestId('linked-evidence-description');
      const clamped = description?.className.includes('line-clamp') ?? false;
      expect({ clamped, hasControl: toggleIn(row) !== null }).toEqual({ clamped, hasControl: clamped });
    }
    // And the two tall manual rows are the clamped ones.
    expect(descriptionIn(linkedRow(MANUAL_LONG.title)).className).toContain('line-clamp-3');
    expect(descriptionIn(linkedRow(manyLines.title)).className).toContain('line-clamp-3');
  });

  it('gives each clamped row its own toggle: aria-controls resolves to that row, state is independent', async () => {
    const secondLongText = `${MANUAL_LONG_DESCRIPTION} A second procedure with its own long text.`;
    const secondLong = evidence({
      id: 11,
      evidence_type: 'procedure',
      title: 'Corrective Action Procedure',
      description: secondLongText,
    });
    tallTexts.add(secondLongText);
    mockedApi.getQMSStandard.mockResolvedValue(withLinks([MANUAL_LONG, secondLong]));
    await openClause();

    const firstRow = linkedRow(MANUAL_LONG.title);
    const secondRow = linkedRow(secondLong.title);
    const firstDescription = descriptionIn(firstRow);
    const secondDescription = descriptionIn(secondRow);
    const firstToggle = within(firstRow).getByRole('button', { name: 'Show more' });
    const secondToggle = within(secondRow).getByRole('button', { name: 'Show more' });

    // The id each toggle names must resolve, document-wide, to its own row's description.
    expect(firstDescription.id).not.toBe('');
    expect(firstDescription.id).not.toBe(secondDescription.id);
    expect(document.getElementById(firstToggle.getAttribute('aria-controls') ?? '')).toBe(firstDescription);
    expect(document.getElementById(secondToggle.getAttribute('aria-controls') ?? '')).toBe(secondDescription);

    fireEvent.click(secondToggle);
    expect(secondDescription.className).not.toContain('line-clamp');
    expect(secondToggle).toHaveAttribute('aria-expanded', 'true');
    expect(firstDescription.className).toContain('line-clamp-3');
    expect(firstToggle).toHaveAttribute('aria-expanded', 'false');
  });

  it('renders no description line for a link with none, or only whitespace', async () => {
    await openClause();

    expect(
      within(linkedRow(NO_DESCRIPTION.title)).queryByTestId('linked-evidence-description')
    ).not.toBeInTheDocument();
    expect(
      within(linkedRow(BLANK_DESCRIPTION.title)).queryByTestId('linked-evidence-description')
    ).not.toBeInTheDocument();
  });
});
