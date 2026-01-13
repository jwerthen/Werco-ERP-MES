/**
 * useFormErrorHandling Tests
 */

import { renderHook, act } from '@testing-library/react';
import axios from 'axios';
import {
  useFormErrorMapping,
  useAsyncValidation,
  isApiValidationError,
  ApiValidationError,
} from './useFormErrorHandling';

// Mock axios
jest.mock('axios', () => ({
  isAxiosError: jest.fn(),
}));

const mockIsAxiosError = axios.isAxiosError as jest.MockedFunction<typeof axios.isAxiosError>;

describe('isApiValidationError', () => {
  it('returns true for valid validation error object', () => {
    const error: ApiValidationError = {
      error: 'VALIDATION_ERROR',
      message: 'Validation failed',
      details: [{ field: 'name', message: 'Name is required', type: 'required' }],
    };

    expect(isApiValidationError(error)).toBe(true);
  });

  it('returns true for business validation error', () => {
    const error: ApiValidationError = {
      error: 'BUSINESS_VALIDATION_ERROR',
      message: 'Business rule violated',
      details: [{ field: 'quantity', message: 'Quantity exceeds limit', type: 'business' }],
    };

    expect(isApiValidationError(error)).toBe(true);
  });

  it('returns false for null', () => {
    expect(isApiValidationError(null)).toBe(false);
  });

  it('returns false for undefined', () => {
    expect(isApiValidationError(undefined)).toBe(false);
  });

  it('returns false for object without error field', () => {
    expect(isApiValidationError({ message: 'Error', details: [] })).toBe(false);
  });

  it('returns false for object without details array', () => {
    expect(isApiValidationError({ error: 'ERROR', message: 'Error' })).toBe(false);
  });

  it('returns false for object with non-array details', () => {
    expect(isApiValidationError({ error: 'ERROR', message: 'Error', details: 'not array' })).toBe(false);
  });
});

describe('useFormErrorMapping', () => {
  const createMockSetError = () => jest.fn();
  const createMockSetFormError = () => jest.fn();

  beforeEach(() => {
    jest.clearAllMocks();
  });

  it('maps validation errors to form fields', () => {
    const setError = createMockSetError();
    const setFormError = createMockSetFormError();

    mockIsAxiosError.mockReturnValue(true);

    const { result } = renderHook(() => useFormErrorMapping({ setError, setFormError }));

    const axiosError = {
      response: {
        data: {
          error: 'VALIDATION_ERROR',
          message: 'Validation failed',
          details: [
            { field: 'name', message: 'Name is required', type: 'required' },
            { field: 'email', message: 'Invalid email format', type: 'format' },
          ],
        },
      },
    };

    act(() => {
      result.current.mapApiErrorToForm(axiosError);
    });

    expect(setError).toHaveBeenCalledTimes(2);
    expect(setError).toHaveBeenCalledWith('name', { type: 'server', message: 'Name is required' });
    expect(setError).toHaveBeenCalledWith('email', { type: 'server', message: 'Invalid email format' });
    expect(setFormError).toHaveBeenCalledWith('Validation failed');
  });

  it('handles nested field names', () => {
    const setError = createMockSetError();
    const setFormError = createMockSetFormError();

    mockIsAxiosError.mockReturnValue(true);

    const { result } = renderHook(() => useFormErrorMapping({ setError, setFormError }));

    const axiosError = {
      response: {
        data: {
          error: 'VALIDATION_ERROR',
          message: 'Validation failed',
          details: [
            { field: 'lines.0.qty', message: 'Quantity must be positive', type: 'min' },
            { field: 'lines.1.part_id', message: 'Part is required', type: 'required' },
          ],
        },
      },
    };

    act(() => {
      result.current.mapApiErrorToForm(axiosError);
    });

    expect(setError).toHaveBeenCalledWith('lines.0.qty', { type: 'server', message: 'Quantity must be positive' });
    expect(setError).toHaveBeenCalledWith('lines.1.part_id', { type: 'server', message: 'Part is required' });
  });

  it('handles generic API errors without validation details', () => {
    const setError = createMockSetError();
    const setFormError = createMockSetFormError();

    mockIsAxiosError.mockReturnValue(true);

    const { result } = renderHook(() => useFormErrorMapping({ setError, setFormError }));

    const axiosError = {
      response: {
        data: {
          message: 'Internal server error',
        },
      },
    };

    act(() => {
      result.current.mapApiErrorToForm(axiosError);
    });

    expect(setError).not.toHaveBeenCalled();
    expect(setFormError).toHaveBeenCalledWith('Internal server error');
  });

  it('handles non-axios errors', () => {
    const setError = createMockSetError();
    const setFormError = createMockSetFormError();

    mockIsAxiosError.mockReturnValue(false);

    const { result } = renderHook(() => useFormErrorMapping({ setError, setFormError }));

    const error = new Error('Network error');

    act(() => {
      result.current.mapApiErrorToForm(error);
    });

    expect(setError).not.toHaveBeenCalled();
    expect(setFormError).toHaveBeenCalledWith('Network error');
  });

  it('handles unexpected error types', () => {
    const setError = createMockSetError();
    const setFormError = createMockSetFormError();

    mockIsAxiosError.mockReturnValue(false);

    const { result } = renderHook(() => useFormErrorMapping({ setError, setFormError }));

    act(() => {
      result.current.mapApiErrorToForm('string error');
    });

    expect(setFormError).toHaveBeenCalledWith('An unexpected error occurred');
  });

  it('works without setFormError', () => {
    const setError = createMockSetError();

    mockIsAxiosError.mockReturnValue(true);

    const { result } = renderHook(() => useFormErrorMapping({ setError }));

    const axiosError = {
      response: {
        data: {
          error: 'VALIDATION_ERROR',
          message: 'Validation failed',
          details: [{ field: 'name', message: 'Required', type: 'required' }],
        },
      },
    };

    act(() => {
      result.current.mapApiErrorToForm(axiosError);
    });

    expect(setError).toHaveBeenCalledWith('name', { type: 'server', message: 'Required' });
  });
});

