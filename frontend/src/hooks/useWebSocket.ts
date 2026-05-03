import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react';
import { getAccessToken } from '../services/realtime';

interface RealtimeMessage {
  type: string;
  data?: any;
  timestamp?: string | null;
}

type WebSocketStatus = 'idle' | 'connecting' | 'open' | 'closed' | 'error';

interface WebSocketOptions {
  url?: string | null;
  enabled?: boolean;
  reconnect?: boolean;
  reconnectMinDelayMs?: number;
  reconnectMaxDelayMs?: number;
  heartbeatIntervalMs?: number;
  onMessage?: (message: RealtimeMessage, event: MessageEvent) => void;
  onOpen?: (event: Event) => void;
  onClose?: (event: CloseEvent) => void;
  onError?: (event: Event) => void;
}

interface Subscriber {
  setStatus: (status: WebSocketStatus) => void;
  callbacksRef: MutableRefObject<Pick<WebSocketOptions, 'onMessage' | 'onOpen' | 'onClose' | 'onError'>>;
}

interface SharedConnection {
  key: string;
  url: string;
  socket: WebSocket | null;
  status: WebSocketStatus;
  subscribers: Set<Subscriber>;
  reconnect: boolean;
  reconnectMinDelayMs: number;
  reconnectMaxDelayMs: number;
  heartbeatIntervalMs: number;
  reconnectAttempt: number;
  reconnectTimer: number | null;
  heartbeatTimer: number | null;
  manualClose: boolean;
}

const connections = new Map<string, SharedConnection>();

interface ConnectionOptions {
  url: string;
  reconnect: boolean;
  reconnectMinDelayMs: number;
  reconnectMaxDelayMs: number;
  heartbeatIntervalMs: number;
}

const parseMessage = (event: MessageEvent): RealtimeMessage => {
  try {
    return JSON.parse(event.data);
  } catch {
    return { type: 'message', data: event.data };
  }
};

const connectionKey = (url: string): string => {
  const parsed = new URL(url);
  parsed.searchParams.delete('token');
  return parsed.toString();
};

const withFreshToken = (url: string): string => {
  const parsed = new URL(url);
  if (parsed.searchParams.has('token')) {
    const token = getAccessToken();
    if (token) {
      parsed.searchParams.set('token', token);
    }
  }
  return parsed.toString();
};

const broadcastStatus = (connection: SharedConnection, status: WebSocketStatus) => {
  connection.status = status;
  connection.subscribers.forEach(subscriber => subscriber.setStatus(status));
};

const clearTimers = (connection: SharedConnection) => {
  if (connection.reconnectTimer) {
    window.clearTimeout(connection.reconnectTimer);
    connection.reconnectTimer = null;
  }
  if (connection.heartbeatTimer) {
    window.clearInterval(connection.heartbeatTimer);
    connection.heartbeatTimer = null;
  }
};

const startHeartbeat = (connection: SharedConnection) => {
  if (connection.heartbeatIntervalMs <= 0) return;
  connection.heartbeatTimer = window.setInterval(() => {
    if (connection.socket?.readyState === WebSocket.OPEN) {
      connection.socket.send(JSON.stringify({ type: 'ping', timestamp: new Date().toISOString() }));
    }
  }, connection.heartbeatIntervalMs);
};

const connect = (connection: SharedConnection) => {
  if (connection.subscribers.size === 0) return;
  if (
    connection.socket &&
    (connection.socket.readyState === WebSocket.CONNECTING || connection.socket.readyState === WebSocket.OPEN)
  ) {
    return;
  }

  connection.manualClose = false;
  broadcastStatus(connection, 'connecting');

  const socket = new WebSocket(withFreshToken(connection.url));
  connection.socket = socket;

  socket.onopen = (event) => {
    connection.reconnectAttempt = 0;
    clearTimers(connection);
    broadcastStatus(connection, 'open');
    startHeartbeat(connection);
    connection.subscribers.forEach(subscriber => subscriber.callbacksRef.current.onOpen?.(event));
  };

  socket.onmessage = (event) => {
    const message = parseMessage(event);
    connection.subscribers.forEach(subscriber => subscriber.callbacksRef.current.onMessage?.(message, event));
  };

  socket.onerror = (event) => {
    broadcastStatus(connection, 'error');
    connection.subscribers.forEach(subscriber => subscriber.callbacksRef.current.onError?.(event));
  };

  socket.onclose = (event) => {
    clearTimers(connection);
    connection.socket = null;
    broadcastStatus(connection, 'closed');
    connection.subscribers.forEach(subscriber => subscriber.callbacksRef.current.onClose?.(event));

    if (!connection.reconnect || connection.manualClose || connection.subscribers.size === 0) return;
    const delay = Math.min(
      connection.reconnectMaxDelayMs,
      connection.reconnectMinDelayMs * Math.pow(2, connection.reconnectAttempt)
    );
    const jitter = Math.floor(delay * 0.2 * Math.random());
    connection.reconnectAttempt += 1;
    connection.reconnectTimer = window.setTimeout(() => connect(connection), delay + jitter);
  };
};

