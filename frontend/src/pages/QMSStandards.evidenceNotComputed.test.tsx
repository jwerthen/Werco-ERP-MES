/**
 * QMSStandards — evidence the server could not compute is never shown as evidence.
 *
 * Owner principle: a false "compliant" in an auditor-facing view is worse than
 * "not assessed". The backend (PR #336) reports an evidence rule whose query
 * raised instead of dropping it:
 *
 *  - `POST /qms-standards/{id}/auto-link` saves nothing for it (an existing
 *    evidence link keeps the counts and `last_refreshed` of the last run that
 *    computed it) and counts it in `total_evidence_failed`. The result card
 *    must say so, and so must a `warning` toast: the run succeeded but did not
 *    do everything asked. A run with no failures says nothing extra.
 *  - `GET /qms-standards/clauses/{id}/auto-evidence` lists it as a placeholder
 *    row with `health_status` `not_computed` and placeholder zero counts. A
 *    genuinely empty module is `no_data` with the same zeros. The placeholder
 *    must not read "0 records"; the empty module still must.
 */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QMSStandards from './QMSStandards';
import { ToastProvider } from '../components/ui';
import api from '../services/api';
import type {
  AutoEvidenceResult,
  AutoLinkSummary,
  ClauseAutoEvidenceResponse,
  QMSAuditReadinessSummary,
  QMSStandardListResponse,
  QMSStandardResponse,
} from '../types/api';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getQMSStandards: jest.fn(),
    getQMSAuditReadiness: jest.fn(),
    getQMSStandard: jest.fn(),
    autoLinkStandard: jest.fn(),
    getClauseAutoEvidence: jest.fn(),
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
    partial_clauses: 0,
    non_compliant_clauses: 0,
    not_assessed_clauses: 1,
    created_at: '2026-11-01T12:00:00Z',
  },
];

const AUDIT_SUMMARY: QMSAuditReadinessSummary = {
  total_standards: 1,
  total_clauses: 1,
  compliant: 0,
  partial: 0,
  non_compliant: 0,
  not_assessed: 1,
  not_applicable: 0,
  compliance_percentage: 0,
  total_evidence_links: 0,
  verified_evidence: 0,
  unverified_evidence: 0,
  clauses_needing_review: 0,
};

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
      clause_number: '7.1.5',
      title: 'Monitoring and measuring resources',
      sort_order: 1,
      compliance_status: 'not_assessed',
      evidence_links: [],
      sub_clauses: [],
      created_at: '2026-11-01T12:00:00Z',
    },
  ],
};

function autoLinkSummary(overrides: Partial<AutoLinkSummary> = {}): AutoLinkSummary {
  return {
    standard_id: 7,
    standard_name: 'AS9100D',
    total_clauses: 1,
    clauses_with_evidence: 1,
    clauses_without_evidence: 0,
    total_evidence_created: 2,
    total_evidence_updated: 3,
    total_evidence_failed: 0,
    compliance_summary: { not_assessed: 1 },
    ...overrides,
  };
}

// Verbatim from backend app/services/auto_evidence_service.py (_evaluation_failed_result).
const NOT_COMPUTED_DESCRIPTION = 'Evidence could not be computed from live records';
const NOT_COMPUTED_DETAIL =
  'Evidence could not be computed: the query for this module failed. ' +
  'Nothing is known about it from this run, so it is not assessed.';

const NOT_COMPUTED_ROW: AutoEvidenceResult = {
  evidence_type: 'module',
  title: 'Preventive Maintenance & Infrastructure',
  description: NOT_COMPUTED_DESCRIPTION,
  module_reference: '/maintenance',
  total_count: 0,
  recent_count: 0,
  health_status: 'not_computed',
  health_detail: NOT_COMPUTED_DETAIL,
  examples: [],
  suggested_compliance: 'not_assessed',
};

const EMPTY_MODULE_ROW: AutoEvidenceResult = {
  evidence_type: 'calibration',
  title: 'Calibration & Measurement Equipment',
  description: 'No calibration records yet',
  module_reference: '/calibration',
  total_count: 0,
  recent_count: 0,
  health_status: 'no_data',
  health_detail: 'No equipment registered',
  examples: [],
  suggested_compliance: 'not_assessed',
};

const COMPUTED_ROW: AutoEvidenceResult = {
  evidence_type: 'ncr',
  title: 'Non-Conformance Reports (NCR)',
  description: '12 NCRs on file',
  module_reference: '/quality/ncr',
  total_count: 12,
  recent_count: 4,
  health_status: 'healthy',
  health_detail: 'All NCRs dispositioned',
  examples: [],
  suggested_compliance: 'compliant',
};

function renderPage() {
  return render(
    <MemoryRouter>
      <ToastProvider>
        <QMSStandards />
      </ToastProvider>
    </MemoryRouter>
  );
}

