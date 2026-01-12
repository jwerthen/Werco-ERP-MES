/**
 * Loading Button Component
 * 
 * A button that shows a loading spinner when in loading state.
 * Prevents double-clicks and provides visual feedback.
 */

import React from 'react';
import { Spinner } from './Skeleton';

interface LoadingButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  loading?: boolean;
  loadingText?: string;
  children: React.ReactNode;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
}

const variantClasses = {
  primary: 'btn-primary',
  secondary: 'btn bg-gray-100 text-gray-700 hover:bg-gray-200',
  danger: 'btn bg-red-600 text-white hover:bg-red-700',
  ghost: 'btn bg-transparent hover:bg-gray-100 text-gray-700',
};

const sizeClasses = {
  sm: 'px-3 py-1.5 text-sm',
  md: 'px-4 py-2',
  lg: 'px-6 py-3 text-lg',
};

export const LoadingButton: React.FC<LoadingButtonProps> = ({
  loading = false,
  loadingText,
  children,
  variant = 'primary',
  size = 'md',
  disabled,
  className = '',
  ...props
}) => {
  const baseClasses = variantClasses[variant];
  const sizeClass = sizeClasses[size];

  return (
    <button
      {...props}
      disabled={loading || disabled}
      className={`${baseClasses} ${sizeClass} ${className} ${
        loading ? 'opacity-75 cursor-not-allowed' : ''
      } inline-flex items-center justify-center`}
    >
      {loading && (
        <Spinner 
          size="sm" 
          className={`mr-2 ${variant === 'primary' ? 'border-white/30 border-t-white' : ''}`} 
        />
      )}
      {loading && loadingText ? loadingText : children}
    </button>
  );
};

export default LoadingButton;
