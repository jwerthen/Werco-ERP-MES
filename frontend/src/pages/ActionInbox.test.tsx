/**
 * ActionInbox — "Top 3 today" hero (B0.3).
 *
 * Covers: the hero renders the top 3 recommendations by score (in order, with rank
 * chips) while the rest flow into the queue below; the hero disappears when there are
 * no recommendations (empty state); and the snooze action calls the API and removes
 * the card. services/api is mocked at the module boundary.
 */

import React from 'react';
import { render, screen, waitFor, within, fireEvent, act } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import ActionInbox from './ActionInbox';
import api from '../services/api';
import { ToastProvider } from '../components/ui/Toast';
import { AIRecommendation, AIRecommendationApplyAction, AIRecommendationApplyResult } from '../types/aiLearning';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getSetupHealth: jest.fn(),
    getOperationalInbox: jest.fn(),
    updateOperationalInbox: jest.fn(),
    getAIRecommendations: jest.fn(),
    acceptAIRecommendation: jest.fn(),
    applyAIRecommendation: jest.fn(),
    dismissAIRecommendation: jest.fn(),
    sendAIRecommendationFeedback: jest.fn(),
    snoozeAIRecommendation: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;
beforeEach(() => {
  mockApi.getOperationalInbox.mockResolvedValue({
    items: [],
    assignees: [],
    checked_at: '2026-09-07T12:00:00Z',
    truncated_sources: [],
  });
});

const makeRecommendation = (overrides: Partial<AIRecommendation>): AIRecommendation => ({
  id: 1,
  source_module: 'quoting',
  recommendation_type: 'correction_pattern',
  status: 'pending',
  priority: 'medium',
  title: 'Recommendation',
  summary: 'Summary',
  confidence_score: 0.8,
  created_at: '2026-06-09T10:00:00Z',
  updated_at: '2026-06-09T10:00:00Z',
  ...overrides,
});

const emptyHealth = { progress: 100, counts: {}, steps: [], issues: [] };

const renderInbox = () =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <ActionInbox />
      </ToastProvider>
    </MemoryRouter>
  );

