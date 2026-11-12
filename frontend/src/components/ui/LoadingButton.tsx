/**
 * Loading Button Component
 *
 * A button that shows a loading spinner when in loading state.
 * Prevents double-clicks and provides visual feedback.
 *
 * `focusableWhileLoading`: while loading, the button stays focusable (`aria-disabled`, clicks
 * ignored) instead of natively `disabled`, so keyboard focus on it never drops to <body> for
 * the length of the request. `disabled` still disables it natively.
 */

import React from 'react';
import { Spinner } from './Skeleton';

interface LoadingButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  loading?: boolean;
  loadingText?: string;
  /** Keep the button focusable while loading (aria-disabled; clicks ignored) instead of `disabled`. */
  focusableWhileLoading?: boolean;
  children: React.ReactNode;
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'md' | 'lg';
}

const variantClasses = {
  primary: 'btn-primary',
  secondary: 'btn bg-slate-700 text-slate-200 hover:bg-slate-600',
  danger: 'btn bg-red-600 text-white hover:bg-red-700',
  ghost: 'btn bg-transparent hover:bg-slate-700 text-slate-200',
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
  focusableWhileLoading = false,
  onClick,
  ...props
}) => {
  const baseClasses = variantClasses[variant];
  const sizeClass = sizeClasses[size];
  const softLock = loading && focusableWhileLoading;

  return (
    <button
      {...props}
      aria-disabled={softLock ? true : props['aria-disabled']}
      onClick={softLock ? event => event.preventDefault() : onClick}
      disabled={softLock ? disabled : loading || disabled}
      className={`${baseClasses} ${sizeClass} ${className} ${
        loading ? 'opacity-75 cursor-not-allowed' : ''
      } inline-flex items-center justify-center`}
    >
      {loading && (
        <Spinner size="sm" className={`mr-2 ${variant === 'primary' ? 'border-white/30 border-t-white' : ''}`} />
      )}
      {loading && loadingText ? loadingText : children}
    </button>
  );
};

export default LoadingButton;
