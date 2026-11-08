/**
 * ContextualAIStrip — the per-record AI strip on Work Order and Part detail.
 *
 * Owner ruling 3 (2026-11-05): nothing AI-driven runs without a person clicking
 * Accept & apply. The strip is the second Accept surface, so it must use the single
 * apply door with the server's fingerprint, stay non-optimistic, show the server's
 * refusal verbatim, and tell its host page to reload after an apply. services/api is
 * mocked at the module boundary.
 */

import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { ContextualAIStrip } from './ContextualAIStrip';
import { ToastProvider } from '../ui/Toast';
import api from '../../services/api';
import { AIRecommendation, AIRecommendationApplyAction, AIRecommendationApplyResult } from '../../types/aiLearning';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getAIRecommendations: jest.fn(),
    acceptAIRecommendation: jest.fn(),
    applyAIRecommendation: jest.fn(),
    dismissAIRecommendation: jest.fn(),
    snoozeAIRecommendation: jest.fn(),
    sendAIRecommendationFeedback: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const applyAction = (overrides: Partial<AIRecommendationApplyAction> = {}): AIRecommendationApplyAction => ({
  action_type: 'adjust_work_order_priority',
  summary: 'Raise WO-2001 priority from P5 to P1',
  warnings: [],
  fingerprint: 'fp0123456789abcd',
  can_apply: true,
  can_mark_handled: true,
  blocked_reason: null,
  blocked_kind: null,
  ...overrides,
});

const makeRec = (overrides: Partial<AIRecommendation> = {}): AIRecommendation => ({
  id: 301,
  source_module: 'delivery',
  recommendation_type: 'delivery_risk',
  status: 'pending',
  priority: 'high',
  title: 'Expedite WO-2001',
  summary: 'WO-2001 is at risk of shipping late.',
  target_entity_type: 'work_order',
  target_entity_id: 2001,
  target_href: '/work-orders/2001',
  suggested_action: { type: 'adjust_work_order_priority', work_order_id: 2001, priority: 1 },
  apply_action: applyAction(),
  confidence_score: 0.9,
  created_at: '2026-11-06T10:00:00Z',
  updated_at: '2026-11-06T10:00:00Z',
  ...overrides,
});

const applyResult = (rec: AIRecommendation): AIRecommendationApplyResult => ({
  recommendation: { ...rec, status: 'accepted', apply_action: null },
  applied: true,
  apply_result: {
    action_type: 'adjust_work_order_priority',
    summary: 'Raise WO-2001 priority from P5 to P1',
    work_order_id: 2001,
    old_priority: 5,
    new_priority: 1,
  },
  apply_error: null,
});

const renderStrip = (props: Partial<React.ComponentProps<typeof ContextualAIStrip>> = {}) =>
  render(
    <MemoryRouter>
      <ToastProvider>
        <ContextualAIStrip entityType="work_order" entityId={2001} title="AI for this work order" {...props} />
      </ToastProvider>
    </MemoryRouter>
  );

// A toast's text alone does not prove its variant: a failed call surfacing as a green
// success (or a success as a red alert) would pass a text-only assertion. Check both.
const expectToast = async (message: string, variant: 'success' | 'error') => {
  const text = await screen.findByText(message);
  const toast = text.closest('[role="alert"], [role="status"]');
  expect(toast).not.toBeNull();
  expect(toast).toHaveAttribute('role', variant === 'error' ? 'alert' : 'status');
  expect(toast).toHaveClass(variant === 'error' ? 'bg-red-600' : 'bg-green-600');
};

const strip = () => screen.findByRole('region', { name: 'AI for this work order' });

beforeEach(() => {
  jest.clearAllMocks();
});