describe('ActionInbox Top 3 hero', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockApi.getSetupHealth.mockResolvedValue(emptyHealth);
  });

  it('renders the top 3 recommendations by score in the hero and the rest in the queue', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRecommendation({ id: 11, title: 'Second best', score: 0.54 }),
      makeRecommendation({ id: 12, title: 'Best action', score: 0.6, priority: 'high' }),
      makeRecommendation({ id: 13, title: 'Fourth place', score: 0.2, priority: 'low' }),
      makeRecommendation({ id: 14, title: 'Third best', score: 0.4 }),
    ]);

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    const heroHeadings = within(hero)
      .getAllByRole('heading', { level: 3 })
      .map(heading => heading.textContent);
    expect(heroHeadings).toEqual(['Best action', 'Second best', 'Third best']);
    expect(within(hero).getByText('#1')).toBeInTheDocument();
    expect(within(hero).getByText('#2')).toBeInTheDocument();
    expect(within(hero).getByText('#3')).toBeInTheDocument();
    expect(within(hero).queryByText('Fourth place')).not.toBeInTheDocument();

    // The fourth recommendation flows into the main queue instead.
    expect(screen.getByText('Fourth place')).toBeInTheDocument();
  });

  it('does not render the hero when there are no recommendations (empty state)', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([]);

    renderInbox();

    await waitFor(() => expect(mockApi.getAIRecommendations).toHaveBeenCalled());
    expect(screen.queryByRole('region', { name: 'Top 3 today' })).not.toBeInTheDocument();
    expect(await screen.findByText('No actions in this view')).toBeInTheDocument();
  });

  it('hides the hero and folds all recommendations into the queue when the AI filter is active', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRecommendation({ id: 31, title: 'First suggestion', score: 0.8 }),
      makeRecommendation({ id: 32, title: 'Second suggestion', score: 0.6 }),
    ]);

    renderInbox();

    // Default view: both recommendations live in the hero, not the queue.
    await screen.findByRole('region', { name: 'Top 3 today' });

    fireEvent.click(screen.getByRole('button', { name: 'AI' }));

    // Non-default filter: no hero, and the filtered queue shows the full recommendation set.
    expect(screen.queryByRole('region', { name: 'Top 3 today' })).not.toBeInTheDocument();
    expect(screen.getByText('First suggestion')).toBeInTheDocument();
    expect(screen.getByText('Second suggestion')).toBeInTheDocument();
    expect(screen.queryByText('No actions in this view')).not.toBeInTheDocument();
  });

  it('hides the hero and searches across all recommendations when a query is active', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRecommendation({ id: 41, title: 'Tune lead time defaults', score: 0.9 }),
      makeRecommendation({ id: 42, title: 'Review vendor overrides', score: 0.7 }),
    ]);

    renderInbox();

    await screen.findByRole('region', { name: 'Top 3 today' });

    fireEvent.change(screen.getByPlaceholderText('Search actions...'), { target: { value: 'lead time' } });

    expect(screen.queryByRole('region', { name: 'Top 3 today' })).not.toBeInTheDocument();
    // The matching top-ranked recommendation is searchable in the queue; the other is filtered out.
    expect(screen.getByText('Tune lead time defaults')).toBeInTheDocument();
    expect(screen.queryByText('Review vendor overrides')).not.toBeInTheDocument();
  });

  it('snoozes a hero recommendation through the API and removes it', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([makeRecommendation({ id: 21, title: 'Snooze me', score: 0.9 })]);
    mockApi.snoozeAIRecommendation.mockResolvedValue(
      makeRecommendation({ id: 21, title: 'Snooze me', status: 'snoozed' })
    );

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: /snooze/i }));
    fireEvent.click(within(hero).getByRole('button', { name: '3 days' }));

    await waitFor(() =>
      expect(mockApi.snoozeAIRecommendation).toHaveBeenCalledWith(21, 3, 'Snoozed from Action Inbox')
    );
    await waitFor(() => expect(screen.queryByText('Snooze me')).not.toBeInTheDocument());
  });

  it('optimistically removes a dismissed recommendation before the API resolves', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([makeRecommendation({ id: 51, title: 'Dismiss me', score: 0.9 })]);
    // A pending promise that never resolves during the assertion window: the card
    // must disappear from the optimistic update alone, not from the server response.
    let resolveDismiss: (value: AIRecommendation) => void = () => {};
    mockApi.dismissAIRecommendation.mockReturnValue(
      new Promise<AIRecommendation>(resolve => {
        resolveDismiss = resolve;
      })
    );

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: 'Dismiss' }));

    // Gone immediately, while the API call is still in flight.
    await waitFor(() => expect(screen.queryByText('Dismiss me')).not.toBeInTheDocument());
    expect(mockApi.dismissAIRecommendation).toHaveBeenCalledWith(51, 'Dismissed from Action Inbox');

    // Resolve inside act so the trailing in-flight-state update is flushed cleanly.
    await act(async () => {
      resolveDismiss(makeRecommendation({ id: 51, status: 'dismissed' }));
    });
  });

  it('rolls the recommendation back into the queue when the dismiss API fails', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRecommendation({ id: 61, title: 'Sticky action', score: 0.9 }),
    ]);
    mockApi.dismissAIRecommendation.mockRejectedValue({
      response: { data: { detail: 'Server refused the dismissal.' } },
    });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: 'Dismiss' }));

    await waitFor(() => expect(mockApi.dismissAIRecommendation).toHaveBeenCalled());
    // The optimistic removal is undone — the card is restored to the view.
    await waitFor(() => expect(screen.getByText('Sticky action')).toBeInTheDocument());
  });
});

it('counts featured recommendations in Open Actions consistently across filter views', async () => {
  jest.clearAllMocks();
  localStorage.clear();
  mockApi.getSetupHealth.mockResolvedValue(emptyHealth);
  mockApi.getAIRecommendations.mockResolvedValue(
    [1, 2, 3, 4].map(id => makeRecommendation({ id, title: `Counted ${id}`, score: 1 / id }))
  );
  renderInbox();
  await screen.findByRole('region', { name: 'Top 3 today' });
  expect(
    within(screen.getByRole('button', { name: /Recommendation & setup actions/ })).getByText('4')
  ).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'AI' }));
  expect(
    within(screen.getByRole('button', { name: /Recommendation & setup actions/ })).getByText('4')
  ).toBeInTheDocument();
});

// ---------------------------------------------------------------------------
// Owner ruling 3 (2026-11-05): nothing AI-driven runs without a person clicking
// Accept & apply. The server decides what the button does (apply_action); Accept &
// apply is server-gated and therefore NON-optimistic; record-only Accept / Mark
// handled, Dismiss and Snooze stay optimistic.
// ---------------------------------------------------------------------------

