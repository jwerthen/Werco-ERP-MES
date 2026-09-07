import { useSyncExternalStore } from 'react';
import api from '../services/api';

interface NotificationState {
  unreadCount: number | null;
  revision: number;
  pending: boolean;
}
let state: NotificationState = { unreadCount: null, revision: 0, pending: false };
const listeners = new Set<() => void>();
let interval: ReturnType<typeof setInterval> | undefined;
let countRequest = 0;
let sessionEpoch = 0;
const writes = new Map<string, Promise<any>>();
const publish = (patch: Partial<NotificationState>) => {
  state = { ...state, ...patch };
  listeners.forEach(listener => listener());
};
async function refreshUnread() {
  const request = ++countRequest;
  try {
    const count = await api.getUnreadCount();
    if (request === countRequest)
      publish({ unreadCount: count, revision: state.revision + (count !== state.unreadCount ? 1 : 0) });
  } catch {
    /* Keep last-known count; null means not yet known. */
  }
}
function resetSession() {
  ++sessionEpoch;
  writes.clear();
  ++countRequest;
  publish({ unreadCount: null, revision: state.revision + 1, pending: false });
  void refreshUnread();
}
function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    void refreshUnread();
    interval = setInterval(refreshUnread, 60_000);
    window.addEventListener('focus', refreshUnread);
    window.addEventListener('werco:auth-token-changed', resetSession);
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      clearInterval(interval);
      ++countRequest;
      ++sessionEpoch;
      writes.clear();
      state = { unreadCount: null, revision: 0, pending: false };
      window.removeEventListener('focus', refreshUnread);
      window.removeEventListener('werco:auth-token-changed', resetSession);
    }
  };
}
function mutate<T>(
  key: string,
  operation: () => Promise<T>,
  nextCount: (count: number | null) => number | null
): Promise<T> {
  const existing = writes.get(key);
  if (existing) return existing;
  const epoch = sessionEpoch;
  ++countRequest; // A pre-mutation poll must never restore an obsolete badge.
  publish({ pending: true });
  const pending = Promise.resolve()
    .then(operation)
    .then(result => {
      if (epoch === sessionEpoch) publish({ unreadCount: nextCount(state.unreadCount), revision: state.revision + 1 });
      return result;
    })
    .finally(() => {
      if (epoch !== sessionEpoch) return;
      writes.delete(key);
      publish({ pending: writes.size > 0 });
      if (state.unreadCount === null) void refreshUnread();
    });
  writes.set(key, pending);
  return pending;
}
export function useNotificationState() {
  const snapshot = useSyncExternalStore(subscribe, () => state);
  return {
    ...snapshot,
    refreshUnread,
    markRead: (id: number) =>
      mutate(
        `read:${id}`,
        () => api.markNotificationRead(id),
        count => (count === null ? null : Math.max(0, count - 1))
      ),
    markAllRead: () =>
      mutate(
        'all',
        () => api.markAllNotificationsRead(),
        () => 0
      ),
  };
}
