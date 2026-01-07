import React from 'react';
import { AlertCircle } from '@heroicons/react/20/solid';

interface FormFieldProps {
  label: string;
  name: string;
  error?: string;
  required?: boolean;
  children: React.ReactNode;
  className?: string;
}

export function FormField({ label, name, error, required, children, className = '' }: FormFieldProps) {
  return (
    <div className={`mb-4 ${className}`}>
      <label htmlFor={name} className="block text-sm font-medium text-gray-700 mb-1">
        {label} {required && <span className="text-red-500">*</span>}
      </label>
      {children}
      {error && (
        <div className="mt-1 text-sm text-red-600 flex items-center gap-1 animate-fade-in">
          <AlertCircle className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
          <span role="alert">{error}</span>
        </div>
      )}
    </div>
  );
}