// A toast's text alone does not prove its variant: a failed call surfacing as a green
// success (or a success as a red alert) would pass a text-only assertion. Check both.
const expectToast = async (message: string, variant: 'success' | 'error') => {
  const text = await screen.findByText(message);
  const toast = text.closest('[role="alert"], [role="status"]');
  expect(toast).not.toBeNull();
  expect(toast).toHaveAttribute('role', variant === 'error' ? 'alert' : 'status');
  expect(toast).toHaveClass(variant === 'error' ? 'bg-red-600' : 'bg-green-600');
};

const makeApplyAction = (overrides: Partial<AIRecommendationApplyAction> = {}): AIRecommendationApplyAction => ({
  action_type: 'adjust_work_order_priority',
  summary: 'Raise WO-3001 priority from P5 to P1',
  warnings: [],
  fingerprint: '0123456789abcdef',
  can_apply: true,
  can_mark_handled: true,
  blocked_reason: null,
  blocked_kind: null,
  ...overrides,
});

const makeActionCard = (overrides: Partial<AIRecommendation> = {}): AIRecommendation =>
  makeRecommendation({
    id: 71,
    title: 'Expedite WO-3001',
    summary: 'WO-3001 is at risk of shipping late.',
    score: 0.9,
    target_entity_type: 'work_order',
    target_entity_id: 3001,
    target_href: '/work-orders/3001',
    suggested_action: { type: 'adjust_work_order_priority', work_order_id: 3001, priority: 1 },
    apply_action: makeApplyAction(),
    ...overrides,
  });

