/**
 * QMSStandards — auto-link's evidence retirements are shown, never silent.
 *
 * Owner principle: a false claim in an auditor-facing view is worse than "not
 * assessed". `POST /qms-standards/{id}/auto-link` (PR #340) soft-deletes its own
 * stale auto-linked evidence links — those whose rule no longer matches their
 * clause, such as the change-log link on 9.2 / 9.3 — and keeps the ones a person
 * verified or annotated. It reports both:
 *
 *  - `total_evidence_retired`: the result dialog must show the count, so a link
 *    does not vanish from a clause with no notice;
 *  - `evidence_kept_reviewed`: the dialog must list each kept link's clause and
 *    reason and ask someone to check it, and a `warning` toast must say so: the
 *    run succeeded but left work for a person.
 *
 * The SPA and the API deploy separately, so an API from before PR #340 omits
 * both fields. Absent is not zero: the dialog must not claim "0 retired" then.
 */

import React from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import QMSStandards from './QMSStandards';
import { ToastProvider } from '../components/ui';
import api from '../services/api';
import type {
  AutoLinkKeptEvidence,
  AutoLinkSummary,
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
  },
}));

const mockedApi = api as jest.Mocked<typeof api>;

const STANDARD_LIST: QMSStandardListResponse[] = [
  {
    id: 7,
    name: 'AS9100D',
    version: 'Rev D',
    is_active: true,
    total_clauses: 2,
    compliant_clauses: 0,
    partial_clauses: 0,
    non_compliant_clauses: 0,
    not_assessed_clauses: 2,
    created_at: '2026-11-01T12:00:00Z',
  },
];

const AUDIT_SUMMARY: QMSAuditReadinessSummary = {
  total_standards: 1,
  total_clauses: 2,
  compliant: 0,
  partial: 0,
  non_compliant: 0,
  not_assessed: 2,
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
      id: 92,
      standard_id: 7,
      clause_number: '9.2',
      title: 'Internal audit',
      sort_order: 1,
      compliance_status: 'not_assessed',
      evidence_links: [],
      sub_clauses: [],
      created_at: '2026-11-01T12:00:00Z',
    },
  ],
};

/** An API response from before PR #340: neither retirement field is present. */
function legacyAutoLinkSummary(overrides: Partial<AutoLinkSummary> = {}): AutoLinkSummary {
  return {
    standard_id: 7,
    standard_name: 'AS9100D',
    total_clauses: 2,
    clauses_with_evidence: 1,
    clauses_without_evidence: 1,
    total_evidence_created: 0,
    total_evidence_updated: 3,
    total_evidence_failed: 0,
    compliance_summary: { not_assessed: 2 },
    ...overrides,
  };
}

function autoLinkSummary(overrides: Partial<AutoLinkSummary> = {}): AutoLinkSummary {
  return legacyAutoLinkSummary({ total_evidence_retired: 0, evidence_kept_reviewed: [], ...overrides });
}

const KEPT_VERIFIED: AutoLinkKeptEvidence = {
  evidence_id: 311,
  clause_id: 92,
  clause_number: '9.2',
  rule_id: 'audit',
  reason: 'verified',
};

