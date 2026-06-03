import React from 'react';
import { act } from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import { AISuggestionCard } from './AISuggestionCard';
import { AIRecommendation } from '../../types/aiLearning';

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