describe('useAsyncValidation', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('initializes with default state', () => {
    const validateFn = jest.fn().mockResolvedValue(null);
    const { result } = renderHook(() => useAsyncValidation(validateFn));

    expect(result.current.isValidating).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it('validates value after debounce delay', async () => {
    const validateFn = jest.fn().mockResolvedValue(null);
    const { result } = renderHook(() => useAsyncValidation(validateFn, 500));

    act(() => {
      result.current.validate('test-value');
    });

    expect(result.current.isValidating).toBe(true);

    // Fast-forward past debounce
    await act(async () => {
      jest.advanceTimersByTime(500);
    });

    expect(validateFn).toHaveBeenCalledWith('test-value');
  });

  it('sets error when validation returns error message', async () => {
    const validateFn = jest.fn().mockResolvedValue('Value already exists');
    const { result } = renderHook(() => useAsyncValidation(validateFn, 100));

    act(() => {
      result.current.validate('duplicate');
    });

    await act(async () => {
      jest.advanceTimersByTime(100);
    });

    expect(result.current.error).toBe('Value already exists');
    expect(result.current.isValidating).toBe(false);
  });

  it('clears error when validation passes', async () => {
    const validateFn = jest.fn().mockResolvedValue(null);
    const { result } = renderHook(() => useAsyncValidation(validateFn, 100));

    act(() => {
      result.current.validate('valid-value');
    });

    await act(async () => {
      jest.advanceTimersByTime(100);
    });

    expect(result.current.error).toBeNull();
    expect(result.current.isValidating).toBe(false);
  });

  it('handles validation function errors', async () => {
    const validateFn = jest.fn().mockRejectedValue(new Error('API Error'));
    const { result } = renderHook(() => useAsyncValidation(validateFn, 100));

    act(() => {
      result.current.validate('test');
    });

    await act(async () => {
      jest.advanceTimersByTime(100);
    });

    expect(result.current.error).toBe('Validation failed');
    expect(result.current.isValidating).toBe(false);
  });

  it('uses default debounce of 500ms', async () => {
    const validateFn = jest.fn().mockResolvedValue(null);
    const { result } = renderHook(() => useAsyncValidation(validateFn));

    act(() => {
      result.current.validate('test');
    });

    // At 400ms, should not have been called yet
    await act(async () => {
      jest.advanceTimersByTime(400);
    });
    expect(validateFn).not.toHaveBeenCalled();

    // At 500ms, should be called
    await act(async () => {
      jest.advanceTimersByTime(100);
    });
    expect(validateFn).toHaveBeenCalled();
  });

  it('returns cleanup function', () => {
    const validateFn = jest.fn().mockResolvedValue(null);
    const { result } = renderHook(() => useAsyncValidation(validateFn, 100));

    let cleanup: (() => void) | void;
    act(() => {
      cleanup = result.current.validate('test');
    });

    expect(typeof cleanup).toBe('function');

    // Call cleanup before debounce completes
    act(() => {
      if (cleanup) cleanup();
      jest.advanceTimersByTime(100);
    });

    // Validate should not have been called due to cleanup
    expect(validateFn).not.toHaveBeenCalled();
  });
});
