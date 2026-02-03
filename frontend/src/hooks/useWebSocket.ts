import { useCallback, useEffect, useRef, useState } from 'react';

export interface RealtimeMessage {
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

const parseMessage = (event: MessageEvent): RealtimeMessage => {
  try {
    return JSON.parse(event.data);
  } catch (error) {
    return { type: 'message', data: event.data };
  }
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
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const heartbeatTimerRef = useRef<number | null>(null);
  const reconnectAttemptRef = useRef(0);
  const manualCloseRef = useRef(false);
  const connectRef = useRef<() => void>(() => {});
  const callbacksRef = useRef({ onMessage, onOpen, onClose, onError });

  callbacksRef.current = { onMessage, onOpen, onClose, onError };

  const clearTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (!reconnect || manualCloseRef.current) return;

    const attempt = reconnectAttemptRef.current;
    const delay = Math.min(reconnectMaxDelayMs, reconnectMinDelayMs * Math.pow(2, attempt));
    const jitter = Math.floor(delay * 0.2 * Math.random());
    const nextDelay = delay + jitter;

    reconnectAttemptRef.current += 1;
    reconnectTimerRef.current = window.setTimeout(() => {
      connectRef.current();
    }, nextDelay);
  }, [reconnect, reconnectMaxDelayMs, reconnectMinDelayMs]);

  const startHeartbeat = useCallback(() => {
    if (heartbeatIntervalMs <= 0) return;
    heartbeatTimerRef.current = window.setInterval(() => {
      if (socketRef.current?.readyState === WebSocket.OPEN) {
        socketRef.current.send(JSON.stringify({ type: 'ping', timestamp: new Date().toISOString() }));
      }
    }, heartbeatIntervalMs);
  }, [heartbeatIntervalMs]);

  const connect = useCallback(() => {
    if (!url || !enabled) {
      setStatus('idle');
      return;
    }

    manualCloseRef.current = false;
    setStatus('connecting');
    const socket = new WebSocket(url);
    socketRef.current = socket;

    socket.onopen = (event) => {
      reconnectAttemptRef.current = 0;
      setStatus('open');
      clearTimers();
      startHeartbeat();
      callbacksRef.current.onOpen?.(event);
    };

    socket.onmessage = (event) => {
      const message = parseMessage(event);
      callbacksRef.current.onMessage?.(message, event);
    };

    socket.onerror = (event) => {
      setStatus('error');
      callbacksRef.current.onError?.(event);
    };

    socket.onclose = (event) => {
      setStatus('closed');
      clearTimers();
      callbacksRef.current.onClose?.(event);
      scheduleReconnect();
    };
  }, [clearTimers, enabled, scheduleReconnect, startHeartbeat, url]);

  connectRef.current = connect;

  useEffect(() => {
    if (!enabled || !url) {
      manualCloseRef.current = true;
      clearTimers();
      socketRef.current?.close();
      socketRef.current = null;
      setStatus('idle');
      return;
    }

    connect();

    return () => {
      manualCloseRef.current = true;
      clearTimers();
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [clearTimers, connect, enabled, url]);

  const sendJson = useCallback((payload: Record<string, unknown>) => {
    if (socketRef.current?.readyState === WebSocket.OPEN) {
      socketRef.current.send(JSON.stringify(payload));
    }
  }, []);

  return {
    status,
    sendJson,
  };
};
