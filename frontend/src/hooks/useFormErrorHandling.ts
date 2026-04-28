import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';

// ============================================================================
// API VALIDATION ERROR TYPES
// ============================================================================

interface ValidationErrorDetail {
  field: string;
  message: string;
  type: string;
}

interface ApiValidationError {
  error: 'VALIDATION_ERROR' | 'BUSINESS_VALIDATION_ERROR' | string;
  message: string;
  details: ValidationErrorDetail[];
}

export interface ApiError {
  error?: string;
  message?: string;
  details?: ValidationErrorDetail[];
}

function isApiValidationError(error: unknown): error is ApiValidationError {
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

interface UseFormErrorMappingProps {
  // Use any for setError to allow compatibility with react-hook-form's UseFormSetError
  setError: (name: string, error: { type?: string; message?: string }) => void;
  setFormError?: (message: string) => void;
}

export function useFormErrorMapping({
  setError,
  setFormError,
}: UseFormErrorMappingProps) {
  const mapApiErrorToForm = useCallback(
    (error: unknown) => {
      console.error('API Error:', error);

      if (axios.isAxiosError(error) && error.response?.data) {
        const data = error.response.data as ApiError | ApiValidationError;

        if (isApiValidationError(data)) {
          // Map validation errors to form fields
          data.details.forEach((detail) => {
            // Handle nested fields (e.g., "lines.0.qty" -> "lines.0.qty")
            setError(detail.field, {
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

type AsyncValidator<T> = (value: T) => Promise<string | null>;

export function useAsyncValidation<T = string>(
  validateFn: AsyncValidator<T>,
  debounceMs = 500
) {
  const [isValidating, setIsValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sequenceRef = useRef(0);

  const clearPending = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
  }, []);

  const validate = useCallback(
    (value: T) => {
      clearPending();
      setIsValidating(true);
      const sequence = ++sequenceRef.current;
      let cancelled = false;

      timeoutRef.current = setTimeout(async () => {
        try {
          const validationError = await validateFn(value);
          if (!cancelled && sequence === sequenceRef.current) {
            setError(validationError);
          }
        } catch {
          if (!cancelled && sequence === sequenceRef.current) {
            setError('Validation failed');
          }
        } finally {
          if (!cancelled && sequence === sequenceRef.current) {
            setIsValidating(false);
            timeoutRef.current = null;
          }
        }
      }, debounceMs);

      return () => {
        cancelled = true;
        clearPending();
        if (sequence === sequenceRef.current) {
          setIsValidating(false);
        }
      };
    },
    [clearPending, debounceMs, validateFn]
  );

  useEffect(() => clearPending, [clearPending]);

  return { isValidating, error, validate };
}
