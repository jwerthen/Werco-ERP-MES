/**
 * useOptimisticForm Hook Tests
 */

import { renderHook, act, waitFor } from '@testing-library/react';
import { useOptimisticForm } from './useOptimisticForm';
import { ConflictError, ConflictData } from '../utils/optimisticLock';

// Mock the optimisticLock module to make instanceof work in tests
jest.mock('../utils/optimisticLock', () => {
  const originalModule = jest.requireActual('../utils/optimisticLock');
  
  // Create a mock ConflictError that works with instanceof
  class MockConflictError extends Error {
    public readonly conflict: any;
    public readonly statusCode: number = 409;
    
    constructor(response: any) {
      super(response.message);
      this.name = 'ConflictError';
      this.conflict = response.conflict;
      Object.setPrototypeOf(this, MockConflictError.prototype);
    }
  }
  
  return {
    ...originalModule,
    ConflictError: MockConflictError,
  };
});

// Re-import after mock
const { ConflictError: MockedConflictError } = jest.requireMock('../utils/optimisticLock');

interface TestData {
  id: number;
  name: string;
  description: string;
  version: number;
}

const createTestData = (overrides?: Partial<TestData>): TestData => ({
  id: 1,
  name: 'Test Item',
  description: 'Test description',
  version: 1,
  ...overrides,
});

