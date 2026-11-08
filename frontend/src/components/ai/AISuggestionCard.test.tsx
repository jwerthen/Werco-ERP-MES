import React from 'react';
import { act } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AISuggestionCard } from './AISuggestionCard';
import {
  AIRecommendation,
  AIRecommendationApplyAction,
  recommendationAcceptMode,
  recommendationIsApplyable,
} from '../../types/aiLearning';

const recommendation: AIRecommendation = {
  id: 42,
  source_module: 'routing',
  recommendation_type: 'correction_pattern',
  status: 'pending',
  priority: 'medium',
  title: 'Teach AI the preferred work center',
  summary: 'Users keep changing the suggested work center.',
  rationale: 'Repeated corrections are a strong learning signal.',
  target_entity_type: 'field_path',
  suggested_action: { type: 'add_preference' },
  evidence: [{ correction_count: 3, window_days: 30 }],
  impact: { expected: 'fewer edits' },
  confidence_score: 0.82,
  created_at: '2026-06-03T10:00:00Z',
  updated_at: '2026-06-03T10:00:00Z',
};

describe('AISuggestionCard', () => {
  it('renders recommendation details and confidence', () => {
    render(<AISuggestionCard recommendation={recommendation} />);

    expect(screen.getByText('Teach AI the preferred work center')).toBeInTheDocument();
    expect(screen.getByText('Users keep changing the suggested work center.')).toBeInTheDocument();
    expect(screen.getByText('82% confidence')).toBeInTheDocument();
  });

  it('shows rationale and evidence when expanded', () => {
    render(<AISuggestionCard recommendation={recommendation} />);

    fireEvent.click(screen.getByRole('button', { name: /why this suggestion/i }));

    expect(screen.getByText('Repeated corrections are a strong learning signal.')).toBeInTheDocument();
    expect(screen.getByText(/correction_count:/)).toBeInTheDocument();
  });

  it('calls feedback callbacks', async () => {
    const onAccept = jest.fn();
    const onDismiss = jest.fn();
    const onFeedback = jest.fn().mockResolvedValue(undefined);

    render(
      <AISuggestionCard
        recommendation={recommendation}
        onAccept={onAccept}
        onDismiss={onDismiss}
        onFeedback={onFeedback}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /accept/i }));
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }));
    fireEvent.click(screen.getByRole('button', { name: /feedback/i }));
    fireEvent.change(screen.getByPlaceholderText('What should AI learn from this?'), {
      target: { value: 'Prefer cell B for thin sheet metal.' },
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /send/i }));
    });

    expect(onAccept).toHaveBeenCalledWith(recommendation);
    expect(onDismiss).toHaveBeenCalledWith(recommendation);
    expect(onFeedback).toHaveBeenCalledWith(recommendation, 'Prefer cell B for thin sheet metal.');
  });
});

const applyAction = (overrides: Partial<AIRecommendationApplyAction> = {}): AIRecommendationApplyAction => ({
  action_type: 'adjust_work_order_priority',
  summary: 'Raise WO-1001 priority from P5 to P1',
  warnings: [],
  fingerprint: 'abcdef0123456789',
  can_apply: true,
  can_mark_handled: true,
  blocked_reason: null,
  blocked_kind: null,
  ...overrides,
});

const actionCard = (overrides: Partial<AIRecommendation> = {}): AIRecommendation => ({
  ...recommendation,
  id: 77,
  title: 'Expedite WO-1001',
  target_entity_type: 'work_order',
  target_entity_id: 1001,
  suggested_action: { type: 'adjust_work_order_priority', work_order_id: 1001, priority: 1 },
  apply_action: applyAction(),
  ...overrides,
});

const renderCard = (rec: AIRecommendation, props: Partial<React.ComponentProps<typeof AISuggestionCard>> = {}) =>
  render(
    <MemoryRouter>
      <AISuggestionCard
        recommendation={rec}
        onAccept={jest.fn()}
        onDismiss={jest.fn()}
        onSnooze={jest.fn()}
        {...props}
      />
    </MemoryRouter>
  );