async function openStandardDetail() {
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: /AS9100D/ }));
  await screen.findByRole('button', { name: /Auto-Link Evidence/ });
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getQMSStandards.mockResolvedValue(STANDARD_LIST);
  mockedApi.getQMSAuditReadiness.mockResolvedValue(AUDIT_SUMMARY);
  mockedApi.getQMSStandard.mockResolvedValue(STANDARD_DETAIL);
});

describe('auto-link result: evidence rules that could not be computed', () => {
  it('shows the failed count on the card and raises a warning toast', async () => {
    mockedApi.autoLinkStandard.mockResolvedValue(autoLinkSummary({ total_evidence_failed: 2 }));
    await openStandardDetail();

    fireEvent.click(screen.getByRole('button', { name: /Auto-Link Evidence/ }));

    const tile = await screen.findByTestId('auto-link-not-computed');
    expect(within(tile).getByText('2')).toBeInTheDocument();
    expect(within(tile).getByText('Evidence Not Computed')).toBeInTheDocument();
    expect(tile).toHaveTextContent(
      '2 evidence rules could not be computed this run. Their evidence links were left as last refreshed, not updated.'
    );
    expect(tile).toHaveTextContent('A clause with an uncomputed rule is not counted as compliant.');
    expect(screen.getByText('Auto-Link Evidence Finished With Gaps')).toBeInTheDocument();
    expect(screen.queryByText('Auto-Link Evidence Complete')).not.toBeInTheDocument();

    // The toast is a warning (role="alert", amber), not a success that hides the gap.
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Auto-link finished with gaps. 2 evidence rules could not be computed this run. ' +
        'Their evidence links were left as last refreshed, not updated.'
    );
    expect(alert.className).toContain('bg-amber-600');
  });

  it('uses the singular for one failed rule', async () => {
    mockedApi.autoLinkStandard.mockResolvedValue(autoLinkSummary({ total_evidence_failed: 1 }));
    await openStandardDetail();

    fireEvent.click(screen.getByRole('button', { name: /Auto-Link Evidence/ }));

    const tile = await screen.findByTestId('auto-link-not-computed');
    expect(tile).toHaveTextContent(
      '1 evidence rule could not be computed this run. Its evidence link was left as last refreshed, not updated.'
    );
  });

  it('adds nothing when every rule was computed', async () => {
    mockedApi.autoLinkStandard.mockResolvedValue(autoLinkSummary({ total_evidence_failed: 0 }));
    await openStandardDetail();

    fireEvent.click(screen.getByRole('button', { name: /Auto-Link Evidence/ }));

    expect(await screen.findByText('Auto-Link Evidence Complete')).toBeInTheDocument();
    expect(screen.getByText('New Evidence Linked')).toBeInTheDocument();
    expect(screen.queryByTestId('auto-link-not-computed')).not.toBeInTheDocument();
    expect(screen.queryByText('Evidence Not Computed')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('auto-discover preview: a placeholder row is not "0 records"', () => {
  function clauseEvidence(rows: AutoEvidenceResult[]): ClauseAutoEvidenceResponse {
    return {
      clause_id: 71,
      clause_number: '7.1.5',
      discovered_evidence: rows,
      overall_suggested_compliance: 'not_assessed',
    };
  }

  async function discover(rows: AutoEvidenceResult[]) {
    mockedApi.getClauseAutoEvidence.mockResolvedValue(clauseEvidence(rows));
    await openStandardDetail();
    fireEvent.click(screen.getByRole('button', { name: /7\.1\.5/ }));
    fireEvent.click(screen.getByRole('button', { name: /Auto-Discover/ }));
    await screen.findByText(/Discovered Evidence \(Live Preview\)/);
  }

  function evidenceRow(title: string): HTMLElement {
    const row = screen.getByText(title).closest('[data-testid="discovered-evidence-row"]');
    if (!(row instanceof HTMLElement)) throw new Error(`no discovered-evidence row for ${title}`);
    return row;
  }

  it('labels a failed rule "Not computed" with its detail, and no record count', async () => {
    await discover([COMPUTED_ROW, NOT_COMPUTED_ROW, EMPTY_MODULE_ROW]);

    const failed = evidenceRow(NOT_COMPUTED_ROW.title);
    expect(within(failed).queryByText(/\d+ records/)).not.toBeInTheDocument();
    expect(within(failed).getByText('Not computed')).toBeInTheDocument();
    expect(within(failed).getByText(NOT_COMPUTED_DETAIL)).toBeInTheDocument();
    expect(within(failed).getByTitle('Not computed')).toBeInTheDocument();

    // A genuinely empty module keeps its honest zero, and a computed one its count.
    expect(within(evidenceRow(EMPTY_MODULE_ROW.title)).getByText('0 records')).toBeInTheDocument();
    expect(within(evidenceRow(COMPUTED_ROW.title)).getByText('12 records')).toBeInTheDocument();
    expect(within(evidenceRow(EMPTY_MODULE_ROW.title)).queryByText('Not computed')).not.toBeInTheDocument();

    expect(screen.getByText(/Suggested compliance:/)).toHaveTextContent('Suggested compliance: Not Assessed');
  });
});
