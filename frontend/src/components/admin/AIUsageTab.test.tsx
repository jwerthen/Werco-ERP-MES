/**
 * AIUsageTab — the Admin > AI Usage & Cost telemetry console.
 *
 * Covers: the loading spinner while the summary is in flight, the rendered
 * totals row + per-task / per-model breakdown tables (USD 2-4 decimals,
 * compact token counts, percent error rate), the "No AI usage recorded yet"
 * empty state, the error state with a working Retry, and the 7/30/90-day
 * window selector driving the `days` query param.
 *
 * The api service is mocked at the module boundary (same pattern as the
 * sibling CarrierIntegrationsTab test) — no real network.
 */

import React from 'react';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import AIUsageTab from './AIUsageTab';
import api from '../../services/api';
import type { AIUsageSummaryResponse } from '../../types/aiUsage';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: {
    getAIUsageSummary: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const http = (status: number, detail?: string) => {
  const err = new Error(detail || 'error') as Error & {
    response: { status: number; data: { detail?: string } };
  };
  err.response = { status, data: { detail } };
  return err;
};

const summary: AIUsageSummaryResponse = {
  window_days: 30,
  since: '2026-05-10T00:00:00Z',
  totals: {
    calls: 1480,
    input_tokens: 1234000,
    output_tokens: 45600,
    cache_creation_tokens: 200000,
    cache_read_tokens: 800000,
    estimated_cost_usd: 12.3456,
    avg_latency_ms: 843.2,
    error_rate: 0.0625,
  },
  by_task: [
    {
      task: 'rfq_parse',
      calls: 980,
      input_tokens: 1000000,
      output_tokens: 30000,
      cache_creation_tokens: 150000,
      cache_read_tokens: 600000,
      estimated_cost_usd: 9.87,
      avg_latency_ms: 1234,
      error_rate: 0.0,
    },
    {
      task: 'quote_learning',
      calls: 500,
      input_tokens: 234000,
      output_tokens: 15600,
      cache_creation_tokens: 50000,
      cache_read_tokens: 200000,
      estimated_cost_usd: null, // unpriced bucket renders an em dash
      avg_latency_ms: null,
      error_rate: 0.185,
    },
  ],
  by_model: [
    {
      model: 'claude-sonnet-4-5',
      calls: 1480,
      input_tokens: 1234000,
      output_tokens: 45600,
      cache_creation_tokens: 200000,
      cache_read_tokens: 800000,
      estimated_cost_usd: 0.0421,
      avg_latency_ms: 843.2,
      error_rate: 0.0625,
    },
  ],
};

const emptySummary: AIUsageSummaryResponse = {
  window_days: 30,
  since: '2026-05-10T00:00:00Z',
  totals: {
    calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_creation_tokens: 0,
    cache_read_tokens: 0,
    estimated_cost_usd: null,
    avg_latency_ms: null,
    error_rate: 0.0,
  },
  by_task: [],
  by_model: [],
};

beforeEach(() => {
  jest.clearAllMocks();
  mockApi.getAIUsageSummary.mockResolvedValue(summary);
});

describe('AIUsageTab — loading', () => {
  it('shows a spinner while the summary request is in flight', async () => {
    let resolveRequest: (value: AIUsageSummaryResponse) => void = () => undefined;
    mockApi.getAIUsageSummary.mockReturnValueOnce(
      new Promise<AIUsageSummaryResponse>((resolve) => {
        resolveRequest = resolve;
      }),
    );
    render(<AIUsageTab />);

    expect(screen.getByTestId('ai-usage-loading')).toBeInTheDocument();

    resolveRequest(summary);
    await waitFor(() => expect(screen.queryByTestId('ai-usage-loading')).toBeNull());
    expect(mockApi.getAIUsageSummary).toHaveBeenCalledWith(30); // default window
  });
});

describe('AIUsageTab — rendered data', () => {
  it('renders the totals row with formatted cost, compact tokens, and error rate', async () => {
    render(<AIUsageTab />);

    // 1,480 calls renders in the totals tile (and again in the model row).
    await waitFor(() => expect(screen.getAllByText('1,480').length).toBeGreaterThan(0));

    // USD with 2-4 decimals.
    expect(screen.getByText('$12.3456')).toBeInTheDocument();
    // Total tokens across all four buckets: 1,234,000 + 45,600 + 200,000 + 800,000 = ~2.3M.
    expect(screen.getByText('2.3M')).toBeInTheDocument();
    // Error rate as a percentage.
    expect(screen.getAllByText('6.3%').length).toBeGreaterThan(0);
    // Avg latency in ms.
    expect(screen.getAllByText('843 ms').length).toBeGreaterThan(0);
  });

  it('renders the per-task breakdown with em dashes for unpriced buckets', async () => {
    render(<AIUsageTab />);

    await waitFor(() => expect(screen.getByText('rfq_parse')).toBeInTheDocument());

    const taskRow = screen.getByText('rfq_parse').closest('tr') as HTMLElement;
    expect(within(taskRow).getByText('980')).toBeInTheDocument();
    expect(within(taskRow).getByText('1M')).toBeInTheDocument(); // compact input tokens
    expect(within(taskRow).getByText('30K')).toBeInTheDocument(); // compact output tokens
    expect(within(taskRow).getByText('$9.87')).toBeInTheDocument();
    expect(within(taskRow).getByText('1.23 s')).toBeInTheDocument(); // >= 1s latency
    expect(within(taskRow).getByText('0.0%')).toBeInTheDocument();

    // Unpriced / latency-less bucket renders em dashes, not $0.00.
    const unpricedRow = screen.getByText('quote_learning').closest('tr') as HTMLElement;
    expect(within(unpricedRow).getAllByText('—').length).toBe(2);
    expect(within(unpricedRow).getByText('18.5%')).toBeInTheDocument();
  });

  it('renders the per-model breakdown with sub-cent cost precision', async () => {
    render(<AIUsageTab />);

    await waitFor(() => expect(screen.getByText('claude-sonnet-4-5')).toBeInTheDocument());

    const modelRow = screen.getByText('claude-sonnet-4-5').closest('tr') as HTMLElement;
    expect(within(modelRow).getByText('$0.0421')).toBeInTheDocument();
  });
});

describe('AIUsageTab — empty state', () => {
  it('shows "No AI usage recorded yet" when the window has zero calls', async () => {
    mockApi.getAIUsageSummary.mockResolvedValueOnce(emptySummary);
    render(<AIUsageTab />);

    await waitFor(() => expect(screen.getByText(/no ai usage recorded yet/i)).toBeInTheDocument());
    // No tables in the empty state.
    expect(screen.queryByText('By Task')).toBeNull();
    expect(screen.queryByText('By Model')).toBeNull();
  });
});

describe('AIUsageTab — error state', () => {
  it('surfaces the API error detail and recovers via Retry', async () => {
    mockApi.getAIUsageSummary
      .mockRejectedValueOnce(http(503, 'Telemetry store unavailable'))
      .mockResolvedValueOnce(summary);
    render(<AIUsageTab />);

    await waitFor(() => expect(screen.getByText(/failed to load ai usage/i)).toBeInTheDocument());
    expect(screen.getByText('Telemetry store unavailable')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /retry/i }));

    await waitFor(() => expect(screen.getByText('rfq_parse')).toBeInTheDocument());
    expect(mockApi.getAIUsageSummary).toHaveBeenCalledTimes(2);
  });
});

describe('AIUsageTab — window selector', () => {
  it('refetches with the selected number of days', async () => {
    render(<AIUsageTab />);
    await waitFor(() => expect(mockApi.getAIUsageSummary).toHaveBeenCalledWith(30));

    fireEvent.click(screen.getByRole('button', { name: '90d' }));
    await waitFor(() => expect(mockApi.getAIUsageSummary).toHaveBeenCalledWith(90));

    fireEvent.click(screen.getByRole('button', { name: '7d' }));
    await waitFor(() => expect(mockApi.getAIUsageSummary).toHaveBeenCalledWith(7));

    // The active option is reflected via aria-pressed.
    expect(screen.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '90d' })).toHaveAttribute('aria-pressed', 'false');
  });
});
