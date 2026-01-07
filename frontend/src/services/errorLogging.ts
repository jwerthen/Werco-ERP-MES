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
    window.addEventListener('unhandledrejection', (event) => {
      this.log({
        error: event.reason instanceof Error 
          ? event.reason 
          : new Error(String(event.reason)),
        boundaryName: 'unhandledrejection',
        boundaryLevel: 'global',
      });
    });

    // Capture global errors
    window.addEventListener('error', (event) => {
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
    // Mark error as logged to prevent duplicates
    if (payload.error) {
      (payload.error as Error & { _boundaryLogged?: boolean })._boundaryLogged = true;
    }

    const entry: ErrorLogEntry = {
      id: generateErrorId(payload.error),
      message: payload.error.message,
      stack: payload.error.stack,
      componentStack: payload.errorInfo?.componentStack ?? undefined,
      boundaryName: payload.boundaryName,
      boundaryLevel: payload.boundaryLevel,
      url: payload.url || window.location.href,
      timestamp: payload.timestamp || new Date().toISOString(),
      userAgent: payload.userAgent || navigator.userAgent,
      userId: payload.userId || this.getCurrentUserId(),
      sessionId: payload.sessionId || this.getSessionId(),
      metadata: {
        preservedData: payload.preservedData,
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

    // Log to console in development
    if (process.env.NODE_ENV === 'development') {
      console.group(`🔴 Error: ${entry.message}`);
      console.error(payload.error);
      if (entry.boundaryName) {
        console.log('Boundary:', entry.boundaryName);
      }
      if (entry.componentStack) {
        console.log('Component Stack:', entry.componentStack);
      }
      console.log('Error ID:', entry.id);
      console.groupEnd();
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
      
      if (sync && navigator.sendBeacon) {
        // Use sendBeacon for sync (page unload)
        navigator.sendBeacon(
          `${apiUrl}/errors/log`,
          JSON.stringify({ errors: entries })
        );
      } else {
        await fetch(`${apiUrl}/errors/log`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ errors: entries }),
        });
      }
    } catch (e) {
      // Re-queue on failure (but don't cause infinite loop)
      if (this.queue.length < this.maxQueueSize) {
        this.queue = [...entries, ...this.queue].slice(-this.maxQueueSize);
      }
      
      // Log to console as fallback
      if (process.env.NODE_ENV === 'development') {
        console.warn('Failed to send error logs:', e);
      }
    } finally {
      this.isProcessing = false;
    }
  }

  private getCurrentUserId(): string | undefined {
    try {
      const authData = localStorage.getItem('auth');
      if (authData) {
        const parsed = JSON.parse(authData);
        return parsed?.user?.id?.toString();
      }
    } catch {
      // Ignore parse errors
    }
    return undefined;
  }

  private getSessionId(): string {
    let sessionId = sessionStorage.getItem('errorSessionId');
    if (!sessionId) {
      sessionId = `session_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
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
export const errorLoggingService = new ErrorLoggingService();

// Convenience function for logging
export const logError = (payload: ErrorLogPayload): void => {
  errorLoggingService.log(payload);
};

// Initialize on module load
if (typeof window !== 'undefined') {
  errorLoggingService.initialize();
}

export default errorLoggingService;
