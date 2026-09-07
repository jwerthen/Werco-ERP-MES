import { useEffect, useMemo, useState } from 'react';
import { useWebSocket } from './useWebSocket';
import { buildWsUrl, getAccessToken } from '../services/realtime';
/** Reports the update transport only; page-level timestamps describe data freshness. */
export function useConnectionStatus() {
  const [online, setOnline] = useState(() => navigator.onLine);
  const [token, setToken] = useState(getAccessToken);
  useEffect(() => {
    const network = () => setOnline(navigator.onLine);
    const auth = () => setToken(getAccessToken());
    window.addEventListener('online', network);
    window.addEventListener('offline', network);
    window.addEventListener('werco:auth-token-changed', auth);
    return () => {
      window.removeEventListener('online', network);
      window.removeEventListener('offline', network);
      window.removeEventListener('werco:auth-token-changed', auth);
    };
  }, []);
  const url = useMemo(() => buildWsUrl('/ws/updates', token ? { token } : undefined), [token]);
  const connection = useWebSocket({ url, enabled: online && !!token });
  const connected = online && connection?.status === 'open';
  return {
    connected,
    label: !online
      ? 'OFFLINE'
      : connected
        ? 'CONNECTED'
        : !token
          ? 'NO SESSION'
          : connection?.status === 'connecting'
            ? 'CONNECTING'
            : 'RECONNECTING',
  };
}