const KEPT_ANNOTATED: AutoLinkKeptEvidence = {
  evidence_id: 312,
  clause_id: 93,
  clause_number: '9.3',
  rule_id: 'audit',
  reason: 'annotated',
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

async function runAutoLink(result: AutoLinkSummary) {
  mockedApi.autoLinkStandard.mockResolvedValue(result);
  renderPage();
  fireEvent.click(await screen.findByRole('button', { name: /AS9100D/ }));
  fireEvent.click(await screen.findByRole('button', { name: /Auto-Link Evidence/ }));
  return screen.findByRole('dialog');
}

beforeEach(() => {
  jest.clearAllMocks();
  mockedApi.getQMSStandards.mockResolvedValue(STANDARD_LIST);
  mockedApi.getQMSAuditReadiness.mockResolvedValue(AUDIT_SUMMARY);
  mockedApi.getQMSStandard.mockResolvedValue(STANDARD_DETAIL);
});

describe('auto-link result: retired stale links', () => {
  it('shows the retired count, says the removals are audited, and that statuses did not change', async () => {
    const dialog = await runAutoLink(autoLinkSummary({ total_evidence_retired: 2 }));

    const tile = within(dialog).getByTestId('auto-link-retired');
    expect(within(tile).getByText('2')).toBeInTheDocument();
    expect(within(tile).getByText('Stale Links Retired')).toBeInTheDocument();
    expect(tile).toHaveTextContent('whose rule no longer matches their clause were removed');
    expect(tile).toHaveTextContent('each removal is recorded in the audit log');
    expect(tile).toHaveTextContent('Clause compliance statuses were not changed');

    // Retiring is the run working as designed, recorded in the audit log: no toast.
    expect(within(dialog).getByText('Auto-Link Evidence Complete')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows a zero count without the explanation when nothing was retired', async () => {
    const dialog = await runAutoLink(autoLinkSummary({ total_evidence_retired: 0 }));

    const tile = within(dialog).getByTestId('auto-link-retired');
    expect(within(tile).getByText('0')).toBeInTheDocument();
    expect(tile).not.toHaveTextContent('audit log');
  });

  it('claims nothing about retirement when the API did not report it', async () => {
    const dialog = await runAutoLink(legacyAutoLinkSummary());

    expect(within(dialog).getByText('Auto-Link Evidence Complete')).toBeInTheDocument();
    expect(within(dialog).queryByTestId('auto-link-retired')).not.toBeInTheDocument();
    expect(within(dialog).queryByText('Stale Links Retired')).not.toBeInTheDocument();
    expect(within(dialog).queryByTestId('auto-link-kept-reviewed')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('auto-link result: stale links kept because a person reviewed them', () => {
  it('lists each kept link with its clause and reason and raises a warning toast', async () => {
    const dialog = await runAutoLink(
      autoLinkSummary({ total_evidence_retired: 1, evidence_kept_reviewed: [KEPT_VERIFIED, KEPT_ANNOTATED] })
    );

    const panel = within(dialog).getByTestId('auto-link-kept-reviewed');
    expect(within(panel).getByText('Kept for Review')).toBeInTheDocument();
    expect(within(panel).getByText('2')).toBeInTheDocument();
    expect(panel).toHaveTextContent(
      '2 auto-linked evidence links no longer match their clauses but were kept because a person ' +
        'verified or annotated them. Check whether each still belongs.'
    );
    expect(panel).toHaveTextContent('If a link no longer applies, remove it from its clause.');

    const items = within(within(panel).getByRole('list', { name: 'Evidence links kept for review' })).getAllByRole(
      'listitem'
    );
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent('Clause 9.2');
    expect(items[0]).toHaveTextContent('Verified');
    expect(items[0]).toHaveTextContent('rule audit');
    expect(items[1]).toHaveTextContent('Clause 9.3');
    expect(items[1]).toHaveTextContent('Document or notes attached');

    // Every rule was computed, so the run is complete — but it left work for a person.
    expect(within(dialog).getByText('Auto-Link Evidence Complete')).toBeInTheDocument();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(
      'Auto-link finished; review needed. 2 auto-linked evidence links no longer match their clauses ' +
        'but were kept because a person verified or annotated them. Check whether each still belongs.'
    );
    expect(alert.className).toContain('bg-amber-600');
  });

  it('uses the singular for one kept link', async () => {
    const dialog = await runAutoLink(autoLinkSummary({ evidence_kept_reviewed: [KEPT_VERIFIED] }));

    expect(within(dialog).getByTestId('auto-link-kept-reviewed')).toHaveTextContent(
      '1 auto-linked evidence link no longer matches its clause but was kept because a person ' +
        'verified or annotated it. Check whether it still belongs.'
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('Check whether it still belongs.');
  });

  it('shows an unrecognized reason as sent, and omits a missing rule', async () => {
    const dialog = await runAutoLink(
      autoLinkSummary({ evidence_kept_reviewed: [{ ...KEPT_VERIFIED, reason: 'signed_off', rule_id: null }] })
    );

    const item = within(within(dialog).getByTestId('auto-link-kept-reviewed')).getByRole('listitem');
    expect(item).toHaveTextContent('signed_off');
    expect(item).not.toHaveTextContent('rule');
  });

  it('folds kept links and uncomputed rules into one warning toast', async () => {
    const dialog = await runAutoLink(
      autoLinkSummary({ total_evidence_failed: 1, evidence_kept_reviewed: [KEPT_VERIFIED] })
    );

    expect(within(dialog).getByText('Auto-Link Evidence Finished With Gaps')).toBeInTheDocument();
    expect(within(dialog).getByTestId('auto-link-not-computed')).toBeInTheDocument();
    expect(within(dialog).getByTestId('auto-link-kept-reviewed')).toBeInTheDocument();

    const alerts = await screen.findAllByRole('alert');
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent(
      'Auto-link finished with gaps. 1 evidence rule could not be computed this run. Its evidence link ' +
        'was left as last refreshed, not updated. 1 auto-linked evidence link no longer matches its clause ' +
        'but was kept because a person verified or annotated it. Check whether it still belongs.'
    );
  });
});

describe('auto-link result dialog', () => {
  it('is the shared modal: labelled by its heading and closed by Escape', async () => {
    const dialog = await runAutoLink(autoLinkSummary());

    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAccessibleName('Auto-Link Evidence Complete');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('closes from its close button', async () => {
    const dialog = await runAutoLink(autoLinkSummary());

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});
