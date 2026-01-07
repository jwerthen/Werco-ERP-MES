/**
 * useOptimisticForm Hook
 * 
 * React hook for form handling with optimistic locking support.
 * Handles version tracking, conflict detection, and resolution.
 */

import { useState, useCallback } from 'react';
import { ConflictError, ConflictData, autoMerge } from '../utils/optimisticLock';

export interface OptimisticFormState<T> {
  data: T;
  originalData: T;
  conflict: ConflictData<T> | null;
  isSubmitting: boolean;
  error: string | null;
}

export interface OptimisticFormActions<T> {
  handleSubmit: (updates: Partial<T>) => Promise<T | null>;
  resolveConflict: (resolution: 'mine' | 'theirs' | 'merge' | T) => void;
  refresh: (newData: T) => void;
  reset: () => void;
  setData: (data: T | ((prev: T) => T)) => void;
}

export type OptimisticFormReturn<T> = OptimisticFormState<T> & OptimisticFormActions<T>;

interface VersionedData extends Record<string, unknown> {
  version: number;
}

/**
 * Hook for managing forms with optimistic locking
 * 
 * @param initialData Initial form data (must include version field)
 * @param updateFn Async function to submit updates to API
 * @returns Form state and actions
 * 
 * @example
 * ```tsx
 * const { data, conflict, isSubmitting, handleSubmit, resolveConflict } = useOptimisticForm(
 *   part,
 *   async (updates) => {
 *     const response = await api.updatePart(part.id, updates);
 *     return response.data;
 *   }
 * );
 * ```
 */
export function useOptimisticForm<T extends VersionedData>(
  initialData: T,
  updateFn: (data: T) => Promise<T>
): OptimisticFormReturn<T> {
  const [data, setDataState] = useState<T>(initialData);
  const [originalData, setOriginalData] = useState<T>(initialData);
  const [conflict, setConflict] = useState<ConflictData<T> | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingChanges, setPendingChanges] = useState<Partial<T> | null>(null);

  const handleSubmit = useCallback(async (updates: Partial<T>): Promise<T | null> => {
    setIsSubmitting(true);
    setError(null);
    setPendingChanges(updates);

    try {
      const updatePayload = { ...data, ...updates } as T;
      const updated = await updateFn(updatePayload);
      
      setDataState(updated);
      setOriginalData(updated);
      setConflict(null);
      setPendingChanges(null);
      
      return updated;
    } catch (err) {
      if (err instanceof ConflictError) {
        setConflict(err.conflict as ConflictData<T>);
        return null;
      }
      
      const message = err instanceof Error ? err.message : 'An error occurred';
      setError(message);
      throw err;
    } finally {
      setIsSubmitting(false);
    }
  }, [data, updateFn]);

  const resolveConflict = useCallback((resolution: 'mine' | 'theirs' | 'merge' | T) => {
    if (!conflict) return;

    if (resolution === 'theirs') {
      // Accept server version
      const serverData = conflict.current_data as T;
      setDataState(serverData);
      setOriginalData(serverData);
      setConflict(null);
      setPendingChanges(null);
    } else if (resolution === 'mine') {
      // Retry with current version
      const updatedData = {
        ...data,
        version: conflict.current_version
      } as T;
      setDataState(updatedData);
      setOriginalData({ ...conflict.current_data, version: conflict.current_version } as T);
      setConflict(null);
      
      // Re-submit with pending changes
      if (pendingChanges) {
        handleSubmit(pendingChanges);
      }
    } else if (resolution === 'merge') {
      // Attempt auto-merge
      const merged = autoMerge(
        originalData,
        pendingChanges || {},
        conflict.current_data as T
      );
      
      if (merged) {
        const mergedWithVersion = { ...merged, version: conflict.current_version } as T;
        setDataState(mergedWithVersion);
        setOriginalData({ ...conflict.current_data, version: conflict.current_version } as T);
        setConflict(null);
        
        // Re-submit merged data
        handleSubmit({});
      } else {
        // Can't auto-merge, keep conflict modal open
        setError('Cannot auto-merge: same fields were modified. Please choose your version or theirs.');
      }
    } else {
      // Custom merged data provided
      const customData = { ...resolution, version: conflict.current_version } as T;
      setDataState(customData);
      setOriginalData({ ...conflict.current_data, version: conflict.current_version } as T);
      setConflict(null);
      
      // Re-submit custom merged data
      handleSubmit({});
    }
  }, [conflict, data, originalData, pendingChanges, handleSubmit]);

  const refresh = useCallback((newData: T) => {
    setDataState(newData);
    setOriginalData(newData);
    setConflict(null);
    setError(null);
    setPendingChanges(null);
  }, []);

  const reset = useCallback(() => {
    setDataState(originalData);
    setConflict(null);
    setError(null);
    setPendingChanges(null);
  }, [originalData]);

  const setData = useCallback((newData: T | ((prev: T) => T)) => {
    setDataState(prev => typeof newData === 'function' ? newData(prev) : newData);
  }, []);

  return {
    data,
    originalData,
    conflict,
    isSubmitting,
    error,
    handleSubmit,
    resolveConflict,
    refresh,
    reset,
    setData
  };
}

export default useOptimisticForm;
