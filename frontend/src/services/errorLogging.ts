/**
 * Error Logging Service
 *
 * Collects, batches, and sends frontend errors to the backend for monitoring.
 * Handles unhandled promise rejections and global errors.
 */

import { generateErrorId } from '../components/ErrorBoundary/errorMessages';

interface ErrorLogPayload {
  error: Error;
  errorInfo?: React.ErrorInfo;
  boundaryName?: string;
  boundaryLevel?: string;
  url?: string;
  timestamp?: string;
  userAgent?: string;
  preservedData?: unknown;
  userId?: string;
  sessionId?: string;
}

interface ErrorLogEntry {
  id: string;
  message: string;
  stack?: string;
  componentStack?: string;
  boundaryName?: string;
  boundaryLevel?: string;
  url: string;
  timestamp: string;
  userAgent: string;
  userId?: string;
  sessionId?: string;
  metadata?: Record<string, unknown>;
}

/**
 * Server-side length caps on each `ErrorLogEntry` field (`ErrorLogEntry` in
 * backend/app/api/endpoints/errors.py). The server REJECTS an over-cap field with a 422
 * rather than truncating it, and one bad entry fails the whole batch -- so a long,
 * filter-laden URL or a huge error message would silently cost every other crash in the
 * same flush. Clamp on the way out instead. (`.length` counts UTF-16 code units, never
 * fewer than the code points the server counts, so a clamped value always fits.)
 */
const FIELD_LIMITS = {
  id: 128,
  message: 4000,
  stack: 20000,
  componentStack: 20000,
  boundaryName: 200,
  boundaryLevel: 50,
  url: 2000,
  timestamp: 64,
  userAgent: 1000,
  userId: 64,
  sessionId: 128,
} as const;

const clamp = (value: string, max: number): string => (value.length > max ? value.slice(0, max) : value);

const clampOptional = (value: string | undefined, max: number): string | undefined =>
  value === undefined ? undefined : clamp(value, max);

/**
 * Queue a batch as a beacon whose body is labelled `text/plain;charset=UTF-8`.
 *
 * The label is deliberate and is the API's supported beacon contract: `POST /errors/log`
 * reads a `text/plain` body as JSON. `text/plain` is CORS-safelisted, so the beacon is a
 * simple request -- no preflight, and it works cross-origin (production is Vercel ->
 * Railway). An `application/json` Blob is not safelisted and Chromium refuses that beacon
 * outright (it throws), which would leave the unload-time crash report with no beacon at
 * all.
 *
 * Returns false when no beacon was queued -- no `sendBeacon`, the browser refused the
 * payload (e.g. over its keepalive quota), or it threw -- so the caller can fall back to
 * a keepalive `fetch`.
 */
const sendBeaconReport = (url: string, body: string): boolean => {
  if (typeof navigator === 'undefined' || typeof navigator.sendBeacon !== 'function') return false;
  try {
    return navigator.sendBeacon(url, new Blob([body], { type: 'text/plain;charset=UTF-8' }));
  } catch {
    return false;
  }
};

/** A failed send worth retrying: the request may succeed later. A 413/422 would not. */
const isRetryableStatus = (status: number): boolean => status === 429 || status >= 500;

class ErrorLoggingService {
  private queue: ErrorLogEntry[] = [];
  private isProcessing = false;
  private maxQueueSize = 50;
  private flushInterval = 5000; // 5 seconds
  private flushTimer: ReturnType<typeof setInterval> | null = null;
  private initialized = false;

  initialize(): void {
    if (this.initialized) return;
    this.initialized = true;

    // Flush queue periodically
    this.flushTimer = setInterval(() => this.flush(), this.flushInterval);

    // Flush on page unload
    window.addEventListener('beforeunload', () => this.flush(true));

    // Capture unhandled promise rejections
    window.addEventListener('unhandledrejection', event => {
      this.log({
        error: event.reason instanceof Error ? event.reason : new Error(String(event.reason)),
        boundaryName: 'unhandledrejection',
        boundaryLevel: 'global',
      });
    });

    // Capture global errors
    window.addEventListener('error', event => {
      // Avoid duplicate logging if error was already caught by boundary
      if (event.error?._boundaryLogged) return;

      this.log({
        error: event.error || new Error(event.message),
        boundaryName: 'window.onerror',
        boundaryLevel: 'global',
      });
    });
  }

