import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { useNotificationState } from './useNotificationState';
import api from '../services/api';
jest.mock('../services/api', () => ({
  __esModule: true,
  default: { getUnreadCount: jest.fn(), markNotificationRead: jest.fn(), markAllNotificationsRead: jest.fn() },
}));
function Probe({ label }: { label: string }) {
  const state = useNotificationState();
  return (
    <>
      <span>
        {label}: {state.unreadCount ?? 'unknown'}
      </span>
      <button
        onClick={() => {
          void state.markRead(1);
        }}
      >
        Read {label}
      </button>
    </>
  );
}
beforeEach(() => {
  jest.clearAllMocks();
  (api.getUnreadCount as jest.Mock).mockResolvedValue(3);
});
it('shares count changes and deduplicates simultaneous reads in bell and inbox', async () => {
  let finish!: () => void;
  (api.markNotificationRead as jest.Mock).mockImplementation(
    () =>
      new Promise<void>(resolve => {
        finish = resolve;
      })
  );
  await act(async () => {
    render(
      <>
        <Probe label="bell" />
        <Probe label="inbox" />
      </>
    );
  });
  expect(screen.getByText('bell: 3')).toBeInTheDocument();
  expect(screen.getByText('inbox: 3')).toBeInTheDocument();
  await act(async () => {
    fireEvent.click(screen.getByText('Read bell'));
    fireEvent.click(screen.getByText('Read inbox'));
  });
  expect(api.markNotificationRead).toHaveBeenCalledTimes(1);
  (api.getUnreadCount as jest.Mock).mockResolvedValue(2);
  await act(async () => finish());
  expect(screen.getByText('bell: 2')).toBeInTheDocument();
  expect(screen.getByText('inbox: 2')).toBeInTheDocument();
});
