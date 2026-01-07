/**
 * Optimistic Locking Utilities
 * 
 * Handles version-based conflict detection for concurrent edits.
 */

export interface ConflictData<T = Record<string, unknown>> {
  current_version: number;
  submitted_version: number;
  current_data: T;
  submitted_changes: Partial<T>;
  updated_by?: string;
  updated_at?: string;
  message: string;
}

export interface ConflictResponse<T = Record<string, unknown>> {
  error: 'CONFLICT';
  message: string;
  conflict: ConflictData<T>;
}

/**
 * Custom error class for version conflicts
 */
export class ConflictError<T = Record<string, unknown>> extends Error {
  public readonly conflict: ConflictData<T>;
  public readonly statusCode: number = 409;

  constructor(response: ConflictResponse<T>) {
    super(response.message);
    this.name = 'ConflictError';
    this.conflict = response.conflict;
  }

  get currentVersion(): number {
    return this.conflict.current_version;
  }

  get submittedVersion(): number {
    return this.conflict.submitted_version;
  }

  get currentData(): T {
    return this.conflict.current_data;
  }

  get submittedChanges(): Partial<T> {
    return this.conflict.submitted_changes;
  }
}

/**
 * Check if an error response is a conflict error
 */
export function isConflictError(error: unknown): error is ConflictError {
  return error instanceof ConflictError;
}

/**
 * Check if an API response indicates a conflict
 */
export function isConflictResponse(response: unknown): response is ConflictResponse {
  return (
    typeof response === 'object' &&
    response !== null &&
    'error' in response &&
    (response as ConflictResponse).error === 'CONFLICT'
  );
}

/**
 * Parse an API error and throw ConflictError if applicable
 */
export function handleApiError(error: unknown): never {
  if (error && typeof error === 'object' && 'response' in error) {
    const axiosError = error as { response?: { status?: number; data?: unknown } };
    if (axiosError.response?.status === 409 && isConflictResponse(axiosError.response.data)) {
      throw new ConflictError(axiosError.response.data);
    }
  }
  throw error;
}

/**
 * Get changed fields between two objects
 */
export function getChangedFields<T extends Record<string, unknown>>(
  original: T,
  current: T
): (keyof T)[] {
  const changed: (keyof T)[] = [];
  
  for (const key of Object.keys(current) as (keyof T)[]) {
    if (key === 'version' || key === 'updated_at') continue;
    if (JSON.stringify(original[key]) !== JSON.stringify(current[key])) {
      changed.push(key);
    }
  }
  
  return changed;
}

/**
 * Attempt to auto-merge non-conflicting changes
 * Returns null if there's a real conflict (same field changed to different values)
 */
export function autoMerge<T extends Record<string, unknown>>(
  original: T,
  yours: Partial<T>,
  theirs: T
): T | null {
  const merged = { ...theirs };
  let hasConflict = false;

  for (const key of Object.keys(yours) as (keyof T)[]) {
    if (key === 'version' || key === 'updated_at' || key === 'id') continue;
    
    const originalValue = original[key];
    const yourValue = yours[key];
    const theirValue = theirs[key];
    
    const youChanged = JSON.stringify(yourValue) !== JSON.stringify(originalValue);
    const theyChanged = JSON.stringify(theirValue) !== JSON.stringify(originalValue);
    
    if (youChanged && theyChanged && JSON.stringify(yourValue) !== JSON.stringify(theirValue)) {
      // Both changed the same field to different values - real conflict
      hasConflict = true;
    } else if (youChanged && !theyChanged) {
      // Only you changed this field - keep yours
      (merged as Record<string, unknown>)[key as string] = yourValue;
    }
    // If only they changed it, merged already has their value
  }

  return hasConflict ? null : merged;
}

/**
 * Format a field name for display
 */
export function formatFieldName(field: string): string {
  return field
    .replace(/_/g, ' ')
    .replace(/([A-Z])/g, ' $1')
    .trim()
    .split(' ')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(' ');
}

/**
 * Format a value for display in conflict modal
 */
export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '(empty)';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