describe('ActionInbox AI Accept & apply (ruling 3)', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockApi.getSetupHealth.mockResolvedValue(emptyHealth);
  });

  it('Accept & apply waits for the server and sends the fingerprint', async () => {
    const card = makeActionCard();
    // First read shows the card; the post-apply re-read no longer has it.
    mockApi.getAIRecommendations.mockResolvedValueOnce([card]).mockResolvedValue([]);
    let resolveApply: (value: AIRecommendationApplyResult) => void = () => {};
    mockApi.applyAIRecommendation.mockReturnValue(
      new Promise<AIRecommendationApplyResult>(resolve => {
        resolveApply = resolve;
      })
    );

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    expect(within(hero).getByText('Accept & apply will: Raise WO-3001 priority from P5 to P1')).toBeInTheDocument();
    fireEvent.click(within(hero).getByRole('button', { name: /accept & apply/i }));

    expect(mockApi.applyAIRecommendation).toHaveBeenCalledWith(
      71,
      '0123456789abcdef',
      'Accepted & applied from Action Inbox'
    );
    expect(mockApi.acceptAIRecommendation).not.toHaveBeenCalled();
    // Non-optimistic: the card is still there, disabled, while the server decides.
    expect(within(hero).getByText('Expedite WO-3001')).toBeInTheDocument();
    expect(within(hero).getByRole('button', { name: /applying/i })).toBeDisabled();
    expect(within(hero).getByRole('button', { name: 'Dismiss' })).toBeDisabled();

    await act(async () => {
      resolveApply({
        recommendation: { ...card, status: 'accepted', apply_action: null },
        applied: true,
        apply_result: { action_type: 'adjust_work_order_priority', summary: 'Raise WO-3001 priority from P5 to P1' },
        apply_error: null,
      });
    });

    await waitFor(() => expect(screen.queryByText('Expedite WO-3001')).not.toBeInTheDocument());
    await expectToast('Applied: Raise WO-3001 priority from P5 to P1', 'success');
  });

  it('a refused apply keeps the card, toasts the server detail verbatim, and refetches on 409', async () => {
    const detail =
      'This action changed since you loaded it. It would now: Raise WO-3001 priority from P3 to P1. Review it and click Accept & apply again.';
    const refreshed = makeActionCard({
      apply_action: makeApplyAction({
        summary: 'Raise WO-3001 priority from P3 to P1',
        fingerprint: 'fedcba9876543210',
      }),
    });
    mockApi.getAIRecommendations.mockResolvedValueOnce([makeActionCard()]).mockResolvedValue([refreshed]);
    mockApi.applyAIRecommendation.mockRejectedValue({ response: { status: 409, data: { detail } } });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: /accept & apply/i }));

    await expectToast(detail, 'error');
    expect(screen.getByText('Expedite WO-3001')).toBeInTheDocument();
    // The 409 re-reads the pending AI list (not a silent refetch: the toast above says why).
    await waitFor(() => expect(mockApi.getAIRecommendations).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('Accept & apply will: Raise WO-3001 priority from P3 to P1')).toBeInTheDocument();
    expect(mockApi.acceptAIRecommendation).not.toHaveBeenCalled();
  });

  it('a 403 refusal keeps the card and toasts verbatim without refetching', async () => {
    const detail =
      'AI actions are off for this company (AI egress is off), so nothing will be applied. An Admin can turn AI egress on in Admin Settings > AI Privacy, or you can act on the record directly.';
    mockApi.getAIRecommendations.mockResolvedValue([makeActionCard()]);
    mockApi.applyAIRecommendation.mockRejectedValue({ response: { status: 403, data: { detail } } });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: /accept & apply/i }));

    await expectToast(detail, 'error');
    expect(screen.getByText('Expedite WO-3001')).toBeInTheDocument();
    expect(mockApi.getAIRecommendations).toHaveBeenCalledTimes(1);
  });

  it("mark-handled card calls acceptAIRecommendation, never applyAIRecommendation, and shows the Won't apply reason", async () => {
    const reason = 'WO-3001 is already P1 (the suggestion was P1); nothing to apply';
    mockApi.getAIRecommendations.mockResolvedValue([
      makeActionCard({
        apply_action: makeApplyAction({
          summary: 'Raise WO-3001 priority from P1 to P1',
          can_apply: false,
          can_mark_handled: true,
          blocked_kind: 'target',
          blocked_reason: reason,
        }),
      }),
    ]);
    mockApi.acceptAIRecommendation.mockResolvedValue({
      recommendation: makeActionCard({ status: 'accepted' }),
      applied: false,
      apply_result: null,
      apply_error: null,
    });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    expect(within(hero).queryByRole('button', { name: /accept & apply/i })).not.toBeInTheDocument();
    expect(
      within(hero).getByText(`Won't apply: ${reason}. Mark handled records that you dealt with it; nothing is changed.`)
    ).toBeInTheDocument();

    fireEvent.click(within(hero).getByRole('button', { name: 'Mark handled' }));

    await waitFor(() =>
      expect(mockApi.acceptAIRecommendation).toHaveBeenCalledWith(71, 'Marked handled from Action Inbox')
    );
    expect(mockApi.applyAIRecommendation).not.toHaveBeenCalled();
    // Record-only accept stays on the optimistic path.
    await waitFor(() => expect(screen.queryByText('Expedite WO-3001')).not.toBeInTheDocument());
  });

  it('role-blocked card shows no accept button but keeps Dismiss/Snooze and shows the reason', async () => {
    const reason =
      'Your role (operator) cannot apply this action: Raise WO-3001 priority from P5 to P1. It needs Supervisor, Manager or Admin.';
    mockApi.getAIRecommendations.mockResolvedValue([
      makeActionCard({
        apply_action: makeApplyAction({
          can_apply: false,
          can_mark_handled: false,
          blocked_kind: 'role',
          blocked_reason: reason,
        }),
      }),
    ]);

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    expect(within(hero).queryByRole('button', { name: /accept/i })).not.toBeInTheDocument();
    expect(within(hero).queryByRole('button', { name: /mark handled/i })).not.toBeInTheDocument();
    expect(within(hero).getByRole('button', { name: 'Dismiss' })).toBeEnabled();
    expect(within(hero).getByRole('button', { name: /snooze/i })).toBeEnabled();
    expect(within(hero).getByText(reason)).toBeInTheDocument();
    // The blocked card never dead-ends: the record is one click away.
    expect(within(hero).getByRole('link', { name: /open record/i })).toHaveAttribute('href', '/work-orders/3001');
  });

  it('AI queue rows render an Open link to target_href', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeActionCard({ id: 81, title: 'Queue item one', score: 0.5, target_href: '/parts/12' }),
      makeActionCard({ id: 82, title: 'Queue item two', score: 0.4, target_href: null }),
    ]);

    renderInbox();

    await screen.findByRole('region', { name: 'Top 3 today' });
    // Non-default filter folds every recommendation into the queue.
    fireEvent.click(screen.getByRole('button', { name: 'AI' }));

    const openLinks = screen.getAllByRole('link', { name: 'Open' });
    expect(openLinks).toHaveLength(1);
    expect(openLinks[0]).toHaveAttribute('href', '/parts/12');
  });

  it('a high-priority AI item drives the "Review affected records" link to its record', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeActionCard({ priority: 'high', target_href: '/work-orders/3001' }),
    ]);

    renderInbox();

    expect(await screen.findByRole('link', { name: 'Review affected records' })).toHaveAttribute(
      'href',
      '/work-orders/3001'
    );
  });

  it('two cards applied at once each stay disabled until their OWN request settles', async () => {
    const first = makeActionCard({ id: 91, title: 'Expedite WO-A', score: 0.9 });
    const second = makeActionCard({
      id: 92,
      title: 'Expedite WO-B',
      score: 0.8,
      apply_action: makeApplyAction({ summary: 'Raise WO-B priority from P5 to P1', fingerprint: 'bbbbbbbbbbbbbbbb' }),
    });
    // Load shows both; the re-read after B's apply still has A; the one after A's has neither.
    mockApi.getAIRecommendations
      .mockResolvedValueOnce([first, second])
      .mockResolvedValueOnce([first])
      .mockResolvedValue([]);
    const resolvers: Record<number, (value: AIRecommendationApplyResult) => void> = {};
    mockApi.applyAIRecommendation.mockImplementation(
      (id: number) =>
        new Promise<AIRecommendationApplyResult>(resolve => {
          resolvers[id] = resolve;
        })
    );
    const done = (rec: AIRecommendation): AIRecommendationApplyResult => ({
      recommendation: { ...rec, status: 'accepted', apply_action: null },
      applied: true,
      apply_result: { action_type: 'adjust_work_order_priority', summary: rec.apply_action?.summary },
      apply_error: null,
    });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    const cardOf = (title: string) => within(hero).getByText(title).closest('article') as HTMLElement;
    fireEvent.click(within(cardOf('Expedite WO-A')).getByRole('button', { name: /accept & apply/i }));
    fireEvent.click(within(cardOf('Expedite WO-B')).getByRole('button', { name: /accept & apply/i }));

    // Clicking B must not re-enable A while A's apply is still in flight.
    expect(within(cardOf('Expedite WO-A')).getByRole('button', { name: /applying/i })).toBeDisabled();
    expect(within(cardOf('Expedite WO-A')).getByRole('button', { name: 'Dismiss' })).toBeDisabled();
    fireEvent.click(within(cardOf('Expedite WO-A')).getByRole('button', { name: /applying/i }));
    expect(mockApi.applyAIRecommendation).toHaveBeenCalledTimes(2);

    // B settles first; its cleanup must not clear A's in-flight state.
    await act(async () => {
      resolvers[92](done(second));
    });
    await waitFor(() => expect(screen.queryByText('Expedite WO-B')).not.toBeInTheDocument());
    expect(within(cardOf('Expedite WO-A')).getByRole('button', { name: /applying/i })).toBeDisabled();

    await act(async () => {
      resolvers[91](done(first));
    });
    await waitFor(() => expect(screen.queryByText('Expedite WO-A')).not.toBeInTheDocument());
    expect(mockApi.applyAIRecommendation).toHaveBeenCalledTimes(2);
  });

  it('a 409 on Mark handled re-reads the list instead of resurrecting a card decided elsewhere', async () => {
    const detail = 'This recommendation is already accepted; it can no longer be accepted. Refresh the Action Inbox.';
    const card = makeActionCard({
      apply_action: makeApplyAction({
        can_apply: false,
        can_mark_handled: true,
        blocked_kind: 'egress',
        blocked_reason: 'AI actions are off for this company (AI egress is off), so nothing will be applied.',
      }),
    });
    // The server has already closed the card, so the re-read no longer returns it.
    mockApi.getAIRecommendations.mockResolvedValueOnce([card]).mockResolvedValue([]);
    mockApi.acceptAIRecommendation.mockRejectedValue({ response: { status: 409, data: { detail } } });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: 'Mark handled' }));

    await expectToast(detail, 'error');
    await waitFor(() => expect(mockApi.getAIRecommendations).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByText('Expedite WO-3001')).not.toBeInTheDocument());
  });

  it('a non-409 dismiss failure rolls the card back without re-reading', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([makeActionCard()]);
    mockApi.dismissAIRecommendation.mockRejectedValue({ response: { status: 503, data: { detail: 'Try again' } } });

    renderInbox();

    const hero = await screen.findByRole('region', { name: 'Top 3 today' });
    fireEvent.click(within(hero).getByRole('button', { name: 'Dismiss' }));

    await expectToast('Try again', 'error');
    expect(screen.getByText('Expedite WO-3001')).toBeInTheDocument();
    expect(mockApi.getAIRecommendations).toHaveBeenCalledTimes(1);
  });
});