describe('AISuggestionCard accept modes (server-decided)', () => {
  it('apply: labels the button "Accept & apply" and says what it will do', () => {
    renderCard(actionCard());

    const button = screen.getByRole('button', { name: /accept & apply/i });
    expect(screen.getByText('Accept & apply will: Raise WO-1001 priority from P5 to P1')).toBeInTheDocument();
    // The note is wired as the button's description for screen readers.
    expect(button).toHaveAccessibleDescription(/raise wo-1001 priority from p5 to p1/i);
  });

  it('apply: renders the server warnings', () => {
    renderCard(
      actionCard({
        apply_action: applyAction({
          action_type: 'create_draft_po',
          summary:
            'Create a DRAFT purchase order to Acme (ACME) for 10 x P-100 at $2.50 each. Nothing is sent to the vendor.',
          warnings: ['P-100 already has open PO(s): PO-20261106-001'],
        }),
      })
    );

    expect(screen.getByText('P-100 already has open PO(s): PO-20261106-001')).toBeInTheDocument();
  });

  it('apply: shows the in-flight state and disables the button while accepting', () => {
    renderCard(actionCard(), { accepting: true });

    const button = screen.getByRole('button', { name: /applying/i });
    expect(button).toBeDisabled();
    expect(screen.queryByRole('button', { name: /accept & apply/i })).not.toBeInTheDocument();
  });

  it('mark_handled: labels the button "Mark handled" and explains it won\'t apply', () => {
    const reason =
      'AI actions are off for this company (AI egress is off), so nothing will be applied. An Admin can turn AI egress on in Admin Settings > AI Privacy, or you can act on the record directly.';
    renderCard(
      actionCard({
        apply_action: applyAction({
          can_apply: false,
          can_mark_handled: true,
          blocked_kind: 'egress',
          blocked_reason: reason,
        }),
      })
    );

    expect(screen.getByRole('button', { name: 'Mark handled' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /accept & apply/i })).not.toBeInTheDocument();
    expect(screen.getByText('Suggested action: Raise WO-1001 priority from P5 to P1')).toBeInTheDocument();
    expect(
      screen.getByText(`Won't apply: ${reason} Mark handled records that you dealt with it; nothing is changed.`)
    ).toBeInTheDocument();
  });

  it('mark_handled: punctuates a target reason that has no trailing period', () => {
    renderCard(
      actionCard({
        apply_action: applyAction({
          can_apply: false,
          can_mark_handled: true,
          blocked_kind: 'target',
          blocked_reason: 'WO-1001 is already P1 (the suggestion was P1); nothing to apply',
        }),
      })
    );

    expect(
      screen.getByText(
        "Won't apply: WO-1001 is already P1 (the suggestion was P1); nothing to apply. Mark handled records that you dealt with it; nothing is changed."
      )
    ).toBeInTheDocument();
  });

  it('none: renders no accept button, keeps Dismiss/Snooze, and shows the server reason', () => {
    const reason =
      'Your role (operator) cannot apply this action: Raise WO-1001 priority from P5 to P1. It needs Supervisor, Manager or Admin.';
    renderCard(
      actionCard({
        apply_action: applyAction({
          can_apply: false,
          can_mark_handled: false,
          blocked_kind: 'role',
          blocked_reason: reason,
        }),
      })
    );

    expect(screen.queryByRole('button', { name: /accept/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /mark handled/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /snooze/i })).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
  });

  it('accept: a suggest-only card (no apply_action) keeps the plain record-only "Accept"', () => {
    renderCard(recommendation);

    expect(screen.getByRole('button', { name: 'Accept' })).toBeInTheDocument();
    expect(screen.queryByText(/accept & apply will/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/won't apply/i)).not.toBeInTheDocument();
  });
});

describe('AISuggestionCard record link', () => {
  it('renders an "Open record" link to the server-computed target_href', () => {
    renderCard(actionCard({ target_href: '/work-orders/1001' }));

    const link = screen.getByRole('link', { name: /open record/i });
    expect(link).toHaveAttribute('href', '/work-orders/1001');
    expect(link).not.toHaveClass('btn-secondary');
  });

  it('makes the link prominent when the plan is blocked, so the card never dead-ends', () => {
    renderCard(
      actionCard({
        target_href: '/work-orders/1001',
        apply_action: applyAction({
          can_apply: false,
          can_mark_handled: true,
          blocked_kind: 'target',
          blocked_reason: 'Work order WO-1001 has been deleted',
        }),
      })
    );

    expect(screen.getByRole('link', { name: /open record/i })).toHaveClass('btn-secondary');
  });

  it('renders no link when there is no target_href, or when showRecordLink is false', () => {
    const { unmount } = renderCard(actionCard({ target_href: null }));
    expect(screen.queryByRole('link', { name: /open record/i })).not.toBeInTheDocument();
    unmount();

    renderCard(actionCard({ target_href: '/work-orders/1001' }), { showRecordLink: false });
    expect(screen.queryByRole('link', { name: /open record/i })).not.toBeInTheDocument();
  });

  it('never uses the stored suggested_action.href', () => {
    renderCard(
      actionCard({
        target_href: '/parts/9',
        suggested_action: { type: 'create_draft_po', href: '/inventory?part=9', autonomy: 'apply_on_accept' },
      })
    );

    expect(screen.getByRole('link', { name: /open record/i })).toHaveAttribute('href', '/parts/9');
  });
});

describe('recommendationIsApplyable / recommendationAcceptMode', () => {
  it('ignores suggested_action.autonomy and trusts only the server apply_action', () => {
    // A legacy auto-execute tag with no server apply_action is NOT applyable.
    const legacy = actionCard({
      suggested_action: { type: 'adjust_work_order_priority', autonomy: 'auto_execute' },
      apply_action: null,
    });
    expect(recommendationIsApplyable(legacy)).toBe(false);
    expect(recommendationAcceptMode(legacy)).toBe('accept');

    // A suggest-only tag the server nonetheless marks applyable IS applyable.
    const serverSaysYes = actionCard({
      suggested_action: { type: 'adjust_work_order_priority', autonomy: 'suggest_only' },
      apply_action: applyAction(),
    });
    expect(recommendationIsApplyable(serverSaysYes)).toBe(true);
    expect(recommendationAcceptMode(serverSaysYes)).toBe('apply');
  });

  it('maps the server flags onto the four modes', () => {
    expect(recommendationAcceptMode(actionCard({ apply_action: applyAction({ can_apply: true }) }))).toBe('apply');
    expect(
      recommendationAcceptMode(actionCard({ apply_action: applyAction({ can_apply: false, can_mark_handled: true }) }))
    ).toBe('mark_handled');
    expect(
      recommendationAcceptMode(actionCard({ apply_action: applyAction({ can_apply: false, can_mark_handled: false }) }))
    ).toBe('none');
    expect(recommendationAcceptMode(actionCard({ apply_action: undefined }))).toBe('accept');
  });
});
