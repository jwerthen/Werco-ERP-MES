import React, { useState, useEffect, useCallback } from 'react';
import { axios } from 'axios';

// ============================================================================
// API VALIDATION ERROR TYPES
// ============================================================================

export interface ValidationErrorDetail {
  field: string;
  message: string;
  type: string;
}

export interface ApiValidationError {
  error: 'VALIDATION_ERROR' | 'BUSINESS_VALIDATION_ERROR' | string;
  message: string;
  details: ValidationErrorDetail[];
}

export interface ApiError {
  error?: string;
  message?: string;
  details?: ValidationErrorDetail[];
}

export function isApiValidationError(error: unknown): error is ApiValidationError {
  return (
    typeof error === 'object' &&
    error !== null &&
    'error' in error &&
    Array.isArray((error as any).details)
  );
}

// ============================================================================
// HOOK TO MAP API ERRORS TO FORM FIELDS
// ============================================================================

interface UseFormErrorMappingProps<T extends Record<string, any>> {
  setError: (name: keyof T, error: { type?: string; message?: string }) => void;
  setFormError?: (message: string) => void;
}

export function useFormErrorMapping<T extends Record<string, any>>({
  setError,
  setFormError,
}: UseFormErrorMappingProps<T>) {
  const mapApiErrorToForm = useCallback(
    (error: unknown) => {
      console.error('API Error:', error);

      if (axios.isAxiosError(error) && error.response?.data) {
        const data = error.response.data as ApiError | ApiValidationError;

        if (isApiValidationError(data)) {
          // Map validation errors to form fields
          data.details.forEach((detail) => {
            // Handle nested fields (e.g., "lines.0.qty" -> "lines.0.qty")
            setError(detail.field as keyof T, {
              type: 'server',
              message: detail.message,
            });
          });

          // Set overall form error if available
          if (setFormError && data.message) {
            setFormError(data.message);
          }
        } else if (setFormError) {
          // Generic error
          setFormError(data.message || 'An error occurred');
        }
      } else if (setFormError) {
        setFormError(error instanceof Error ? error.message : 'An unexpected error occurred');
      }
    },
    [setError, setFormError]
  );

  return { mapApiErrorToForm };
}

// ============================================================================
// DEBOUNCED ASYNC VALIDATION HOOK
// ============================================================================

export function useAsyncValidation<T>(
  validateFn: (value: T) => Promise<string | null>,
  debounceMs: number = 500
) {
  const [isValidating, setIsValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const validate = useCallback(
    (value: T) => {
      setIsValidating(true);
      setError(null);

      const timer = setTimeout(async () => {
        try {
          const result = await validateFn(value);
          setError(result);
        } catch (err) {
          setError('Validation failed');
        } finally {
          setIsValidating(false);
        }
      }, debounceMs);

      return () => clearTimeout(timer);
    },
    [validateFn, debounceMs]
  );

  return { validate, isValidating, error };
}
