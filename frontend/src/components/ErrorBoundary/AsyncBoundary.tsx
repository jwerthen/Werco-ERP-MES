/**
 * AsyncBoundary Component
 * 
 * Combines ErrorBoundary with React Suspense for async component loading.
 * Provides unified loading and error states.
 */

import React, { Suspense, ReactNode } from 'react';
import { ErrorBoundary, ErrorBoundaryLevel } from './ErrorBoundary';

interface AsyncBoundaryProps {
  children: ReactNode;
  fallback?: ReactNode;
  errorFallback?: ReactNode | ((error: Error, reset: () => void) => ReactNode);
  loadingMessage?: string;
  name?: string;
  level?: ErrorBoundaryLevel;
}

/**
 * Loading Spinner component
 */
function LoadingSpinner({ label = 'Loading...' }: { label?: string }) {
  return (
    <div 
      className="flex flex-col items-center justify-center p-8"
      role="status"
      aria-live="polite"
    >
      <div className="relative">
        {/* Outer ring */}
        <div className="w-12 h-12 border-4 border-rose-200 rounded-full"></div>
        {/* Spinning ring */}
        <div className="absolute top-0 left-0 w-12 h-12 border-4 border-transparent border-t-rose-500 rounded-full animate-spin"></div>
      </div>
      <p className="mt-4 text-sm text-gray-600 font-medium">{label}</p>
    </div>
  );
}

/**
 * Skeleton loader for card-like content
 */
export function CardSkeleton() {
  return (
    <div className="animate-pulse bg-white rounded-xl p-6 shadow-sm">
      <div className="flex items-center gap-4 mb-4">
        <div className="w-12 h-12 bg-gray-200 rounded-lg"></div>
        <div className="flex-1">
          <div className="h-4 bg-gray-200 rounded w-3/4 mb-2"></div>
          <div className="h-3 bg-gray-200 rounded w-1/2"></div>
        </div>
      </div>
      <div className="space-y-3">
        <div className="h-3 bg-gray-200 rounded"></div>
        <div className="h-3 bg-gray-200 rounded w-5/6"></div>
        <div className="h-3 bg-gray-200 rounded w-4/6"></div>
      </div>
    </div>
  );
}

/**
 * Skeleton loader for table content
 */
export function TableSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="animate-pulse">
      {/* Header */}
      <div className="flex gap-4 p-4 bg-gray-100 rounded-t-xl">
        <div className="h-4 bg-gray-300 rounded w-1/6"></div>
        <div className="h-4 bg-gray-300 rounded w-1/4"></div>
        <div className="h-4 bg-gray-300 rounded w-1/5"></div>
        <div className="h-4 bg-gray-300 rounded w-1/6"></div>
      </div>
      {/* Rows */}
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="flex gap-4 p-4 border-b border-gray-100">
          <div className="h-4 bg-gray-200 rounded w-1/6"></div>
          <div className="h-4 bg-gray-200 rounded w-1/4"></div>
          <div className="h-4 bg-gray-200 rounded w-1/5"></div>
          <div className="h-4 bg-gray-200 rounded w-1/6"></div>
        </div>
      ))}
    </div>
  );
}

/**
 * Skeleton loader for chart content
 */
export function ChartSkeleton() {
  return (
    <div className="animate-pulse bg-white rounded-xl p-6">
      <div className="h-5 bg-gray-200 rounded w-1/4 mb-6"></div>
      <div className="flex items-end justify-between h-48 gap-2">
        {Array.from({ length: 7 }).map((_, i) => (
          <div 
            key={i} 
            className="bg-gray-200 rounded-t w-full"
            style={{ height: `${Math.random() * 60 + 40}%` }}
          ></div>
        ))}
      </div>
      <div className="flex justify-between mt-4">
        {Array.from({ length: 7 }).map((_, i) => (
          <div key={i} className="h-3 bg-gray-200 rounded w-8"></div>
        ))}
      </div>
    </div>
  );
}

/**
 * AsyncBoundary - Combines Suspense with ErrorBoundary
 */
export function AsyncBoundary({
  children,
  fallback,
  errorFallback,
  loadingMessage = 'Loading...',
  name,
  level = 'section'
}: AsyncBoundaryProps) {
  return (
    <ErrorBoundary 
      fallback={errorFallback} 
      name={name} 
      level={level}
    >
      <Suspense fallback={fallback || <LoadingSpinner label={loadingMessage} />}>
        {children}
      </Suspense>
    </ErrorBoundary>
  );
}

/**
 * Lazy load a component with error boundary and loading state
 */
export function withAsyncBoundary<P extends object>(
  Component: React.ComponentType<P>,
  options: Omit<AsyncBoundaryProps, 'children'> = {}
): React.FC<P> {
  return function WrappedComponent(props: P) {
    return (
      <AsyncBoundary {...options}>
        <Component {...props} />
      </AsyncBoundary>
    );
  };
}

export { LoadingSpinner };
export default AsyncBoundary;