describe('useOptimisticForm', () => {
  describe('initial state', () => {
    it('initializes with provided data', () => {
      const initialData = createTestData();
      const updateFn = jest.fn();

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      expect(result.current.data).toEqual(initialData);
      expect(result.current.originalData).toEqual(initialData);
      expect(result.current.conflict).toBeNull();
      expect(result.current.isSubmitting).toBe(false);
      expect(result.current.error).toBeNull();
    });
  });

  describe('handleSubmit', () => {
    it('submits data successfully and updates state', async () => {
      const initialData = createTestData();
      const updatedData = createTestData({ name: 'Updated Name', version: 2 });
      const updateFn = jest.fn().mockResolvedValue(updatedData);

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      let returnedData: TestData | null = null;
      await act(async () => {
        returnedData = await result.current.handleSubmit({ name: 'Updated Name' });
      });

      expect(updateFn).toHaveBeenCalledWith({ ...initialData, name: 'Updated Name' });
      expect(returnedData).toEqual(updatedData);
      expect(result.current.data).toEqual(updatedData);
      expect(result.current.originalData).toEqual(updatedData);
      expect(result.current.isSubmitting).toBe(false);
      expect(result.current.conflict).toBeNull();
    });

    it('sets isSubmitting during submission', async () => {
      const initialData = createTestData();
      let resolvePromise: (value: TestData) => void;
      const updateFn = jest.fn().mockReturnValue(
        new Promise<TestData>((resolve) => {
          resolvePromise = resolve;
        })
      );

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      let submitPromise: Promise<TestData | null>;
      act(() => {
        submitPromise = result.current.handleSubmit({ name: 'Updated' });
      });

      // Check isSubmitting is true during submission
      expect(result.current.isSubmitting).toBe(true);

      // Resolve the promise
      await act(async () => {
        resolvePromise!(createTestData({ name: 'Updated', version: 2 }));
        await submitPromise;
      });

      expect(result.current.isSubmitting).toBe(false);
    });

    it('handles conflict errors and sets conflict state', async () => {
      const initialData = createTestData();
      const conflictError = new MockedConflictError({
        error: 'CONFLICT',
        message: 'Version conflict detected',
        conflict: {
          current_version: 2,
          submitted_version: 1,
          current_data: createTestData({ name: 'Server Name', version: 2 }),
          submitted_changes: { name: 'My Name' },
          message: 'Data was modified by another user',
        },
      });
      const updateFn = jest.fn().mockRejectedValue(conflictError);

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      let returnedData: TestData | null;
      await act(async () => {
        returnedData = await result.current.handleSubmit({ name: 'My Name' });
      });

      expect(returnedData!).toBeNull();
      expect(result.current.conflict).not.toBeNull();
      expect(result.current.conflict?.current_version).toBe(2);
      expect(result.current.conflict?.submitted_version).toBe(1);
      expect(result.current.isSubmitting).toBe(false);
    });

    it('handles non-conflict errors', async () => {
      const initialData = createTestData();
      const error = new Error('Network error');
      const updateFn = jest.fn().mockRejectedValue(error);

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      // The hook sets error state and then re-throws
      // We need to catch the error but the state should be updated
      await act(async () => {
        try {
          await result.current.handleSubmit({ name: 'Updated' });
        } catch (e) {
          // Expected to throw
          expect((e as Error).message).toBe('Network error');
        }
      });

      expect(result.current.error).toBe('Network error');
      expect(result.current.conflict).toBeNull();
      expect(result.current.isSubmitting).toBe(false);
    });
  });

  describe('resolveConflict', () => {
    const setupConflict = async () => {
      const initialData = createTestData();
      const serverData = createTestData({ name: 'Server Name', description: 'Server desc', version: 2 });
      const conflictError = new MockedConflictError({
        error: 'CONFLICT',
        message: 'Version conflict',
        conflict: {
          current_version: 2,
          submitted_version: 1,
          current_data: serverData,
          submitted_changes: { name: 'My Name' },
          message: 'Conflict',
        },
      });
      const updateFn = jest.fn().mockRejectedValueOnce(conflictError);

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      await act(async () => {
        await result.current.handleSubmit({ name: 'My Name' });
      });

      return { result, updateFn, serverData };
    };

    it('resolves conflict with "theirs" option', async () => {
      const { result, serverData } = await setupConflict();

      act(() => {
        result.current.resolveConflict('theirs');
      });

      expect(result.current.conflict).toBeNull();
      expect(result.current.data).toEqual(serverData);
      expect(result.current.originalData).toEqual(serverData);
    });

    it('resolves conflict with "mine" option and re-submits', async () => {
      const { result, updateFn } = await setupConflict();

      // Mock successful update on retry
      const updatedData = createTestData({ name: 'My Name', version: 3 });
      updateFn.mockResolvedValueOnce(updatedData);

      await act(async () => {
        result.current.resolveConflict('mine');
      });

      // Should have updated version and re-submitted
      expect(result.current.conflict).toBeNull();
    });

    it('resolves conflict with custom data', async () => {
      const { result, updateFn } = await setupConflict();

      const customData = createTestData({ name: 'Merged Name', description: 'Merged desc', version: 2 });
      updateFn.mockResolvedValueOnce(customData);

      await act(async () => {
        result.current.resolveConflict(customData);
      });

      expect(result.current.conflict).toBeNull();
    });

    it('does nothing when no conflict exists', () => {
      const initialData = createTestData();
      const updateFn = jest.fn();

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      act(() => {
        result.current.resolveConflict('theirs');
      });

      // Should not change anything
      expect(result.current.data).toEqual(initialData);
      expect(updateFn).not.toHaveBeenCalled();
    });
  });

  describe('refresh', () => {
    it('replaces data with new data', () => {
      const initialData = createTestData();
      const updateFn = jest.fn();

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      const newData = createTestData({ name: 'Refreshed', version: 5 });
      act(() => {
        result.current.refresh(newData);
      });

      expect(result.current.data).toEqual(newData);
      expect(result.current.originalData).toEqual(newData);
      expect(result.current.conflict).toBeNull();
      expect(result.current.error).toBeNull();
    });
  });

  describe('reset', () => {
    it('resets data to original data', async () => {
      const initialData = createTestData();
      const updatedData = createTestData({ name: 'Updated', version: 2 });
      const updateFn = jest.fn().mockResolvedValue(updatedData);

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      // First update
      await act(async () => {
        await result.current.handleSubmit({ name: 'Updated' });
      });

      // Then modify data locally
      act(() => {
        result.current.setData({ ...result.current.data, name: 'Local change' });
      });

      expect(result.current.data.name).toBe('Local change');

      // Reset should go back to the updated (original) data
      act(() => {
        result.current.reset();
      });

      expect(result.current.data).toEqual(updatedData);
    });
  });

  describe('setData', () => {
    it('sets data with direct value', () => {
      const initialData = createTestData();
      const updateFn = jest.fn();

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      const newData = createTestData({ name: 'Direct Update' });
      act(() => {
        result.current.setData(newData);
      });

      expect(result.current.data).toEqual(newData);
      // originalData should not change
      expect(result.current.originalData).toEqual(initialData);
    });

    it('sets data with updater function', () => {
      const initialData = createTestData();
      const updateFn = jest.fn();

      const { result } = renderHook(() => useOptimisticForm(initialData, updateFn));

      act(() => {
        result.current.setData((prev) => ({ ...prev, name: prev.name + ' Modified' }));
      });

      expect(result.current.data.name).toBe('Test Item Modified');
    });
  });
});
