import React from 'react';
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import RuntimeMetricsTab from './RuntimeMetricsTab';
import api from '../../services/api';
import type { RuntimeMetricSummary } from '../../types/runtimeMetrics';

jest.mock('../../services/api', () => ({
  __esModule: true,
  default: { getRuntimeMetricSummary: jest.fn(), setRuntimeMetricsEnabled: jest.fn(), clearRuntimeMetrics: jest.fn() },
}));
const mockApi = api as jest.Mocked<typeof api>;
const result: RuntimeMetricSummary = {
  enabled: true,
  retention_days: 30,
  days: 7,
  page: 1,
  has_more: false,
  rows: [
    {
      route: '/work-orders',
      device: 'mobile',
      name: 'LCP',
      navigation: 'document',
      release: 'a'.repeat(40),
      samples: 12,
      p75: 3200,
      good_percent: 60,
    },
  ],
};

beforeEach(() => {
  jest.resetAllMocks();
  mockApi.getRuntimeMetricSummary.mockResolvedValue(result);
  mockApi.setRuntimeMetricsEnabled.mockResolvedValue({ enabled: false, retention_days: 30 });
  mockApi.clearRuntimeMetrics.mockResolvedValue(undefined);
});

test('shows measured units, small sample warning and filter scope', async () => {
  render(<RuntimeMetricsTab />);
  expect(await screen.findByText('3,200 ms')).toBeInTheDocument();
  expect(screen.getByText('Small sample')).toBeInTheDocument();
  expect(screen.getByText('Needs improvement')).toBeInTheDocument();
  expect(screen.getByText('60%')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Device size'), { target: { value: 'mobile' } });
  await waitFor(() =>
    expect(mockApi.getRuntimeMetricSummary).toHaveBeenLastCalledWith({
      days: 7,
      page: 1,
      device: 'mobile',
      route: undefined,
    })
  );
});

test('pause updates the server and clearing requires a concrete confirmation', async () => {
  render(<RuntimeMetricsTab />);
  fireEvent.click(await screen.findByRole('button', { name: 'Pause collection' }));
  expect(await screen.findByText('Collection: Paused')).toBeInTheDocument();
  expect(mockApi.setRuntimeMetricsEnabled).toHaveBeenCalledWith(false);
  fireEvent.click(screen.getByRole('button', { name: 'Clear measurements' }));
  const dialog = await screen.findByRole('dialog');
  expect(mockApi.clearRuntimeMetrics).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole('button', { name: 'Clear measurements' }));
  await waitFor(() => expect(mockApi.clearRuntimeMetrics).toHaveBeenCalledTimes(1));
});

test('empty and error views do not fabricate zero performance scores', async () => {
  mockApi.getRuntimeMetricSummary.mockRejectedValueOnce(new Error('offline'));
  mockApi.getRuntimeMetricSummary.mockResolvedValue({ ...result, rows: [] });
  render(<RuntimeMetricsTab />);
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be loaded');
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByText('No measurements in this view')).toBeInTheDocument();
  expect(screen.queryByRole('table')).not.toBeInTheDocument();
});

test('pages bounded summaries and resets to the first page when filtering', async () => {
  mockApi.getRuntimeMetricSummary.mockResolvedValue({ ...result, has_more: true });
  render(<RuntimeMetricsTab />);
  fireEvent.click(await screen.findByRole('button', { name: 'Next page' }));
  await waitFor(() =>
    expect(mockApi.getRuntimeMetricSummary).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }))
  );
  await waitFor(() => expect(screen.getByRole('button', { name: 'Previous page' })).toBeEnabled());
  fireEvent.change(screen.getByLabelText('Device size'), { target: { value: 'mobile' } });
  await waitFor(() =>
    expect(mockApi.getRuntimeMetricSummary).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, device: 'mobile' })
    )
  );
});

test('switching company clears the report and ignores an old setting response', async () => {
  let finishSetting!: (value: { enabled: boolean; retention_days: number }) => void;
  mockApi.setRuntimeMetricsEnabled.mockImplementation(
    () =>
      new Promise(resolve => {
        finishSetting = resolve;
      })
  );
  render(<RuntimeMetricsTab />);
  fireEvent.click(await screen.findByRole('button', { name: 'Pause collection' }));
  mockApi.getRuntimeMetricSummary.mockResolvedValue({ ...result, rows: [] });
  act(() => {
    window.dispatchEvent(new Event('werco:auth-token-changed'));
  });
  expect(await screen.findByText('No measurements in this view')).toBeInTheDocument();
  await act(async () => {
    finishSetting({ enabled: false, retention_days: 30 });
  });
  expect(screen.getByText('Collection: On')).toBeInTheDocument();
  expect(screen.queryByText('3,200 ms')).not.toBeInTheDocument();
});