const getConnection = (options: ConnectionOptions) => {
  const key = connectionKey(options.url);
  const existing = connections.get(key);
  if (existing) {
    existing.url = options.url;
    existing.reconnect = options.reconnect;
    existing.reconnectMinDelayMs = options.reconnectMinDelayMs;
    existing.reconnectMaxDelayMs = options.reconnectMaxDelayMs;
    existing.heartbeatIntervalMs = options.heartbeatIntervalMs;
    return existing;
  }

  const connection: SharedConnection = {
    key,
    url: options.url,
    socket: null,
    status: 'idle',
    subscribers: new Set(),
    reconnect: options.reconnect,
    reconnectMinDelayMs: options.reconnectMinDelayMs,
    reconnectMaxDelayMs: options.reconnectMaxDelayMs,
    heartbeatIntervalMs: options.heartbeatIntervalMs,
    reconnectAttempt: 0,
    reconnectTimer: null,
    heartbeatTimer: null,
    manualClose: false,
  };
  connections.set(key, connection);
  return connection;
};

export const useWebSocket = ({
  url,
  enabled = true,
  reconnect = true,
  reconnectMinDelayMs = 1000,
  reconnectMaxDelayMs = 15000,
  heartbeatIntervalMs = 25000,
  onMessage,
  onOpen,
  onClose,
  onError,
}: WebSocketOptions) => {
  const [status, setStatus] = useState<WebSocketStatus>('idle');
  const callbacksRef = useRef({ onMessage, onOpen, onClose, onError });
  callbacksRef.current = { onMessage, onOpen, onClose, onError };
  const connectionRef = useRef<SharedConnection | null>(null);

  useEffect(() => {
    if (!enabled || !url) {
      setStatus('idle');
      return;
    }

    const connection = getConnection({
      url,
      reconnect,
      reconnectMinDelayMs,
      reconnectMaxDelayMs,
      heartbeatIntervalMs,
    });
    connectionRef.current = connection;

    const subscriber: Subscriber = { setStatus, callbacksRef };
    connection.subscribers.add(subscriber);
    setStatus(connection.status);
    connect(connection);

    return () => {
      connection.subscribers.delete(subscriber);
      if (connection.subscribers.size === 0) {
        connection.manualClose = true;
        clearTimers(connection);
        connection.socket?.close();
        connection.socket = null;
        connections.delete(connection.key);
      }
      connectionRef.current = null;
    };
  }, [enabled, heartbeatIntervalMs, reconnect, reconnectMaxDelayMs, reconnectMinDelayMs, url]);

  useEffect(() => {
    const reconnectWithLatestToken = () => {
      connections.forEach(connection => {
        if (!connection.url.includes('token=')) return;
        connection.manualClose = true;
        clearTimers(connection);
        const socket = connection.socket;
        connection.socket = null;
        if (socket) {
          socket.onclose = null;
          socket.close();
        }
        connection.manualClose = false;
        connect(connection);
      });
    };

    window.addEventListener('werco:auth-token-changed', reconnectWithLatestToken);
    return () => window.removeEventListener('werco:auth-token-changed', reconnectWithLatestToken);
  }, []);

  const sendJson = useCallback((payload: Record<string, unknown>) => {
    const socket = connectionRef.current?.socket;
    if (socket?.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(payload));
    }
  }, []);

  return {
    status,
    sendJson,
  };
};
