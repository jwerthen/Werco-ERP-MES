import React from 'react';
import { ExclamationCircleIcon } from '@heroicons/react/20/solid';

interface FormFieldProps {
  label: string;
  name: string;
  // Accept string, FieldError objects from react-hook-form, or undefined
  error?: string | { message?: string } | { message?: { message?: string } } | null | undefined;
  required?: boolean;
  children: React.ReactNode;
  className?: string;
}

export function FormField({ label, name, error, required, children, className = '' }: FormFieldProps) {
  // Handle both string errors and FieldError objects from react-hook-form
  let errorMessage: string | undefined;
  if (typeof error === 'string') {
    errorMessage = error;
  } else if (error && typeof error === 'object' && 'message' in error) {
    // Could be FieldError or nested error
    const msg = error.message;
    errorMessage = typeof msg === 'string' ? msg : (msg as { message?: string })?.message;
  }

  return (
    <div className={`mb-4 ${className}`}>
      <label htmlFor={name} className="block text-sm font-medium text-gray-700 mb-1">
        {label} {required && <span className="text-red-500">*</span>}
      </label>
      {children}
      {errorMessage && (
        <div className="mt-1 text-sm text-red-600 flex items-center gap-1 animate-fade-in">
          <ExclamationCircleIcon className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
          <span role="alert">{errorMessage}</span>
        </div>
      )}
    </div>
  );
}