describe('ContextualAIStrip', () => {
  it('applies through the single door with the fingerprint, then calls onApplied', async () => {
    const rec = makeRec();
    mockApi.getAIRecommendations.mockResolvedValueOnce([rec]).mockResolvedValue([]);
    const result = applyResult(rec);
    let resolveApply: (value: AIRecommendationApplyResult) => void = () => {};
    mockApi.applyAIRecommendation.mockReturnValue(
      new Promise<AIRecommendationApplyResult>(resolve => {
        resolveApply = resolve;
      })
    );
    const onApplied = jest.fn();

    renderStrip({ onApplied });

    const region = await strip();
    fireEvent.click(within(region).getByRole('button', { name: /accept & apply/i }));

    expect(mockApi.applyAIRecommendation).toHaveBeenCalledWith(
      301,
      'fp0123456789abcd',
      'Accepted & applied from context strip'
    );
    expect(mockApi.acceptAIRecommendation).not.toHaveBeenCalled();
    // Non-optimistic: the card stays (disabled) until the server answers.
    expect(within(region).getByText('Expedite WO-2001')).toBeInTheDocument();
    expect(within(region).getByRole('button', { name: /applying/i })).toBeDisabled();
    expect(onApplied).not.toHaveBeenCalled();

    await act(async () => {
      resolveApply(result);
    });

    expect(onApplied).toHaveBeenCalledWith(result);
    await expectToast('Applied: Raise WO-2001 priority from P5 to P1', 'success');
    await waitFor(() => expect(screen.queryByText('Expedite WO-2001')).not.toBeInTheDocument());
  });

  it('shows a refused apply (403) verbatim and keeps the card', async () => {
    const detail =
      'Your role (operator) cannot apply this action: Raise WO-2001 priority from P5 to P1. It needs Supervisor, Manager or Admin.';
    mockApi.getAIRecommendations.mockResolvedValue([makeRec()]);
    mockApi.applyAIRecommendation.mockRejectedValue({ response: { status: 403, data: { detail } } });
    const onApplied = jest.fn();

    renderStrip({ onApplied });

    const region = await strip();
    fireEvent.click(within(region).getByRole('button', { name: /accept & apply/i }));

    await expectToast(detail, 'error');
    expect(within(region).getByText('Expedite WO-2001')).toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
    // A 403 is not a changed card: no reload.
    expect(mockApi.getAIRecommendations).toHaveBeenCalledTimes(1);
  });

  it('reloads after a 409 so a changed plan is shown', async () => {
    const detail =
      'This action changed since you loaded it. It would now: Raise WO-2001 priority from P3 to P1. Review it and click Accept & apply again.';
    mockApi.getAIRecommendations.mockResolvedValue([makeRec()]);
    mockApi.applyAIRecommendation.mockRejectedValue({ response: { status: 409, data: { detail } } });

    renderStrip();

    const region = await strip();
    fireEvent.click(within(region).getByRole('button', { name: /accept & apply/i }));

    await expectToast(detail, 'error');
    await waitFor(() => expect(mockApi.getAIRecommendations).toHaveBeenCalledTimes(2));
  });

  it('mark handled is record-only: acceptAIRecommendation, never applyAIRecommendation', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRec({
        apply_action: applyAction({
          can_apply: false,
          can_mark_handled: true,
          blocked_kind: 'egress',
          blocked_reason: 'AI actions are off for this company (AI egress is off), so nothing will be applied.',
        }),
      }),
    ]);
    mockApi.acceptAIRecommendation.mockResolvedValue({
      recommendation: makeRec({ status: 'accepted' }),
      applied: false,
      apply_result: null,
      apply_error: null,
    });

    renderStrip();

    const region = await strip();
    fireEvent.click(within(region).getByRole('button', { name: 'Mark handled' }));

    await waitFor(() =>
      expect(mockApi.acceptAIRecommendation).toHaveBeenCalledWith(301, 'Marked handled from context strip')
    );
    expect(mockApi.applyAIRecommendation).not.toHaveBeenCalled();
  });

  it('toasts a dismiss failure with no unhandled rejection', async () => {
    const unhandled = jest.fn();
    process.on('unhandledRejection', unhandled);
    try {
      mockApi.getAIRecommendations.mockResolvedValue([makeRec()]);
      mockApi.dismissAIRecommendation.mockRejectedValue({
        response: {
          status: 409,
          data: { detail: 'This recommendation is already accepted; it can no longer be dismissed.' },
        },
      });

      renderStrip();

      const region = await strip();
      fireEvent.click(within(region).getByRole('button', { name: 'Dismiss' }));

      await expectToast('This recommendation is already accepted; it can no longer be dismissed.', 'error');
      // Let any stray rejection surface before asserting.
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 0));
      });
      expect(unhandled).not.toHaveBeenCalled();
    } finally {
      process.off('unhandledRejection', unhandled);
    }
  });

  it('renders no record link: the strip already sits on the record', async () => {
    mockApi.getAIRecommendations.mockResolvedValue([makeRec({ target_href: '/work-orders/2001' })]);

    renderStrip();

    const region = await strip();
    expect(within(region).queryByRole('link', { name: /open record/i })).not.toBeInTheDocument();
    // The strip's own Action Inbox link is unaffected.
    expect(within(region).getByRole('link', { name: 'Open Action Inbox' })).toBeInTheDocument();
  });

  it('keeps each card disabled until its OWN apply settles when two are in flight', async () => {
    const first = makeRec({ id: 311, title: 'Expedite WO-A' });
    const second = makeRec({
      id: 312,
      title: 'Expedite WO-B',
      apply_action: applyAction({ summary: 'Raise WO-B priority from P5 to P1', fingerprint: 'fpbbbbbbbbbbbbbb' }),
    });
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

    renderStrip();

    const region = await strip();
    const cardOf = (title: string) => within(region).getByText(title).closest('article') as HTMLElement;
    fireEvent.click(within(cardOf('Expedite WO-A')).getByRole('button', { name: /accept & apply/i }));
    fireEvent.click(within(cardOf('Expedite WO-B')).getByRole('button', { name: /accept & apply/i }));

    expect(within(cardOf('Expedite WO-A')).getByRole('button', { name: /applying/i })).toBeDisabled();
    fireEvent.click(within(cardOf('Expedite WO-A')).getByRole('button', { name: /applying/i }));
    expect(mockApi.applyAIRecommendation).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolvers[312](applyResult(second));
    });
    await waitFor(() => expect(within(region).queryByText('Expedite WO-B')).not.toBeInTheDocument());
    expect(within(cardOf('Expedite WO-A')).getByRole('button', { name: /applying/i })).toBeDisabled();

    await act(async () => {
      resolvers[311](applyResult(first));
    });
    await waitFor(() => expect(screen.queryByText('Expedite WO-A')).not.toBeInTheDocument());
    expect(mockApi.applyAIRecommendation).toHaveBeenCalledTimes(2);
  });
});
