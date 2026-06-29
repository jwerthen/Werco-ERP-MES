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
import { AIRecommendation } from '../types/aiLearning';

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {
    getSetupHealth: jest.fn(),
    getNotificationLogs: jest.fn(),
    getAIRecommendations: jest.fn(),
    acceptAIRecommendation: jest.fn(),
    dismissAIRecommendation: jest.fn(),
    sendAIRecommendationFeedback: jest.fn(),
    snoozeAIRecommendation: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

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
      <ActionInbox />
    </MemoryRouter>
  );

describe('ActionInbox Top 3 hero', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    localStorage.clear();
    mockApi.getSetupHealth.mockResolvedValue(emptyHealth);
    mockApi.getNotificationLogs.mockResolvedValue([]);
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
      .map((heading) => heading.textContent);
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
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRecommendation({ id: 21, title: 'Snooze me', score: 0.9 }),
    ]);
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
    mockApi.getAIRecommendations.mockResolvedValue([
      makeRecommendation({ id: 51, title: 'Dismiss me', score: 0.9 }),
    ]);
    // A pending promise that never resolves during the assertion window: the card
    // must disappear from the optimistic update alone, not from the server response.
    let resolveDismiss: (value: unknown) => void = () => {};
    mockApi.dismissAIRecommendation.mockReturnValue(
      new Promise((resolve) => {
        resolveDismiss = resolve;
      }) as ReturnType<typeof api.dismissAIRecommendation>
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
