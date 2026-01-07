/**
 * User-Friendly Error Messages
 * 
 * Maps technical errors to clear, actionable messages for non-technical users.
 */

export interface ErrorMessage {
  title: string;
  description: string;
  shortDescription: string;
  recoveryHint?: string;
}

type ErrorType = 
  | 'network_offline'
  | 'network_timeout'
  | 'network_server_error'
  | 'auth_session_expired'
  | 'auth_unauthorized'
  | 'data_not_found'
  | 'data_conflict'
  | 'data_validation'
  | 'render_error'
  | 'unknown';

const ERROR_MESSAGES: Record<ErrorType, ErrorMessage> = {
  network_offline: {
    title: "You're offline",
    description: "Please check your internet connection and try again.",
    shortDescription: "No internet connection",
    recoveryHint: "Check your WiFi or network cable",
  },
  network_timeout: {
    title: "Request timed out",
    description: "The server is taking too long to respond. This might be due to high traffic.",
    shortDescription: "Request timed out",
    recoveryHint: "Wait a moment and try again",
  },
  network_server_error: {
    title: "Server error",
    description: "Something went wrong on our end. Our team has been notified.",
    shortDescription: "Server error",
    recoveryHint: "Try again in a few minutes",
  },
  auth_session_expired: {
    title: "Session expired",
    description: "Your session has expired for security. Please log in again.",
    shortDescription: "Please log in again",
    recoveryHint: "Click here to log in",
  },
  auth_unauthorized: {
    title: "Access denied",
    description: "You don't have permission to view this content. Contact your supervisor if you need access.",
    shortDescription: "Access denied",
  },
  data_not_found: {
    title: "Not found",
    description: "The item you're looking for doesn't exist or may have been deleted.",
    shortDescription: "Item not found",
  },
  data_conflict: {
    title: "Conflict detected",
    description: "This record was modified by someone else. Please refresh and try again.",
    shortDescription: "Data conflict",
    recoveryHint: "Refresh to see the latest version",
  },
  data_validation: {
    title: "Invalid data",
    description: "Some of the information entered is invalid. Please check your inputs.",
    shortDescription: "Validation error",
  },
  render_error: {
    title: "Display error",
    description: "We had trouble displaying this content. Try refreshing the page.",
    shortDescription: "Display error",
    recoveryHint: "Refresh the page",
  },
  unknown: {
    title: "Something went wrong",
    description: "An unexpected error occurred. Please try again or contact support if the problem persists.",
    shortDescription: "Error occurred",
    recoveryHint: "Try refreshing the page",
  },
};

/**
 * Classify an error into a user-friendly category
 */
export function classifyError(error: Error): ErrorType {
  const message = error.message.toLowerCase();
  const name = error.name.toLowerCase();

  // Network errors
  if (!navigator.onLine) {
    return 'network_offline';
  }
  if (message.includes('timeout') || message.includes('timed out')) {
    return 'network_timeout';
  }
  if (message.includes('network') || message.includes('fetch') || message.includes('failed to fetch')) {
    return 'network_server_error';
  }

  // HTTP status code patterns
  if (message.includes('401') || message.includes('unauthorized')) {
    return 'auth_session_expired';
  }
  if (message.includes('403') || message.includes('forbidden')) {
    return 'auth_unauthorized';
  }
  if (message.includes('404') || message.includes('not found')) {
    return 'data_not_found';
  }
  if (message.includes('409') || message.includes('conflict')) {
    return 'data_conflict';
  }
  if (message.includes('422') || message.includes('validation')) {
    return 'data_validation';
  }
  if (message.includes('500') || message.includes('internal server')) {
    return 'network_server_error';
  }

  // React-specific errors
  if (name.includes('invariant') || message.includes('render') || message.includes('component')) {
    return 'render_error';
  }

  return 'unknown';
}

/**
 * Get user-friendly error message for an error
 */
export function getErrorMessage(error: Error): ErrorMessage {
  const errorType = classifyError(error);
  return ERROR_MESSAGES[errorType];
}

/**
 * Generate a unique error ID for support reference
 */
export function generateErrorId(error: Error): string {
  const timestamp = Date.now().toString(36);
  const random = Math.random().toString(36).substring(2, 8);
  const hash = hashString(error.message).toString(36).substring(0, 4);
  return `ERR-${timestamp}-${hash}-${random}`.toUpperCase();
}

/**
 * Simple string hash for error ID generation
 */
function hashString(str: string): number {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    const char = str.charCodeAt(i);
    hash = ((hash << 5) - hash) + char;
    hash = hash & hash;
  }
  return Math.abs(hash);
}

export default ERROR_MESSAGES;