  log(payload: ErrorLogPayload): void {
    // `throw 'text'` (or any other non-Error) reaches the global listeners as a non-Error
    // `event.error`. Normalise it: reading `.message` off it below would throw inside the
    // error listener, and that crash report would be lost without a trace.
    const raw: unknown = payload.error;
    const error = raw instanceof Error ? raw : new Error(String(raw));

    // Mark error as logged to prevent duplicates (only an object can carry the flag)
    if (raw !== null && typeof raw === 'object') {
      (raw as { _boundaryLogged?: boolean })._boundaryLogged = true;
    }

    const entry: ErrorLogEntry = {
      id: clamp(generateErrorId(error), FIELD_LIMITS.id),
      message: clamp(error.message, FIELD_LIMITS.message),
      stack: clampOptional(error.stack, FIELD_LIMITS.stack),
      componentStack: clampOptional(payload.errorInfo?.componentStack ?? undefined, FIELD_LIMITS.componentStack),
      boundaryName: clampOptional(payload.boundaryName, FIELD_LIMITS.boundaryName),
      boundaryLevel: clampOptional(payload.boundaryLevel, FIELD_LIMITS.boundaryLevel),
      url: clamp(payload.url || window.location.href, FIELD_LIMITS.url),
      timestamp: clamp(payload.timestamp || new Date().toISOString(), FIELD_LIMITS.timestamp),
      userAgent: clamp(payload.userAgent || navigator.userAgent, FIELD_LIMITS.userAgent),
      userId: clampOptional(payload.userId || this.getCurrentUserId(), FIELD_LIMITS.userId),
      sessionId: clampOptional(payload.sessionId || this.getSessionId(), FIELD_LIMITS.sessionId),
      // `payload.preservedData` (a form boundary's recovered, half-typed form contents) is
      // deliberately NOT sent: the server never reads `metadata`, and that unbounded value
      // could push the batch past the beacon's keepalive quota or the API's body cap, which
      // loses every crash in the flush, not just this one.
      metadata: {
        screenWidth: window.innerWidth,
        screenHeight: window.innerHeight,
        language: navigator.language,
      },
    };

    // Add to queue
    this.queue.push(entry);

    // Trim queue if too large
    if (this.queue.length > this.maxQueueSize) {
      this.queue = this.queue.slice(-this.maxQueueSize);
    }

    // Immediate flush for critical errors
    if (payload.boundaryLevel === 'global') {
      this.flush(true);
    }
  }

  async flush(sync = false): Promise<void> {
    if (this.isProcessing || this.queue.length === 0) return;

    this.isProcessing = true;
    const entries = [...this.queue];
    this.queue = [];

    try {
      const apiUrl = process.env.REACT_APP_API_URL || '/api/v1';
      const url = `${apiUrl}/errors/log`;
      const body = JSON.stringify({ errors: entries });

      // Sync (a global crash, or page unload): a beacon outlives the page. If the
      // browser declines it, fall through to a keepalive fetch rather than dropping
      // the batch -- this service is the only channel frontend crashes have.
      if (sync && sendBeaconReport(url, body)) return;

      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        keepalive: sync,
      });
      // fetch resolves on any HTTP status. A rate-limited or failed server can take the
      // batch later; a 413/422 never will, so those are not retried.
      if (response && !response.ok && isRetryableStatus(response.status)) {
        this.requeue(entries);
      }
    } catch {
      this.requeue(entries);
    } finally {
      this.isProcessing = false;
    }
  }

  /** Put an unsent batch back at the front of the queue (bounded, so it cannot grow forever). */
  private requeue(entries: ErrorLogEntry[]): void {
    if (this.queue.length < this.maxQueueSize) {
      this.queue = [...entries, ...this.queue].slice(-this.maxQueueSize);
    }
  }

  private getCurrentUserId(): string | undefined {
    try {
      const authData = sessionStorage.getItem('user');
      if (authData) {
        const parsed = JSON.parse(authData);
        return (parsed?.id ?? parsed?.user?.id)?.toString();
      }
    } catch {
      // Ignore parse errors
    }
    return undefined;
  }

  private getSessionId(): string {
    let sessionId = sessionStorage.getItem('errorSessionId');
    if (!sessionId) {
      const randomBytes = new Uint8Array(16);
      crypto.getRandomValues(randomBytes);
      const hex = Array.from(randomBytes, b => b.toString(16).padStart(2, '0')).join('');
      sessionId = `session_${hex}`;
      sessionStorage.setItem('errorSessionId', sessionId);
    }
    return sessionId;
  }

  destroy(): void {
    if (this.flushTimer) {
      clearInterval(this.flushTimer);
      this.flushTimer = null;
    }
    this.flush(true);
    this.initialized = false;
  }
}

// Singleton instance
const errorLoggingService = new ErrorLoggingService();

// Convenience function for logging
export const logError = (payload: ErrorLogPayload): void => {
  errorLoggingService.log(payload);
};

// Initialize on module load
if (typeof window !== 'undefined') {
  errorLoggingService.initialize();
}
